# Arbitrage Deal Finder & Dynamic Valuation Bot

A modular, asynchronous Python bot that scrapes local e-commerce listings,
isolates statistical price anomalies, dynamically discounts each item's value
based on detected damage/wear, and fires instant **BUY_SIGNAL** alerts to a
Discord webhook.

> ⚠️ **Use responsibly.** Only scrape marketplaces you are authorised to scrape.
> Respect each site's Terms of Service and `robots.txt`, and keep request rates
> polite (the bot ships with randomised 2–7s delays for exactly this reason).

---

## Architecture

```
config/settings.py     → env vars, API keys, proxy rules, thresholds,
                          category economics, scrape targets + selectors
config/logging_config  → project-wide logging setup
schemas.py             → shared Pydantic data contracts

scraper/engine.py      → Module A: async Playwright scraper
                          (endless scroll + pagination, UA rotation,
                           randomised delays, retry/backoff)
analyzer/grading.py    → Module B: regex condition gatekeeper
                          Module D: pandas mean/σ + arbitrage valuation
analyzer/llm_client.py → Module C: Claude structured-output condition grading
notifier/alerts.py     → Module E: async Discord rich-embed alerts

main.py                → central concurrent orchestrator loop
```

### Pipeline

```
scrape → gatekeeper screen → batch stats (mean/σ) → LLM enrich (worn only)
       → classify condition → valuation → BUY_SIGNAL → Discord alert
```

### Valuation logic

| Condition | Realistic value | Repair cost          |
| --------- | --------------- | -------------------- |
| Mint      | mean price      | 0                    |
| Fair      | mean × 0.75     | 0                    |
| Damaged   | mean × 0.45     | category parts cost  |

```
max_allowable_buy_price = realistic_value − repair_cost − min_target_profit
net_profit              = realistic_value − repair_cost − listed_price
BUY_SIGNAL              = price ≤ max_allowable_buy_price
                          AND net_profit ≥ min_target_profit
```

---

## Setup

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Install the Playwright browser
playwright install chromium

# 3. Configure secrets
cp .env.example .env
#   …then edit .env: add ANTHROPIC_API_KEY and DISCORD_WEBHOOK_URL
```

### Point it at a marketplace

Edit `SCRAPE_TARGETS` and `CATEGORY_PROFILES` in `config/settings.py`:

- **`CATEGORY_PROFILES`** — per-model economics: base mint value, default parts
  cost, minimum target profit.
- **`SCRAPE_TARGETS`** — the search URL plus the CSS selectors for the listing
  card, title, price, link, and (optionally) description / next-page link. Ships
  pointed at **books.toscrape.com**, a sandbox explicitly published for scraper
  practice (robots-clean), so `python main.py --once` works out of the box.

> ⚠️ Most real marketplaces prohibit HTML scraping in their ToS. Prefer an
> official API (below), and only point the DOM scraper at sites whose ToS /
> `robots.txt` permit it.

### Official API sources (eBay)

For real marketplace data the authorised route is an official API. The bot ships
an **eBay Browse API** adapter (`scraper/ebay_source.py`) that pulls listings
over HTTPS (OAuth client-credentials — no user login) and feeds them through the
*exact same* pipeline as the DOM scraper.

1. Create an app at <https://developer.ebay.com> and copy its Client ID / Secret.
2. Put them in `.env` (`EBAY_CLIENT_ID`, `EBAY_CLIENT_SECRET`) and set
   `EBAY_MARKETPLACE_ID` (e.g. `EBAY_GB`).
3. Tune `EBAY_TARGETS` in `config/settings.py` (query + price/condition filters).

With credentials set, eBay targets run automatically alongside any DOM targets;
without them, the eBay source is skipped. The adapter has an offline test that
mocks eBay's OAuth + search responses — no keys needed to run it:

```bash
.venv/bin/python tests/test_ebay_source.py
```

---

## Running

```bash
python main.py --once      # single scan cycle, then exit (great for testing)
python main.py             # continuous loop (SCAN_INTERVAL_SECONDS between runs)
```

The bot degrades gracefully:

- No `ANTHROPIC_API_KEY` → worn items are conservatively graded **FAIR** (no LLM).
- No `DISCORD_WEBHOOK_URL` → signals are logged but not broadcast.

---

## Roadmap (optional next steps)

- **Local history cache** — a `database/history.db` (SQLite) to persist past
  listings so rolling averages get smarter over time and alerts de-duplicate
  across cycles.
- **Scheduling** — a `cron` entry or systemd service to run `python main.py
  --once` every 10–15 minutes instead of the in-process loop.
