"""Configuration package: settings, category profiles, site configs, logging."""

from config.settings import (  # noqa: F401
    CATEGORY_PROFILES,
    SCRAPE_TARGETS,
    CategoryProfile,
    ScrapeTarget,
    Settings,
    get_settings,
)
