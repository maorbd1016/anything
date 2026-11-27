"""
scan_engine.py
-------------------------------------------------------------------------
The actual scanning logic -- every pillar, guardrail, tier, and pattern
check from the whole project -- extracted so both the CLI (scanner.py)
and the web app (app.py) run the exact same code. One set of rules, two
ways to see the output. If you change a rule, change it here (or in
pillars_config.py / candle_patterns.py) and both surfaces pick it up.

Also holds build_reference() -- the weekly cache build (build_reference_cache.py's
logic) -- so the web app can run it as a background job triggered by
visiting a link, not just from a terminal.
-------------------------------------------------------------------------
"""

import json
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import GetAssetsRequest
from alpaca.trading.enums import AssetClass, AssetStatus
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.historical.news import NewsClient
from alpaca.data.requests import StockSnapshotRequest, NewsRequest, StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.data.enums import DataFeed, Adjustment

from pillars_config import THRESHOLDS, VOLUME_TO_WATCH, HIGHEST_QUALITY, MIN_AVG_VOLUME_FOR_CACHE
from candle_patterns import find_pullback_setup, is_topping_tail, macd_crossed_below, volume_fading, CONFIRM_ON_CLOSE

REFERENCE_CACHE_PATH = "reference_cache.json"
SNAPSHOT_BATCH_SIZE = 200
NEWS_LOOKBACK_HOURS = 24
NEWS_CHECK_MAX = 40              # look up news for the top N stocks that are up enough
PATTERN_LOOKBACK_MINUTES = 90    # enough bars for MACD (needs ~35 one-minute bars)
PULLBACK_WINDOW_MINUTES = 30     # the pullback pattern still looks at the last 30 minutes
SUSPECT_MOVE_PCT = 300           # moves this big are usually bad data (e.g. a reverse split) -- flagged, never ranked as a leader
LEADER_COUNT = 3                 # tag the top N percentage gainers
EARLY_CLOSE_DATES = {"2026-11-27", "2026-12-24"}   # market closes 1:00pm ET
MARKET_HOLIDAYS = {"2026-12-25", "2027-01-01"}
MARKET_TZ = ZoneInfo("America/New_York")

BAR_LOOKBACK_DAYS = 75           # calendar days back, comfortably covers 50 sessions
VOLUME_BATCH_SIZE = 200          # symbols per historical-bars request
FINNHUB_CALLS_PER_MINUTE = 55    # stay safely under the free 60/min limit


def load_reference():
    with open(REFERENCE_CACHE_PATH) as f:
        return json.load(f)


def time_of_day_fraction(now_et):
    """
    Fraction of the regular session elapsed, used to scale average volume
    into an 'expected by now' figure for relative volume. Pre-market gets
    a small fixed floor so RVOL stays meaningful before 9:30am ET -- a
    simplification, not a precise time-of-day volume curve.
    """
    open_t = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
    close_hour = 13 if now_et.strftime("%Y-%m-%d") in EARLY_CLOSE_DATES else 16
    close_t = now_et.replace(hour=close_hour, minute=0, second=0, microsecond=0)
    if now_et < open_t:
        return 0.05
    if now_et > close_t:
        return 1.0
    elapsed = (now_et - open_t).total_seconds()
    total = (close_t - open_t).total_seconds()
    return max(elapsed / total, 0.05)


def session_note(now_et):
    """
    Informational only. 7:00-9:30am ET is the cleanest window: before the
    regular session opens, market makers don't yet have visible market
    orders, stop orders, or halt mechanics working in their favor against
    retail. Once the 9:30 bell rings, trading generally gets harder.
    """
    today = now_et.strftime("%Y-%m-%d")
    if today in MARKET_HOLIDAYS:
        return "Market holiday -- closed today"
    total_min = now_et.hour * 60 + now_et.minute
    if today in EARLY_CLOSE_DATES and total_min >= 780:
        return "Market closed (early close at 1:00pm ET)"
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


