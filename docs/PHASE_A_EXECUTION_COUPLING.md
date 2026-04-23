# Phase A: Execution Coupling Documentation

## Current State (Mar 18 2026)

### What IS decoupled
- **Bar printer** runs on its own thread with its own IBKR connection (clientId+5)
- **Strategy engine** reads bars from SQLite only, never touches IBKR
- **Old scan loop** is fully disabled (`run_scan_job()` returns immediately)
- **Dashboard** reads from DB only, no IBKR data calls

### What is NOT decoupled
Strategy evaluation and trade execution are still coupled inside each scanner's
`_scan_ticker()` method. When the strategy engine calls `scanner._scan_ticker()`,
that method both:

1. Evaluates the setup (features, ML score, gates) — **pure logic, no IBKR**
2. Creates a paper trade if setup passes — **side effect inside evaluation**

Example from `vwap_mr_live.py` line ~654:
```python
def _scan_ticker(self, ticker, now_et, cached_bars=None):
    # ... evaluate features, ML score, gates ...
    if setup_passes:
        trade_id = self._pt.enter_idea_trade(idea)  # <-- creates trade inline
        return True
```

### Why this matters
- The strategy engine cannot "emit signals" without also executing them
- If the paper trader or order executor fails, it can affect strategy evaluation
- Signal history and execution history are not independently auditable

### Why it's deferred
- Refactoring requires rewriting `_scan_ticker()` in 3 files (~600 lines each)
- The current behavior is the same pattern that has been working in production
- Decoupling is Phase H in the master build plan

### What would full decoupling look like
```
Current:
  strategy_engine → scanner._scan_ticker() → evaluates + creates trade

Target (Phase H):
  strategy_engine → scanner._evaluate_setup() → returns signal dict
  execution_engine → reads signal from live_strategy_signals → creates trade
```

### Risk assessment
- **Low risk for paper trading**: trades are created but no real money moves
- **Medium risk for IBKR live**: order placement happens inside evaluation thread
- **Mitigation**: IBKR order executor has its own error handling and doesn't block

### Recommendation
Ship Phase A with this coupling documented. Fix in Phase H after the core
architecture is proven and stable.
