"""Shared data-structure schemas for the Arbitrage Deal Finder.

Every cross-module payload flows through one of the Pydantic models defined here.
Centralising the schemas keeps the scraper, analyzer, LLM client, and notifier
strictly contract-bound to each other and gives us runtime validation for free.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, HttpUrl, field_validator


class Condition(str, Enum):
    """Coarse condition tiers that drive the valuation multipliers."""

    MINT = "mint"
    FAIR = "fair"
    DAMAGED = "damaged"
    JUNK = "junk"


class ScrapedListing(BaseModel):
    """Strict, normalised shape emitted by the scraper engine.

    Matches the blueprint contract:
    {'title': str, 'description': str, 'price': float, 'url': str, 'raw_metadata': dict}
    """

    title: str
    description: str = ""
    price: float = Field(..., ge=0.0, description="Listed price in the source currency.")
    url: str
    raw_metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("title", "description")
    @classmethod
    def _strip(cls, value: str) -> str:
        return value.strip()


class GatekeeperVerdict(BaseModel):
    """Output of the fast regex condition gatekeeper (grading.py — Part 1)."""

    is_viable: bool = True
    has_wear: bool = False
    label: Condition = Condition.MINT
    critical_flags: list[str] = Field(default_factory=list)
    wear_flags: list[str] = Field(default_factory=list)


class ConditionAssessment(BaseModel):
    """Strict structured-output schema returned by the LLM enrichment step.

    The field set is the exact contract the LLM is forced to emit via Claude's
    JSON-schema structured-output mode.
    """

    functional_integrity_score: float = Field(
        ..., ge=0.0, le=1.0,
        description="1.0 = fully functional, 0.0 = non-functional.",
    )
    cosmetic_wear_score: float = Field(
        ..., ge=0.0, le=1.0,
        description="0.0 = flawless, 1.0 = heavily worn/cracked.",
    )
    estimated_repair_difficulty: Literal["none", "easy", "hard"]
    justification_snippet: str = Field(
        ..., description="One-sentence rationale grounded in the listing text.",
    )


class ValuationResult(BaseModel):
    """Final per-listing arbitrage verdict (grading.py — Part 2)."""

    listing: ScrapedListing
    condition: Condition

    mean_price: float
    std_dev: float
    z_score: float = Field(..., description="(price - mean) / std_dev; negative = underpriced.")
    is_price_anomaly: bool

    realistic_value: float
    repair_cost: float
    max_allowable_buy_price: float
    net_profit: float

    buy_signal: bool
    assessment: Optional[ConditionAssessment] = None

    @property
    def margin_pct(self) -> float:
        """Net profit as a percentage of the listed price (guards div-by-zero)."""
        if self.listing.price <= 0:
            return 0.0
        return round((self.net_profit / self.listing.price) * 100.0, 1)
