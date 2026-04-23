"""Train Flow Predictor v1 — LightGBM model for 2-3 day stock movement prediction.

Walk-forward validation:
  Train: 2019-2023
  Validate: 2024
  Test: 2025

Target: We train to predict +3% in 3 days, then analyze precision at various
probability thresholds to find the "sweet spot" where hit rate approaches 70-80%.
"""
import pickle, time, os
import numpy as np
import pandas as pd
from sklearn.metrics import (
    roc_auc_score, precision_recall_curve, classification_report,
    precision_score, recall_score
)
import lightgbm as lgb

t0 = time.time()

# ---------------------------------------------------------------
# 1. Load feature matrix
# ---------------------------------------------------------------
print("Loading feature matrix...")
df = pd.read_parquet("data/ml_training/flow_predictor_features.parquet")
print(f"  {len(df):,} rows, {df.shape[1]} columns")

# Feature columns (everything except labels, identifiers)
id_cols = ["ticker", "trade_date", "close", "report_quarter"]
label_cols = ["ret_3d", "ret_5d", "label_3pct_3d", "label_2pct_3d", "label_up_3d"]
feature_cols = [c for c in df.columns if c not in id_cols + label_cols]
print(f"  {len(feature_cols)} features: {feature_cols}")

# Drop rows with NaN in features
before = len(df)
df = df.dropna(subset=feature_cols)
print(f"  Dropped {before - len(df):,} NaN rows, {len(df):,} remain")

# ---------------------------------------------------------------
# 2. Walk-forward splits
# ---------------------------------------------------------------
df["year"] = pd.to_datetime(df["trade_date"]).dt.year

train = df[df["year"] <= 2023]
val = df[df["year"] == 2024]
test = df[df["year"] == 2025]

print(f"\n  Train: {len(train):,} rows (2019-2023), pos rate: {train['label_3pct_3d'].mean()*100:.1f}%")
print(f"  Val:   {len(val):,} rows (2024), pos rate: {val['label_3pct_3d'].mean()*100:.1f}%")
print(f"  Test:  {len(test):,} rows (2025), pos rate: {test['label_3pct_3d'].mean()*100:.1f}%")

X_train, y_train = train[feature_cols], train["label_3pct_3d"]
X_val, y_val = val[feature_cols], val["label_3pct_3d"]
X_test, y_test = test[feature_cols], test["label_3pct_3d"]

# ---------------------------------------------------------------
# 3. Train LightGBM
# ---------------------------------------------------------------
print("\nTraining LightGBM...")

# Class imbalance: ~20% positive. Use scale_pos_weight.
scale = (1 - y_train.mean()) / y_train.mean()

params = {
    "objective": "binary",
    "metric": "auc",
    "boosting_type": "gbdt",
    "n_estimators": 2000,
    "learning_rate": 0.02,
    "max_depth": 6,
    "num_leaves": 40,
    "min_child_samples": 100,
    "subsample": 0.8,
    "colsample_bytree": 0.8,
    "reg_alpha": 0.1,
    "reg_lambda": 1.0,
    "scale_pos_weight": scale,
    "verbose": -1,
    "n_jobs": -1,
    "random_state": 42,
}

model = lgb.LGBMClassifier(**params)

model.fit(
    X_train, y_train,
    eval_set=[(X_val, y_val)],
    callbacks=[
        lgb.early_stopping(50, verbose=True),
        lgb.log_evaluation(100),
    ],
)

# ---------------------------------------------------------------
# 4. Evaluate
# ---------------------------------------------------------------
print("\n=== VALIDATION (2024) ===")
val_probs = model.predict_proba(X_val)[:, 1]
val_auc = roc_auc_score(y_val, val_probs)
print(f"  AUC: {val_auc:.4f}")

print("\n=== TEST (2025) ===")
test_probs = model.predict_proba(X_test)[:, 1]
test_auc = roc_auc_score(y_test, test_probs)
print(f"  AUC: {test_auc:.4f}")

# ---------------------------------------------------------------
# 5. Precision at various thresholds (THE KEY ANALYSIS)
# ---------------------------------------------------------------
print("\n=== PRECISION AT PROBABILITY THRESHOLDS (Test 2025) ===")
print(f"{'Threshold':>10} {'Precision':>10} {'Recall':>8} {'N signals':>10} {'Avg 3d ret':>12}")
print("-" * 55)

