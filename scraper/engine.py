"""Module A — the asynchronous Playwright scraper engine.

Responsibilities:
  * Drive JavaScript-heavy marketplaces with Playwright (Chromium).
  * Support both endless-scroll and classic pagination.
  * Rotate User-Agents and inject randomised human-like delays (2-7s).
  * Wrap every navigation in resilient retry/backoff logic.
  * Emit strictly-typed `ScrapedListing` objects.

The engine is selector-driven (see `ScrapeTarget` in config.settings) so adding
a new source is configuration, not code.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
from contextlib import asynccontextmanager
from typing import AsyncIterator, Optional
from urllib.parse import urljoin

from playwright.async_api import (
    Browser,
    BrowserContext,
    Error as PlaywrightError,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

from config.settings import ScrapeTarget, Settings, get_settings
from schemas import ScrapedListing

logger = logging.getLogger(__name__)

# Matches the first decimal/thousands-grouped number in a price string.
_PRICE_RE = re.compile(r"[-+]?\d{1,3}(?:[.,]\d{3})*(?:[.,]\d+)?|\d+(?:[.,]\d+)?")


def parse_price(raw: str) -> Optional[float]:
    """Best-effort extraction of a float price from arbitrary marketplace text.

    Handles currency symbols, thousands separators, and both ``1.234,56`` and
    ``1,234.56`` groupings. Returns ``None`` when no number is present.
    """
    if not raw:
        return None
    match = _PRICE_RE.search(raw.replace("\xa0", " "))
    if not match:
        return None
    token = match.group(0)

    # Normalise separators: assume the rightmost separator is the decimal point.
    if "," in token and "." in token:
        if token.rfind(",") > token.rfind("."):
            token = token.replace(".", "").replace(",", ".")
        else:
            token = token.replace(",", "")
    elif token.count(",") == 1 and len(token.split(",")[-1]) in (1, 2):
        token = token.replace(",", ".")
    else:
        token = token.replace(",", "")

    try:
        return float(token)
    except ValueError:
        return None


class ScraperEngine:
    """Manages a Playwright browser and scrapes configured targets.

    Use as an async context manager so the browser is always torn down::

        async with ScraperEngine() as engine:
            listings = await engine.scrape_target(target)
    """

    def __init__(self, settings: Optional[Settings] = None) -> None:
        self._settings = settings or get_settings()
        self._playwright = None
        self._browser: Optional[Browser] = None

    # ----------------------------- lifecycle ----------------------------- #
    async def __aenter__(self) -> "ScraperEngine":
        await self.start()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.stop()

    async def start(self) -> None:
        logger.info("Launching Chromium (headless=%s)…", self._settings.headless)
        self._playwright = await async_playwright().start()
        launch_kwargs: dict = {"headless": self._settings.headless}
        if self._settings.proxy_server:
            launch_kwargs["proxy"] = {"server": self._settings.proxy_server}
        self._browser = await self._playwright.chromium.launch(**launch_kwargs)

    async def stop(self) -> None:
        if self._browser:
            await self._browser.close()
            self._browser = None
        if self._playwright:
            await self._playwright.stop()
            self._playwright = None
        logger.info("Browser shut down.")

    # ----------------------------- helpers ------------------------------- #
    async def _structural_delay(self) -> None:
        """Randomised 2-7s pause to defeat naive rate-based anti-bot triggers."""
        delay = random.uniform(
            self._settings.min_delay_seconds, self._settings.max_delay_seconds
        )
        logger.debug("Structural delay %.2fs", delay)
        await asyncio.sleep(delay)

    @asynccontextmanager
    async def _new_context(self) -> AsyncIterator[BrowserContext]:
        """Fresh context with a rotated User-Agent and a sane viewport."""
        assert self._browser is not None, "Engine not started."
        user_agent = random.choice(self._settings.user_agents)
        context = await self._browser.new_context(
            user_agent=user_agent,
            viewport={"width": 1366, "height": 900},
            locale="en-GB",
        )
        context.set_default_navigation_timeout(self._settings.nav_timeout_seconds * 1000)
        context.set_default_timeout(self._settings.nav_timeout_seconds * 1000)
        logger.debug("New context with UA: %s", user_agent)
        try:
            yield context
        finally:
            await context.close()

    async def _goto_with_retry(self, page: Page, url: str) -> bool:
        """Navigate with exponential backoff. Returns True on success."""
        for attempt in range(1, self._settings.max_retries + 1):
            try:
                await page.goto(url, wait_until="domcontentloaded")
                return True
            except (PlaywrightTimeoutError, PlaywrightError) as exc:
                backoff = 2 ** attempt
                logger.warning(
                    "Navigation to %s failed (attempt %d/%d): %s — backing off %ds",
                    url, attempt, self._settings.max_retries, exc, backoff,
                )
                await asyncio.sleep(backoff)
        logger.error("Giving up on %s after %d attempts.", url, self._settings.max_retries)
        return False

    async def _auto_scroll(self, page: Page, target: ScrapeTarget) -> None:
        """Repeatedly scroll to the bottom until no new cards load."""
        previous_count = -1
        for round_idx in range(self._settings.max_scroll_rounds):
            count = await page.locator(target.listing_selector).count()
            if count >= target.max_items:
                logger.debug("Reached max_items (%d) during scroll.", target.max_items)
                break
            if count == previous_count:
                logger.debug("Scroll plateaued at %d cards.", count)
                break
            previous_count = count
            await page.mouse.wheel(0, 12000)
            await self._structural_delay()

    async def _extract_listings(
        self, page: Page, target: ScrapeTarget
    ) -> list[ScrapedListing]:
        """Pull strict listing dicts out of the rendered DOM."""
        listings: list[ScrapedListing] = []
        cards = page.locator(target.listing_selector)
        count = min(await cards.count(), target.max_items)

        for i in range(count):
            card = cards.nth(i)
            try:
                title = (await self._safe_text(card, target.title_selector)) or ""
                price_text = await self._safe_text(card, target.price_selector)
                price = parse_price(price_text or "")
                href = await self._safe_attr(card, target.link_selector, "href")
                description = ""
                if target.description_selector:
                    description = (
                        await self._safe_text(card, target.description_selector)
                    ) or ""

                if not title or price is None or not href:
                    logger.debug("Skipping card %d: missing title/price/url.", i)
                    continue

                listings.append(
                    ScrapedListing(
                        title=title,
                        description=description,
                        price=price,
                        url=urljoin(page.url, href),
                        raw_metadata={
                            "source": target.name,
                            "category": target.category,
                            "raw_price_text": price_text,
                        },
                    )
                )
            except PlaywrightError as exc:
                logger.warning("Failed to extract card %d: %s", i, exc)
                continue

        return listings

    @staticmethod
    async def _safe_text(scope, selector: str) -> Optional[str]:
        loc = scope.locator(selector).first
        if await loc.count() == 0:
            return None
        return (await loc.inner_text()).strip()

    @staticmethod
    async def _safe_attr(scope, selector: str, attr: str) -> Optional[str]:
        loc = scope.locator(selector).first
        if await loc.count() == 0:
            return None
        return await loc.get_attribute(attr)

    # ------------------------------- API --------------------------------- #
    async def scrape_target(self, target: ScrapeTarget) -> list[ScrapedListing]:
        """Scrape a single target end-to-end (scroll/paginate + extract)."""
        logger.info("Scraping target '%s' (%s)", target.name, target.url)
        collected: list[ScrapedListing] = []

        async with self._new_context() as context:
            page = await context.new_page()
            current_url = target.url
            page_num = 0

            while current_url and len(collected) < target.max_items:
                page_num += 1
                if not await self._goto_with_retry(page, current_url):
                    break

                # Wait for at least one card; tolerate empty result pages.
                try:
                    await page.locator(target.listing_selector).first.wait_for(
                        timeout=self._settings.nav_timeout_seconds * 1000
                    )
                except PlaywrightTimeoutError:
                    logger.warning("No listings found on %s.", current_url)
                    break

                if target.infinite_scroll:
                    await self._auto_scroll(page, target)

                page_listings = await self._extract_listings(page, target)
                logger.info(
                    "Page %d of '%s': extracted %d listings.",
                    page_num, target.name, len(page_listings),
                )
                collected.extend(page_listings)

                # Pagination (only when not relying on infinite scroll).
                current_url = ""
                if target.next_page_selector and not target.infinite_scroll:
                    next_href = await self._safe_attr(
                        page, target.next_page_selector, "href"
                    )
                    if next_href:
                        current_url = urljoin(page.url, next_href)
                        await self._structural_delay()

        # De-duplicate by URL while preserving order.
        seen: set[str] = set()
        unique = [l for l in collected if not (l.url in seen or seen.add(l.url))]
        logger.info(
            "Target '%s' complete: %d unique listings.", target.name, len(unique)
        )
        return unique[: target.max_items]
