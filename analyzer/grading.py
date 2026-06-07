"""Modules B & D — the condition gatekeeper and the arbitrage math engine.

Part 1 (Module B): a fast, regex-based keyword gatekeeper that screens listing
text *before* any LLM tokens are spent — instantly junking non-viable items and
flagging cosmetic/functional wear for deeper analysis.

Part 2 (Module D): a pure, deterministic valuation engine. Given a batch of
freshly scraped listings and the category's economic profile, it computes the
rolling mean / standard deviation with pandas, derives a realistic value from
the condition tier, and emits a BUY_SIGNAL when the listed price clears the
maximum allowable buy price with sufficient margin.
"""

from __future__ import annotations

import logging
import re
from typing import Optional

import pandas as pd

from config.settings import CategoryProfile, Settings, get_settings
from schemas import (
    Condition,
    ConditionAssessment,
    GatekeeperVerdict,
    ScrapedListing,
    ValuationResult,
)

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
#  PART 1 — Condition Keyword Gatekeeper (Module B)                            #
# --------------------------------------------------------------------------- #

# Hard-fail terms: any match => item is "Junk", non-viable, valuation skipped.
CRITICAL_FAIL: list[str] = [
    "icloud locked",
    "activation lock",
    "for parts",
    "burn",
    "water damage",
    "spares",
    "broken",
]

# Soft-wear terms: any match => has_wear=True, hand off to the LLM/math engine.
WEAR_FLAGS: list[str] = [
    "crack",
    "scratch",
    "heavy wear",
    "dent",
    "fair",
    "scuff",
    "as is",
    "untested",
]


def _compile(terms: list[str]) -> re.Pattern[str]:
    """Compile a case-insensitive, stem-aware alternation pattern.

    A leading ``\\b`` anchors each term at a word start (so "broken" does not
    match inside "unbroken"), while a trailing ``\\w*`` lets a stem catch its
    inflections — "crack" matches "cracked"/"cracks", "scratch" matches
    "scratched", etc. Multi-word phrases like "water damage" are matched whole.
    """
    escaped = [r"\b" + re.escape(term) + r"\w*" for term in terms]
    return re.compile("|".join(escaped), flags=re.IGNORECASE)


_CRITICAL_RE = _compile(CRITICAL_FAIL)
_WEAR_RE = _compile(WEAR_FLAGS)


def screen_listing(listing: ScrapedListing) -> GatekeeperVerdict:
    """Regex pre-filter over title + description (Module B).

    Returns a verdict marking the item as junk, worn, or clean. Critical fails
    short-circuit: a worn *and* critically-failed listing is still junk.
    """
    haystack = f"{listing.title}\n{listing.description}".lower()

    critical_hits = sorted({m.group(0).lower() for m in _CRITICAL_RE.finditer(haystack)})
    if critical_hits:
        logger.debug("Junk '%s' — critical flags: %s", listing.title, critical_hits)
        return GatekeeperVerdict(
            is_viable=False,
            has_wear=True,
            label=Condition.JUNK,
            critical_flags=critical_hits,
        )

    wear_hits = sorted({m.group(0).lower() for m in _WEAR_RE.finditer(haystack)})
    if wear_hits:
        logger.debug("Wear '%s' — flags: %s", listing.title, wear_hits)
        return GatekeeperVerdict(is_viable=True, has_wear=True, wear_flags=wear_hits)

    return GatekeeperVerdict(is_viable=True, has_wear=False, label=Condition.MINT)


def classify_condition(
    verdict: GatekeeperVerdict,
    assessment: Optional[ConditionAssessment],
    settings: Optional[Settings] = None,
) -> Condition:
    """Resolve the final condition tier from the gatekeeper + LLM assessment.

    * No wear flags          -> MINT
    * Wear but LLM unavailable-> FAIR (conservative; no repair cost assumed)
    * Wear + sound function   -> FAIR
    * Wear + functional/repair concerns -> DAMAGED
    """
    settings = settings or get_settings()

    if not verdict.is_viable:
        return Condition.JUNK
    if not verdict.has_wear:
        return Condition.MINT
    if assessment is None:
        # Worn but we could not enrich — treat as FAIR rather than over-discount.
        return Condition.FAIR

    damaged = (
        assessment.functional_integrity_score < settings.functional_integrity_threshold
        or assessment.estimated_repair_difficulty != "none"
        or assessment.cosmetic_wear_score > settings.cosmetic_fair_ceiling
    )
    return Condition.DAMAGED if damaged else Condition.FAIR


