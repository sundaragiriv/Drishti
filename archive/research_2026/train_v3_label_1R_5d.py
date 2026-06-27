"""F1-A: Retrain flow_predictor_v3 with label_hit_1R_5d.

Same R-frame as label_hit_1R_10d (close + 2*ATR before close - 2*ATR)
but 5-day window instead of 10-day. Hypothesis: v2 features were
engineered for 3-5 day forward returns, so a 5-day path-dependent
target should preserve more signal than the 10-day version.

If this also returns ~0.51 AUC, we know v2 features cannot predict
the path-dependent 1R/2*ATR target at any horizon and the next
research path is feature redesign (option C).

Run:
    python -m research.train_v3_label_1R_5d
"""
import os
import pickle
import time
from pathlib import Path

import duckdb
import lightgbm as lgb
import numpy as np
import pandas as pd
from sklearn.metrics import roc_auc_score

from signal_scanner.intelligence.purged_cv import PurgedKFold

t0 = time.time()
PARQUET_IN = "data/ml_training/flow_predictor_features_v2.parquet"
MODEL_OUT = "data/models/flow_predictor_v3_1R_5d.pkl"
WAREHOUSE = "data/warehouse/sec_intel.duckdb"

print("Loading v2 features...")
df = pd.read_parquet(PARQUET_IN)
print(f"  {len(df):,} rows, {df['ticker'].nunique():,} tickers, "
      f"{df['trade_date'].min()} -> {df['trade_date'].max()}")

print("\nComputing label_hit_1R_5d from DuckDB...")
conn = duckdb.connect(WAREHOUSE, read_only=True)
parquet_keys = df[["ticker", "trade_date"]].drop_duplicates()
print(f"  unique (ticker, trade_date) keys to label: {len(parquet_keys):,}")
conn.register("parquet_keys", parquet_keys)

label_df = conn.execute("""
    WITH base AS (
        SELECT p.ticker, p.trade_date, p.close,
               AVG(p.high - p.low) OVER (
                   PARTITION BY p.ticker ORDER BY p.trade_date
                   ROWS BETWEEN 19 PRECEDING AND CURRENT ROW
               ) AS atr20,
               LEAD(p.high, 1) OVER w AS h1,  LEAD(p.high, 2) OVER w AS h2,
               LEAD(p.high, 3) OVER w AS h3,  LEAD(p.high, 4) OVER w AS h4,
               LEAD(p.high, 5) OVER w AS h5,
               LEAD(p.low,  1) OVER w AS l1,  LEAD(p.low,  2) OVER w AS l2,
               LEAD(p.low,  3) OVER w AS l3,  LEAD(p.low,  4) OVER w AS l4,
               LEAD(p.low,  5) OVER w AS l5
        FROM fact_daily_prices p
        INNER JOIN parquet_keys pk
            ON p.ticker = pk.ticker AND p.trade_date = pk.trade_date
        WINDOW w AS (PARTITION BY p.ticker ORDER BY p.trade_date)
    )
    SELECT ticker, trade_date,
        CASE
            WHEN h5 IS NULL OR l5 IS NULL OR atr20 IS NULL OR atr20 <= 0
                THEN NULL
            WHEN GREATEST(h1,h2,h3,h4,h5) >= close + 2*atr20
                 AND LEAST(l1,l2,l3,l4,l5) > close - 2*atr20
                THEN 1
            ELSE 0
        END AS label_hit_1R_5d
    FROM base
""").df()
conn.close()

print(f"  Label rows: {len(label_df):,}")
hit_rate = label_df["label_hit_1R_5d"].mean()
print(f"  Overall hit rate: {hit_rate*100:.1f}%")

df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date
label_df["trade_date"] = pd.to_datetime(label_df["trade_date"]).dt.date
df = df.merge(label_df, on=["ticker", "trade_date"], how="inner")
df = df.dropna(subset=["label_hit_1R_5d"])
df["label_hit_1R_5d"] = df["label_hit_1R_5d"].astype(int)
print(f"  After merge + dropna: {len(df):,} rows")

# Same feature engineering as v3
print("\nEngineering features (same as v3)...")
id_cols = ["ticker", "trade_date", "close", "report_quarter"]
label_cols = ["ret_3d", "ret_5d", "label_3pct_3d", "label_2pct_3d", "label_up_3d",
              "label_rr_2to1", "label_rr_1_5to1", "max_up_5d", "max_down_5d",
              "label_hit_1R_5d"]
spy_features = [c for c in df.columns if c.startswith("spy_")]
feature_cols_no_spy = [c for c in df.columns
                      if c not in id_cols + label_cols + spy_features]

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
print(f"  {len(df):,} rows after feature dropna; {len(all_features)} features")

df = df.sort_values("trade_date").reset_index(drop=True)


