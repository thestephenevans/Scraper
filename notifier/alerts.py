"""Module E — the asynchronous Discord notification gateway.

When a `ValuationResult` carries a BUY_SIGNAL, this module formats a rich Discord
embed ("card") and POSTs it to the configured webhook. The embed surfaces every
metric a buyer needs to act in seconds: live link, detected condition, listed vs.
realistic value, repair cost, and projected net margin.
"""

from __future__ import annotations

import logging
from typing import Optional

import httpx

from config.settings import CategoryProfile, Settings, get_settings
from schemas import Condition, ValuationResult

logger = logging.getLogger(__name__)

# Embed accent colours by condition (Discord uses decimal RGB ints).
_CONDITION_COLOUR: dict[Condition, int] = {
    Condition.MINT: 0x2ECC71,     # green
    Condition.FAIR: 0xF1C40F,     # amber
    Condition.DAMAGED: 0xE67E22,  # orange
    Condition.JUNK: 0x95A5A6,     # grey
}


class DiscordNotifier:
    """Posts rich BUY_SIGNAL alert cards to a Discord webhook."""

    def __init__(self, settings: Optional[Settings] = None) -> None:
        self._settings = settings or get_settings()
        self._client = httpx.AsyncClient(timeout=self._settings.notifier_timeout_seconds)

    @property
    def enabled(self) -> bool:
        return self._settings.notifier_enabled

    def _build_embed(
        self, result: ValuationResult, profile: Optional[CategoryProfile]
    ) -> dict:
        listing = result.listing
        category_name = profile.name if profile else listing.raw_metadata.get("category", "?")
        condition = result.condition

        fields = [
            {
                "name": "💷 Listed Price",
                "value": f"`{listing.price:,.2f}`",
                "inline": True,
            },
            {
                "name": "📊 Realistic Value",
                "value": f"`{result.realistic_value:,.2f}`",
                "inline": True,
            },
            {
                "name": "🔧 Repair Cost",
                "value": f"`{result.repair_cost:,.2f}`",
                "inline": True,
            },
            {
                "name": "✅ Max Buy Price",
                "value": f"`{result.max_allowable_buy_price:,.2f}`",
                "inline": True,
            },
            {
                "name": "💰 Net Profit",
                "value": f"`{result.net_profit:,.2f}` ({result.margin_pct}%)",
                "inline": True,
            },
            {
                "name": "🏷️ Condition",
                "value": f"`{condition.value.upper()}`",
                "inline": True,
            },
            {
                "name": "📉 Market (mean ± σ)",
                "value": f"`{result.mean_price:,.2f} ± {result.std_dev:,.2f}` "
                         f"(z={result.z_score})",
                "inline": False,
            },
        ]

        if result.assessment is not None:
            a = result.assessment
            fields.append(
                {
                    "name": "🤖 AI Condition Read",
                    "value": (
                        f"Functional `{a.functional_integrity_score:.2f}` · "
                        f"Cosmetic `{a.cosmetic_wear_score:.2f}` · "
                        f"Repair `{a.estimated_repair_difficulty}`\n"
                        f"_{a.justification_snippet}_"
                    ),
                    "inline": False,
                }
            )

        return {
            "title": f"🚀 BUY SIGNAL — {listing.title[:230]}",
            "url": listing.url,
            "description": f"**{category_name}** · source: "
                           f"`{listing.raw_metadata.get('source', 'unknown')}`",
            "color": _CONDITION_COLOUR.get(condition, 0x3498DB),
            "fields": fields,
            "footer": {"text": "Arbitrage Deal Finder"},
        }

    async def send_buy_signal(
        self, result: ValuationResult, profile: Optional[CategoryProfile] = None
    ) -> bool:
        """POST a single BUY_SIGNAL alert. Returns True on success."""
        if not self.enabled:
            logger.warning("Discord webhook not configured — skipping alert.")
            return False
        if not result.buy_signal:
            logger.debug("send_buy_signal called for non-signal listing; ignoring.")
            return False

        payload = {
            "username": "Arbitrage Bot",
            "embeds": [self._build_embed(result, profile)],
        }

        try:
            response = await self._client.post(
                self._settings.discord_webhook_url, json=payload
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            logger.error(
                "Discord rejected alert for '%s': %s — %s",
                result.listing.title, exc.response.status_code, exc.response.text[:200],
            )
            return False
        except httpx.HTTPError as exc:
            logger.error("Network error posting alert for '%s': %s", result.listing.title, exc)
            return False

        logger.info("Alert dispatched for '%s'.", result.listing.title)
        return True

    async def aclose(self) -> None:
        await self._client.aclose()
