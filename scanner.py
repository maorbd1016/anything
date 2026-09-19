"""
scanner.py
-------------------------------------------------------------------------
Run this each morning. Reads reference_cache.json (built by
build_reference_cache.py) for the slow-changing numbers, then pulls a
live snapshot for the pre-filtered universe and scores all five pillars.

DATA COVERAGE NOTE: on an Alpaca free account, this uses the IEX feed,
which represents roughly 2-3% of total US equity volume -- real enough
to test your rules, but relative volume and % change can meaningfully
understate reality versus a full consolidated (SIP) feed. Alpaca's
AlgoTrader Plus plan (~$99/mo as of writing -- check current pricing)
upgrades this to full SIP data. See README.md.

VOLUME_TO_WATCH (pillars_config.py, default 25,000,000 shares) is
informational, not a requirement -- it never excludes a stock from
candidate/squeeze/A+ status. It just marks once a name has crossed a
volume level worth noticing.
-------------------------------------------------------------------------
"""

import os
import json
import csv
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.historical.news import NewsClient
from alpaca.data.requests import StockSnapshotRequest, NewsRequest, StockBarsRequest
from alpaca.data.timeframe import TimeFrame

from pillars_config import THRESHOLDS, VOLUME_TO_WATCH, HIGHEST_QUALITY
from candle_patterns import find_pullback_setup, is_topping_tail, CONFIRM_ON_CLOSE

load_dotenv()

ALPACA_KEY = os.getenv("ALPACA_API_KEY")
ALPACA_SECRET = os.getenv("ALPACA_SECRET_KEY")

REFERENCE_CACHE_PATH = "reference_cache.json"
RESULTS_CSV_PATH = "scan_results.csv"
RESULTS_JSON_PATH = "scan_results.json"
SNAPSHOT_BATCH_SIZE = 200
NEWS_LOOKBACK_HOURS = 24
PATTERN_LOOKBACK_MINUTES = 30
MARKET_TZ = ZoneInfo("America/New_York")

GREEN, RED, BOLD, RESET = "\033[92m", "\033[91m", "\033[1m", "\033[0m"


def load_reference():
    with open(REFERENCE_CACHE_PATH) as f:
        return json.load(f)


def time_of_day_fraction(now_et):
    """
    Fraction of the regular session elapsed, used to scale average volume
    into an 'expected by now' figure for relative volume. Pre-market gets
    a small fixed floor so RVOL stays meaningful before 9:30am ET -- this
    is a simplification, not a precise time-of-day volume curve. Refine
    it once you've watched how your real candidates behave pre-market.
    """
    open_t = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
    close_t = now_et.replace(hour=16, minute=0, second=0, microsecond=0)
    if now_et < open_t:
        return 0.05
    if now_et > close_t:
        return 1.0
    elapsed = (now_et - open_t).total_seconds()
    total = (close_t - open_t).total_seconds()
    return max(elapsed / total, 0.05)


def fetch_snapshots(symbols, hist_client):
    snapshots = {}
    for i in range(0, len(symbols), SNAPSHOT_BATCH_SIZE):
        batch = symbols[i:i + SNAPSHOT_BATCH_SIZE]
        try:
            req = StockSnapshotRequest(symbol_or_symbols=batch)
            result = hist_client.get_stock_snapshot(req)
            snapshots.update(result)
        except Exception as e:
            print(f"  ! snapshot batch failed: {e}")
    return snapshots


def fetch_recent_bars(symbols, hist_client, minutes=PATTERN_LOOKBACK_MINUTES):
    """1-minute bars for the shortlist only -- this is a different, more
    expensive pull than a snapshot, so it's never run against the full
    universe, only the handful of names that already earned a look."""
    if not symbols:
        return {}
    end = datetime.now(timezone.utc)
    start = end - timedelta(minutes=minutes)
    try:
        req = StockBarsRequest(symbol_or_symbols=symbols, timeframe=TimeFrame.Minute, start=start, end=end)
        bars = hist_client.get_stock_bars(req)
        return bars.data if hasattr(bars, "data") else bars
    except Exception as e:
        print(f"  ! couldn't fetch recent bars: {e}")
        return {}