test_df = test.copy()
test_df["prob"] = test_probs

for thresh in [0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.90]:
    mask = test_df["prob"] >= thresh
    n = mask.sum()
    if n == 0:
        print(f"{thresh:>10.2f} {'---':>10} {'---':>8} {0:>10} {'---':>12}")
        continue
    prec = test_df.loc[mask, "label_3pct_3d"].mean()
    recall = test_df.loc[mask, "label_3pct_3d"].sum() / test_df["label_3pct_3d"].sum()
    avg_ret = test_df.loc[mask, "ret_3d"].mean() * 100
    print(f"{thresh:>10.2f} {prec*100:>9.1f}% {recall*100:>7.1f}% {n:>10,} {avg_ret:>11.3f}%")

# Also check: what's precision for "stock goes up at all"?
print("\n=== DIRECTION ACCURACY (stock up in 3d) at thresholds ===")
print(f"{'Threshold':>10} {'Up Rate':>10} {'N signals':>10} {'Avg 3d ret':>12}")
print("-" * 45)

for thresh in [0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.90]:
    mask = test_df["prob"] >= thresh
    n = mask.sum()
    if n == 0:
        continue
    up_rate = test_df.loc[mask, "label_up_3d"].mean()
    avg_ret = test_df.loc[mask, "ret_3d"].mean() * 100
    print(f"{thresh:>10.2f} {up_rate*100:>9.1f}% {n:>10,} {avg_ret:>11.3f}%")

# ---------------------------------------------------------------
# 6. Feature importance
# ---------------------------------------------------------------
print("\n=== TOP 15 FEATURES ===")
importance = pd.Series(model.feature_importances_, index=feature_cols)
importance = importance.sort_values(ascending=False)
for feat, imp in importance.head(15).items():
    print(f"  {feat:<25} {imp:>6}")

# ---------------------------------------------------------------
# 7. Analyze the TOP signals (high probability) in more detail
# ---------------------------------------------------------------
print("\n=== TOP SIGNAL DEEP DIVE (prob >= 0.70, Test 2025) ===")
top = test_df[test_df["prob"] >= 0.70].copy()
if len(top) > 0:
    print(f"  N = {len(top):,}")
    print(f"  +3% hit rate: {top['label_3pct_3d'].mean()*100:.1f}%")
    print(f"  +2% hit rate: {top['label_2pct_3d'].mean()*100:.1f}%")
    print(f"  Up rate: {top['label_up_3d'].mean()*100:.1f}%")
    print(f"  Avg 3d return: {top['ret_3d'].mean()*100:.3f}%")
    print(f"  Avg 5d return: {top['ret_5d'].mean()*100:.3f}%")
    print(f"  Median 3d return: {top['ret_3d'].median()*100:.3f}%")

    # By month
    top["month"] = pd.to_datetime(top["trade_date"]).dt.month
    print("\n  By Month:")
    for m, g in top.groupby("month"):
        print(f"    Month {m:>2}: N={len(g):>5}, +3% rate={g['label_3pct_3d'].mean()*100:.1f}%, "
              f"avg ret={g['ret_3d'].mean()*100:.3f}%")

    # Top tickers
    print("\n  Top tickers (most signals):")
    tc = top.groupby("ticker").agg(
        n=("label_3pct_3d", "count"),
        hit_rate=("label_3pct_3d", "mean"),
        avg_ret=("ret_3d", "mean")
    ).sort_values("n", ascending=False).head(15)
    for tk, row in tc.iterrows():
        print(f"    {tk:<8} N={row['n']:>4}, +3% rate={row['hit_rate']*100:.1f}%, avg ret={row['avg_ret']*100:.3f}%")
else:
    print("  No signals at prob >= 0.70")

# ---------------------------------------------------------------
# 8. Save model
# ---------------------------------------------------------------
os.makedirs("data/models", exist_ok=True)
model_data = {
    "model": model,
    "feature_cols": feature_cols,
    "metrics": {
        "val_auc": val_auc,
        "test_auc": test_auc,
        "train_samples": len(train),
        "val_samples": len(val),
        "test_samples": len(test),
    },
    "version": "flow_predictor_v1",
}
with open("data/models/flow_predictor_v1.pkl", "wb") as f:
    pickle.dump(model_data, f)
print(f"\nModel saved: data/models/flow_predictor_v1.pkl")
print(f"Total time: {time.time()-t0:.1f}s")
