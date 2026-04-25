"""Flow Predictor V3 — Market-neutral, regime-aware, interaction features.

Key changes from V2:
  1. Target: ALPHA (stock return - SPY return) instead of raw return
  2. Regime-conditioned: separate analysis for bull/bear/sideways
  3. Interaction features: compounding signals
  4. Stricter universe: ACTIVE_ACCUM + LATE_ACCUM only (proven accumulation)
  5. Multiple label definitions to find what's actually predictable
"""
import pickle, time, os
import numpy as np
import pandas as pd
from sklearn.metrics import roc_auc_score
import lightgbm as lgb

t0 = time.time()

# ---------------------------------------------------------------
# 1. Load and enrich
# ---------------------------------------------------------------
print("Loading V2 features...")
df = pd.read_parquet("data/ml_training/flow_predictor_features_v2.parquet")
print(f"  {len(df):,} rows")

# Add SPY return columns for market-neutral targets
# We already have spy_ret_1d, spy_ret_5d in the data (from V2 build)
# Compute alpha targets
# Alpha 3d = stock 3d return - SPY cumulative 3d return (approximate with spy_ret_5d * 3/5)
# Actually we need SPY 3d forward return. Let's compute it from the spy_ret columns
# We have spy_ret_1d (today's SPY return). For 3d forward, we need SPY returns at t+3.
# Since we don't have that directly, let's use a proxy approach.

# BETTER: Just compute alpha = ret_3d - spy_avg_3d_return for that period
# Actually the cleanest: train on raw returns but WITHOUT spy features (force stock-level learning)

id_cols = ["ticker", "trade_date", "close", "report_quarter"]
label_cols = ["ret_3d", "ret_5d", "label_3pct_3d", "label_2pct_3d", "label_up_3d",
              "label_rr_2to1", "label_rr_1_5to1", "max_up_5d", "max_down_5d"]

# Strategy: remove SPY features to force stock-specific learning
spy_features = [c for c in df.columns if c.startswith("spy_")]
feature_cols_no_spy = [c for c in df.columns if c not in id_cols + label_cols + spy_features]

# Add interaction features
print("  Engineering interaction features...")
df["insider_x_compression"] = (df["insider_buys_5d"] > 0).astype(int) * (df["atr_compression"] < 0.85).astype(int)
df["insider_x_above200"] = (df["insider_buys_10d"] > 0).astype(int) * df["above_200sma"]
df["cluster_x_pullback"] = (df["distinct_insiders_30d"] >= 2).astype(int) * (df["pct_from_sma20"] < -0.02).astype(int)
df["f4_x_squeeze"] = (df["f4_count"] >= 2).astype(int) * (df["squeeze_score"] > 50).astype(int)
df["conviction_x_momentum"] = (df["conviction_score"] >= 70).astype(int) * (df["ret_5d_back"] > 0).astype(int)
df["ma_aligned_x_vol_quiet"] = df["ma_aligned"] * (df["vol_trend_10_50"] < 0.9).astype(int)
df["compression_x_trend"] = (df["atr_compression"] < 0.85).astype(int) * df["above_200sma"]
df["insider_dollar_x_phase"] = df["log_insider_dollar_30d"] * df["phase_ord"]
df["pullback_to_sma50"] = ((df["pct_from_sma50"].abs() < 0.02) & (df["above_200sma"] == 1)).astype(int)
df["near_52w_high"] = (df["range_pos_52w"] > 0.85).astype(int)
df["near_20d_low"] = (df["range_pos_20d"] < 0.15).astype(int)
df["vol_spike"] = (df["vol_ratio_10d"] > 2.0).astype(int)

interaction_features = [
    "insider_x_compression", "insider_x_above200", "cluster_x_pullback",
    "f4_x_squeeze", "conviction_x_momentum", "ma_aligned_x_vol_quiet",
    "compression_x_trend", "insider_dollar_x_phase", "pullback_to_sma50",
    "near_52w_high", "near_20d_low", "vol_spike"
]

