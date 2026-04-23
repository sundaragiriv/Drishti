# Quant-Bridge TODO — Fix List
Generated: 2026-03-12

---

## CRITICAL — Why Nothing is Trading Right Now

### ROOT CAUSE 1: Scanner First Pass Takes 2+ Hours (KILLS Intraday Trading)
- Scanner started 8:25 AM, still no signals at 10:20 AM (2 hrs in)
- VWAP_MR entry window: 10:00–11:30 AM ET
- VWAP_MR needs `_intelligence_snapshot` populated — only happens after first scan pass
- Result: VWAP_MR has been blind all morning. Zero chance of a trade during today's window.
- This has been the case EVERY day. VWAP_MR has NEVER had a working snapshot in time.
- **Fix**: Load intelligence snapshot directly from DB at scanner startup, BEFORE first pass
  - 54 qualifying tickers (accum + conv>=65) already in DB — load them in <1 second
  - No scanner restart needed going forward — just add startup DB load
  - Risk: LOW (read-only query, no DB write)

### ROOT CAUSE 2: Sniper Board Showing Zero (Regime Toggle)
- Data IS there: 20 ideas (CGTX, WELL, AMAT, MS, VRT, LMT, XOM etc.)
- Dashboard "Regime Aligned" toggle is ON → filters all LONG ideas in DISTRIBUTION
- **Immediate fix**: Turn OFF the Regime Aligned toggle in the dashboard
- **Proper fix**: Toggle should DEFAULT to OFF, or show ideas with regime warning badge
  instead of hiding them entirely

### ROOT CAUSE 3: Live Scanner 812 Signals Are Stale (March 6)
- Signals table last updated 8 PM March 6 — 6 days old
- Today's scanner hasn't finished first pass, so no new signals written yet
- Not a bug — scanner is just slow. Will self-resolve when first pass completes (if ever)
- **Fix**: Same as Root Cause 1 — faster first pass or pre-loaded snapshot

---

## TONIGHT — Architectural Redesign (Not a Patch)

### REDESIGN: Decouple Intelligence from Live Scan [CRITICAL — BLOCKS ALL INTRADAY TRADING]

**The Design Flaw:**
Scanner conflates two jobs with completely different data sources and lifecycles into one sequential loop:
- Job 1 (Intelligence): conviction, ML score, accum phase → comes from DuckDB, static, changes once/day → loads in <1 second
- Job 2 (Live Market): GEX, VWAP, real-time confluence → comes from IBKR, dynamic, slow across 2929 tickers

These must be separated. Right now Job 1 is blocked behind Job 2. That's why VWAP_MR has never fired.

**Three-Layer Architecture:**
```
LAYER 1 — Intelligence Snapshot (DB-first, startup + EOD refresh)
  → Loads at scanner __init__ from DuckDB in <1 second
  → Today's Tier 1: 167 ACCUM tickers, 54 with conviction>=65
  → Refreshed once daily after EOD pipeline completes
  → NEVER waits for scan loop

LAYER 2 — Intraday Strategies (VWAP_MR, FPB, ORB_V2)
  → Reads from Layer 1 snapshot (populated at startup)
  → Fetches only Tier 1 tickers from IBKR (54 tickers, <5 min)
  → Completely independent of main scan loop
  → Armed and ready by 9:30 AM open

LAYER 3 — Main Scan Loop (full 2929 tickers)
  → Runs in background throughout the day
  → ENRICHES existing snapshot entries with live data (GEX, confluence)
  → Slow is fine — not blocking anything
  → Tier 1 tickers scanned FIRST (priority queue)
```

**Tiered Scan Order (fix the 2-hour problem):**
```
Tier 1 (54 tickers): ACCUM + conviction>=65 → scanned first, done in <10 min
Tier 2 (113 tickers): ACCUM + conviction>=55 → scanned next
Tier 3 (2762 tickers): full universe → background, throughout day
```

**Files to change:**
1. `signal_scanner/scanner/multi_symbol_scanner.py`
   - `__init__`: call `_load_intelligence_snapshot()` from DB immediately (1 line)
   - `scan_watchlist()`: sort tickers — ACCUM + high conviction first, rest after
   - Scan loop: call `_intelligence_snapshot[ticker].update(live_data)` immediately per ticker (not at end of pass)

2. `signal_scanner/paper/vwap_mr_live.py` / `fpb_live.py` / `orb_v2_live.py`
   - No change needed — they already read from `_intelligence_snapshot`
   - Will just work once snapshot is pre-loaded

**Result:** Scanner starts 8:25 AM → snapshot loaded 8:26 AM → Tier 1 enriched by 8:35 AM → VWAP_MR fully armed at 10:00 AM open. Every day, reliably.

