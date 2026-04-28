# Plan — Yang-Zhang + Hurst Features for v3_5d (executing 2026-04-27 night)

**Status:** Approved 2026-04-27 night. Bundled Hurst pack. Lyapunov +
Rough Vol explicitly REJECTED (not deferred — final). Executing tonight
in parallel with EOD pipeline finishing.

## Hypothesis

Adding Yang-Zhang volatility features to v3_5d's feature set improves
honest AUC over the 0.5510 baseline. The bulk of the lift (if any) will
come from `yz_overnight_share` because 5-day-hold positions span 4
overnight windows where ATR is blind to gap risk.

## Features to add (4 YZ + 3 Hurst = 7 total)

### Yang-Zhang pack — volatility structure

| Feature | Definition |
|---|---|
| `yz_vol_14d` | 14-day rolling Yang-Zhang annualized volatility |
| `yz_vol_5d`  | 5-day rolling YZ vol (matches our hold horizon) |
| `yz_overnight_share` | `overnight_var / total_var` — gap-risk indicator |
| `yz_vs_atr_ratio_14` | `yz_vol_14d / (atr14/close × √252)` — **windows matched 14 vs 14** |

Note: window-matched orthogonality ratio per user feedback (YZ_14 vs ATR_14,
NOT YZ_22 vs ATR_14). Cleaner test of "what does YZ add over ATR at the
same lookback?"

### Hurst pack — return persistence (added per user expansion)

| Feature | Definition |
|---|---|
| `hurst_64d` | 64-day Hurst exponent via DFA (detrended fluctuation analysis) |
| `hurst_252d` | 252-day Hurst (annual structural regime) |
| `hurst_regime` | categorical: TRENDING (H>0.55) / RANDOM (0.45-0.55) / MEAN_REVERT (H<0.45) |

Hurst rationale: orthogonal to ATR/SMA/RSI which are short-term dynamics.
Hurst measures **structural persistence** — directly answers "is this
ticker currently trending or mean-reverting?" which is what our 5-day
swing target needs to know. 70+ years of academic citation depth, used
operationally by Renaissance / DE Shaw stat-arb.

REJECTED (final, not deferred):
- **Lyapunov exponents** — Prediction Company's success was an ensemble,
  not Lyapunov in isolation. Empirical λ on equity returns has CIs as
  wide as the estimate. HMM regime already answers the same question
  more directly.
- **Rough Volatility** — solves vol forecasting, useful for options /
  variance swaps. We trade direction. Existing ATR + atr_compression
  capture 80% of what rough vol gives us for stop sizing. Wrong tool
  for our frame.

## Yang-Zhang formula (reference)

```
σ²_YZ = σ²_overnight  +  k·σ²_open-to-close  +  (1-k)·σ²_RS

where:
  σ²_overnight     = Var( ln(O_t / C_{t-1}) )       [overnight gap]
  σ²_open-to-close = Var( ln(C_t / O_t) )           [intraday drift]
  σ²_RS = mean[ ln(H/C)·ln(H/O) + ln(L/C)·ln(L/O) ] [Rogers-Satchell]
  k     = 0.34 / (1.34 + (n+1)/(n-1))               [optimal weight]
```

## Locked holdout (per user modification 3)

- Reserve **last 90 calendar days of data as a locked holdout**.
- Locked holdout is **evaluated EXACTLY ONCE** at the end.
- All purged-CV training and feature selection use rows **before** the
  holdout boundary.
- This is the final "did we lie to ourselves" check. Prevents the
  look-then-tweak loop.

## Workflow

1. **Compute YZ features in DuckDB** (~30 min)
   - SQL window functions on `fact_daily_prices` for the 4 features
   - Persist as a separate parquet so we don't churn the v2 build
2. **Merge with existing v2 features** (~10 min)
3. **Define holdout boundary**: rows with `trade_date >= today - 90d` are
   LOCKED. All other rows are training/CV pool.
4. **Purged 5-fold CV on the training pool** (~50 min)
   - `label_hit_1R_5d` (same as F1-A baseline)
   - PurgedKFold(horizon=5, embargo=5)
   - Same LightGBM hyperparameters as F1-A
5. **Single evaluation on the locked holdout** at the end (~30 sec)
6. **Generate report** (per user modification 2):
   - Honest mean purged AUC + std (vs 0.5510 baseline)
   - **Locked-holdout AUC** (single evaluation)
   - **Brier score** on holdout
   - **Calibration plot** — predicted prob deciles vs actual hit rate
   - **Stratified AUC** — split holdout into low/mid/high YZ_22d
     quintiles (or YZ_14d since that's our feature), report AUC per
     quintile to see if YZ helps more in high-vol regimes
   - **SHAP feature importance** — which YZ feature contributes most?
     Hypothesis says `yz_overnight_share` should rank high.

## Decision criteria

| Outcome | Action |
|---|---|
| Holdout AUC > 0.575 + Brier improves + SHAP shows YZ_overnight_share top-10 | Ship — wire as the new v3_5d artifact |
| Holdout AUC 0.55-0.575 with neutral Brier | Wash — accept and move on |
| Holdout AUC < 0.55 or Brier regresses | YZ doesn't add over ATR — document and shelve |

All three outcomes are valid. We don't torture the data to make YZ work.

## Files to create

- `signal_scanner/features/yang_zhang.py` — YZ implementation + tests
- `signal_scanner/features/hurst.py` — Hurst (DFA) implementation + tests
- `research/build_yz_features.py` — DuckDB query writing yz_*.parquet
- `research/build_hurst_features.py` — DuckDB/pandas writing hurst_*.parquet
- `research/train_v3_label_1R_5d_structural.py` — training + holdout eval + report (YZ + Hurst combined)
- `tests/unit/test_yang_zhang.py` — synthetic-data correctness tests
- `tests/unit/test_hurst.py` — synthetic FBM (known H) correctness tests
- `docs/structural_features_eval_2026-04-28.md` — final report with all metrics

## SHAP attribution check

Critical for the bundled experiment: SHAP must rank the 7 new features
so we can isolate YZ contribution from Hurst contribution. If only one
pack pulls weight, we know which to keep.

## Effort estimate

- 2-3 hours wall clock per original plan
- Mostly compute-bound (training takes ~50 min)
- Could run in parallel with other work
