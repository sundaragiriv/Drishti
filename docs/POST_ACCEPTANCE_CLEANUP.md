# Post-Acceptance Cleanup

Non-blocking technical debt from accepted phases. Batch efficiently.

## 1. Session Archiver Dedupe (Phase A)
- `archived_strategy_signals` and `archived_runtime_health` have no uniqueness guard
- Rerunning archiver duplicates rows
- **Fix**: Add PRIMARY KEY or INSERT ON CONFLICT DO NOTHING
- **Risk**: Low — only affects EOD replay data counts

## 2. Stale Comments / Docstrings
- `swing_feature_engine.py` line ~879: comment claims settled-quarter mapping but code sets `intel_quarter = quarter`
- Any remaining references to "Sniper Board" (now "Swing Snipers") in non-UI code
- Any remaining references to "Live Scanner" (now "Intraday") in non-UI code
- Any references to `option_setups` where code now uses `fact_options_contracts`
- **Fix**: Search-and-replace pass
- **Risk**: Zero — comments only

## 3. Options Board Direction-Awareness (Phase C follow-up)
- Board always calls `recommend_expressions(ul, "LONG", ...)` for non-SHORT names
- Should check intelligence signal direction per underlying
- **Fix**: Already partially done in reports_callbacks.py but may still default LONG for some
- **Risk**: Low — only affects recommended contract type, not data correctness

## 4. ISR Composite Alignment (Phase G follow-up)
- `Interconnected` and `Options Quality` scorecard components are wired and displayed
- They do NOT affect the composite score or verdict
- **Fix**: Add them to composite weighted sum with small weights (e.g., 0.05 each)
- **Risk**: Low — only changes verdict sensitivity slightly

## 5. ExecutionConsumer Telemetry (Phase H follow-up)
- Already fixed: log now shows actual routing outcome
- Verify in live testing that SIM vs IBKR is accurately reflected
- **Risk**: Zero

## 6. FPB/ORB_V2 Structural Cleanup
- `_evaluate_only` wrapper pattern works but is less clean than VWAP_MR's full extraction
- Full refactor would extract pure evaluation into `_evaluate_setup()` the same way VWAP_MR does
- **Fix**: Optional refactor (~150 lines each)
- **Risk**: Low — current wrapper works correctly

## Implementation Order
1. Archiver dedupe (5 min)
2. Stale comments (10 min)
3. ISR composite alignment (10 min)
4. Options Board direction (already mostly done)
5. FPB/ORB_V2 refactor (optional, 30 min each)