def _params(scale: float) -> dict:
    return {
        "objective": "binary",
        "metric": "auc",
        "boosting_type": "gbdt",
        "n_estimators": 2500,
        "learning_rate": 0.02,
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


print("\n" + "=" * 60)
print("NAIVE TEMPORAL: train<=2023 / val=2024 / test>=2025")
print("=" * 60)
df["year"] = pd.to_datetime(df["trade_date"]).dt.year
train = df[df["year"] <= 2023]
val = df[df["year"] == 2024]
test = df[df["year"] >= 2025]
y_tr, y_va, y_te = train["label_hit_1R_5d"], val["label_hit_1R_5d"], test["label_hit_1R_5d"]
print(f"  train={len(train):,}  val={len(val):,}  test={len(test):,}")
print(f"  hit rate: train={y_tr.mean()*100:.1f}%  val={y_va.mean()*100:.1f}%  test={y_te.mean()*100:.1f}%")

if y_tr.mean() <= 0 or y_tr.mean() >= 1:
    raise SystemExit("Degenerate label distribution — abort.")

scale = (1 - y_tr.mean()) / y_tr.mean()
naive_model = lgb.LGBMClassifier(**_params(scale))
naive_model.fit(
    train[all_features], y_tr,
    eval_set=[(val[all_features], y_va)],
    callbacks=[lgb.early_stopping(80, verbose=False)],
)
naive_val_auc = roc_auc_score(y_va, naive_model.predict_proba(val[all_features])[:, 1])
naive_test_auc = roc_auc_score(y_te, naive_model.predict_proba(test[all_features])[:, 1])
print(f"  naive val AUC  = {naive_val_auc:.4f}")
print(f"  naive test AUC = {naive_test_auc:.4f}")

print("\n" + "=" * 60)
print("PURGED 5-FOLD CV (horizon=5d, embargo=5d)")
print("=" * 60)
cv = PurgedKFold(n_splits=5, label_horizon_days=5, embargo_days=5)
fold_aucs = []
fold_n = []
for fold_i, (tr_idx, te_idx) in enumerate(cv.split(df["trade_date"]), 1):
    if len(tr_idx) < 1000 or len(te_idx) < 100:
        print(f"  fold {fold_i}: too few samples; skip")
        continue
    y_tr_f = df["label_hit_1R_5d"].iloc[tr_idx]
    y_te_f = df["label_hit_1R_5d"].iloc[te_idx]
    if y_tr_f.mean() <= 0 or y_tr_f.mean() >= 1:
        continue
    scale_f = (1 - y_tr_f.mean()) / y_tr_f.mean()
    m = lgb.LGBMClassifier(**_params(scale_f))
    m.fit(df[all_features].iloc[tr_idx], y_tr_f, callbacks=[lgb.log_evaluation(0)])
    proba = m.predict_proba(df[all_features].iloc[te_idx])[:, 1]
    auc = roc_auc_score(y_te_f, proba)
    fold_aucs.append(auc)
    fold_n.append(len(te_idx))
    print(f"  fold {fold_i}: train={len(tr_idx):>7,}  test={len(te_idx):>6,}  AUC={auc:.4f}")

if fold_aucs:
    mean_auc = float(np.mean(fold_aucs))
    std_auc = float(np.std(fold_aucs))
    weighted_auc = float(np.average(fold_aucs, weights=fold_n))
    print(f"\n  mean AUC = {mean_auc:.4f}  +/-{std_auc:.4f}  (N-weighted = {weighted_auc:.4f})")
else:
    mean_auc = std_auc = weighted_auc = float("nan")

print("\nSaving naive-trained model + meta...")
Path("data/models").mkdir(parents=True, exist_ok=True)
model_data = {
    "model": naive_model,
    "feature_cols": all_features,
    "label": "label_hit_1R_5d",
    "label_definition": (
        "high reaches close + 2*atr20 BEFORE low touches close - 2*atr20 "
        "within next 5 trading days"
    ),
    "metrics": {
        "naive_val_auc": naive_val_auc,
        "naive_test_auc": naive_test_auc,
        "purged_mean_auc": mean_auc,
        "purged_std_auc": std_auc,
        "purged_weighted_auc": weighted_auc,
        "fold_aucs": fold_aucs,
    },
    "version": "flow_predictor_v3_1R_5d_2026-04-26",
}
with open(MODEL_OUT, "wb") as f:
    pickle.dump(model_data, f)
print(f"  saved: {MODEL_OUT}")

print("\n" + "=" * 60)
print("HONESTY DELTA (label_hit_1R_5d)")
print("=" * 60)
print(f"  Naive val AUC    : {naive_val_auc:.4f}")
print(f"  Naive test AUC   : {naive_test_auc:.4f}")
if fold_aucs:
    print(f"  Purged mean AUC  : {mean_auc:.4f}  (delta vs naive val: {mean_auc-naive_val_auc:+.4f})")
print(f"\nDone in {time.time() - t0:.1f}s")
