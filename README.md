# Morning Scan — live scanner built from your 5 pillars + everything since

## What's in this folder

| File | Run it | What it does |
|---|---|---|
| `build_reference_cache.py` | **weekly** | Builds the tradable universe, 50-session average volume, and a float proxy (shares outstanding). Slow (Finnhub's free tier is rate-limited), so it's kept separate from the daily run. |
| `scanner.py` | **every morning** | Reads that cache, pulls a live snapshot for every symbol that clears the float pillar, scores all five pillars plus every guardrail and tier below, then checks the entry/exit candle pattern on whatever made the shortlist. |
| `peers.py TICKER` | as needed | Manual research aid — looks up a ticker's peers and their live stats. Not part of the automated scan. |
| `candle_patterns.py` | (imported by scanner.py) | The pullback/breakout entry pattern and the topping-tail exit signal. |
| `pillars_config.py` | (imported) | All the numbers in one place — edit here, not in the scripts. |

The **Morning Scan dashboard** (the published artifact) is a separate,
sample-data prototype for dialing in thresholds visually, plus the one
piece of this system that needs to persist state across your trading
day: the **Walk Away Check** panel (your running P&L). See that section
below.

## Setup

1. **Alpaca account** (free): sign up at alpaca.markets, create a paper
   trading account, grab your API key + secret. Free accounts get
   real-time IEX data automatically — no separate data subscription to
   start.
2. **Finnhub account** (free): sign up at finnhub.io for an API key —
   used for the float proxy and for `peers.py`.
3. Copy `.env.example` to `.env`, fill in your three keys.
4. `pip install -r requirements.txt`
5. `python build_reference_cache.py` (slow the first time — grab coffee)
6. `python scanner.py` — the one you actually run each morning.

## The five pillars, as you defined them

1. **Up 10%+ on the day** — demand.
2. **Relative volume ≥ 5x** — today's volume vs. the symbol's own
   **50-session** average daily volume. A longer average is a steadier
   baseline, so a stock has to work harder to hit 5x against it.
3. **News catalyst required** — the causal link: it's the reason the
   stock is up and the reason volume surged, not a coincidence.
   **Exception:** see "Short squeeze exception" below — not a hard
   discard rule.
4. **Price $2–$20** — capital-efficient for smaller accounts, and this
   range draws the retail volume that higher-priced, lower-volume,
   higher-risk names don't.
5. **Float under 20M shares** — supply. A small, fixed pool of shares
   (set at IPO) means a real demand surge has fewer sellers to absorb
   it against — the imbalance that drives the outsized moves this
   system is built to catch.

All five live in `THRESHOLDS` in `pillars_config.py`.

## Guardrails and tiers on top of the five

**Volume worth noticing — not a requirement.** RVOL alone can look
impressive off a tiny baseline. The scanner tracks whether a stock has
crossed **25,000,000 shares today** (`VOLUME_TO_WATCH`) as an
informational marker (✓ in the table) — it never excludes a stock from
candidate/squeeze/A+ status. A stock can be a perfectly good candidate
at 7am on 3M shares of volume.

**Short squeeze exception (4/5, missing only news).** Not every huge
move has a catalyst — sometimes shorts get squeezed past their max pain
point and are forced to cover, fueling the move with no news behind it
(GameStop 2021 is the famous case). A stock clearing pillars 1, 2, 4,
and 5 but missing only news isn't a random partial miss — it's flagged
separately as `squeeze_watch`, with the single leading gainer among
them called out. Still not a full candidate — size it smaller, since
there's no catalyst confirming the move holds — but don't discard it
just because pillar 3 is empty.

**Highest-quality tier — the "sweet spot."** A tighter overlay, not a
separate standard — every stock clearing this also clears the baseline
five. Tracked as `HIGHEST_QUALITY`:
- Up 30%+ (vs. 10%+ baseline)
- Breaking news headline / PR specifically
- Price $5.00–$10.00 (inside the $2–$20 baseline band)
- RVOL ≥ 5x
- Float under 10M shares (vs. 20M baseline)
- Time between 7:00–10:00am ET

Printed with a `*` and `A+ SETUP`, sorted above ordinary candidates.
Note the window runs to 10:00am — thirty minutes into the part of the
session flagged below as "harder." That's taken as given from how you
defined this tier, not auto-reconciled; tighten `window_end_min` if
that overlap wasn't intentional.

**Session timing — context, not a filter.** 7:00–9:30am ET is the
cleanest window: before the regular session opens, market makers don't
yet have visible market orders, stop orders, or halt mechanics working
in their favor against retail. Once the 9:30 bell rings, trading
generally gets harder. Both scripts surface the current phase; it never
hides, downranks, or filters a stock.

## Entry pattern: pullback + crossing candle

From the whiteboard sketch and the "Patiently Waiting for a Pattern to
Form" slide: a run-up sets a high, a pullback sets a new support level,
and a candle breaking back above that high is the entry trigger, with
risk = entry price − new support.

**The one assumption made on your behalf, flagged on purpose:** this
uses **close-based confirmation** — the candle has to actually close
above the prior high, not just wick through it. You described the
trigger as "enter right when it breaks," which reads as intrabar/live,
but never fully confirmed that against the close-based "confirmation"
language in the course slide. Close-based is the safer default (fewer
false triggers from a wick that fails). To switch to the live,
intrabar version, set `CONFIRM_ON_CLOSE = False` in
`candle_patterns.py` — a real behavior change, not a style tweak.

This only runs on your shortlist (candidate / squeeze-watch / A+),
since 1-minute bar history is a heavier pull than a snapshot — printed
as `Entry / Exit Signals` at the end of each scan.

## Exit signal: topping tail

Per your clarification: not a wick-to-body ratio — just a candle with a
wick on top and none on the bottom, regardless of body size or color.
Checked on the latest bar for every shortlisted name, flagged inline as
`⚠ TOPPING TAIL`.

**Still fully manual, by your own call:** big seller on Level 2, a
hidden seller (buy orders filling, ask not budging), and a burst of red
on the tape. These need order-book depth and tape data no free API
provides — see the Level 2 discussion from earlier in the conversation
if you want to revisit that (Databento is the realistic path if you
ever want this in code; a platform like DAS Trader, Lightspeed, or
TradeZero if you want it as a visual skill).

## Walk Away Check

Your 5 rules split into two groups:

- **Objective, from data already on hand:** best trading window closed
  (past 9:30am ET) and no A+ setups this scan — `scanner.py` prints
  both at the end of every run.
- **Needs your running P&L or a market-feel call** (lost half the day
  from peak, hit max loss, today's theme is bearish): these live in the
  dashboard's **Walk Away Check** panel — the one piece with state that
  persists across your day. Not connected to your broker; you enter
  your own P&L. Max loss defaults to the average of your logged winning
  days once you've logged at least one, with a manual override always
  available. Two reminders show as plain text once any condition
  triggers: don't come back once you've left, and the right moment
  usually feels too soon.

## Step 1 research aid: peers.py

Stock selection starts with a judgment call — an "obvious" stock other
traders are likely watching, often similar to something that made a big
move recently. That stays yours; `peers.py` speeds up the legwork:

```
python peers.py ZTG
```

Looks up ZTG's peers (Finnhub's `/stock/peers`), pulls each one's live
price/% change/volume from Alpaca. A lookup tool, not a filter —
doesn't touch candidate/squeeze/A+ status, doesn't run automatically.

## Important limitations

**Volume coverage.** Alpaca's free tier streams IEX only (~2-3% of
total market volume). Fine for testing; before trusting RVOL for real
decisions, look at Alpaca's AlgoTrader Plus (~$99/mo as of writing —
confirm current pricing) for full SIP data.