---

### FIX 1: Pre-load Intelligence Snapshot at Scanner Startup [CRITICAL]
**File**: `signal_scanner/scanner/multi_symbol_scanner.py`
**Change**: In `__init__`, call `_load_intelligence_snapshot()` from DB immediately at startup
**Impact**: VWAP_MR gets 54 qualifying tickers (today) before 9:30 AM open — number varies daily
**Risk**: None — read-only

### FIX 2: Sniper Board Regime Toggle Default = OFF [HIGH]
**File**: `signal_scanner/dashboard/layouts/sniper_board_view.py`
**Change**: Default toggle value to False (show all, badge them instead of hiding)
**Impact**: 20 ideas immediately visible on Sniper Board
**Risk**: None

### FIX 3: Re-run ML Scoring Clean [HIGH]
- 8 ACCUM tickers have ml_score_v2=0.0 (ALB, AON, BA, BG, BIIB, CCB, CHTR, CRGY)
- Run after market close with NO other processes touching DuckDB
- Command: `python -m signal_scanner.institutional_intel.intelligence.ml_signal_v2 --score --write`

### FIX 4: Make run_premarket.py Strictly Sequential [HIGH]
**File**: `run_premarket.py`
**Problem**: Short data + ML scoring + squeeze all launch as subprocesses, race for DuckDB write lock
**Fix**: Add explicit sleep/wait between each step, or use subprocess.run() sequentially
**Impact**: No more failed premarket steps due to lock collisions

### FIX 5: Fix Short Data Self-Deadlock [MEDIUM]
**File**: `signal_scanner/institutional_intel/jobs/short_data_loader.py`
**Problem**: CTB step opens second DuckDB write connection while short volume step holds first
**Fix**: Single connection passed through all steps, or CTB runs as separate sequential call

---

## TONIGHT — Medium Priority

### FIX 6: Wire `intraday_ml_predictions` to Daily Refresh or Drop It
- Table only written during backtests, never read in live trading
- Either: add to EOD pipeline to write today's live ML predictions (useful for review)
- Or: remove the write entirely (dead code)
- Decision needed before fix

### FIX 7: Add Self-Check Logging to VWAP_MR
**Problem**: No way to know if VWAP_MR is alive and scanning without grepping logs
**Fix**: Every 5-min cycle, log: "VWAP_MR: snapshot={n} tickers, window=OPEN/CLOSED, scanned={x}"
**Impact**: Immediate visibility into whether the strategy is working

### FIX 8: Tighten Live Scanner Signal Criteria
- 812 signals (stale) suggests too-broad criteria
- All 812 result in HOLD recommendation — not actionable
- Only 413 BUY signals actually reach recommendation=BUY
- **Tighten**: Only write to signals table if recommendation=BUY or SELL (not HOLD)
- **Impact**: Cleaner Live Scanner, fewer noise signals

---

## THIS WEEK — AI Signals Improvements

### IMPROVEMENT 1: Cross-Signal Stacking
- Post-process all 9 signal types, group by ticker
- Surface tickers with 2+ signals firing in same week as CONVERGENCE_STACK
- Rank above any single signal on AI Signals tab
- **Effort**: Small — logic layer on top of existing signals, no new data

### IMPROVEMENT 2: Neural Interconnectivity (Lead-Lag)
- Use `fact_stock_correlations` + `dim_related_companies` already in warehouse
- When signal fires on ticker A, check correlated tickers (r>0.80)
- Flag correlated tickers as secondary signals: "XOM signal — CVX likely to follow"
- Supply chain / sector cascade: NVDA fires → AMD, AVGO, MRVL flagged
- **Effort**: Medium — needs relationship graph built from correlation table

### IMPROVEMENT 3: Regime-Aware Signal Weighting
- In DISTRIBUTION: upweight PULLBACK_SNIPER + CONTRARIAN (mean reversion works better)
- In BULL_TREND: upweight ACCUMULATION_BREAKOUT + SMART_MONEY_CONVERGENCE
- Currently all regimes treated equally
- **Effort**: Small — multiply signal score by regime weight table

### IMPROVEMENT 4: Earnings Catalyst Overlay on Predictions
- Flag HIGH_CONVICTION_PREDICTION tickers with earnings/8-K in prediction window
- Source: `fact_form8k_events` already in warehouse
- Show as risk flag or catalyst booster in AI Signals card
- **Effort**: Small — join to existing 8-K table

### IMPROVEMENT 5: Conviction Momentum in Predictions
- HIGH_CONVICTION_PREDICTION uses static snapshot score
- Add: is conviction score rising or falling over last 3 weeks?
- Rising conviction = stronger signal; falling = downgrade
- **Effort**: Medium — need conviction score history (may need new table)