def check_news(symbol, news_client):
    """
    Alpaca's news SDK response shape has shifted between versions, so this
    is defensive on purpose. If it always returns False for you, print()
    the raw `articles` object here to see what your installed version
    actually returns and adjust the parsing below.
    """
    try:
        end = datetime.now(timezone.utc)
        start = end - timedelta(hours=NEWS_LOOKBACK_HOURS)
        req = NewsRequest(symbols=symbol, start=start, end=end, limit=1)
        articles = news_client.get_news(req)
        data = articles.data if hasattr(articles, "data") else {}
        items = data.get(symbol, []) if isinstance(data, dict) else list(articles)
        return len(items) > 0
    except Exception:
        return False


def evaluate(symbol, snap, ref, now_et, th):
    daily = getattr(snap, "daily_bar", None)
    prev_daily = getattr(snap, "previous_daily_bar", None)
    if not daily or not prev_daily or not prev_daily.close:
        return None

    price = daily.close
    change_pct = (daily.close - prev_daily.close) / prev_daily.close * 100
    volume_so_far = daily.volume or 0

    avg_volume = ref["avg_volume"]
    expected_by_now = max(avg_volume * time_of_day_fraction(now_et), 1)
    rvol = volume_so_far / expected_by_now

    shares_out_m = ref["shares_out_m"]

    return {
        "symbol": symbol,
        "price": price,
        "change_pct": change_pct,
        "rvol": rvol,
        "volume_today": volume_so_far,
        "shares_out_m": shares_out_m,
        "pass_up": change_pct >= th["up_pct"],
        "pass_rvol": rvol >= th["rvol"],
        "pass_price": th["price_min"] <= price <= th["price_max"],
        "pass_float": shares_out_m <= th["float_max_m"],
        "crossed_volume_watch": volume_so_far >= VOLUME_TO_WATCH,  # informational only
    }


def colorize(text_padded, passed):
    return f"{GREEN}{text_padded}{RESET}" if passed else f"{RED}{text_padded}{RESET}"


def session_note(now_et):
    """
    Informational only -- doesn't filter or downrank anything. 7:00-9:30am
    ET is the cleanest window: before the regular session opens, market
    makers don't yet have visible market orders, stop orders, or halt
    mechanics working in their favor against retail. Once the bell rings
    at 9:30, those all become live, and trading generally gets harder
    from there.
    """
    total_min = now_et.hour * 60 + now_et.minute
    if total_min < 420:
        return "Early pre-market -- thin liquidity before the target window"
    if total_min < 570:
        return "Pre-market -- cleanest window (before stops/halts kick in)"
    if total_min < 660:
        return "Regular session just opened -- market/stop orders and halts now live, harder from here"
    if total_min < 840:
        return "Midday -- normal caution"
    if total_min < 930:
        return "Afternoon -- catalysts still possible, less time to recover"
    if total_min < 960:
        return "Final 30 min -- little runway left before the close"
    return "Market closed"


