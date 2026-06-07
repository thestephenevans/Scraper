"""Module C — structured LLM enrichment via the Anthropic (Claude) API.

When the gatekeeper flags an item as worn, this client asks Claude to read the
listing text and return a strict, schema-validated condition assessment. We use
Claude's native structured-output mode (`messages.parse` with a Pydantic
`output_format`), which constrains the response to exactly the
`ConditionAssessment` schema — no brittle JSON-in-prose parsing.

The default model is `claude-opus-4-8`; set ``LLM_MODEL=claude-haiku-4-5`` in
the environment for cheaper, faster analysis on high listing volumes.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

import anthropic

from config.settings import Settings, get_settings
from schemas import ConditionAssessment, ScrapedListing

logger = logging.getLogger(__name__)


_SYSTEM_PROMPT = (
    "You are a meticulous second-hand electronics grader for an arbitrage desk. "
    "Read the marketplace listing and assess its true condition strictly from the "
    "text provided. Do not invent facts the listing does not state. Output only the "
    "structured assessment.\n\n"
    "Scoring guidance:\n"
    "- functional_integrity_score: 1.0 = works perfectly; 0.0 = non-functional. "
    "Treat 'untested'/'as is' as uncertain (~0.5) unless other signals say otherwise.\n"
    "- cosmetic_wear_score: 0.0 = flawless; 1.0 = heavy cracks/dents/scuffs.\n"
    "- estimated_repair_difficulty: 'none' if purely cosmetic and usable, 'easy' for "
    "screen/battery-class swaps, 'hard' for board-level or water-related faults.\n"
    "- justification_snippet: one sentence grounded in the listing wording."
)


def _build_user_prompt(listing: ScrapedListing, wear_flags: list[str]) -> str:
    flags = ", ".join(wear_flags) if wear_flags else "none detected"
    return (
        f"TITLE: {listing.title}\n"
        f"DESCRIPTION: {listing.description or '(none provided)'}\n"
        f"LISTED_PRICE: {listing.price}\n"
        f"PRE-FLAGGED WEAR KEYWORDS: {flags}\n\n"
        "Assess the condition."
    )


class ConditionLLMClient:
    """Async wrapper around Claude for structured condition assessment."""

    def __init__(self, settings: Optional[Settings] = None) -> None:
        self._settings = settings or get_settings()
        self._client: Optional[anthropic.AsyncAnthropic] = None
        # Bound concurrency so a large batch doesn't hammer the API / rate limits.
        self._semaphore = asyncio.Semaphore(self._settings.llm_max_concurrency)

        if self._settings.llm_enabled:
            self._client = anthropic.AsyncAnthropic(
                api_key=self._settings.anthropic_api_key,
                timeout=self._settings.llm_timeout_seconds,
            )
        else:
            logger.warning(
                "ANTHROPIC_API_KEY not set — LLM enrichment disabled; worn items "
                "will be graded conservatively as FAIR."
            )

    @property
    def enabled(self) -> bool:
        return self._client is not None

    async def assess(
        self, listing: ScrapedListing, wear_flags: Optional[list[str]] = None
    ) -> Optional[ConditionAssessment]:
        """Return a structured assessment, or ``None`` if disabled/failed."""
        if self._client is None:
            return None

        async with self._semaphore:
            try:
                message = await self._client.messages.parse(
                    model=self._settings.llm_model,
                    max_tokens=self._settings.llm_max_tokens,
                    system=_SYSTEM_PROMPT,
                    messages=[
                        {
                            "role": "user",
                            "content": _build_user_prompt(listing, wear_flags or []),
                        }
                    ],
                    output_format=ConditionAssessment,
                )
            except anthropic.APIError as exc:
                logger.error("LLM assessment failed for '%s': %s", listing.title, exc)
                return None

            assessment = message.parsed_output
            if assessment is None:
                logger.warning(
                    "LLM returned no parsable assessment for '%s' (stop_reason=%s)",
                    listing.title, message.stop_reason,
                )
                return None

            logger.debug(
                "Assessed '%s': func=%.2f cosmetic=%.2f repair=%s",
                listing.title,
                assessment.functional_integrity_score,
                assessment.cosmetic_wear_score,
                assessment.estimated_repair_difficulty,
            )
            return assessment

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.close()
