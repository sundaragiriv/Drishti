# Execution Loop Refactor — Technical Summary for Codex Review

**Date**: 2026-03-16
**Author**: Claude Code
**Scope**: Runtime orchestration, data freshness, broker safety, dashboard visibility

---

## Problem Statement

The Quant-Bridge scanner was designed around a single scan loop that processes the full 2,929-ticker universe through one shared IBKR connection. During market hours, this architecture causes:

1. **Intraday ML starvation**: VWAP_MR, FPB, and ORB_V2 scanners (running every 5 min) were blocked by the main scan holding the IBKR lock for 3-34 minutes.
2. **No freshness enforcement**: Scanner starts happily with prices days stale — degrading all intelligence-driven decisions.
3. **Orphan position risk**: Unreconciled IBKR positions (6 detected today) allowed new entries while broker state was divergent from DB state.
4. **No visibility**: Sniper Board showed no "data as of" timestamp — users couldn't tell if data was current or days old.
5. **No latency tracking**: No way to measure how long scans take or detect missed intraday windows.

---

## Changes Made

### 1. Execution Loop vs Research Loop Separation (`signal_scanner/main.py`)

**Before**: Single `run_scan_job()` that ran every 15 min, tried to use priority subset during market hours but still held the IBKR lock.

**After**: Two independent scan functions:

- **`run_execution_scan()`** — Market hours only (9:30-15:55 ET)
  - Uses `scanner.get_live_universe()` (runtime-budgeted, ~120s/~170 tickers)
  - Unconditionally yields during intraday ML window (9:40-11:35 ET)
  - Does NOT run off-hours

- **`run_research_scan()`** — Off-hours only (pre/post market)
  - Full universe (2,929 tickers)
  - No time budget
  - No competition with intraday ML scanners

- **`run_scan_job()`** — Dispatcher that routes to the correct loop based on `_is_market_hours()`

**Files**: `signal_scanner/main.py` lines 223-295 (approx)

### 2. Runtime-Budgeted Live Universe (`signal_scanner/scanner/multi_symbol_scanner.py`)

**New method**: `get_live_universe(watchlist_name, runtime_budget_seconds=120.0, min_conviction=40.0)`

Unlike the old `get_priority_symbols()` (fixed cap of 250), this method:
- Sizes the universe by **runtime budget** (`budget / 0.7s per symbol`)
- Filters by **quality**: only ACCUM/EXPANSION phases, conviction >= min threshold
- Sorts by: triple_lock > conviction > ML v2 score
- Returns a list that fits within the time envelope

The old `get_priority_symbols()` is preserved for backward compatibility.

**Constant**: `_ESTIMATED_SECONDS_PER_SYMBOL = 0.7` (empirical: GEX + 3 TFs × fetch+score)

**Files**: `signal_scanner/scanner/multi_symbol_scanner.py` lines 93-160 (approx)

### 3. Hard Data Freshness Gate (`signal_scanner/main.py`)

**New function**: `_check_data_freshness()` — runs at startup before scanner initialization.

- Queries `MAX(trade_date) FROM fact_daily_prices`
- Calculates business-day lag (skips weekends)
- If prices are >1 trading day stale: logs ERROR, sets `_data_degraded = True`
- Scanner runs in **DEGRADED mode** (still operates, but dashboard shows warning)
- Freshness state is propagated to `scanner.data_degraded` and `scanner.data_freshness`

**Files**: `signal_scanner/main.py` lines 117-155 (approx)

### 4. IBKR Lock Priority for Intraday Scanners (`signal_scanner/main.py`)

**Before**: Intraday ML scanners had 10s lock timeout, logged at DEBUG level when blocked.

**After**:
- Timeout increased to **30 seconds** (intraday scans take 10-30s, should always succeed if main scan respects budget)
- Blocked scans now log at **WARNING** level with message "MISSED — IBKR lock held for >30s (main scan too slow?)"
- This makes lock contention visible in logs instead of silently swallowed

**Files**: `signal_scanner/main.py` — VWAP_MR, FPB, ORB_V2 scan wrappers

### 5. Broker Reconciliation Hard Gate (`signal_scanner/core/order_executor.py`)

**New attributes**:
- `_orphan_symbols: list` — tracks unresolved orphan IBKR positions
- `_orphan_gate_active: bool` — blocks new entries when orphans exist

**Modified**: `reconcile_on_startup()` now sets the gate when orphan positions are detected.

**Modified**: `place_bracket_order()` — checks `_orphan_gate_active` before placing any order. If active, returns `False` with WARNING log.

**New method**: `acknowledge_orphans()` — clears the gate after manual review in TWS. Can be called via:
```python
scanner._paper_trader._order_executor.acknowledge_orphans()
```