def main():
    if not ALPACA_KEY or not ALPACA_SECRET:
        raise SystemExit("Set ALPACA_API_KEY and ALPACA_SECRET_KEY in your .env file first.")

    try:
        reference = load_reference()
    except FileNotFoundError:
        raise SystemExit(
            "reference_cache.json not found. Run build_reference_cache.py first "
            "(and re-run it weekly to keep it fresh)."
        )

    hist_client = StockHistoricalDataClient(ALPACA_KEY, ALPACA_SECRET)
    news_client = NewsClient(api_key=ALPACA_KEY, secret_key=ALPACA_SECRET)

    # Float rarely changes day to day, so pre-filter on it now using the
    # cache -- no need to fetch a live snapshot for a name that can never
    # pass pillar 5.
    symbols = [s for s, r in reference.items() if r["shares_out_m"] <= THRESHOLDS["float_max_m"]]
    print(f"Scanning {len(symbols)} symbols that already clear the float pillar...")

    now_et = datetime.now(MARKET_TZ)
    print(f"Session: {session_note(now_et)}")
    hq_start_h, hq_end_h = HIGHEST_QUALITY["window_start_min"] // 60, HIGHEST_QUALITY["window_end_min"] // 60
    in_hq_window_now = HIGHEST_QUALITY["window_start_min"] <= (now_et.hour * 60 + now_et.minute) < HIGHEST_QUALITY["window_end_min"]
    if not in_hq_window_now:
        print(f"(Highest-quality tier only applies {hq_start_h}:00-{hq_end_h}:00am ET -- outside that window right now)")
    snapshots = fetch_snapshots(symbols, hist_client)
    print(f"Got {len(snapshots)} snapshots. Scoring demand pillars...")

    scored = []
    for symbol, snap in snapshots.items():
        if snap is None or symbol not in reference:
            continue
        result = evaluate(symbol, snap, reference[symbol], now_et, THRESHOLDS)
        if result:
            scored.append(result)

    # Only spend news-API calls on names that already clear the other
    # four pillars -- this is usually a short list. Volume isn't part
    # of this gate: it's a number worth noticing, not a requirement.
    near_candidates = [
        r for r in scored if r["pass_up"] and r["pass_rvol"] and r["pass_price"] and r["pass_float"]
    ]
    print(f"{len(near_candidates)} clear price/volume/float -- checking news catalyst on those...")

    for r in near_candidates:
        r["pass_news"] = check_news(r["symbol"], news_client) if THRESHOLDS["require_news"] else True
        r["_was_near_candidate"] = True
    for r in scored:
        r.setdefault("pass_news", False if THRESHOLDS["require_news"] else True)
        r["pillars_met"] = sum([r["pass_up"], r["pass_rvol"], r["pass_price"], r["pass_float"], r["pass_news"]])
        r["is_candidate"] = r["pillars_met"] == 5
        # near_candidates already cleared up%, RVOL, price, and float --
        # if it still failed news, that's not a random 4/5 miss, it's
        # the classic short-squeeze profile (a real move with no
        # catalyst behind it, often because stubborn shorts are getting
        # forced to cover). Worth less size than a true 5/5 candidate,
        # but not worth discarding, especially if it's leading the pack
        # -- so it gets its own flag rather than getting lost among
        # ordinary partial misses.
        r["squeeze_watch"] = r.pop("_was_near_candidate", False) and not r["pass_news"]

    now_min = now_et.hour * 60 + now_et.minute
    in_hq_window = HIGHEST_QUALITY["window_start_min"] <= now_min < HIGHEST_QUALITY["window_end_min"]
    for r in scored:
        # Independent of the adjustable baseline thresholds above -- this
        # checks the "sweet spot" numbers directly, so it stays meaningful
        # even if you loosen or tighten THRESHOLDS for testing.
        r["is_highest_quality"] = (
            r["pass_news"]
            and r["change_pct"] >= HIGHEST_QUALITY["up_pct"]
            and HIGHEST_QUALITY["price_min"] <= r["price"] <= HIGHEST_QUALITY["price_max"]
            and r["rvol"] >= HIGHEST_QUALITY["rvol"]
            and r["shares_out_m"] <= HIGHEST_QUALITY["float_max_m"]
            and in_hq_window
        )

    scored.sort(
        key=lambda r: (r["is_highest_quality"], r["is_candidate"], r["pillars_met"], r["change_pct"]),
        reverse=True,
    )

    top_squeeze = next((r for r in scored if r["squeeze_watch"]), None)

    print(f"\n{BOLD}{'':<3}{'Ticker':<8}{'Price':>9}{'%Chg':>11}{'RVOL':>10}{'Vol(M)':>11}{'Float(M)':>11}{'News':>9}{'Pillars':>9}{RESET}")
    for r in scored[:40]:
        flag = ""
        if r["is_highest_quality"]:
            flag = "  <- A+ SETUP: highest-quality criteria met"
        elif r["squeeze_watch"]:
            flag = "  <- SQUEEZE WATCH: no catalyst, size down"
            if r is top_squeeze:
                flag += " (leading gainer -- don't just discard this one)"
        star = " *" if r["is_highest_quality"] else "  "
        vol_m = r["volume_today"] / 1_000_000
        vol_str = format(vol_m, ".1f").rjust(9) + (f"{GREEN}\u2713{RESET}" if r["crossed_volume_watch"] else "  ")
        row = (
            f"{star:<3}"
            f"{r['symbol']:<8}"
            f"{colorize(('$' + format(r['price'], '.2f')).rjust(9), r['pass_price'])}"
            f"{colorize(format(r['change_pct'], '+.1f').rjust(9) + '%', r['pass_up'])}"
            f"{colorize((format(r['rvol'], '.1f') + 'x').rjust(9), r['pass_rvol'])}"
            f"{vol_str}"
            f"{format(r['shares_out_m'], '.1f').rjust(11)}"
            f"{colorize(('Yes' if r['pass_news'] else 'No').rjust(8), r['pass_news'])}"
            f"{(str(r['pillars_met']) + '/5').rjust(9)}"
            f"{flag}"
        )
        print(row)

    print(f"\n\u2713 = crossed {VOLUME_TO_WATCH:,} shares today (a number worth noticing, not a requirement)")

    # Two of the five "when to walk away" signals -- the other three
    # (lost half the day, hit max loss, today's theme is bearish) need
    # your running P&L or a market-feel judgment call this script has no
    # way to know, so those live in the dashboard's Walk Away Check panel.
    best_window_closed = now_min >= 570  # 9:30am ET, same line used in session_note()
    no_a_plus = not any(r["is_highest_quality"] for r in scored)
    print(f"\n{BOLD}Walk Away Check (partial -- P&L-based rules live in the dashboard){RESET}")
    print(f"  [{'x' if best_window_closed else ' '}] Best trading window has closed (past 9:30am ET)")
    print(f"  [{'x' if no_a_plus else ' '}] No A+ setups found this scan")

    # Entry (pullback + crossing candle) and exit (topping tail) pattern
    # checks -- only on names that already earned a look, since 1-minute
    # bar history is a heavier pull than a snapshot.
    watchlist = [r["symbol"] for r in scored if r["is_candidate"] or r["squeeze_watch"] or r["is_highest_quality"]]
    if watchlist:
        mode = "close" if CONFIRM_ON_CLOSE else "intrabar"
        print(f"\nChecking entry/exit pattern on {len(watchlist)} shortlisted name(s) ({mode}-confirmed breakout)...")
        bar_data = fetch_recent_bars(watchlist, hist_client)
        print(f"\n{BOLD}Entry / Exit Signals (last {PATTERN_LOOKBACK_MINUTES} min){RESET}")
        for symbol in watchlist:
            bars = bar_data.get(symbol)
            if not bars:
                print(f"  {symbol:<8} no recent bar data")
                continue
            setup = find_pullback_setup(bars)
            topping = is_topping_tail(bars[-1])

            line = f"  {symbol:<8}"
            if setup is None:
                line += "still trending, no pullback formed yet"
            elif setup["crossed"]:
                line += (
                    f"{GREEN}BREAKOUT{RESET} entry ${setup['entry_price']:.2f}, "
                    f"support ${setup['new_support']:.2f}, "
                    f"risk ${setup['risk']:.2f} ({setup['risk_pct']:.1f}%)"
                )
            else:
                line += f"pulled back to ${setup['new_support']:.2f}, watching ${setup['swing_high']:.2f} to break"
            if topping:
                line += f"  {RED}\u26a0 TOPPING TAIL on latest candle{RESET}"
            print(line)
    else:
        print("\nNo candidate/squeeze-watch/A+ names to check entry/exit patterns on this scan.")


    with open(RESULTS_JSON_PATH, "w") as f:
        json.dump(scored, f, indent=2, default=str)

    if scored:
        with open(RESULTS_CSV_PATH, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(scored[0].keys()))
            writer.writeheader()
            writer.writerows(scored)

    print(f"\nSaved full results to {RESULTS_CSV_PATH} and {RESULTS_JSON_PATH}")


if __name__ == "__main__":
    main()
