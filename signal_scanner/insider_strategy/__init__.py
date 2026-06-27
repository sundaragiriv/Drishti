"""Insider Director-Cluster Strategy — live engine.

Backtested at:
   ML_55 (ML exit threshold 0.55 + ADV>=$1M + regime gate)
   CAGR +19.5%/yr, Sharpe 2.67, Max DD -21.4%, win 60.8% over 9.5 yr OOS

See docs/STRATEGY_BACKTEST_VERDICT.md for the full verdict.

Modules:
  detector  — daily new Director-cluster scanner (PIT correct)
  exiter    — ML-driven exit monitor for open positions
  ledger    — SQLite-backed position tracker
  runner    — orchestrator: detect -> enter -> monitor -> exit

Run:
  python -m signal_scanner.insider_strategy.runner --daily
"""
