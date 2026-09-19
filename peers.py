"""
peers.py
-------------------------------------------------------------------------
Research aid for step 1 of your process ("choose a stock other traders
are likely watching -- e.g. names similar to yesterday's squeeze").

This does NOT score, filter, or rank anything, and nothing it shows
affects candidate/squeeze-watch/A+ status in scanner.py. It's here to
speed up a judgment call you're already making, not to replace it.

Given a ticker, looks up its peers (same sector/sub-industry, via
Finnhub's /stock/peers) and pulls each one's live snapshot from Alpaca
so you can quickly see which peers are already moving today.

Usage:
    python peers.py TICKER

Example:
    python peers.py ZTG
-------------------------------------------------------------------------
"""

import os
import sys

import requests
from dotenv import load_dotenv

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockSnapshotRequest

load_dotenv()

ALPACA_KEY = os.getenv("ALPACA_API_KEY")
ALPACA_SECRET = os.getenv("ALPACA_SECRET_KEY")
FINNHUB_KEY = os.getenv("FINNHUB_API_KEY")

BOLD, RESET = "\033[1m", "\033[0m"


def get_peers(symbol):
    url = "https://finnhub.io/api/v1/stock/peers"
    r = requests.get(url, params={"symbol": symbol, "token": FINNHUB_KEY}, timeout=10)
    r.raise_for_status()
    peers = r.json() or []
    return [p for p in peers if p.upper() != symbol.upper()]


def main():
    if len(sys.argv) != 2:
        raise SystemExit("Usage: python peers.py TICKER")
    symbol = sys.argv[1].upper()

    if not FINNHUB_KEY:
        raise SystemExit("Set FINNHUB_API_KEY in your .env file first.")
    if not ALPACA_KEY or not ALPACA_SECRET:
        raise SystemExit("Set ALPACA_API_KEY and ALPACA_SECRET_KEY in your .env file first.")

    print(f"Looking up peers of {symbol} (same sector/sub-industry, via Finnhub)...")
    try:
        peers = get_peers(symbol)
    except Exception as e:
        raise SystemExit(f"Couldn't fetch peers: {e}")

    if not peers:
        print("No peers returned -- Finnhub may not have coverage for this ticker.")
        return
    print(f"Found {len(peers)} peers: {', '.join(peers)}\n")

    hist_client = StockHistoricalDataClient(ALPACA_KEY, ALPACA_SECRET)
    try:
        req = StockSnapshotRequest(symbol_or_symbols=peers)
        snapshots = hist_client.get_stock_snapshot(req)
    except Exception as e:
        raise SystemExit(f"Couldn't pull live snapshots: {e}")

    rows = []
    for peer, snap in snapshots.items():
        daily = getattr(snap, "daily_bar", None)
        prev_daily = getattr(snap, "previous_daily_bar", None)
        if not daily or not prev_daily or not prev_daily.close:
            continue
        change_pct = (daily.close - prev_daily.close) / prev_daily.close * 100
        rows.append((peer, daily.close, change_pct, daily.volume or 0))

    if not rows:
        print("None of the peers had usable snapshot data (market may be closed).")
        return

    rows.sort(key=lambda r: r[2], reverse=True)
    print(f"{BOLD}{'Ticker':<8}{'Price':>9}{'%Chg':>11}{'Volume(M)':>13}{RESET}")
    for peer, price, change_pct, volume in rows:
        print(
            f"{peer:<8}"
            f"{'$' + format(price, '.2f'):>9}"
            f"{format(change_pct, '+.1f').rjust(9)}%"
            f"{format(volume / 1_000_000, '.1f').rjust(12)}"
        )

    print(
        "\nResearch aid only -- none of this feeds into candidate/squeeze/A+ "
        "status. Use your own judgment on which of these are actually worth "
        "watching today."
    )


if __name__ == "__main__":
    main()
