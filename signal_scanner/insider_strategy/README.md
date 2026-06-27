# Insider Director-Cluster Strategy — Live Engine

Built 2026-06-27. Backed by the strategy backtest in
[docs/STRATEGY_BACKTEST_VERDICT.md](../../docs/STRATEGY_BACKTEST_VERDICT.md):

> **ML_55 + ADV filter + Regime exit** — over 9.5 years OOS:
> **CAGR +19.5%/yr · Sharpe 2.67 · Max DD −18.1% · Win 60.8%**

## What it does

Each day (typically after the EOD pipeline ingests fresh Form 4 data):

1. **Regime check** — long is only allowed when SPY (or composite proxy) is
   above its 200-day SMA. If blocked, no new entries; existing positions
   get force-exited.
2. **Detect** — query `fact_form4_transactions` for clusters that crystallised
   in the last 3 days (catches weekends) meeting:
   - ≥ 2 distinct insiders inc. ≥ 1 Director, in a trailing 30-day window
   - "Known" date = MAX(transaction_date) + 2 trading days (SEC lag)
   - Current price ≥ $5
   - 20-day average dollar volume ≥ $1M
3. **Enter** — for each fresh cluster that passes dedupe (no entry in the
   same ticker within 60 days), open a position sized at **5% of paper
   equity** with:
   - Entry: current close + 10 bps slippage
   - Stop: entry − 2.0 × ATR(14)
   - Target: entry + 2.0 × stop distance (2R)
   - Time stop: 30 days
   Subject to: max 10 concurrent positions, max 50% deployed capital.
4. **Monitor** — walk every open position and check, in order:
   - Stop hit? → exit at stop_price, reason = STOP
   - Target hit? → exit at target_price, reason = TARGET
   - Days held ≥ 30? → exit at today's close, reason = TIME
   - Regime turned bearish? → exit at today's close, reason = REGIME
   - **ML model says exit?** → call `ml_exit_model.pkl` with current trade
     features; if `p(exit-better) > 0.55`, exit at today's close, reason = ML
5. **Log** — append a row to `insider_strategy_runs` with all counts;
   summary printed to stdout.

## Modules

| File | Purpose |
|---|---|
| `detector.py` | Daily cluster scanner + regime proxy |
| `exiter.py` | Per-position ML/regime/time/stop/target check |
| `ledger.py` | SQLite tables `insider_strategy_positions` + `_runs` |
| `runner.py` | Orchestrator — the CLI entry point |

## Usage

```powershell
# Daily run (called by EOD pipeline; can be invoked manually anytime)
.\.venv\Scripts\python -m signal_scanner.insider_strategy.runner --daily

# Dry-run — see what it would do without writing to the ledger
.\.venv\Scripts\python -m signal_scanner.insider_strategy.runner --daily --dry-run

# Show current open positions
.\.venv\Scripts\python -m signal_scanner.insider_strategy.runner --status

# Use a different paper-equity for sizing (default $100k)
.\.venv\Scripts\python -m signal_scanner.insider_strategy.runner --daily --paper-equity 50000
```

## What's stored where

- **Trades + run history**: `signal_scanner/data/signals.db` — two new tables
  `insider_strategy_positions` and `insider_strategy_runs`. Indexed by
  ticker / status / entry_date.
- **ML model**: `research/artifacts/insider_strategy/ml_exit_model.pkl`
  (trained by `research/insider_strategy_backtest.py --train-ml`).
- **Equity & trade artifacts**: `research/artifacts/insider_strategy/` —
  backtest outputs that this live runner is the production version of.

## What it does NOT do yet

- **IBKR live brackets** — `--live` flag exists but is a stub. Currently the
  runner only writes to the strategy ledger (SIM mode). Wiring to
  `signal_scanner/core/order_executor.py` (`place_bracket_order`) is the
  next phase.
- **Multi-account sizing** — single paper-equity tracker.
- **Intraday exits** — entries and exits are once-per-day (matching the
  backtest, which used daily bars). Intraday exit precision would require
  live tick data and is not necessary for a 30-day-average-hold strategy.

## Daily integration (when the EOD pipeline runs)

To register inside the `QB_EOD` scheduled task, add as a final step in
`signal_scanner/institutional_intel/jobs/run_eod_pipeline.py`:

```python
# Run insider strategy daily after Form 4 incremental + other refreshes
if not _run("Insider Strategy daily", [
    "signal_scanner.insider_strategy.runner", "--daily",
]):
    failures.append("insider-strategy")
```

This isn't wired in yet — keeping it manual until we have 2-3 days of
clean output to inspect.

## Why this is different from the existing IdeaBridge

`IdeaBridge` (paper/idea_bridge.py) auto-enters paper trades from the **Kubera
conviction** scoring (the slow 13F-derived signal). This module is **separate**
because the Director-cluster + ML-exit strategy is the only one we've validated
end-to-end with realistic costs and a 9.5-year OOS backtest. Keeping it isolated
makes the live track-record clean for analytics.

## Verification before live $$

Before the strategy ever sees real dollars:
1. Run `--daily` for **30+ days** of paper.
2. Compare actual fills vs the backtest's assumed fills (slippage realism).
3. Compare actual P&L per trade vs the backtest's distribution.
4. If both match within a reasonable band, **then** wire `--live` to IBKR
   `place_bracket_order` and start with the $10K real plan.

The discipline that earned the strategy must not be wasted by skipping this.
