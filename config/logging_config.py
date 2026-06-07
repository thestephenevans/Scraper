"""Project-wide logging setup.

Every module obtains its logger via ``logging.getLogger(__name__)``; the root
configuration here gives consistent, timestamped, level-aware output. Call
``setup_logging()`` once at process start (main.py does this).
"""

from __future__ import annotations

import logging
import sys

_CONFIGURED = False


def setup_logging(level: str = "INFO") -> None:
    """Configure root logging idempotently."""
    global _CONFIGURED
    if _CONFIGURED:
        return

    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )

    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    root.handlers.clear()
    root.addHandler(handler)

    # Playwright/httpx are chatty at DEBUG; keep them at WARNING unless we are
    # explicitly debugging.
    if root.level > logging.DEBUG:
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)

    _CONFIGURED = True