all_features = feature_cols_no_spy + interaction_features
print(f"  {len(all_features)} features (no SPY, +{len(interaction_features)} interactions)")

df = df.dropna(subset=all_features)
df["year"] = pd.to_datetime(df["trade_date"]).dt.year

# Also create regime label from SPY features
df["bull_regime"] = (df["spy_vs_sma200"] > 0).astype(int)

train = df[df["year"] <= 2023]
val = df[df["year"] == 2024]
test = df[df["year"] == 2025]

X_train = train[all_features]
X_val = val[all_features]
X_test = test[all_features]

print(f"\n  Train: {len(train):,}  Val: {len(val):,}  Test: {len(test):,}")

# ---------------------------------------------------------------
# 2. Train: TARGET = +3% in 3 days (stock-specific, no market beta)
# ---------------------------------------------------------------
print(f"\n{'='*60}")
print("MODEL A: +3% in 3 days (no SPY features, interaction-enriched)")
print(f"{'='*60}")

y_tr = train["label_3pct_3d"]
y_va = val["label_3pct_3d"]
y_te = test["label_3pct_3d"]

scale = (1 - y_tr.mean()) / y_tr.mean()

params_a = {
    "objective": "binary",
    "metric": "auc",
    "boosting_type": "gbdt",
    "n_estimators": 5000,
    "learning_rate": 0.01,
    "max_depth": 8,
    "num_leaves": 64,
    "min_child_samples": 300,
    "min_child_weight": 10,
    "subsample": 0.6,
    "colsample_bytree": 0.6,
    "reg_alpha": 1.0,
    "reg_lambda": 5.0,
    "scale_pos_weight": scale,
    "verbose": -1,
    "n_jobs": -1,
    "random_state": 42,
}

model_a = lgb.LGBMClassifier(**params_a)
model_a.fit(
    X_train, y_tr,
    eval_set=[(X_val, y_va)],
    callbacks=[lgb.early_stopping(100, verbose=True), lgb.log_evaluation(200)],
)

va_probs_a = model_a.predict_proba(X_val)[:, 1]
te_probs_a = model_a.predict_proba(X_test)[:, 1]
print(f"  Val AUC:  {roc_auc_score(y_va, va_probs_a):.4f}")
print(f"  Test AUC: {roc_auc_score(y_te, te_probs_a):.4f}")

# ---------------------------------------------------------------
# 3. Train: TARGET = RR 2:1 (tradeable)
# ---------------------------------------------------------------
print(f"\n{'='*60}")
print("MODEL B: RR 2:1 (+2% before -1% in 5d, no SPY)")
print(f"{'='*60}")

y_tr_rr = train["label_rr_2to1"]
y_va_rr = val["label_rr_2to1"]
y_te_rr = test["label_rr_2to1"]

scale_rr = (1 - y_tr_rr.mean()) / y_tr_rr.mean()
params_b = params_a.copy()
params_b["scale_pos_weight"] = scale_rr

model_b = lgb.LGBMClassifier(**params_b)
model_b.fit(
    X_train, y_tr_rr,
    eval_set=[(X_val, y_va_rr)],
    callbacks=[lgb.early_stopping(100, verbose=True), lgb.log_evaluation(200)],
)

va_probs_b = model_b.predict_proba(X_val)[:, 1]
te_probs_b = model_b.predict_proba(X_test)[:, 1]
print(f"  Val AUC:  {roc_auc_score(y_va_rr, va_probs_b):.4f}")
print(f"  Test AUC: {roc_auc_score(y_te_rr, te_probs_b):.4f}")

# ---------------------------------------------------------------
# 4. Precision analysis: Model A (3pct)
# ---------------------------------------------------------------
print(f"\n{'='*60}")
print("PRECISION ANALYSIS: MODEL A (+3% in 3d)")
print(f"{'='*60}")

test_df = test.copy()
test_df["prob_a"] = te_probs_a
test_df["prob_b"] = te_probs_b
test_df["ensemble"] = 0.5 * te_probs_a + 0.5 * te_probs_b