def fetch_snapshots(symbols, hist_client):
    snapshots = {}
    for i in range(0, len(symbols), SNAPSHOT_BATCH_SIZE):
        batch = symbols[i:i + SNAPSHOT_BATCH_SIZE]
        try:
            req = StockSnapshotRequest(symbol_or_symbols=batch, feed=DataFeed.IEX)
            result = hist_client.get_stock_snapshot(req)
            snapshots.update(result)
        except Exception:
            pass
    return snapshots


def fetch_recent_bars(symbols, hist_client, minutes=PATTERN_LOOKBACK_MINUTES):
    if not symbols:
        return {}
    end = datetime.now(timezone.utc)
    start = end - timedelta(minutes=minutes)
    try:
        req = StockBarsRequest(symbol_or_symbols=symbols, timeframe=TimeFrame.Minute, start=start, end=end, feed=DataFeed.IEX)
        bars = hist_client.get_stock_bars(req)
        return bars.data if hasattr(bars, "data") else bars
    except Exception:
        return {}


def fetch_split_adjusted_prev_closes(symbols, hist_client, session_date):
    """
    Alpaca's snapshot 'previous bar' is NOT adjusted for stock splits, so a
    reverse split makes a flat stock look like it is up 1000%+. For the
    few stocks that look like movers, re-fetch recent daily bars adjusted
    for splits and take the last close before today's session.
    Returns {symbol: previous_close}; a symbol is simply missing if the
    lookup failed, and the caller keeps the original number for it.
    """
    out = {}
    if not symbols or not session_date:
        return out
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=12)
    try:
        req = StockBarsRequest(symbol_or_symbols=symbols, timeframe=TimeFrame.Day, start=start, end=end,
                               adjustment=Adjustment.SPLIT, feed=DataFeed.IEX)
        bars = hist_client.get_stock_bars(req)
        data = bars.data if hasattr(bars, "data") else bars
    except Exception:
        return out
    for sym, day_bars in data.items():
        prior = [b for b in day_bars if b.timestamp.date().isoformat() < session_date]
        if prior:
            out[sym] = prior[-1].close
    return out


def fetch_news(symbol, news_client, max_items=3):
    """
    Recent headlines for one symbol (newest first), as a list of dicts:
    {headline, source, time, url}. Empty list = no news found in the
    lookback window. Returns None if the lookup itself failed, so a
    broken lookup is never mistaken for "no news".
    Alpaca groups articles under a "news" key, so this collects articles
    from anywhere in the response.
    """
    try:
        end = datetime.now(timezone.utc)
        start = end - timedelta(hours=NEWS_LOOKBACK_HOURS)
        req = NewsRequest(symbols=symbol, start=start, end=end, limit=max_items)
        articles = news_client.get_news(req)
        data = articles.data if hasattr(articles, "data") else {}
        if isinstance(data, dict):
            items = [a for v in data.values() if isinstance(v, (list, tuple)) for a in v]
        else:
            items = list(articles)
        out = []
        for a in items[:max_items]:
            created = getattr(a, "created_at", None)
            out.append({
                "headline": getattr(a, "headline", "") or "",
                "source": getattr(a, "source", "") or "",
                "time": created.isoformat() if created else "",
                "url": getattr(a, "url", "") or "",
            })
        return out
    except Exception:
        return None


def evaluate(symbol, snap, ref, now_et, th):
    daily = getattr(snap, "daily_bar", None)
    prev_daily = getattr(snap, "previous_daily_bar", None)
    if not daily or not prev_daily or not prev_daily.close:
        return None

    # The 'previous' bar must really be the session before today's bar.
    # If the free feed has no recent prints for a thin stock, it can hand
    # back a previous bar from weeks ago, which makes the % change nonsense.
    d_ts = getattr(daily, "timestamp", None)
    p_ts = getattr(prev_daily, "timestamp", None)
    if d_ts and p_ts:
        gap_days = (d_ts.date() - p_ts.date()).days
        if gap_days < 1 or gap_days > 5:
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
        "suspect": change_pct >= SUSPECT_MOVE_PCT,
        "split_adjusted": False,
        "bar_date": d_ts.date().isoformat() if d_ts else None,
        "rvol": rvol,
        "volume_today": volume_so_far,
        "shares_out_m": shares_out_m,
        "pass_up": change_pct >= th["up_pct"],
        "pass_rvol": rvol >= th["rvol"],
        "pass_price": th["price_min"] <= price <= th["price_max"],
        "pass_float": shares_out_m <= th["float_max_m"],
        "crossed_volume_watch": volume_so_far >= VOLUME_TO_WATCH,
    }


