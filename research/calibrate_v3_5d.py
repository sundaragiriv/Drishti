"""F2: Calibrate flow_predictor_v3_1R_5d probabilities (isotonic).

The v3_1R_5d LightGBM classifier has honest AUC 0.5510 — real signal
that's well-ranked but poorly calibrated. Raw `predict_proba` outputs
don't match observed hit rates. Isotonic regression on a held-out
validation fold maps the raw probabilities back to actual hit rates.

After calibration, when the model says 73% it should mean 73% hit rate
empirically.

Output: extends data/models/flow_predictor_v3_1R_5d.pkl with a fitted
`isotonic` object so inference code can do:
    raw = model.predict_proba(X)[:, 1]
    calibrated = isotonic.predict(raw)

Run:
    python -m research.calibrate_v3_5d
"""
import pickle
import time
from pathlib import Path

import duckdb
import lightgbm as lgb
import numpy as np
import pandas as pd
from sklearn.metrics import roc_auc_score

from signal_scanner.intelligence.probability_calibration import (
    fit_isotonic, calibration_report, print_calibration_report,
)

t0 = time.time()
PARQUET = "data/ml_training/flow_predictor_features_v2.parquet"
WAREHOUSE = "data/warehouse/sec_intel.duckdb"
MODEL_PATH = "data/models/flow_predictor_v3_1R_5d.pkl"

# ---------------------------------------------------------------
# 1. Load model + recreate the val-fold inputs/labels
# ---------------------------------------------------------------
print("Loading model + features...")
with open(MODEL_PATH, "rb") as f:
    model_data = pickle.load(f)
model = model_data["model"]
all_features = model_data["feature_cols"]
print(f"  model: {model_data['version']}  features: {len(all_features)}")
print(f"  prior metrics: purged AUC = {model_data['metrics']['purged_mean_auc']:.4f}")

df = pd.read_parquet(PARQUET)

# Recompute label_hit_1R_5d via DuckDB (same as training script).
print("\nRecomputing label_hit_1R_5d...")
conn = duckdb.connect(WAREHOUSE, read_only=True)
parquet_keys = df[["ticker", "trade_date"]].drop_duplicates()
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
            WHEN h5 IS NULL OR l5 IS NULL OR atr20 IS NULL OR atr20 <= 0 THEN NULL
            WHEN GREATEST(h1,h2,h3,h4,h5) >= close + 2*atr20
                 AND LEAST(l1,l2,l3,l4,l5) > close - 2*atr20 THEN 1
            ELSE 0
        END AS label_hit_1R_5d
    FROM base
""").df()
conn.close()

df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date
label_df["trade_date"] = pd.to_datetime(label_df["trade_date"]).dt.date
df = df.merge(label_df, on=["ticker", "trade_date"], how="inner")
df = df.dropna(subset=["label_hit_1R_5d"])
df["label_hit_1R_5d"] = df["label_hit_1R_5d"].astype(int)

# Same interaction features as training
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
df = df.dropna(subset=all_features)
df["year"] = pd.to_datetime(df["trade_date"]).dt.year

# ---------------------------------------------------------------
# 2. Calibrate on the SAME val fold the model was trained against
#    (train<=2023, val=2024, test>=2025)
# ---------------------------------------------------------------
val = df[df["year"] == 2024].copy()
test = df[df["year"] >= 2025].copy()
print(f"\nVal (2024): {len(val):,}  Test (>=2025): {len(test):,}")

val_proba = model.predict_proba(val[all_features])[:, 1]
test_proba = model.predict_proba(test[all_features])[:, 1]
y_val = val["label_hit_1R_5d"].values
y_test = test["label_hit_1R_5d"].values

print(f"  raw val AUC  = {roc_auc_score(y_val, val_proba):.4f}")
print(f"  raw test AUC = {roc_auc_score(y_test, test_proba):.4f}")

# ---------------------------------------------------------------
# 3. Fit isotonic on val
# ---------------------------------------------------------------
print("\nFitting isotonic regression on val fold...")
iso = fit_isotonic(y_val, val_proba)

val_cal = iso.predict(val_proba)
test_cal = iso.predict(test_proba)

# AUC is rank-preserving, so calibrated AUC == raw AUC
# What matters is calibration error — does 0.7 mean 70% empirically?
print("\n--- VAL FOLD calibration buckets (raw vs calibrated) ---")
val_buckets = calibration_report(y_val, val_proba, iso, n_buckets=10)
print_calibration_report(val_buckets)

print("\n--- TEST FOLD (held-out) calibration ---")
test_buckets = calibration_report(y_test, test_proba, iso, n_buckets=10)
print_calibration_report(test_buckets)

# ---------------------------------------------------------------
# 4. Save calibrator alongside model
# ---------------------------------------------------------------
print("\nExtending model artifact with calibrator...")
model_data["isotonic_calibrator"] = iso
model_data["calibration_meta"] = {
    "fitted_on": "val_2024",
    "raw_val_auc": float(roc_auc_score(y_val, val_proba)),
    "raw_test_auc": float(roc_auc_score(y_test, test_proba)),
    "val_n": len(val),
    "test_n": len(test),
    "fitted_at": time.strftime("%Y-%m-%d %H:%M:%S"),
}
with open(MODEL_PATH, "wb") as f:
    pickle.dump(model_data, f)
print(f"  saved: {MODEL_PATH}")

print(f"\nDone in {time.time()-t0:.1f}s")