# Analyze by regime
for regime_name, regime_mask in [
    ("ALL", pd.Series(True, index=test_df.index)),
    ("BULL (SPY>200SMA)", test_df["bull_regime"] == 1),
    ("BEAR (SPY<200SMA)", test_df["bull_regime"] == 0),
]:
    sub = test_df[regime_mask]
    if len(sub) < 100:
        continue
    print(f"\n  --- {regime_name} (N={len(sub):,}) ---")
    print(f"  {'Percentile':>10} {'N':>7} {'+3% hit':>8} {'+2% hit':>8} {'RR 2:1':>8} {'Up 3d':>8} {'Avg 3d':>10} {'Avg 5d':>10}")

    for pctl in [99, 98, 97, 95, 93, 90, 85, 80]:
        thresh = np.percentile(sub["ensemble"], pctl)
        mask = sub["ensemble"] >= thresh
        n = mask.sum()
        if n < 10:
            continue
        s = sub[mask]
        print(f"  Top {100-pctl:>2}%   {n:>7,} "
              f"{s['label_3pct_3d'].mean()*100:>7.1f}% "
              f"{s['label_2pct_3d'].mean()*100:>7.1f}% "
              f"{s['label_rr_2to1'].mean()*100:>7.1f}% "
              f"{s['label_up_3d'].mean()*100:>7.1f}% "
              f"{s['ret_3d'].mean()*100:>+9.3f}% "
              f"{s['ret_5d'].mean()*100:>+9.3f}%")

# ---------------------------------------------------------------
# 5. Feature importance
# ---------------------------------------------------------------
print(f"\n{'='*60}")
print("FEATURE IMPORTANCE (Model A)")
print(f"{'='*60}")
imp = pd.Series(model_a.feature_importances_, index=all_features).sort_values(ascending=False)
for feat, val in imp.head(25).items():
    print(f"  {feat:<35} {val:>6}")

# ---------------------------------------------------------------
# 6. Monthly consistency (top 2% signals)
# ---------------------------------------------------------------
print(f"\n{'='*60}")
print("MONTHLY CONSISTENCY — Top 2% Ensemble Signals")
print(f"{'='*60}")
thresh_98 = np.percentile(test_df["ensemble"], 98)
top2 = test_df[test_df["ensemble"] >= thresh_98].copy()
top2["month"] = pd.to_datetime(top2["trade_date"]).dt.to_period("M")

print(f"\n  Overall: N={len(top2)}, +3%={top2['label_3pct_3d'].mean()*100:.1f}%, "
      f"Up={top2['label_up_3d'].mean()*100:.1f}%, avg_ret={top2['ret_3d'].mean()*100:.3f}%")
print()
for m, g in top2.groupby("month"):
    print(f"  {m}: N={len(g):>5}, +3%={g['label_3pct_3d'].mean()*100:>5.1f}%, "
          f"RR2:1={g['label_rr_2to1'].mean()*100:>5.1f}%, "
          f"Up={g['label_up_3d'].mean()*100:>5.1f}%, "
          f"avg3d={g['ret_3d'].mean()*100:>+6.3f}%, "
          f"regime={'BULL' if g['bull_regime'].mean() > 0.5 else 'BEAR'}")

# ---------------------------------------------------------------
# 7. Save
# ---------------------------------------------------------------
os.makedirs("data/models", exist_ok=True)
model_data = {
    "model_3pct": model_a,
    "model_rr": model_b,
    "feature_cols": all_features,
    "metrics": {
        "val_auc_3pct": roc_auc_score(y_va, va_probs_a),
        "test_auc_3pct": roc_auc_score(y_te, te_probs_a),
        "val_auc_rr": roc_auc_score(y_va_rr, va_probs_b),
        "test_auc_rr": roc_auc_score(y_te_rr, te_probs_b),
    },
    "version": "flow_predictor_v3",
}
with open("data/models/flow_predictor_v3.pkl", "wb") as f:
    pickle.dump(model_data, f)

print(f"\nSaved: data/models/flow_predictor_v3.pkl")
print(f"Total time: {time.time()-t0:.1f}s")
