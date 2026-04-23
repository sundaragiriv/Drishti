"""Train Flow Predictor V2 — multiple targets, enriched features.

Walk-forward:
  Train: 2019-2023 (1.05M rows)
  Validate: 2024 (480K rows)
  Test: 2025 (210K rows)

Trains 3 models:
  1. label_3pct_3d: +3% in 3 days (aggressive)
  2. label_rr_2to1: +2% up before -1% down in 5 days (tradeable)
  3. label_up_3d: stock goes up in 3 days (direction)

Then creates an ENSEMBLE score and analyzes precision at thresholds.
"""
import pickle, time, os
import numpy as np
import pandas as pd
from sklearn.metrics import roc_auc_score
import lightgbm as lgb

t0 = time.time()

# ---------------------------------------------------------------
# 1. Load
# ---------------------------------------------------------------
print("Loading V2 feature matrix...")
df = pd.read_parquet("data/ml_training/flow_predictor_features_v2.parquet")
print(f"  {len(df):,} rows, {df.shape[1]} columns")

id_cols = ["ticker", "trade_date", "close", "report_quarter"]
label_cols = ["ret_3d", "ret_5d", "label_3pct_3d", "label_2pct_3d", "label_up_3d",
              "label_rr_2to1", "label_rr_1_5to1", "max_up_5d", "max_down_5d"]
feature_cols = [c for c in df.columns if c not in id_cols + label_cols]
print(f"  {len(feature_cols)} features")

df = df.dropna(subset=feature_cols)
df["year"] = pd.to_datetime(df["trade_date"]).dt.year

train = df[df["year"] <= 2023]
val = df[df["year"] == 2024]
test = df[df["year"] == 2025]

print(f"\n  Train: {len(train):,} (2019-2023)")
print(f"  Val:   {len(val):,} (2024)")
print(f"  Test:  {len(test):,} (2025)")

X_train = train[feature_cols]
X_val = val[feature_cols]
X_test = test[feature_cols]

# ---------------------------------------------------------------
# 2. Train 3 models
# ---------------------------------------------------------------
targets = {
    "3pct_3d": "label_3pct_3d",
    "rr_2to1": "label_rr_2to1",
    "up_3d": "label_up_3d",
}

models = {}
val_aucs = {}
test_aucs = {}

for name, label in targets.items():
    print(f"\n{'='*60}")
    print(f"Training model: {name} (target: {label})")
    print(f"{'='*60}")

    y_tr = train[label]
    y_va = val[label]
    y_te = test[label]

    pos_rate = y_tr.mean()
    scale = (1 - pos_rate) / pos_rate

    params = {
        "objective": "binary",
        "metric": "auc",
        "boosting_type": "gbdt",
        "n_estimators": 3000,
        "learning_rate": 0.015,
        "max_depth": 7,
        "num_leaves": 50,
        "min_child_samples": 200,
        "subsample": 0.7,
        "colsample_bytree": 0.7,
        "reg_alpha": 0.5,
        "reg_lambda": 2.0,
        "scale_pos_weight": scale,
        "verbose": -1,
        "n_jobs": -1,
        "random_state": 42,
    }

    model = lgb.LGBMClassifier(**params)
    model.fit(
        X_train, y_tr,
        eval_set=[(X_val, y_va)],
        callbacks=[
            lgb.early_stopping(80, verbose=True),
            lgb.log_evaluation(200),
        ],
    )

    va_probs = model.predict_proba(X_val)[:, 1]
    te_probs = model.predict_proba(X_test)[:, 1]

    va_auc = roc_auc_score(y_va, va_probs)
    te_auc = roc_auc_score(y_te, te_probs)

    print(f"  Val AUC:  {va_auc:.4f}")
    print(f"  Test AUC: {te_auc:.4f}")

    models[name] = model
    val_aucs[name] = va_auc
    test_aucs[name] = te_auc

    # Precision analysis
    print(f"\n  Precision @ thresholds (Test 2025):")
    test_sub = test.copy()
    test_sub["prob"] = te_probs
    for thresh in [0.30, 0.35, 0.40, 0.45, 0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80]:
        mask = test_sub["prob"] >= thresh
        n = mask.sum()
        if n < 5:
            continue
        prec = test_sub.loc[mask, label].mean()
        avg_ret = test_sub.loc[mask, "ret_3d"].mean() * 100
        avg_up = test_sub.loc[mask, "max_up_5d"].mean() * 100
        avg_dn = test_sub.loc[mask, "max_down_5d"].mean() * 100
        print(f"    p>={thresh:.2f}: prec={prec*100:>5.1f}%, N={n:>6,}, "
              f"avg_3d_ret={avg_ret:>+6.3f}%, max_up_5d={avg_up:>5.2f}%, max_dn_5d={avg_dn:>5.2f}%")

# ---------------------------------------------------------------
# 3. ENSEMBLE: combine all 3 models
# ---------------------------------------------------------------
print(f"\n{'='*60}")
print("ENSEMBLE ANALYSIS (weighted average of 3 models)")
print(f"{'='*60}")

