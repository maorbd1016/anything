"""
candle_patterns.py
-------------------------------------------------------------------------
Entry and exit pattern detection from 1-minute bars.

ENTRY -- the "crossing candle" pattern: a run-up to a high, a pullback
that sets a new support level, then a candle that breaks back above
that high (the "crossing candle").

    ASSUMPTION CALLED OUT ON PURPOSE: this uses CLOSE-based confirmation
    by default -- the candle has to actually CLOSE above the prior high,
    not just wick above it. This was never fully settled in conversation
    (you described it as "enter right when it breaks," which reads as
    intrabar/live, but the course slide you shared separately describes
    the close-based version as "confirmation"). Close-based is the safer
    default -- fewer false triggers from a wick that pokes through and
    fails. If you actually want the live, intrabar version, set
    CONFIRM_ON_CLOSE = False below. This is a real behavior change, not
    a style preference, so it's flagged here rather than buried.

EXIT -- the "topping tail" candle: a candle with a wick on top and
none on the bottom, regardless of body size or color (per your
clarification -- not a wick-to-body ratio, just presence/absence).
-------------------------------------------------------------------------
"""

CONFIRM_ON_CLOSE = True  # False = trigger the instant price trades above the level, not on candle close
LOWER_WICK_TOLERANCE_PCT = 0.05  # % of price treated as noise, not a real lower wick


def _hi(b): return b.high if hasattr(b, "high") else b["high"]
def _lo(b): return b.low if hasattr(b, "low") else b["low"]
def _op(b): return b.open if hasattr(b, "open") else b["open"]
def _cl(b): return b.close if hasattr(b, "close") else b["close"]
def _vol(b):
    v = b.volume if hasattr(b, "volume") else b.get("volume")
    return v or 0


def find_pullback_setup(bars):
    """
    bars: list of bar objects/dicts with open/high/low/close, oldest
    first, most recent last (as Alpaca returns them).

    Finds the most recent swing high, the pullback low that followed it
    (the new support level), and whether the latest bar has broken back
    above that high. Returns None if no complete setup exists yet --
    either too little data, or price is still making new highs with no
    pullback formed since the last peak.

    This is a first-pass heuristic on a rolling window, not a polished
    pattern-recognition algorithm -- it will need real-world tuning once
    you've watched it run against live candidates (e.g. what happens
    with multiple pullback/breakout cycles inside one window, or a peak
    right at the edge of the lookback).
    """
    if len(bars) < 3:
        return None

    peak_idx = max(range(len(bars) - 1), key=lambda i: _hi(bars[i]))
    swing_high = _hi(bars[peak_idx])

    pullback_bars = bars[peak_idx + 1:-1]
    if not pullback_bars:
        return None  # no pullback has formed since the peak yet

    new_support = min(_lo(b) for b in pullback_bars)
    last = bars[-1]

    if CONFIRM_ON_CLOSE:
        crossed = _cl(last) > swing_high
        trigger_price = _cl(last)
    else:
        crossed = _hi(last) > swing_high
        trigger_price = swing_high  # entry at the level itself, not wherever the bar ends up

    result = {"swing_high": swing_high, "new_support": new_support, "crossed": crossed}
    if crossed:
        result["entry_price"] = trigger_price
        result["risk"] = trigger_price - new_support
        result["risk_pct"] = (result["risk"] / trigger_price * 100) if trigger_price else None
    return result


def is_topping_tail(bar):
    """Wick on top, none on the bottom. Body size and candle color don't matter."""
    o, h, l, c = _op(bar), _hi(bar), _lo(bar), _cl(bar)
    body_bottom = min(o, c)
    body_top = max(o, c)
    has_upper_wick = h > body_top
    tolerance = body_bottom * (LOWER_WICK_TOLERANCE_PCT / 100)
    has_lower_wick = l < (body_bottom - tolerance)
    return has_upper_wick and not has_lower_wick


# ---------------------------------------------------------------------
# Extra exit warnings from the sample trading plan: MACD crossing below
# its signal line, and fading volume. Both come from 1-minute bars.
# ---------------------------------------------------------------------

def _ema(values, period):
    k = 2.0 / (period + 1)
    out = []
    for i, v in enumerate(values):
        out.append(v if i == 0 else v * k + out[-1] * (1 - k))
    return out


def macd_crossed_below(bars, fast=12, slow=26, signal=9, lookback_bars=3):
    """
    True if the MACD line crossed from above to below its signal line
    within the last `lookback_bars` bars and is below it now. Looking back
    a few bars (not just one) keeps a cross from slipping between the
    page's 30-second refreshes. Returns None when there aren't enough
    bars yet (needs about 35) -- 'unknown', not 'no'.
    """
    closes = [_cl(b) for b in bars]
    if len(closes) < slow + signal:
        return None
    macd = [f - s for f, s in zip(_ema(closes, fast), _ema(closes, slow))]
    sig = _ema(macd, signal)
    diff = [m - g for m, g in zip(macd, sig)]
    if diff[-1] >= 0:
        return False
    recent = diff[-(lookback_bars + 1):]
    return any(recent[i] >= 0 and recent[i + 1] < 0 for i in range(len(recent) - 1))


def volume_fading(bars, n=3):
    """
    True if volume fell on each of the last `n` COMPLETED bars (the newest
    bar is still forming, so its partial volume is ignored). None if there
    aren't enough bars.
    """
    if len(bars) < n + 2:
        return None
    vols = [_vol(b) for b in bars[-(n + 1) - 1:-1]]
    return all(vols[i] > vols[i + 1] for i in range(len(vols) - 1))