---

## MONITORING CHECKLIST (Every Morning Before Open)

```
[ ] IBKR TWS started and connected
[ ] run_premarket.py completed with 0 FAIL
[ ] HMM regime noted (DISTRIBUTION = SHORT only via IdeaBridge)
[ ] VWAP_MR qualifying tickers > 0 (check intelligence snapshot loaded)
[ ] Sniper Board showing ideas (toggle regime filter OFF if needed)
[ ] No stale DuckDB processes (check tasklist for hanging python.exe)
[ ] Scanner started before 9:00 AM (gives 90 min before VWAP window)
```

---

---

## PARKED — Options & UI CTA (Not Blocking, High Complexity)

### OPTIONS TRADING (PARKED — Infrastructure Not Ready)
- Polygon v3/trades API requires premium ($199/mo) for real-time options flow
- Dark pool derivation from FINRA short volume is a proxy, not actual dark pool
- Options chain data: yfinance OK for scanning but not for live greeks
- **When ready**: `fact_options_flow` table exists; `options_bridge.py` has framework
- **Pre-requisites**: Premium data subscription + IBKR options account enabled

### CTA "ENTER TRADE" BUTTON ON SNIPER/AI SIGNAL CARDS (PARKED)
- On each Sniper card and AI Signal card: "Enter Trade" button
- Prefills My Trades form with signal's entry/stop/target levels
- User can override price before submitting
- **Files to touch**: `sniper_callbacks.py`, `reports_callbacks.py`, `my_trades_callbacks.py`
- **Complexity**: Medium — needs inter-component state via `dcc.Store`

### MANUAL TRADE STOP/TARGET MONITOR + EXIT ALERT (PARKED)
- Background thread polls My Trades open positions every 5 min during market hours
- Fetches latest close from `fact_daily_prices`; compares to stop/target
- When stop or target hit: set `alert_status = 'EXIT_NOW'`, log timestamp
- Dashboard banner/badge: "EXIT NOW — AAPL hit stop $181.72 at 10:43 AM"
- **Needs**: `alert_status` column in My Trades table, alert banner component in layout
- **Files to touch**: `my_trades_callbacks.py`, `my_trades_view.py`, new `trade_monitor.py`

### DARK POOL BADGE ON SNIPER CARDS (PARKED)
- Show `dark_pool_pct` badge when > 50% on Sniper Board cards
- New `DARK_POOL_PRESSURE` AI Signal type: dark_pool_pct > 55% + rising trend → bearish
- **Files to touch**: `sniper_callbacks.py`, `ai_signals.py`
- **Pre-requisite**: Polygon premium for real dark pool data (FINRA proxy available)

---

## DONE TODAY (2026-03-12)
- [x] **tests/test_trading_pipeline.py** — 7-tier automated pre-market test suite. Run `pytest tests/test_trading_pipeline.py -v` before open.
- [x] **Sniper Board regime toggle** — defaulted to OFF. 20 LONG ideas immediately visible.
- [x] **Snapshot pre-load at startup** — `_intelligence_snapshot` loads from DB in `__init__`. VWAP_MR armed before 9:30 AM open from tomorrow.
- [x] **Tiered scan order** — ACCUM tickers scan first. 167 ACCUM tickers front of queue, then 2762 rest.
- [x] **ML scoring** — re-ran clean. All 3140 tickers scored.
- [x] **SHORT Conviction Engine built** — `short_conviction_engine.py`. 6-dimensional parallel to LONG conviction. 21 SHORT signals in 2025-Q4. Wired into pipeline as Step 6j.
- [x] **Sniper Board symmetric** — now shows both LONG (conv>=65) and SHORT (short_conv>=45) ideas, EV-ranked together.
- [x] **IdeaBridge SHORT path** — `_get_short_distribution_ideas()` added. Auto-enters SHORT trades in DISTRIBUTION regime. Runs before Triple Lock check.
- [x] **FIX 4 & 5 were misdiagnosed** — premarket IS sequential. Short data issue was external rogue PID 60720.

---

## DONE / CONFIRMED WORKING
- [x] HMM Regime Detection — fitted daily, gates IdeaBridge correctly
- [x] Paper trade execution path — SWING trades from Mar 5 confirmed working
- [x] Intelligence scoring (conviction, ML v2, triple lock) — data fresh
- [x] Short data / dark pool — now current through Mar 11
- [x] ML models loaded — VWAP_MR (AUC 0.823), FPB (AUC 0.856), ORB_V2 (AUC 0.731)
- [x] IBKR connection with auto-fallback clientId — working
- [x] GEX calculation — working for liquid-options tickers
- [x] EOD pipeline embedded in scanner — runs daily if scanner stays up