# --------------------------------------------------------------------------- #
#  PART 2 — Dynamic Math & Arbitrage Engine (Module D)                         #
# --------------------------------------------------------------------------- #

# Condition -> realistic-value multiplier of the category mean price.
_VALUE_MULTIPLIER: dict[Condition, float] = {
    Condition.MINT: 1.00,
    Condition.FAIR: 0.75,
    Condition.DAMAGED: 0.45,
}


class ArbitrageEngine:
    """Computes batch price statistics and per-listing buy verdicts."""

    def __init__(self, settings: Optional[Settings] = None) -> None:
        self._settings = settings or get_settings()

    def compute_statistics(
        self, listings: list[ScrapedListing], profile: CategoryProfile
    ) -> tuple[float, float]:
        """Return (mean_price, std_dev) for a batch using pandas.

        Falls back to the profile's ``base_mint_value`` for the mean when the
        sample is too small to be trustworthy; std_dev defaults to 0.0 in that
        case (no anomaly signal without spread).
        """
        prices = pd.Series([l.price for l in listings if l.price > 0], dtype="float64")
        if len(prices) < profile.min_sample_size:
            logger.info(
                "Sample for '%s' too small (%d < %d) — anchoring mean to base value %.2f",
                profile.name, len(prices), profile.min_sample_size, profile.base_mint_value,
            )
            return profile.base_mint_value, 0.0

        mean = float(prices.mean())
        # Population std would understate spread on small samples; sample std (ddof=1).
        std = float(prices.std(ddof=1)) if len(prices) > 1 else 0.0
        logger.info("'%s' stats — mean=%.2f std=%.2f (n=%d)", profile.name, mean, std, len(prices))
        return mean, std

    def evaluate(
        self,
        listing: ScrapedListing,
        condition: Condition,
        mean_price: float,
        std_dev: float,
        profile: CategoryProfile,
        assessment: Optional[ConditionAssessment] = None,
    ) -> ValuationResult:
        """Apply the full arbitrage calculation to a single listing.

        Logic (per blueprint):
            realistic_value         = mean * multiplier(condition)
            repair_cost             = parts_cost if DAMAGED else 0
            max_allowable_buy_price = realistic_value - repair_cost - target_profit
            net_profit              = realistic_value - repair_cost - listed_price
            BUY_SIGNAL              = price <= max_allowable AND net_profit >= target_profit
        """
        multiplier = _VALUE_MULTIPLIER.get(condition, 0.0)
        realistic_value = round(mean_price * multiplier, 2)
        repair_cost = round(profile.default_parts_cost, 2) if condition == Condition.DAMAGED else 0.0

        max_allowable = round(realistic_value - repair_cost - profile.min_target_profit, 2)
        net_profit = round(realistic_value - repair_cost - listing.price, 2)

        z_score = round((listing.price - mean_price) / std_dev, 2) if std_dev > 0 else 0.0
        is_anomaly = std_dev > 0 and z_score <= self._settings.anomaly_z_threshold

        buy_signal = (
            condition != Condition.JUNK
            and listing.price <= max_allowable
            and net_profit >= profile.min_target_profit
        )

        result = ValuationResult(
            listing=listing,
            condition=condition,
            mean_price=round(mean_price, 2),
            std_dev=round(std_dev, 2),
            z_score=z_score,
            is_price_anomaly=is_anomaly,
            realistic_value=realistic_value,
            repair_cost=repair_cost,
            max_allowable_buy_price=max_allowable,
            net_profit=net_profit,
            buy_signal=buy_signal,
            assessment=assessment,
        )

        if buy_signal:
            logger.info(
                "BUY_SIGNAL '%s' @ %.2f (value=%.2f, max_buy=%.2f, profit=%.2f, %s)",
                listing.title, listing.price, realistic_value, max_allowable,
                net_profit, condition.value,
            )
        return result