**Float is a proxy.** No free API gives true public float. This uses
*shares outstanding* from Finnhub as a conservative stand-in — a
"pass" is worth a manual look, not gospel.

**RVOL is simplified.** Compares today's cumulative volume to a
time-of-day-scaled average, not a true minute-by-minute curve. Refine
once you've watched real candidates behave, especially pre-market.

**The entry/exit pattern logic is a first pass.** `find_pullback_setup`
in `candle_patterns.py` is a straightforward heuristic on a rolling
30-minute window — it hasn't been tested against real multi-cycle
pullback/breakout sequences yet. Expect to tune it once you've watched
it run.

**This scans and ranks — it doesn't trade.** On purpose.

**SDK drift.** Alpaca's Python SDK evolves; if an import errors, check
https://alpaca.markets/sdks/python/api_reference/ — the concepts here
map directly even if a path moves.

## Scheduling it

macOS/Linux (`crontab -e`), assuming this folder is `~/stock-scanner`:

```
# Weekly refresh, Sunday 8pm
0 20 * * 0 cd ~/stock-scanner && /usr/bin/python3 build_reference_cache.py >> cache.log 2>&1

# Daily scan, 8:45am ET on weekdays
45 8 * * 1-5 cd ~/stock-scanner && /usr/bin/python3 scanner.py >> scan.log 2>&1
```

On Windows, Task Scheduler with the same idea: one weekly task, one
weekday-morning task.

## Connecting this to the dashboard

`scan_results.json` after each run has the same shape of data as the
dashboard (ticker, price, % change, RVOL, float, pillar pass/fail). A
published web page can't reach into a file on your computer directly —
bridging the two means either running the dashboard locally too, or a
small local server the page can fetch from. Say the word if you want
the live data actually showing up in the dashboard UI instead of the
terminal.