def run_scan(alpaca_key, alpaca_secret):
    """
    Runs one full scan -- every pillar, guardrail, tier, and pattern
    check -- and returns a single JSON-serializable dict. This is the one
    function scanner.py and app.py both call; neither has its own copy
    of the scoring logic.

    Raises FileNotFoundError if reference_cache.json is missing.
    """
    reference = load_reference()
    hist_client = StockHistoricalDataClient(alpaca_key, alpaca_secret)
    news_client = NewsClient(api_key=alpaca_key, secret_key=alpaca_secret)

    symbols = [s for s, r in reference.items() if r["shares_out_m"] <= THRESHOLDS["float_max_m"]]
    now_et = datetime.now(MARKET_TZ)
    now_min = now_et.hour * 60 + now_et.minute

    snapshots = fetch_snapshots(symbols, hist_client)

    # Today's bar must come from the latest session seen across all stocks;
    # anything older is a stock with no recent prints, not a live mover.
    bar_dates = [
        getattr(getattr(s, "daily_bar", None), "timestamp", None)
        for s in snapshots.values() if s is not None
    ]
    bar_dates = [d.date().isoformat() for d in bar_dates if d]
    session_date = max(bar_dates) if bar_dates else None

    scored = []
    for symbol, snap in snapshots.items():
        if snap is None or symbol not in reference:
            continue
        result = evaluate(symbol, snap, reference[symbol], now_et, THRESHOLDS)
        if not result:
            continue
        if session_date and result.get("bar_date") and result["bar_date"] != session_date:
            continue
        scored.append(result)

    # Fix reverse/forward-split distortions for the stocks that look like
    # movers: recompute the % change from split-adjusted previous closes.
    raw_movers = sorted([r for r in scored if r["pass_up"]], key=lambda r: r["change_pct"], reverse=True)[:NEWS_CHECK_MAX]
    adjusted_prev = fetch_split_adjusted_prev_closes([r["symbol"] for r in raw_movers], hist_client, session_date)
    for r in raw_movers:
        pc = adjusted_prev.get(r["symbol"])
        if pc and pc > 0:
            new_change = (r["price"] - pc) / pc * 100
            if abs(new_change - r["change_pct"]) > 1:
                r["split_adjusted"] = True
            r["change_pct"] = new_change
            r["pass_up"] = new_change >= THRESHOLDS["up_pct"]
            r["suspect"] = new_change >= SUSPECT_MOVE_PCT

    # News is looked up for every stock that is up enough (not just ones
    # passing all the other pillars) so you can see WHAT is behind each move.
    movers = sorted([r for r in scored if r["pass_up"]], key=lambda r: r["change_pct"], reverse=True)[:NEWS_CHECK_MAX]
    for r in movers:
        r["_was_near_candidate"] = r["pass_rvol"] and r["pass_price"] and r["pass_float"]
        r["news_checked"] = True
        if THRESHOLDS["require_news"]:
            items = fetch_news(r["symbol"], news_client)
            r["news_error"] = items is None
            r["news_items"] = items or []
            r["pass_news"] = bool(items)
        else:
            r["news_items"] = []
            r["pass_news"] = True
    for r in scored:
        r.setdefault("news_checked", False)
        r.setdefault("news_items", [])
        r.setdefault("news_error", False)
        r.setdefault("pass_news", False if THRESHOLDS["require_news"] else True)
        r["pillars_met"] = sum([r["pass_up"], r["pass_rvol"], r["pass_price"], r["pass_float"], r["pass_news"]])
        r["is_candidate"] = r["pillars_met"] == 5
        r["squeeze_watch"] = r.pop("_was_near_candidate", False) and not r["pass_news"]

    in_hq_window = HIGHEST_QUALITY["window_start_min"] <= now_min < HIGHEST_QUALITY["window_end_min"]
    for r in scored:
        r["is_highest_quality"] = (
            r["pass_news"]
            and r["change_pct"] >= HIGHEST_QUALITY["up_pct"]
            and HIGHEST_QUALITY["price_min"] <= r["price"] <= HIGHEST_QUALITY["price_max"]
            and r["rvol"] >= HIGHEST_QUALITY["rvol"]
            and r["shares_out_m"] <= HIGHEST_QUALITY["float_max_m"]
            and in_hq_window
        )

    # Leading gainers: the top few percentage gainers inside your price range.
    for r in scored:
        r["leader_rank"] = None
    leaders = sorted([r for r in scored if r["pass_price"] and r["pass_up"] and not r["suspect"]], key=lambda r: r["change_pct"], reverse=True)[:LEADER_COUNT]
    for i, r in enumerate(leaders, 1):
        r["leader_rank"] = i

    scored.sort(
        key=lambda r: (r["is_highest_quality"], r["is_candidate"], r["pillars_met"], r["change_pct"]),
        reverse=True,
    )

    watchlist = [r["symbol"] for r in scored if r["is_candidate"] or r["squeeze_watch"] or r["is_highest_quality"] or r["leader_rank"]]
    bar_data = fetch_recent_bars(watchlist, hist_client) if watchlist else {}

    patterns = {}
    for symbol in watchlist:
        bars = bar_data.get(symbol)
        if not bars:
            patterns[symbol] = {"status": "no_data"}
            continue
        last_ts = getattr(bars[-1], "timestamp", None)
        if last_ts is not None:
            window = [b for b in bars if getattr(b, "timestamp", last_ts) >= last_ts - timedelta(minutes=PULLBACK_WINDOW_MINUTES)]
        else:
            window = bars[-PULLBACK_WINDOW_MINUTES:]
        setup = find_pullback_setup(window)
        topping = is_topping_tail(bars[-1])
        entry = None
        if setup and setup.get("crossed"):
            entry = {
                "entry_price": setup["entry_price"],
                "risk": setup["risk"],
                "risk_pct": setup["risk_pct"],
                # 2:1 profit-to-loss target (from the course's risk rule)
                "target_2to1": setup["entry_price"] + 2 * setup["risk"],
            }
        patterns[symbol] = {
            "status": "breakout" if entry else ("pulling_back" if setup else "trending"),
            "swing_high": setup["swing_high"] if setup else None,
            "new_support": setup["new_support"] if setup else None,
            "entry": entry,
            "topping_tail": topping,
            "macd_cross_below": macd_crossed_below(bars),
            "volume_fading": volume_fading(bars),
        }

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "session_note": session_note(now_et),
        "confirm_on_close": CONFIRM_ON_CLOSE,
        "volume_to_watch": VOLUME_TO_WATCH,
        "highest_quality_window": [HIGHEST_QUALITY["window_start_min"], HIGHEST_QUALITY["window_end_min"]],
        "walk_away": {
            "best_window_closed": now_min >= 570,
            "no_a_plus": not any(r["is_highest_quality"] for r in scored),
        },
        "results": scored,
        "patterns": patterns,
    }