test_df = test.copy()
# Weight: rr_2to1 and 3pct_3d more, direction less
test_df["prob_3pct"] = models["3pct_3d"].predict_proba(X_test)[:, 1]
test_df["prob_rr"] = models["rr_2to1"].predict_proba(X_test)[:, 1]
test_df["prob_up"] = models["up_3d"].predict_proba(X_test)[:, 1]

# Ensemble: 40% RR + 40% 3pct + 20% direction
test_df["ensemble"] = 0.40 * test_df["prob_rr"] + 0.40 * test_df["prob_3pct"] + 0.20 * test_df["prob_up"]

# Percentile-based thresholds (more robust than absolute)
print("\n  Percentile-based analysis:")
for pctl in [99, 98, 97, 95, 93, 90, 85, 80, 75]:
    thresh = np.percentile(test_df["ensemble"], pctl)
    mask = test_df["ensemble"] >= thresh
    n = mask.sum()
    if n < 5:
        continue
    hit_3pct = test_df.loc[mask, "label_3pct_3d"].mean()
    hit_rr = test_df.loc[mask, "label_rr_2to1"].mean()
    hit_up = test_df.loc[mask, "label_up_3d"].mean()
    avg_ret = test_df.loc[mask, "ret_3d"].mean() * 100
    avg_5d = test_df.loc[mask, "ret_5d"].mean() * 100
    avg_max_up = test_df.loc[mask, "max_up_5d"].mean() * 100
    avg_max_dn = test_df.loc[mask, "max_down_5d"].mean() * 100
    print(f"    Top {100-pctl}% (>={thresh:.3f}): N={n:>5,}  "
          f"+3%={hit_3pct*100:>5.1f}%  RR2:1={hit_rr*100:>5.1f}%  "
          f"Up={hit_up*100:>5.1f}%  avg3d={avg_ret:>+6.3f}%  avg5d={avg_5d:>+6.3f}%  "
          f"maxUp={avg_max_up:>5.2f}%  maxDn={avg_max_dn:>5.2f}%")

# ---------------------------------------------------------------
# 4. Feature importance (from RR model — most tradeable)
# ---------------------------------------------------------------
print(f"\n{'='*60}")
print("TOP 20 FEATURES (from RR 2:1 model)")
print(f"{'='*60}")
importance = pd.Series(models["rr_2to1"].feature_importances_, index=feature_cols)
importance = importance.sort_values(ascending=False)
for feat, imp in importance.head(20).items():
    print(f"  {feat:<30} {imp:>6}")

# ---------------------------------------------------------------
# 5. Deep dive: top 1% signals
# ---------------------------------------------------------------
print(f"\n{'='*60}")
print("TOP 1% SIGNALS DEEP DIVE")
print(f"{'='*60}")
thresh_99 = np.percentile(test_df["ensemble"], 99)
top1 = test_df[test_df["ensemble"] >= thresh_99].copy()
if len(top1) > 20:
    print(f"\n  N = {len(top1)}")
    print(f"  +3% in 3d hit rate:  {top1['label_3pct_3d'].mean()*100:.1f}%")
    print(f"  +2% in 3d hit rate:  {top1['label_2pct_3d'].mean()*100:.1f}%")
    print(f"  RR 2:1 hit rate:     {top1['label_rr_2to1'].mean()*100:.1f}%")
    print(f"  Direction (up 3d):   {top1['label_up_3d'].mean()*100:.1f}%")
    print(f"  Avg 3d return:       {top1['ret_3d'].mean()*100:.3f}%")
    print(f"  Avg 5d return:       {top1['ret_5d'].mean()*100:.3f}%")
    print(f"  Avg max up 5d:       {top1['max_up_5d'].mean()*100:.2f}%")
    print(f"  Avg max down 5d:     {top1['max_down_5d'].mean()*100:.2f}%")

    # Monthly consistency
    top1["month"] = pd.to_datetime(top1["trade_date"]).dt.to_period("M")
    print("\n  Monthly breakdown:")
    for m, g in top1.groupby("month"):
        print(f"    {m}: N={len(g):>4}, +3%={g['label_3pct_3d'].mean()*100:>5.1f}%, "
              f"RR2:1={g['label_rr_2to1'].mean()*100:>5.1f}%, "
              f"up={g['label_up_3d'].mean()*100:>5.1f}%, "
              f"avg_ret={g['ret_3d'].mean()*100:>+6.3f}%")

# ---------------------------------------------------------------
# 6. Save all models
# ---------------------------------------------------------------
os.makedirs("data/models", exist_ok=True)
model_data = {
    "models": {k: v for k, v in models.items()},
    "feature_cols": feature_cols,
    "metrics": {
        "val_aucs": val_aucs,
        "test_aucs": test_aucs,
        "ensemble_weights": {"rr_2to1": 0.40, "3pct_3d": 0.40, "up_3d": 0.20},
    },
    "version": "flow_predictor_v2",
}
with open("data/models/flow_predictor_v2.pkl", "wb") as f:
    pickle.dump(model_data, f)

print(f"\nModels saved: data/models/flow_predictor_v2.pkl")
print(f"Total time: {time.time()-t0:.1f}s")
