"""eBay Browse API source adapter — an authorised alternative to DOM scraping.

Most major marketplaces (eBay included) prohibit HTML scraping in their ToS but
expose an official API. This module talks to eBay's **Browse API** over HTTPS and
emits the very same ``ScrapedListing`` objects the Playwright scraper produces,
so the rest of the pipeline (gatekeeper → stats → LLM → valuation → alert) is
entirely unchanged. The orchestrator treats it interchangeably with the DOM
scraper: both are async context managers exposing ``collect(target)``.

Auth uses the OAuth 2.0 *client-credentials* grant (an "application access
token") — only your app's Client ID + Secret, no end-user login. The token is
cached in-process until shortly before it expires.

Docs:
  OAuth:  https://developer.ebay.com/api-docs/static/oauth-client-credentials-grant.html
  Browse: https://developer.ebay.com/api-docs/buy/browse/resources/item_summary/methods/search
"""

from __future__ import annotations

import asyncio
import base64
import logging
import time
from typing import Any, Optional

import httpx

from config.settings import EbayTarget, Settings, get_settings
from schemas import ScrapedListing

logger = logging.getLogger(__name__)

# API hosts per environment. The OAuth scope string is the same literal for both.
_HOSTS = {
    "production": "https://api.ebay.com",
    "sandbox": "https://api.sandbox.ebay.com",
}
_OAUTH_SCOPE = "https://api.ebay.com/oauth/api_scope"
_TOKEN_REFRESH_SKEW_SECONDS = 60  # refresh this long before nominal expiry


