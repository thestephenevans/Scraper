"""Offline verification of the eBay Browse API adapter — no credentials needed.

We stand up a fake eBay using ``httpx.MockTransport`` (returning a canned OAuth
token + a realistic Browse API search payload), run the real ``EbayBrowseSource``
against it, then push the mapped listings through the *real* gatekeeper and
arbitrage engine. This proves the whole eBay path — auth → search → JSON mapping
→ pipeline — works end-to-end before production keys ever exist.

Run:  .venv/bin/python tests/test_ebay_source.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from types import SimpleNamespace

# Make the project importable when run as a plain script.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx

from analyzer.grading import ArbitrageEngine, classify_condition, screen_listing
from config.settings import CATEGORY_PROFILES, EbayTarget
from schemas import Condition
from scraper.ebay_source import EbayBrowseSource

# --- A realistic slice of an eBay Browse API item_summary/search response ------
# 5 clean "Used" items, 1 cosmetically cracked (-> FAIR), 1 "For parts" (-> JUNK).
_ITEMS = [
    {"itemId": "v1|1|0", "title": "Apple iPhone 13 128GB Unlocked", "condition": "Used",
     "conditionId": "3000", "price": {"value": "300.00", "currency": "GBP"},
     "itemWebUrl": "https://www.ebay.co.uk/itm/1", "buyingOptions": ["FIXED_PRICE"],
     "seller": {"username": "seller_a"}},
    {"itemId": "v1|2|0", "title": "Apple iPhone 13 Good Condition", "condition": "Used",
     "conditionId": "3000", "price": {"value": "280.00", "currency": "GBP"},
     "itemWebUrl": "https://www.ebay.co.uk/itm/2"},
    {"itemId": "v1|3|0", "title": "Apple iPhone 13 256GB Blue", "condition": "Used",
     "conditionId": "3000", "price": {"value": "350.00", "currency": "GBP"},
     "itemWebUrl": "https://www.ebay.co.uk/itm/3"},
    {"itemId": "v1|4|0", "title": "Apple iPhone 13 mini 128GB", "condition": "Used",
     "conditionId": "3000", "price": {"value": "250.00", "currency": "GBP"},
     "itemWebUrl": "https://www.ebay.co.uk/itm/4"},
    {"itemId": "v1|5|0", "title": "Apple iPhone 13 64GB Bargain", "condition": "Used",
     "conditionId": "3000", "price": {"value": "150.00", "currency": "GBP"},
     "itemWebUrl": "https://www.ebay.co.uk/itm/5"},  # underpriced MINT -> BUY_SIGNAL
    {"itemId": "v1|6|0", "title": "Apple iPhone 13 128GB", "condition": "Used",
     "shortDescription": "small cracked corner", "conditionId": "3000",
     "price": {"value": "180.00", "currency": "GBP"},
     "itemWebUrl": "https://www.ebay.co.uk/itm/6"},  # cosmetic wear -> FAIR
    {"itemId": "v1|7|0", "title": "Apple iPhone 13", "condition": "For parts or not working",
     "conditionId": "7000", "price": {"value": "90.00", "currency": "GBP"},
     "itemWebUrl": "https://www.ebay.co.uk/itm/7"},  # critical fail -> JUNK
]
_SEARCH_PAYLOAD = {"href": "...", "total": len(_ITEMS), "limit": 50, "offset": 0,
                   "itemSummaries": _ITEMS}

_calls: list[tuple[str, str]] = []        # (method, path)
_search_requests: list[httpx.Request] = []  # captured for header/param assertions


def _handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    _calls.append((request.method, path))
    if path.endswith("/identity/v1/oauth2/token"):
        assert request.headers["Authorization"].startswith("Basic "), "missing Basic auth"
        return httpx.Response(200, json={
            "access_token": "TEST.TOKEN.VALUE",
            "expires_in": 7200,
            "token_type": "Application Access Token",
        })
    if path.endswith("/buy/browse/v1/item_summary/search"):
        _search_requests.append(request)
        return httpx.Response(200, json=_SEARCH_PAYLOAD)
    return httpx.Response(404, json={"error": f"unexpected path {path}"})


def _check(label: str, ok: bool) -> bool:
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    return ok


async def main() -> int:
    settings = SimpleNamespace(
        ebay_client_id="fake-client-id",
        ebay_client_secret="fake-client-secret",
        ebay_env="production",
        ebay_marketplace_id="EBAY_GB",
        ebay_timeout_seconds=10.0,
        # thresholds used by the analyzer (assessment is None here, so unused, but present)
        anomaly_z_threshold=-1.0,
        functional_integrity_threshold=0.85,
        cosmetic_fair_ceiling=0.4,
    )
    target = EbayTarget(
        category="iphone_13", name="ebay:iphone-13", query="iPhone 13",
        price_min=120, price_max=420,
        conditions=("USED", "FOR_PARTS_OR_NOT_WORKING"), sort="price", max_items=50,
    )

    client = httpx.AsyncClient(transport=httpx.MockTransport(_handler))
    async with EbayBrowseSource(settings=settings, client=client) as src:
        listings = await src.collect(target)
    await client.aclose()

    results: list[bool] = []
    print("Adapter — auth, search, mapping:")
    results.append(_check("all 7 items mapped to ScrapedListing", len(listings) == 7))
    results.append(_check("token endpoint hit exactly once",
                          sum(1 for m, p in _calls if p.endswith("/oauth2/token")) == 1))
    results.append(_check("search endpoint hit exactly once", len(_search_requests) == 1))

    req = _search_requests[0]
    results.append(_check("search carried Bearer token",
                          req.headers.get("Authorization") == "Bearer TEST.TOKEN.VALUE"))
    results.append(_check("marketplace header set",
                          req.headers.get("X-EBAY-C-MARKETPLACE-ID") == "EBAY_GB"))
    flt = req.url.params.get("filter") or ""
    results.append(_check("price filter built", "price:[120..420]" in flt and "priceCurrency:GBP" in flt))
    results.append(_check("condition filter built",
                          "conditions:{USED|FOR_PARTS_OR_NOT_WORKING}" in flt))

    by_price = {l.price: l for l in listings}
    results.append(_check("price parsed (£150 item present)", 150.0 in by_price))
    cracked = next((l for l in listings if "cracked" in l.description.lower()), None)
    results.append(_check("eBay condition folded into description",
                          cracked is not None and "cracked" in cracked.description.lower()))

    print("\nGatekeeper — condition screening on real eBay condition text:")
    verdicts = {l.url: screen_listing(l) for l in listings}
    parts = next(l for l in listings if l.price == 90.0)
    results.append(_check("'For parts or not working' -> JUNK / non-viable",
                          not verdicts[parts.url].is_viable
                          and verdicts[parts.url].label == Condition.JUNK))
    results.append(_check("'cracked corner' -> viable but flagged as wear",
                          verdicts[cracked.url].is_viable and verdicts[cracked.url].has_wear))

    print("\nEnd-to-end — stats + valuation through the real engine:")
    engine = ArbitrageEngine(settings)
    profile = CATEGORY_PROFILES["iphone_13"]
    viable = [l for l in listings if verdicts[l.url].is_viable]
    mean, std = engine.compute_statistics(viable, profile)
    signals = []
    for l in viable:
        cond = classify_condition(verdicts[l.url], None, settings)
        res = engine.evaluate(listing=l, condition=cond, mean_price=mean, std_dev=std, profile=profile)
        if res.buy_signal:
            signals.append(res)

    results.append(_check("6 viable listings after gatekeeper", len(viable) == 6))
    results.append(_check(f"batch mean ≈ 251.67 (got {mean:.2f})", abs(mean - 251.67) < 0.5))
    results.append(_check("exactly 1 BUY_SIGNAL", len(signals) == 1))
    results.append(_check("the £150 item is the BUY_SIGNAL",
                          bool(signals) and signals[0].listing.price == 150.0))

    passed, total = sum(results), len(results)
    print(f"\n{'=' * 48}\n{passed}/{total} checks passed — "
          f"{'ALL GREEN ✅' if passed == total else 'FAILURES ❌'}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