**Behavior**: If 6 orphan positions exist (like today's STX, VRT, WELL, XOM, LMT, MS), ALL new IBKR entries are blocked until the user either:
1. Closes the orphan positions in TWS, or
2. Calls `acknowledge_orphans()` to explicitly override

**Files**: `signal_scanner/core/order_executor.py` lines 58-63 (attrs), 79-92 (acknowledge), 108-115 (gate check), 318-332 (reconcile gate activation)

### 6. Dashboard Freshness Badge + Degraded Banner (`signal_scanner/dashboard/`)

**Layout** (`layouts/sniper_board_view.py`):
- Added `sniper-freshness-badge` span — shows "Prices as of: 2026-03-12"
- Added `sniper-degraded-banner` span — shows "DEGRADED — prices N trading days stale" (hidden by default)

**Callbacks** (`sniper_callbacks.py`):
- New `update_freshness_badge()` callback — fires every 60s (via `sniper-refresh-interval`)
- Queries `MAX(trade_date) FROM fact_daily_prices` for the badge
- Checks `scanner.data_degraded` for the degraded banner

**Scanner status** (`multi_symbol_scanner.py`):
- New attributes: `data_degraded: bool`, `data_freshness: Dict`
- Exposed in `get_status()` → `data_degraded`, `data_freshness` keys

### 7. Scan Latency Metrics (`signal_scanner/database/`)

**Schema** (`database/models.py`):
- Added `duration_seconds REAL` and `scan_type TEXT` columns to `scan_history`

**Migration** (`database/db_manager.py`):
- New `_migrate_scan_history()` — adds columns to existing DBs via `ALTER TABLE`
- Called in `init_db()`

**Recording** (`scanner/multi_symbol_scanner.py`):
- `scan_symbols()` now times the entire scan with `time.monotonic()`
- Passes `duration_seconds` and `scan_type` (live/priority/research) to `record_scan()`
- End-of-scan log now includes duration and type: `"... 42.3s (live)"`

---

## Files Modified

| File | Changes |
|------|---------|
| `signal_scanner/main.py` | Freshness gate, execution/research loop split, lock timeout increase |
| `signal_scanner/scanner/multi_symbol_scanner.py` | `get_live_universe()`, latency timing, `data_degraded`/`data_freshness` attrs, status exposure |
| `signal_scanner/core/order_executor.py` | Orphan gate, `acknowledge_orphans()`, entry blocking |
| `signal_scanner/dashboard/layouts/sniper_board_view.py` | Freshness badge + degraded banner HTML |
| `signal_scanner/dashboard/sniper_callbacks.py` | `update_freshness_badge()` callback |
| `signal_scanner/database/models.py` | `duration_seconds` + `scan_type` columns |
| `signal_scanner/database/db_manager.py` | `record_scan()` updated, `_migrate_scan_history()` added |

---

## Not Changed (Deferred)

| Item | Reason |
|------|--------|
| Phase 2B: Pre-market readiness blocking gate | `daily_health_check` exists but wiring it as a hard gate requires more testing of edge cases (what if health check itself fails?) |
| Phase 4B: Lock contention integration tests | Requires mocking IBKR + scheduler timing — better done in a separate test-focused PR |
| MCP server paths | Verified already correct (`signal_scanner/watchlists/`, `signal_scanner/logs/`) |

---

## Testing Checklist

- [ ] Import verification: `python -c "from signal_scanner.main import _check_data_freshness; print(_check_data_freshness())"` — should return freshness dict
- [ ] Import verification: `python -c "from signal_scanner.scanner.multi_symbol_scanner import MultiSymbolScanner; print('OK')"`
- [ ] Import verification: `python -c "from signal_scanner.core.order_executor import OrderExecutor; print('OK')"`
- [ ] Start scanner: `python -m signal_scanner --watchlist universe_master --ibkr-port 7497` — should show:
  - FRESHNESS warnings if prices stale
  - "EXECUTION scan: N symbols (budget=120s)" during market hours
  - "RESEARCH scan: full universe" off-hours
  - Orphan gate messages if orphan positions exist
- [ ] Dashboard: Sniper Board should show "Prices as of: YYYY-MM-DD" badge
- [ ] Dashboard: If data is stale, degraded banner should appear
- [ ] Existing `pytest tests/test_trading_pipeline.py` should still pass (no behavioral changes to core scan logic)

---

## Risk Assessment

| Risk | Mitigation |
|------|-----------|
| Live universe too small → misses signals | `min_conviction=40` is conservative; `get_priority_symbols()` preserved as fallback |
| Freshness gate blocks scanner on weekends | Uses business-day calculation (skips Sat/Sun) |
| Orphan gate too aggressive | `acknowledge_orphans()` method provides explicit override |
| `scan_history` migration fails on old DB | SQLite `ALTER TABLE ADD COLUMN` is safe (no data loss, nullable columns) |
| Intraday ML window (9:40-11:35) too wide | Same window as before — only the scan type changed, not the window |
