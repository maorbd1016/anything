"""
Shared five-pillar thresholds.

Edit these to match your evolving rules -- both build_reference_cache.py
and scanner.py import from here so they never drift out of sync with
each other (or with the dashboard prototype you already have).
"""

THRESHOLDS = {
    "up_pct": 10.0,        # Demand: up on the day, % or more
    "rvol": 5.0,           # Demand: relative volume, x average or more
    "price_min": 2.0,      # Demand: price range, $ low end
    "price_max": 20.0,     # Demand: price range, $ high end
    "require_news": True,  # Demand: news catalyst required
    "float_max_m": 20.0,   # Supply: float, under X million shares
}

# Liquidity floor used only when building the reference cache, to keep
# it from wasting Finnhub calls on completely dead, illiquid tickers
# that could never pass the demand pillars anyway.
MIN_AVG_VOLUME_FOR_CACHE = 50_000

# NOT a requirement and NOT one of the five pillars -- a number worth
# noticing as it builds, not a threshold a stock has to clear. A stock
# can be a perfectly good candidate on 3M shares of volume at 7am; this
# just flags once a name has real, unambiguous participation behind it.
# Never used to exclude a stock from candidate/squeeze/A+ status.
VOLUME_TO_WATCH = 25_000_000

# An even tighter overlay on top of the five pillars -- "the highest
# quality stocks" tier. Every stock that clears this also clears the
# baseline THRESHOLDS above (the price band sits inside the baseline
# band, the float cap is tighter, and 30% up implies 10%+ up) -- this
# just marks the subset of candidates that are exceptional, not a
# separate or looser standard.
HIGHEST_QUALITY = {
    "up_pct": 30.0,
    "price_min": 5.0,
    "price_max": 10.0,
    "rvol": 5.0,
    "float_max_m": 10.0,
    "require_news": True,          # specifically a breaking news headline / PR
    "window_start_min": 7 * 60,    # 7:00am ET
    "window_end_min": 10 * 60,     # 10:00am ET
}