class EbayBrowseSource:
    """Fetches listings from eBay's Browse API and maps them to ScrapedListing.

    Usage mirrors ScraperEngine::

        async with EbayBrowseSource(settings) as src:
            listings = await src.collect(target)

    A pre-built ``httpx.AsyncClient`` can be injected (e.g. one wired to an
    ``httpx.MockTransport``) so the adapter is fully testable without real eBay
    credentials or network access.
    """

    def __init__(
        self,
        settings: Optional[Settings] = None,
        client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        self._settings = settings or get_settings()
        env = (getattr(self._settings, "ebay_env", "production") or "production").lower()
        self._host = _HOSTS.get(env, _HOSTS["production"])
        self._client = client
        self._owns_client = client is None
        self._token: Optional[str] = None
        self._token_expiry: float = 0.0  # monotonic-clock seconds

    @property
    def enabled(self) -> bool:
        return bool(self._settings.ebay_client_id and self._settings.ebay_client_secret)

    # ----------------------------- lifecycle ----------------------------- #
    async def __aenter__(self) -> "EbayBrowseSource":
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._settings.ebay_timeout_seconds)
        return self

    async def __aexit__(self, *_exc: object) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    # ------------------------------- auth -------------------------------- #
    async def _get_token(self) -> str:
        """Return a valid application access token, refreshing if near expiry."""
        if self._token and time.monotonic() < self._token_expiry - _TOKEN_REFRESH_SKEW_SECONDS:
            return self._token

        assert self._client is not None, "Source not started (use as async context manager)."
        creds = f"{self._settings.ebay_client_id}:{self._settings.ebay_client_secret}"
        basic = base64.b64encode(creds.encode()).decode()
        resp = await self._client.post(
            f"{self._host}/identity/v1/oauth2/token",
            headers={
                "Authorization": f"Basic {basic}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={"grant_type": "client_credentials", "scope": _OAUTH_SCOPE},
        )
        resp.raise_for_status()
        payload = resp.json()
        self._token = payload["access_token"]
        self._token_expiry = time.monotonic() + float(payload.get("expires_in", 7200))
        logger.info("Obtained eBay application token (expires in %ss).", payload.get("expires_in"))
        return self._token

    # ------------------------------ search ------------------------------- #
    @staticmethod
    def _build_filter(target: EbayTarget) -> str:
        """Assemble the Browse API ``filter`` string from a target's knobs."""
        parts: list[str] = []
        if target.price_min is not None or target.price_max is not None:
            lo = "" if target.price_min is None else _num(target.price_min)
            hi = "" if target.price_max is None else _num(target.price_max)
            parts.append(f"price:[{lo}..{hi}]")
            parts.append(f"priceCurrency:{target.currency}")
        if target.conditions:
            parts.append("conditions:{" + "|".join(target.conditions) + "}")
        if target.buying_options:
            parts.append("buyingOptions:{" + "|".join(target.buying_options) + "}")
        return ",".join(parts)

    async def collect(self, target: EbayTarget) -> list[ScrapedListing]:
        """Search eBay for ``target.query``, paginating up to ``target.max_items``."""
        if not self.enabled:
            logger.warning("eBay source disabled (no client id/secret) — skipping '%s'.", target.name)
            return []

        token = await self._get_token()
        marketplace = target.marketplace_id or self._settings.ebay_marketplace_id
        headers = {
            "Authorization": f"Bearer {token}",
            "X-EBAY-C-MARKETPLACE-ID": marketplace,
            "Content-Type": "application/json",
        }
        filter_str = self._build_filter(target)
        url = f"{self._host}/buy/browse/v1/item_summary/search"

        collected: list[ScrapedListing] = []
        seen: set[str] = set()
        limit = max(1, min(200, target.max_items))  # Browse API caps limit at 200
        offset = 0

        logger.info("Searching eBay Browse API for '%s' (q=%r)", target.name, target.query)
        while len(collected) < target.max_items:
            params: dict[str, str] = {
                "q": target.query,
                "limit": str(limit),
                "offset": str(offset),
            }
            if filter_str:
                params["filter"] = filter_str
            if target.sort:
                params["sort"] = target.sort
            if target.category_ids:
                params["category_ids"] = target.category_ids

            assert self._client is not None
            resp = await self._client.get(url, headers=headers, params=params)
            if resp.status_code != 200:
                logger.error(
                    "eBay search failed for '%s' (HTTP %d): %s",
                    target.name, resp.status_code, resp.text[:300],
                )
                break

            data = resp.json()
            summaries = data.get("itemSummaries") or []
            if not summaries:
                break

            for item in summaries:
                listing = self._to_listing(item, target)
                if listing is None or listing.url in seen:
                    continue
                seen.add(listing.url)
                collected.append(listing)
                if len(collected) >= target.max_items:
                    break

            total = int(data.get("total", 0))
            offset += limit
            if offset >= total:
                break
            await asyncio.sleep(target.page_delay_seconds)

        logger.info("eBay '%s' complete: %d listings via Browse API.", target.name, len(collected))
        return collected[: target.max_items]

    @staticmethod
    def _to_listing(item: dict[str, Any], target: EbayTarget) -> Optional[ScrapedListing]:
        """Map one eBay ``itemSummary`` to the shared ScrapedListing contract.

        eBay exposes an authoritative structured ``condition`` ("Used", "For
        parts or not working", …). We fold it (plus any short description) into
        ``description`` so the existing regex gatekeeper / grader act on it for
        free — e.g. "For parts or not working" trips the critical-fail screen.
        """
        title = (item.get("title") or "").strip()
        # Fixed-price items carry `price`; auctions carry `currentBidPrice`.
        price_block = item.get("price") or item.get("currentBidPrice") or {}
        raw_price = price_block.get("value")
        url = item.get("itemWebUrl") or item.get("itemHref") or ""
        if not title or raw_price is None or not url:
            return None
        try:
            price = float(raw_price)
        except (TypeError, ValueError):
            return None

        condition = item.get("condition") or ""
        short_desc = item.get("shortDescription") or ""
        description = " ".join(p for p in (condition, short_desc) if p).strip()

        return ScrapedListing(
            title=title,
            description=description,
            price=price,
            url=url,
            raw_metadata={
                "source": target.name,
                "category": target.category,
                "ebay_item_id": item.get("itemId"),
                "condition": condition,
                "condition_id": item.get("conditionId"),
                "currency": price_block.get("currency"),
                "buying_options": item.get("buyingOptions"),
                "seller": (item.get("seller") or {}).get("username"),
            },
        )


def _num(value: float) -> str:
    """Render a price bound without a redundant trailing ``.0`` (120.0 -> "120")."""
    return str(int(value)) if float(value).is_integer() else str(value)
