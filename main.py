"""Central orchestrator for the Arbitrage Deal Finder & Dynamic Valuation Bot.

Workflow per scan cycle, run concurrently across all configured targets:

    scrape  ->  gatekeeper screen  ->  batch statistics  ->  LLM enrich (worn)
            ->  classify condition ->  valuation         ->  BUY_SIGNAL alert

Run once:        python main.py --once
Run on a loop:   python main.py            (interval from SCAN_INTERVAL_SECONDS)
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from contextlib import AsyncExitStack

from analyzer.grading import ArbitrageEngine, classify_condition, screen_listing
from analyzer.llm_client import ConditionLLMClient
from config.logging_config import setup_logging
from config.settings import (
    CATEGORY_PROFILES,
    EBAY_TARGETS,
    SCRAPE_TARGETS,
    CategoryProfile,
    EbayTarget,
    ScrapeTarget,
    Settings,
    get_settings,
)
from notifier.alerts import DiscordNotifier
from schemas import ConditionAssessment, ScrapedListing, ValuationResult
from scraper.ebay_source import EbayBrowseSource
from scraper.engine import ScraperEngine

logger = logging.getLogger(__name__)


class Orchestrator:
    """Wires the modules together and drives scan cycles."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._llm = ConditionLLMClient(self._settings)
        self._engine = ArbitrageEngine(self._settings)
        self._notifier = DiscordNotifier(self._settings)

    async def process_target(
        self, source, target: ScrapeTarget | EbayTarget
    ) -> list[ValuationResult]:
        """Full pipeline for one target from any source. Returns BUY_SIGNAL results.

        ``source`` is anything exposing ``async collect(target) -> list[ScrapedListing]``
        — the Playwright ``ScraperEngine`` or the ``EbayBrowseSource``. The pipeline
        below is identical regardless of where the listings came from.
        """
        profile: CategoryProfile | None = CATEGORY_PROFILES.get(target.category)
        if profile is None:
            logger.error("No category profile for '%s' — skipping target.", target.category)
            return []

        try:
            listings = await source.collect(target)
        except Exception as exc:  # one bad target must not kill the whole cycle
            logger.exception("Collect failed for '%s': %s", target.name, exc)
            return []

        if not listings:
            return []

        # --- Module B: regex gatekeeper screen ---
        verdicts = {l.url: screen_listing(l) for l in listings}
        viable = [l for l in listings if verdicts[l.url].is_viable]
        logger.info(
            "'%s': %d viable, %d junked by gatekeeper.",
            target.name, len(viable), len(listings) - len(viable),
        )
        if not viable:
            return []

        # --- Module D: batch statistics over viable listings ---
        mean_price, std_dev = self._engine.compute_statistics(viable, profile)

        # --- Module C: concurrent LLM enrichment for worn items only ---
        worn = [l for l in viable if verdicts[l.url].has_wear]
        assessments: dict[str, ConditionAssessment | None] = {}
        if worn and self._llm.enabled:
            logger.info("Enriching %d worn listings via LLM…", len(worn))
            results = await asyncio.gather(
                *(self._llm.assess(l, verdicts[l.url].wear_flags) for l in worn)
            )
            assessments = {l.url: a for l, a in zip(worn, results)}

        # --- Classify + value every viable listing ---
        signals: list[ValuationResult] = []
        for listing in viable:
            verdict = verdicts[listing.url]
            assessment = assessments.get(listing.url)
            condition = classify_condition(verdict, assessment, self._settings)
            result = self._engine.evaluate(
                listing=listing,
                condition=condition,
                mean_price=mean_price,
                std_dev=std_dev,
                profile=profile,
                assessment=assessment,
            )
            if result.buy_signal:
                signals.append(result)

        # --- Module E: broadcast alerts concurrently ---
        if signals and self._notifier.enabled:
            await asyncio.gather(
                *(self._notifier.send_buy_signal(s, profile) for s in signals)
            )

        logger.info("'%s': %d BUY_SIGNALS.", target.name, len(signals))
        return signals

    async def run_once(self) -> list[ValuationResult]:
        """Execute one full scan cycle across every configured source concurrently."""
        n_targets = len(SCRAPE_TARGETS) + (len(EBAY_TARGETS) if self._settings.ebay_enabled else 0)
        logger.info("=== Scan cycle starting (%d targets) ===", n_targets)
        all_signals: list[ValuationResult] = []

        async with AsyncExitStack() as stack:
            tasks = []
            # DOM scraper — only spun up (launching Chromium) when there's web work.
            if SCRAPE_TARGETS:
                web = await stack.enter_async_context(ScraperEngine(self._settings))
                tasks += [self.process_target(web, t) for t in SCRAPE_TARGETS]
            # eBay Browse API — only when credentials are configured.
            if EBAY_TARGETS and self._settings.ebay_enabled:
                ebay = await stack.enter_async_context(EbayBrowseSource(self._settings))
                tasks += [self.process_target(ebay, t) for t in EBAY_TARGETS]

            results = await asyncio.gather(*tasks) if tasks else []

        for batch in results:
            all_signals.extend(batch)
        logger.info("=== Scan cycle complete: %d total BUY_SIGNALS ===", len(all_signals))
        return all_signals

    async def run_forever(self) -> None:
        """Loop scan cycles forever, sleeping between runs."""
        while True:
            try:
                await self.run_once()
            except Exception as exc:  # never let the loop die on a transient error
                logger.exception("Scan cycle errored: %s", exc)
            logger.info(
                "Sleeping %.0fs until next cycle…", self._settings.scan_interval_seconds
            )
            await asyncio.sleep(self._settings.scan_interval_seconds)

    async def aclose(self) -> None:
        await self._llm.aclose()
        await self._notifier.aclose()


async def _amain(run_once: bool) -> None:
    settings = get_settings()
    setup_logging(settings.log_level)

    if not settings.llm_enabled:
        logger.warning("Running without LLM enrichment (no ANTHROPIC_API_KEY).")
    if not settings.notifier_enabled:
        logger.warning("Running without alerting (no DISCORD_WEBHOOK_URL).")
    if EBAY_TARGETS and not settings.ebay_enabled:
        logger.warning(
            "%d eBay target(s) configured but EBAY_CLIENT_ID/SECRET unset — eBay source skipped.",
            len(EBAY_TARGETS),
        )

    orchestrator = Orchestrator(settings)
    try:
        if run_once:
            await orchestrator.run_once()
        else:
            await orchestrator.run_forever()
    finally:
        await orchestrator.aclose()


def main() -> None:
    parser = argparse.ArgumentParser(description="Arbitrage Deal Finder Bot")
    parser.add_argument(
        "--once", action="store_true", help="Run a single scan cycle and exit."
    )
    args = parser.parse_args()
    try:
        asyncio.run(_amain(run_once=args.once))
    except KeyboardInterrupt:
        logging.getLogger(__name__).info("Interrupted — shutting down.")


if __name__ == "__main__":
    main()