# ---------------------------------------------------------------------
# Weekly reference cache build (moved from build_reference_cache.py so
# the web app can trigger it as a background job from a link, not just
# a terminal). progress_cb, if given, is called as
# progress_cb(stage: str, done: int, total: int) periodically -- this
# can take several minutes, so a caller running it in the background
# uses this to report status without blocking.
# ---------------------------------------------------------------------

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


def compute_avg_volumes(symbols, hist_client, progress_cb=None):
    """50-session average volume per symbol, from Alpaca's free historical bars."""
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=BAR_LOOKBACK_DAYS)
    avg_volumes = {}
    last_error = None

    for i in range(0, len(symbols), VOLUME_BATCH_SIZE):
        batch = symbols[i:i + VOLUME_BATCH_SIZE]
        try:
            # IEX feed on purpose: the free plan can't query recent SIP data,
            # and using the same feed for the average and for today's live
            # volume keeps the relative-volume ratio apples-to-apples.
            req = StockBarsRequest(symbol_or_symbols=batch, timeframe=TimeFrame.Day, start=start, end=end, feed=DataFeed.IEX)
            bars = hist_client.get_stock_bars(req)
        except Exception as e:
            last_error = e
            continue

        data = bars.data if hasattr(bars, "data") else bars
        for symbol, day_bars in data.items():
            volumes = [b.volume for b in day_bars if getattr(b, "volume", None)]
            if volumes:
                avg_volumes[symbol] = sum(volumes) / len(volumes)

        if progress_cb:
            progress_cb("Computing average volume", min(i + VOLUME_BATCH_SIZE, len(symbols)), len(symbols))
        time.sleep(0.2)

    if not avg_volumes and last_error is not None:
        raise RuntimeError(f"Alpaca history request failed: {last_error}")
    return avg_volumes


