"""
build_reference_cache.py
-------------------------------------------------------------------------
Run this weekly (or whenever you want fresher numbers) -- NOT every
morning. It does the two slow-changing lookups so scanner.py can run
fast and cheap at 6am:

  1. Pulls the tradable US equity universe from Alpaca.
  2. Computes each symbol's 50-session average volume from Alpaca's free
     historical bars (no rate-limit worries -- this is the free tier).
  3. Pre-filters down to a liquid universe BEFORE calling Finnhub,
     because Finnhub's free tier is rate-limited (~60 calls/minute) and
     you don't want to burn that on thousands of tickers that were
     never going to pass the demand pillars anyway.
  4. Looks up shares outstanding for the survivors via Finnhub, as a
     free proxy for float (see README.md for why it's a proxy, not the
     real thing).

Writes reference_cache.json:
  { "TICK": {"avg_volume": 123456, "shares_out_m": 8.4}, ... }
-------------------------------------------------------------------------
"""

import os
import json
import time
from datetime import datetime, timedelta, timezone

import requests
from dotenv import load_dotenv

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import GetAssetsRequest
from alpaca.trading.enums import AssetClass, AssetStatus
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

from pillars_config import MIN_AVG_VOLUME_FOR_CACHE

load_dotenv()

ALPACA_KEY = os.getenv("ALPACA_API_KEY")
ALPACA_SECRET = os.getenv("ALPACA_SECRET_KEY")
FINNHUB_KEY = os.getenv("FINNHUB_API_KEY")

CACHE_PATH = "reference_cache.json"
BAR_LOOKBACK_DAYS = 75           # calendar days back, comfortably covers 50 sessions
                                 # (~50 trading days * 7/5 for weekends, plus a holiday buffer)
VOLUME_BATCH_SIZE = 200          # symbols per historical-bars request
FINNHUB_CALLS_PER_MINUTE = 55    # stay safely under the free 60/min limit


def get_universe(trading_client):
    """Active, tradable US equities on the main listed exchanges."""
    req = GetAssetsRequest(asset_class=AssetClass.US_EQUITY, status=AssetStatus.ACTIVE)
    assets = trading_client.get_all_assets(req)
    symbols = []
    for a in assets:
        if not a.tradable:
            continue
        if a.exchange not in ("NASDAQ", "NYSE", "ARCA", "AMEX", "BATS"):
            continue
        if "." in a.symbol or "/" in a.symbol:
            continue  # skip unusual share-class / warrant-style symbols for now
        symbols.append(a.symbol)
    return symbols


def compute_avg_volumes(symbols, hist_client):
    """50-session average volume per symbol, from Alpaca's free historical bars."""
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=BAR_LOOKBACK_DAYS)
    avg_volumes = {}

    for i in range(0, len(symbols), VOLUME_BATCH_SIZE):
        batch = symbols[i:i + VOLUME_BATCH_SIZE]
        try:
            req = StockBarsRequest(
                symbol_or_symbols=batch,
                timeframe=TimeFrame.Day,
                start=start,
                end=end,
            )
            bars = hist_client.get_stock_bars(req)
        except Exception as e:
            print(f"  ! bars batch {i}-{i + len(batch)} failed: {e}")
            continue

        data = bars.data if hasattr(bars, "data") else bars
        for symbol, day_bars in data.items():
            volumes = [b.volume for b in day_bars if getattr(b, "volume", None)]
            if volumes:
                avg_volumes[symbol] = sum(volumes) / len(volumes)

        print(f"  volumes: {min(i + VOLUME_BATCH_SIZE, len(symbols))}/{len(symbols)}")
        time.sleep(0.2)  # be polite to the API

    return avg_volumes


def get_shares_outstanding(symbol):
    """
    Finnhub company profile -- shareOutstanding is reported in millions.
    This is TOTAL shares outstanding, not free float (float excludes
    insider/locked-up shares, so the real number is typically smaller).
    Treat this as a conservative proxy: a stock that fails this check
    would almost certainly fail a real float check too, but a pass here
    still deserves a manual float check before you trust it.
    """
    url = "https://finnhub.io/api/v1/stock/profile2"
    try:
        r = requests.get(url, params={"symbol": symbol, "token": FINNHUB_KEY}, timeout=10)
        r.raise_for_status()
        return r.json().get("shareOutstanding")
    except Exception:
        return None


def main():
    if not ALPACA_KEY or not ALPACA_SECRET:
        raise SystemExit("Set ALPACA_API_KEY and ALPACA_SECRET_KEY in your .env file first.")
    if not FINNHUB_KEY:
        raise SystemExit("Set FINNHUB_API_KEY in your .env file first (the free tier is fine).")

    trading_client = TradingClient(ALPACA_KEY, ALPACA_SECRET, paper=True)
    hist_client = StockHistoricalDataClient(ALPACA_KEY, ALPACA_SECRET)

    print("Fetching tradable universe...")
    symbols = get_universe(trading_client)
    print(f"  {len(symbols)} symbols to evaluate")

    print("Computing 50-session average volume (Alpaca free tier)...")
    avg_volumes = compute_avg_volumes(symbols, hist_client)

    # Pre-filter BEFORE spending Finnhub calls: keep only names with
    # enough liquidity to ever matter for a momentum scan.
    candidates = [s for s, v in avg_volumes.items() if v >= MIN_AVG_VOLUME_FOR_CACHE]
    print(f"  {len(candidates)} pass the liquidity pre-filter -> checking float on these only")

    print("Looking up shares outstanding via Finnhub (rate-limited -- this is the slow part)...")
    reference = {}
    delay = 60.0 / FINNHUB_CALLS_PER_MINUTE
    for idx, symbol in enumerate(candidates, 1):
        shares_out_m = get_shares_outstanding(symbol)
        if shares_out_m is not None:
            reference[symbol] = {
                "avg_volume": avg_volumes[symbol],
                "shares_out_m": shares_out_m,
            }
        if idx % 50 == 0:
            print(f"  float lookups: {idx}/{len(candidates)}")
        time.sleep(delay)

    with open(CACHE_PATH, "w") as f:
        json.dump(reference, f, indent=2)

    print(f"\nDone. Wrote {len(reference)} symbols to {CACHE_PATH}")
    print("Re-run this weekly to keep average volume and share counts current.")


if __name__ == "__main__":
    main()
