# Failed Research Track

## Predictive Intelligence v1

**Status**: Failed validation. Correctly blocked from production.

### What Was Built (Complete)
- Label pipeline: 2.4M rows, 3d/5d forward returns + SPY alpha
- Feature join engine: 65 features, point-in-time safe with settled-quarter mapping
- Interconnected stocks features: 10 peer/sector features, 208K rows
- LightGBM quantile regression + direction classifier
- Platt calibration
- Validation gate with 5 metrics

### Validation Results
| Metric | Threshold | Actual | Status |
|--------|-----------|--------|--------|
| Direction accuracy | > 55% | 48.1% | FAIL |
| ECE | < 0.05 | 0.075 | FAIL |
| IC (Spearman) | > 0.05 | 0.005 | FAIL |
| Top-decile Sharpe | > 1.5 | 0.37 | FAIL |
| Discrimination | 20-80% | 100% positive | FAIL |

### Root Causes
1. Single regime training (Oct 2023 - Jul 2024 = pure bull market)
2. No discrimination (100% positive predictions)
3. Feature sparsity (34% missing interconnected, 34% missing institutional)
4. Short training period (~190 trading days)

### Rules
- Do not ship to UI
- Do not score daily
- `predictive_fwd_v1.pkl` is research-only

---

## Predictive Intelligence v2 — Research Plan

### Core Concept: Sniper Filter, Not Broad Prediction

v2 is NOT trying to predict direction for 3,000 stocks at 80% accuracy.
That is not achievable on 3-5 day stock returns.

v2 IS trying to find 1-5 stocks per day where everything aligns,
and be right 75% of the time on those few.

This is the same pattern that works for VWAP_MR P99 (72% on 321 trades
out of 36,000 evaluated). The model scores everything broadly, then an

```
Broad universe (3,000 stocks)
  → ML scores all (base model, 55-58% accuracy)
    → P95+ confidence filter (top 5%)
      → Institutional thesis alignment (conviction >= 65, accum phase)
        → Fresh catalyst gate (insider buy, squeeze change, 8-K)
          → Regime gate (not CRASH/DISTRIBUTION)
            → 1-5 names fire per day
              → Target: 75% hit rate on THESE picks only
```

Accept 0 picks on most days. Never force a mediocre pick.

### v2 Design

#### Data: 10-Year Panel
- Expand training to 2016-2026 (covers COVID crash, 2022 bear, rate hikes, recovery)
- ~5M+ rows (10 years × 2,000+ tickers × 252 days)
- Fixes the single-regime failure of v1

Source: `fact_daily_prices` already has 12.5M rows back to 2016.
Requires: extending `fact_swing_features` backward from Oct 2023 to 2016.

#### New Features (Beyond v1's 65)

**Options surface features (from Massive Options Starter backfill):**
- IV skew (OTM put IV - OTM call IV) — steepening precedes corrections
- Put/call ratio (daily OI-based, not just volume)
- IV term structure (near-term vs far-term IV spread)
- OI concentration at key strikes (wall detection)

**Sector-residual returns:**
- `stock_return - sector_etf_return` instead of raw returns
- Isolates true stock-level alpha from sector tide
- Compute from `fact_daily_prices` + `dim_issuer.sector`

**Fundamental decay (from Massive financial ratios):**
- P/E change over last 3 quarters
- EV/EBITDA trend direction
- Valuation expansion vs contraction heading into 5-day window

**Existing features (retained from v1):**
- 42 daily technicals (price, momentum, volatility, volume, trend, candle)
- 13 institutional features (conviction, phase, squeeze, pressure — point-in-time safe)
- 10 interconnected features (peer momentum, sector breadth, cluster confirmation)

**Total target: ~85-90 features**

#### Model Architecture

**Base model: LightGBM direction classifier**
- Trained on full 10-year panel
- Target: `fwd_direction` (5-day return > 0)
- Expected base accuracy: 55-58% across full universe

**Confidence scoring:**
- Calibrated probability output (Platt scaling)
- Quantile regression for expected return magnitude
- Only surface names where `calibrated_prob >= 0.70`

**Sniper gate (post-model):**
```python
def is_golden_pick(ticker, ml_prob, intel, regime, catalysts):
    if ml_prob < 0.70:        return False  # model not confident enough
    if intel.conv < 65:       return False  # no institutional thesis
    if intel.phase not in ACCUM_PHASES: return False  # not accumulating
    if regime in (CRASH, DISTRIBUTION): return False  # wrong regime
    if not has_fresh_catalyst(ticker, 5d): return False  # no recent confirmation
    return True  # all stars aligned
```

Each gate independently reduces false positives. Five gates stacked
should push conditional accuracy from 55% (base) toward 70-75%.

#### Critical Pipeline Fix: Purged Cross-Validation

v1 had overlapping 5-day labels (Day 1 and Day 2 share 4 days of returns).
This causes the model to "cheat" during validation.

v2 must implement **purged k-fold cross-validation**:
- 5-day purge gap between train and validation sets
- Embargo period after each fold
- No shared label days between train and test

#### Validation Gate (Unchanged Thresholds for Base Model)

| Metric | Threshold |
|--------|-----------|
| Base direction accuracy (full universe) | > 55% |
| ECE (calibration) | < 0.05 |
| IC (rank correlation) | > 0.05 |
| Top-decile Sharpe | > 1.5 |
| Discrimination | 20-80% positive |

**Additional v2 metric: Sniper Hit Rate**
| Metric | Threshold |
|--------|-----------|
| Golden pick hit rate (filtered output) | > 70% |
| Average picks per day | 0-5 (accept zero) |
| Golden pick sample size | > 50 over test period |

#### SHAP Analysis (Required Before UI)

Before shipping, verify with SHAP values:
- Model is actually using options/institutional/catalyst features
- Not just chasing momentum (which breaks on trend reversal)
- Feature importance distribution is diversified, not concentrated

### v2 Build Order

1. Backfill `fact_swing_features` to 2016 (compute job from existing prices)
2. Ingest Massive fundamental ratios (P/E, EV/EBITDA quarterly history)
3. Backfill daily options OI/IV history from Massive Options Starter
4. Compute new features: IV skew, sector-residual returns, fundamental decay
5. Implement purged cross-validation
6. Train base model on 10-year panel
7. Run validation gate on base model
8. If base passes: implement sniper gate + track golden pick hit rate
9. SHAP analysis
10. If golden pick hit rate > 70% on 50+ samples: ship to Predictive Intelligence UI

### What This Does NOT Promise

- 80% accuracy on general stock prediction (not achievable)
- Daily picks every day (most days will have 0 picks)
- Replacement for Swing Snipers (this is complementary)
- Working model on first attempt (research may require multiple iterations)

### Dependencies

| Dependency | Status |
|-----------|--------|
| 10-year daily prices | Ready (fact_daily_prices) |
| Swing features backfill to 2016 | Needs compute job |
| Massive fundamental ratios | Not ingested |
| Options OI/IV history backfill | Not done (1 day exists) |
| Purged CV implementation | Not built |
| Sniper gate logic | Exists conceptually (Triple Lock pattern) |
| Validation gate | Built and working |
| SHAP integration | Not built (LightGBM supports it natively) |

### Do Not Start Until

- Feature backfill is complete (at least prices + swing features to 2016)
- At least 30 days of options history exists for IV skew computation
- Research hypothesis is approved
- No production promises before golden pick validation passes