def get_shares_outstanding(symbol, finnhub_key):
    """
    Finnhub company profile -- shareOutstanding is reported in millions.
    This is TOTAL shares outstanding, not free float -- see README.md.
    """
    url = "https://finnhub.io/api/v1/stock/profile2"
    try:
        r = requests.get(url, params={"symbol": symbol, "token": finnhub_key}, timeout=10)
        r.raise_for_status()
        return r.json().get("shareOutstanding")
    except Exception:
        return None


def build_reference(alpaca_key, alpaca_secret, finnhub_key, progress_cb=None):
    """
    Runs the full weekly cache build and writes reference_cache.json.
    Returns a small summary dict. Raises if credentials are missing.
    This is slow (the Finnhub lookups are rate-limited) -- expect
    several minutes for a normal-sized universe.
    """
    if not alpaca_key or not alpaca_secret:
        raise RuntimeError("Missing Alpaca credentials.")
    if not finnhub_key:
        raise RuntimeError("Missing Finnhub credentials.")

    trading_client = TradingClient(alpaca_key, alpaca_secret, paper=True)
    hist_client = StockHistoricalDataClient(alpaca_key, alpaca_secret)

    if progress_cb:
        progress_cb("Fetching tradable universe", 0, 0)
    symbols = get_universe(trading_client)

    avg_volumes = compute_avg_volumes(symbols, hist_client, progress_cb=progress_cb)

    candidates = [s for s, v in avg_volumes.items() if v >= MIN_AVG_VOLUME_FOR_CACHE]
    if not candidates:
        raise RuntimeError(
            f"No stocks passed the volume filter (universe {len(symbols)}, with volume data {len(avg_volumes)}). "
            "Check the Alpaca keys and plan."
        )

    reference = {}
    delay = 60.0 / FINNHUB_CALLS_PER_MINUTE
    for idx, symbol in enumerate(candidates, 1):
        shares_out_m = get_shares_outstanding(symbol, finnhub_key)
        if shares_out_m is not None:
            reference[symbol] = {"avg_volume": avg_volumes[symbol], "shares_out_m": shares_out_m}
        if progress_cb and idx % 10 == 0:
            progress_cb("Looking up float via Finnhub (the slow part)", idx, len(candidates))
        time.sleep(delay)

    with open(REFERENCE_CACHE_PATH, "w") as f:
        json.dump(reference, f, indent=2)

    if progress_cb:
        progress_cb("Done", len(candidates), len(candidates))

    return {"universe_size": len(symbols), "liquid_candidates": len(candidates), "cached": len(reference)}
