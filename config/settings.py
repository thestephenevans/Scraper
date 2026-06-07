"""Central configuration: environment variables, API keys, proxy rules, thresholds.

All tunables live here so the rest of the codebase imports a single, validated
`Settings` object via `get_settings()`. Secrets are sourced from the environment
(or a local `.env`); structural config (category economics, scrape targets,
selectors) lives as typed Python objects so they are diffable and reviewable.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from schemas import Condition  # noqa: F401  (re-exported convenience)


# --------------------------------------------------------------------------- #
#  Static structural config (not secret) — category economics & scrape targets #
# --------------------------------------------------------------------------- #
from dataclasses import dataclass, field


@dataclass(frozen=True)
class CategoryProfile:
    """Economic profile for a model category, fed into the math engine.

    `base_mint_value` is a sanity anchor for the category's mint price. The live
    `mean_price` from a freshly scraped batch is what actually drives valuation;
    `base_mint_value` is used as a fallback when the batch is too small for a
    statistically meaningful mean.
    """

    name: str
    base_mint_value: float
    default_parts_cost: float
    min_target_profit: float
    # Minimum batch size before we trust the live mean over base_mint_value.
    min_sample_size: int = 5


@dataclass(frozen=True)
class ScrapeTarget:
    """A single search page to scrape, plus the CSS selectors to extract it.

    Selectors are intentionally externalised: pointing the bot at a new
    marketplace is a config change, not a code change.
    """

    category: str  # must match a key in CATEGORY_PROFILES
    name: str
    url: str
    listing_selector: str          # repeating card element
    title_selector: str
    price_selector: str
    link_selector: str
    description_selector: str = ""  # optional; falls back to title text
    next_page_selector: str = ""    # optional; enables pagination
    infinite_scroll: bool = True    # endless-scroll vs. paginated
    max_items: int = 60


# Category economics. Tune base values / parts cost / target profit per model.
CATEGORY_PROFILES: dict[str, CategoryProfile] = {
    "iphone_13": CategoryProfile(
        name="Apple iPhone 13",
        base_mint_value=420.0,
        default_parts_cost=85.0,   # typical screen + battery refurb cost
        min_target_profit=70.0,
    ),
    "ps5": CategoryProfile(
        name="Sony PlayStation 5",
        base_mint_value=380.0,
        default_parts_cost=60.0,
        min_target_profit=55.0,
    ),
}


# Scrape targets. The selectors below are placeholders — set them to match the
# actual marketplace DOM you are authorised to scrape.
SCRAPE_TARGETS: list[ScrapeTarget] = [
    ScrapeTarget(
        category="iphone_13",
        name="example-marketplace:iphone-13",
        url="https://example-marketplace.test/search?q=iphone+13",
        listing_selector="[data-testid='listing-card']",
        title_selector="[data-testid='listing-title']",
        price_selector="[data-testid='listing-price']",
        link_selector="a[data-testid='listing-link']",
        description_selector="[data-testid='listing-subtitle']",
        infinite_scroll=True,
        max_items=60,
    ),
]


# --------------------------------------------------------------------------- #
#  Runtime settings (env-sourced)                                              #
# --------------------------------------------------------------------------- #
class Settings(BaseSettings):
    """Env-backed runtime settings. Reads from process env and an optional .env."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- LLM (Anthropic / Claude) ---
    anthropic_api_key: str = Field(default="", alias="ANTHROPIC_API_KEY")
    # Defaults to the current flagship. Swap to "claude-haiku-4-5" for cheaper,
    # higher-throughput condition analysis if cost matters more than nuance.
    llm_model: str = Field(default="claude-opus-4-8", alias="LLM_MODEL")
    llm_max_tokens: int = Field(default=512, alias="LLM_MAX_TOKENS")
    llm_timeout_seconds: float = Field(default=30.0, alias="LLM_TIMEOUT_SECONDS")
    llm_max_concurrency: int = Field(default=5, alias="LLM_MAX_CONCURRENCY")

    # --- Notifier (Discord) ---
    discord_webhook_url: str = Field(default="", alias="DISCORD_WEBHOOK_URL")
    notifier_timeout_seconds: float = Field(default=10.0, alias="NOTIFIER_TIMEOUT_SECONDS")

    # --- Scraper anti-bot / resilience ---
    headless: bool = Field(default=True, alias="SCRAPER_HEADLESS")
    min_delay_seconds: float = Field(default=2.0, alias="SCRAPER_MIN_DELAY")
    max_delay_seconds: float = Field(default=7.0, alias="SCRAPER_MAX_DELAY")
    max_retries: int = Field(default=3, alias="SCRAPER_MAX_RETRIES")
    nav_timeout_seconds: float = Field(default=30.0, alias="SCRAPER_NAV_TIMEOUT")
    max_scroll_rounds: int = Field(default=15, alias="SCRAPER_MAX_SCROLL_ROUNDS")
    # Optional outbound proxy, e.g. "http://user:pass@host:port".
    proxy_server: str = Field(default="", alias="SCRAPER_PROXY")

    # --- Analyzer thresholds ---
    # Below this functional score (or with any non-trivial repair difficulty) a
    # worn item is graded "damaged" rather than "fair".
    functional_integrity_threshold: float = Field(
        default=0.85, alias="FUNCTIONAL_INTEGRITY_THRESHOLD"
    )
    cosmetic_fair_ceiling: float = Field(default=0.4, alias="COSMETIC_FAIR_CEILING")
    # z-score below this => statistically underpriced anomaly worth deep analysis.
    anomaly_z_threshold: float = Field(default=-1.0, alias="ANOMALY_Z_THRESHOLD")

    # --- Orchestrator ---
    scan_interval_seconds: float = Field(default=600.0, alias="SCAN_INTERVAL_SECONDS")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    # Rotating User-Agent pool used by the scraper to blend in.
    user_agents: list[str] = Field(
        default_factory=lambda: [
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
            "(KHTML, like Gecko) Version/17.4 Safari/605.1.15",
            "Mozilla/5.0 (X11; Linux x86_64; rv:124.0) Gecko/20100101 Firefox/124.0",
            "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 "
            "(KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1",
        ]
    )

    @property
    def llm_enabled(self) -> bool:
        return bool(self.anthropic_api_key)

    @property
    def notifier_enabled(self) -> bool:
        return bool(self.discord_webhook_url)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide singleton settings instance."""
    return Settings()
