"""Honest AUC for flow_predictor_v3 — purged K-Fold vs naive temporal split.

Loads v2 features, runs the same model architecture as
research/train_flow_predictor_v3.py, and reports BOTH:
  - naive temporal AUC (train ≤ 2023, val 2024, test 2025) — the original
  - purged 5-fold CV AUC with embargo — the honest one

The DELTA is the leakage that naive CV was hiding.

Run:
    python -m research.eval_v3_purged
"""
import os
import time

import lightgbm as lgb
import numpy as np
import pandas as pd
from sklearn.metrics import roc_auc_score

from signal_scanner.intelligence.purged_cv import PurgedKFold

t0 = time.time()

# ── Same data + features as v3 ──────────────────────────────────────────
print("Loading V2 features...")
df = pd.read_parquet("data/ml_training/flow_predictor_features_v2.parquet")
print(f"  {len(df):,} rows")

id_cols = ["ticker", "trade_date", "close", "report_quarter"]
label_cols = ["ret_3d", "ret_5d", "label_3pct_3d", "label_2pct_3d", "label_up_3d",
              "label_rr_2to1", "label_rr_1_5to1", "max_up_5d", "max_down_5d"]
spy_features = [c for c in df.columns if c.startswith("spy_")]
feature_cols_no_spy = [c for c in df.columns if c not in id_cols + label_cols + spy_features]

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
    "near_52w_high", "near_20d_low", "vol_spike",
]
all_features = feature_cols_no_spy + interaction_features
df = df.dropna(subset=all_features)
df["year"] = pd.to_datetime(df["trade_date"]).dt.year
print(f"  {len(df):,} rows after dropna; {len(all_features)} features")


# Two label targets to evaluate
LABELS = ["label_3pct_3d", "label_rr_2to1"]
HORIZON_DAYS = {"label_3pct_3d": 3, "label_rr_2to1": 5}


def _params(scale: float) -> dict:
    return {
        "objective": "binary",
        "metric": "auc",
        "boosting_type": "gbdt",
        "n_estimators": 2500,    # halved from 5000 to keep CV runtime reasonable
        "learning_rate": 0.02,   # bumped from 0.01 to match earlier iteration count
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


# ── 1. Naive temporal split (the v3 original) ───────────────────────────
print("\n" + "=" * 60)
print("NAIVE TEMPORAL: train<=2023 / val=2024 / test=2025")
print("=" * 60)

naive_results: dict[str, dict] = {}
train = df[df["year"] <= 2023]
val = df[df["year"] == 2024]
test = df[df["year"] == 2025]
print(f"  train={len(train):,}  val={len(val):,}  test={len(test):,}")

for label in LABELS:
    y_tr, y_va, y_te = train[label], val[label], test[label]
    if y_tr.mean() <= 0 or y_tr.mean() >= 1:
        print(f"  [{label}] degenerate label distribution; skipping")
        continue
    scale = (1 - y_tr.mean()) / y_tr.mean()
    model = lgb.LGBMClassifier(**_params(scale))
    model.fit(train[all_features], y_tr,
              eval_set=[(val[all_features], y_va)],
              callbacks=[lgb.early_stopping(80, verbose=False)])
    auc_va = roc_auc_score(y_va, model.predict_proba(val[all_features])[:, 1])
    auc_te = roc_auc_score(y_te, model.predict_proba(test[all_features])[:, 1])
    naive_results[label] = {"val_auc": auc_va, "test_auc": auc_te}
    print(f"  [{label}] val AUC = {auc_va:.4f}   test AUC = {auc_te:.4f}")


# ── 2. Purged 5-fold CV with 5-day embargo ─────────────────────────────
print("\n" + "=" * 60)
print("PURGED 5-FOLD CV (embargo = label horizon)")
print("=" * 60)

purged_results: dict[str, dict] = {}
df_sorted = df.sort_values("trade_date").reset_index(drop=True)

for label in LABELS:
    horizon = HORIZON_DAYS[label]
    cv = PurgedKFold(n_splits=5, label_horizon_days=horizon, embargo_days=horizon)
    fold_aucs = []
    fold_n = []
    print(f"\n  --- {label}  (horizon={horizon}d, embargo={horizon}d) ---")
    for fold_i, (tr_idx, te_idx) in enumerate(cv.split(df_sorted["trade_date"]), 1):
        if len(tr_idx) < 1000 or len(te_idx) < 100:
            print(f"    fold {fold_i}: too few samples (train={len(tr_idx)}, test={len(te_idx)}); skip")
            continue
        y_tr = df_sorted[label].iloc[tr_idx]
        y_te = df_sorted[label].iloc[te_idx]
        if y_tr.mean() <= 0 or y_tr.mean() >= 1 or y_te.mean() <= 0 or y_te.mean() >= 1:
            print(f"    fold {fold_i}: degenerate label split; skip")
            continue
        scale = (1 - y_tr.mean()) / y_tr.mean()
        model = lgb.LGBMClassifier(**_params(scale))
        model.fit(df_sorted[all_features].iloc[tr_idx], y_tr,
                  callbacks=[lgb.log_evaluation(0)])  # silent
        proba = model.predict_proba(df_sorted[all_features].iloc[te_idx])[:, 1]
        auc = roc_auc_score(y_te, proba)
        fold_aucs.append(auc)
        fold_n.append(len(te_idx))
        print(f"    fold {fold_i}: train={len(tr_idx):>7,}  test={len(te_idx):>6,}  AUC={auc:.4f}")
    if fold_aucs:
        mean_auc = float(np.mean(fold_aucs))
        std_auc = float(np.std(fold_aucs))
        weighted_auc = float(np.average(fold_aucs, weights=fold_n))
        purged_results[label] = {
            "mean_auc": mean_auc,
            "std_auc": std_auc,
            "weighted_auc": weighted_auc,
            "fold_aucs": fold_aucs,
        }
        print(f"  [{label}] mean AUC = {mean_auc:.4f}  +/-{std_auc:.4f}  "
              f"(N-weighted = {weighted_auc:.4f})")


# ── 3. Delta report ─────────────────────────────────────────────────────
print("\n" + "=" * 60)
print("HONESTY DELTA")
print("=" * 60)
print(f"{'Label':<20} {'Naive val':>10} {'Naive test':>11} {'Purged mean':>12} {'D vs val':>10} {'D vs test':>10}")
print("-" * 75)
for label in LABELS:
    n = naive_results.get(label)
    p = purged_results.get(label)
    if n is None or p is None:
        continue
    d_val = p["mean_auc"] - n["val_auc"]
    d_test = p["mean_auc"] - n["test_auc"]
    print(f"{label:<20} {n['val_auc']:>10.4f} {n['test_auc']:>11.4f} {p['mean_auc']:>10.4f} "
          f"{d_val:>+10.4f} {d_test:>+10.4f}")

print(f"\nTotal time: {time.time() - t0:.1f}s")
