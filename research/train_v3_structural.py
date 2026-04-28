"""F1-B: Retrain v3_5d with Yang-Zhang + Hurst structural features.

Adds 7 new features to the v3 baseline:
  YZ pack:    yz_vol_14d, yz_vol_5d, yz_overnight_share, yz_vs_atr_ratio_14
  Hurst pack: hurst_64d, hurst_252d, hurst_regime (ordinal)

Methodology (per locked plan, docs/plan_yz_features_2026-04-28.md):
  - Locked 90-day holdout — evaluated EXACTLY ONCE at the end.
  - Purged 5-fold CV with 5d embargo on the pre-holdout pool.
  - Eval report: honest mean AUC + std, holdout AUC, Brier score,
    calibration buckets, vol-regime stratified AUC, gain-based feature
    importance (SHAP fallback — pip install shap unavailable in 3.13).

Decision criteria:
  Holdout AUC > 0.575 + Brier improves + YZ_overnight in top-10:  SHIP
  Holdout AUC 0.55-0.575, neutral Brier:                          WASH
  Holdout AUC < 0.55 OR Brier regresses:                          SHELVE

Run:
    python -u -m research.train_v3_structural
"""
from __future__ import annotations

import json
import pickle
import time
from datetime import timedelta
from pathlib import Path

import duckdb
import lightgbm as lgb
import numpy as np
import pandas as pd
from sklearn.metrics import brier_score_loss, roc_auc_score

from signal_scanner.features.hurst import hurst_regime, hurst_rolling
from signal_scanner.features.yang_zhang import (
    add_yz_features,
    yang_zhang_vol,
    yz_overnight_share,
)
from signal_scanner.intelligence.purged_cv import PurgedKFold

# Guard against import side-effects: this script is for `python -m` only.
if __name__ != "__main__":
    raise SystemExit(
        "research.train_v3_structural runs heavy DuckDB queries and trains "
        "an ML model on import. Run as `python -m research.train_v3_structural`."
    )

t0 = time.time()
PARQUET_IN = "data/ml_training/flow_predictor_features_v2.parquet"
WAREHOUSE = "data/warehouse/sec_intel.duckdb"
MODEL_OUT = "data/models/flow_predictor_v3_structural.pkl"
REPORT_OUT = "docs/structural_features_eval_2026-04-28.md"
HOLDOUT_DAYS = 90  # last 90 calendar days = locked holdout

# ----------------------------------------------------------------------
# 1. Load v2 features
# ----------------------------------------------------------------------
print("=" * 70)
print("F1-B: Train v3_5d with Yang-Zhang + Hurst structural features")
print("=" * 70)
print("\nLoading v2 features...")
df = pd.read_parquet(PARQUET_IN)
df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date
print(f"  {len(df):,} rows, {df['ticker'].nunique():,} tickers, "
      f"{df['trade_date'].min()} -> {df['trade_date'].max()}")

# ----------------------------------------------------------------------
# 2. Pull OHLC from DuckDB to compute YZ + Hurst per ticker
# ----------------------------------------------------------------------
print("\nLoading OHLC + ATR for YZ + Hurst computation...")
conn = duckdb.connect(WAREHOUSE, read_only=True)
parquet_keys = df[["ticker", "trade_date"]].drop_duplicates()
conn.register("parquet_keys", parquet_keys)

# Pull all OHLC bars for the tickers we need, with enough history pre-min(trade_date)
# for the 252-day Hurst window.
min_date = df["trade_date"].min() - timedelta(days=400)
ohlc = conn.execute("""
    SELECT p.ticker, p.trade_date, p.open, p.high, p.low, p.close
    FROM fact_daily_prices p
    WHERE p.ticker IN (SELECT DISTINCT ticker FROM parquet_keys)
      AND p.trade_date >= ?
    ORDER BY p.ticker, p.trade_date
""", [min_date]).df()
conn.close()
ohlc["trade_date"] = pd.to_datetime(ohlc["trade_date"]).dt.date
print(f"  OHLC bars loaded: {len(ohlc):,}")

# ----------------------------------------------------------------------
# 3. Compute label_hit_1R_5d (same as F1-A baseline)
# ----------------------------------------------------------------------
print("\nComputing label_hit_1R_5d...")
conn = duckdb.connect(WAREHOUSE, read_only=True)
conn.register("parquet_keys", parquet_keys)
label_df = conn.execute("""
    WITH base AS (
        SELECT p.ticker, p.trade_date, p.close,
               AVG(p.high - p.low) OVER (
                   PARTITION BY p.ticker ORDER BY p.trade_date
                   ROWS BETWEEN 19 PRECEDING AND CURRENT ROW
               ) AS atr20,
               LEAD(p.high, 1) OVER w AS h1, LEAD(p.high, 2) OVER w AS h2,
               LEAD(p.high, 3) OVER w AS h3, LEAD(p.high, 4) OVER w AS h4,
               LEAD(p.high, 5) OVER w AS h5,
               LEAD(p.low,  1) OVER w AS l1, LEAD(p.low,  2) OVER w AS l2,
               LEAD(p.low,  3) OVER w AS l3, LEAD(p.low,  4) OVER w AS l4,
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
label_df["trade_date"] = pd.to_datetime(label_df["trade_date"]).dt.date

# ----------------------------------------------------------------------
# 4. Compute YZ + Hurst per ticker (vectorized via groupby.apply)
# ----------------------------------------------------------------------
print("\nComputing YZ + Hurst per ticker (this is the long step)...")
HURST_REGIME_ORDINAL = {"MEAN_REVERT": -1, "RANDOM": 0, "TRENDING": 1, "UNKNOWN": 0}


def _per_ticker_features(group: pd.DataFrame) -> pd.DataFrame:
    g = group.sort_values("trade_date").reset_index(drop=True)
    # YZ features
    g["yz_vol_14d"] = yang_zhang_vol(g, window=14, annualize=True)
    g["yz_vol_5d"] = yang_zhang_vol(g, window=5, annualize=True)
    g["yz_overnight_share"] = yz_overnight_share(g, window=14)
    # Hurst features (need at least 64 returns for short, 252 for long)
    rets = np.log(g["close"]).diff().fillna(0.0)
    if len(g) >= 65:
        g["hurst_64d"] = hurst_rolling(rets, window=64).values
    else:
        g["hurst_64d"] = np.nan
    if len(g) >= 253:
        g["hurst_252d"] = hurst_rolling(rets, window=252).values
    else:
        g["hurst_252d"] = np.nan
    # Hurst regime (use 64d as the basis)
    h64_series = pd.Series(g["hurst_64d"].values, index=g.index)
    regime_str = hurst_regime(h64_series)
    g["hurst_regime"] = regime_str.map(HURST_REGIME_ORDINAL).fillna(0).astype(int).values
    return g


# Group by ticker, apply, concat
struct_chunks = []
for ticker, group in ohlc.groupby("ticker", sort=False):
    if len(group) < 65:
        continue
    out = _per_ticker_features(group)
    struct_chunks.append(out[["ticker", "trade_date",
                              "yz_vol_14d", "yz_vol_5d",
                              "yz_overnight_share",
                              "hurst_64d", "hurst_252d", "hurst_regime"]])
struct_df = pd.concat(struct_chunks, ignore_index=True)
struct_df["trade_date"] = pd.to_datetime(struct_df["trade_date"]).dt.date
print(f"  YZ+Hurst rows: {len(struct_df):,}")

# ----------------------------------------------------------------------
# 5. Merge structural features into main df, also compute ratio
# ----------------------------------------------------------------------
print("\nMerging structural features into base v2 set...")
df = df.merge(struct_df, on=["ticker", "trade_date"], how="left")

# Window-matched orthogonality ratio (YZ_14 vs ATR_14 — both annualized)
# atr14_pct in v2 features is already a percentage of close
atr14_annualized = (df["atr14_pct"] / 100.0) * np.sqrt(252)
df["yz_vs_atr_ratio_14"] = df["yz_vol_14d"] / atr14_annualized.replace(0, np.nan)

df = df.merge(label_df, on=["ticker", "trade_date"], how="inner")
df = df.dropna(subset=["label_hit_1R_5d"])
df["label_hit_1R_5d"] = df["label_hit_1R_5d"].astype(int)
print(f"  After label merge: {len(df):,}")

# ----------------------------------------------------------------------
# 6. Feature engineering — same v3 base + 7 structural
# ----------------------------------------------------------------------
print("\nEngineering features (v3 base + structural)...")
id_cols = ["ticker", "trade_date", "close", "report_quarter"]
label_cols = ["ret_3d", "ret_5d", "label_3pct_3d", "label_2pct_3d", "label_up_3d",
              "label_rr_2to1", "label_rr_1_5to1", "max_up_5d", "max_down_5d",
              "label_hit_1R_5d"]
spy_features = [c for c in df.columns if c.startswith("spy_")]
feature_cols_no_spy = [c for c in df.columns
                      if c not in id_cols + label_cols + spy_features]

# v3 interactions
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

structural_features = [
    "yz_vol_14d", "yz_vol_5d", "yz_overnight_share", "yz_vs_atr_ratio_14",
    "hurst_64d", "hurst_252d", "hurst_regime",
]

all_features = feature_cols_no_spy + interaction_features
# structural features are already in df via merge, just need to be in all_features
for sf in structural_features:
    if sf not in all_features and sf in df.columns:
        all_features.append(sf)

df = df.dropna(subset=all_features)
df = df.sort_values("trade_date").reset_index(drop=True)
print(f"  After feature dropna: {len(df):,} rows; {len(all_features)} features "
      f"({len(structural_features)} structural)")

# ----------------------------------------------------------------------
# 7. LOCKED HOLDOUT split — last 90 calendar days
# ----------------------------------------------------------------------
max_date = df["trade_date"].max()
holdout_start = max_date - timedelta(days=HOLDOUT_DAYS)
print(f"\nLocked holdout: {holdout_start} to {max_date} ({HOLDOUT_DAYS} days)")

train_pool = df[df["trade_date"] < holdout_start].reset_index(drop=True)
holdout = df[df["trade_date"] >= holdout_start].reset_index(drop=True)
print(f"  Train pool: {len(train_pool):,}  Holdout: {len(holdout):,}")
print(f"  Train hit rate: {train_pool['label_hit_1R_5d'].mean()*100:.1f}%  "
      f"Holdout hit rate: {holdout['label_hit_1R_5d'].mean()*100:.1f}%")


def _params(scale: float) -> dict:
    return {
        "objective": "binary", "metric": "auc",
        "boosting_type": "gbdt",
        "n_estimators": 2500, "learning_rate": 0.02,
        "max_depth": 8, "num_leaves": 64,
        "min_child_samples": 300, "min_child_weight": 10,
        "subsample": 0.6, "colsample_bytree": 0.6,
        "reg_alpha": 1.0, "reg_lambda": 5.0,
        "scale_pos_weight": scale, "verbose": -1,
        "n_jobs": -1, "random_state": 42,
    }


# ----------------------------------------------------------------------
# 8. Purged 5-fold CV on TRAIN POOL only (no peeking at holdout)
# ----------------------------------------------------------------------
print("\n" + "=" * 60)
print("PURGED 5-FOLD CV (horizon=5d, embargo=5d) on training pool")
print("=" * 60)
cv = PurgedKFold(n_splits=5, label_horizon_days=5, embargo_days=5)
fold_aucs, fold_n = [], []
for fold_i, (tr_idx, te_idx) in enumerate(cv.split(train_pool["trade_date"]), 1):
    if len(tr_idx) < 1000 or len(te_idx) < 100:
        continue
    y_tr = train_pool["label_hit_1R_5d"].iloc[tr_idx]
    y_te = train_pool["label_hit_1R_5d"].iloc[te_idx]
    if y_tr.mean() <= 0 or y_tr.mean() >= 1:
        continue
    scale = (1 - y_tr.mean()) / y_tr.mean()
    m = lgb.LGBMClassifier(**_params(scale))
    m.fit(train_pool[all_features].iloc[tr_idx], y_tr,
          callbacks=[lgb.log_evaluation(0)])
    proba = m.predict_proba(train_pool[all_features].iloc[te_idx])[:, 1]
    auc = roc_auc_score(y_te, proba)
    fold_aucs.append(auc)
    fold_n.append(len(te_idx))
    print(f"  fold {fold_i}: train={len(tr_idx):>7,}  test={len(te_idx):>6,}  AUC={auc:.4f}")

mean_auc = float(np.mean(fold_aucs)) if fold_aucs else float("nan")
std_auc = float(np.std(fold_aucs)) if fold_aucs else float("nan")
print(f"\n  Purged mean AUC = {mean_auc:.4f}  +/-{std_auc:.4f}")
print(f"  vs F1-A baseline (no structural features): 0.5510 +/-0.0044")
print(f"  Delta from baseline:           {mean_auc - 0.5510:+.4f}")

# ----------------------------------------------------------------------
# 9. Train final model on full training pool (used for holdout eval)
# ----------------------------------------------------------------------
print("\nTraining final model on full training pool...")
y_train = train_pool["label_hit_1R_5d"]
scale_train = (1 - y_train.mean()) / y_train.mean()
final_model = lgb.LGBMClassifier(**_params(scale_train))
final_model.fit(train_pool[all_features], y_train,
                callbacks=[lgb.log_evaluation(0)])

# ----------------------------------------------------------------------
# 10. SINGLE evaluation on locked holdout
# ----------------------------------------------------------------------
print("\n" + "=" * 60)
print("LOCKED HOLDOUT — single evaluation")
print("=" * 60)
y_holdout = holdout["label_hit_1R_5d"].values
proba_holdout = final_model.predict_proba(holdout[all_features])[:, 1]
holdout_auc = roc_auc_score(y_holdout, proba_holdout)
holdout_brier = brier_score_loss(y_holdout, proba_holdout)
print(f"  Holdout AUC:    {holdout_auc:.4f}")
print(f"  Holdout Brier:  {holdout_brier:.4f}")

# Calibration buckets (deciles by predicted prob, observed hit rate per bucket)
print("\n  Calibration buckets (predicted-prob deciles vs actual hit rate):")
print(f"  {'Range':>14} {'N':>7} {'Pred':>7} {'Actual':>7} {'Err':>7}")
print("  " + "-" * 50)
edges = np.linspace(proba_holdout.min(), proba_holdout.max() + 1e-9, 11)
calibration_rows = []
for i in range(10):
    lo, hi = edges[i], edges[i + 1]
    mask = (proba_holdout >= lo) & (proba_holdout < hi)
    n = int(mask.sum())
    if n == 0:
        continue
    pred_mean = float(proba_holdout[mask].mean())
    actual = float(y_holdout[mask].mean())
    err = pred_mean - actual
    calibration_rows.append({"low": lo, "high": hi, "n": n,
                             "pred": pred_mean, "actual": actual, "err": err})
    print(f"  {lo:.3f}-{hi:.3f}  {n:>7,} {pred_mean:>6.3f} {actual:>6.3f} {err:>+6.3f}")

# Stratified AUC by yz_vol_14d quintiles
print("\n  Stratified AUC by yz_vol_14d quintile (low vol -> high vol):")
print(f"  {'Quintile':>10} {'N':>7} {'AUC':>7} {'Hit%':>7}")
holdout = holdout.reset_index(drop=True)
holdout["_proba"] = proba_holdout
quint = pd.qcut(holdout["yz_vol_14d"], q=5, labels=False, duplicates="drop")
strat_rows = []
for q in sorted(quint.dropna().unique()):
    mask = (quint == q).values
    if mask.sum() < 100:
        continue
    sub_y = holdout.loc[mask, "label_hit_1R_5d"].values
    sub_p = holdout.loc[mask, "_proba"].values
    if sub_y.mean() <= 0 or sub_y.mean() >= 1:
        continue
    auc = roc_auc_score(sub_y, sub_p)
    hit = sub_y.mean()
    strat_rows.append({"quintile": int(q), "n": int(mask.sum()),
                       "auc": auc, "hit_rate": hit})
    print(f"  Q{int(q)}        {int(mask.sum()):>7,} {auc:>6.4f} {hit*100:>6.1f}%")

# Feature importance (LightGBM gain — SHAP fallback)
print("\n  Feature importance (LightGBM gain — SHAP unavailable in this env):")
imp = pd.Series(final_model.booster_.feature_importance(importance_type="gain"),
                index=all_features).sort_values(ascending=False)
top20 = imp.head(20)
print("  Top 20:")
structural_in_top10 = 0
for rank, (feat, gain) in enumerate(top20.items(), 1):
    is_struct = feat in structural_features
    marker = "STRUCTURAL" if is_struct else ""
    if is_struct and rank <= 10:
        structural_in_top10 += 1
    print(f"  {rank:>3}. {feat:<35} {gain:>10.0f}  {marker}")
print(f"\n  Structural features in TOP 10: {structural_in_top10} of {len(structural_features)}")

# YZ_overnight specifically
yz_on_rank = list(imp.index).index("yz_overnight_share") + 1 if "yz_overnight_share" in imp.index else None
print(f"  yz_overnight_share rank: {yz_on_rank}")

# ----------------------------------------------------------------------
# 11. Decision
# ----------------------------------------------------------------------
print("\n" + "=" * 60)
print("DECISION")
print("=" * 60)
brier_baseline = 0.21 * (1 - 0.21)  # Bernoulli base-rate Brier
brier_improves = holdout_brier < brier_baseline
yz_on_top10 = (yz_on_rank is not None) and (yz_on_rank <= 10)

if holdout_auc > 0.575 and brier_improves and yz_on_top10:
    decision = "SHIP"
elif holdout_auc < 0.55 or holdout_brier > brier_baseline:
    decision = "SHELVE"
else:
    decision = "WASH"

print(f"  Holdout AUC:           {holdout_auc:.4f}  (ship>0.575)")
print(f"  Brier score:           {holdout_brier:.4f}  (baseline {brier_baseline:.4f}; improves={brier_improves})")
print(f"  yz_overnight in top10: {yz_on_top10}  (rank={yz_on_rank})")
print(f"  --> DECISION: {decision}")

# ----------------------------------------------------------------------
# 12. Save model + metrics
# ----------------------------------------------------------------------
Path("data/models").mkdir(parents=True, exist_ok=True)
model_data = {
    "model": final_model,
    "feature_cols": all_features,
    "structural_features": structural_features,
    "label": "label_hit_1R_5d",
    "holdout_days": HOLDOUT_DAYS,
    "metrics": {
        "purged_mean_auc": mean_auc,
        "purged_std_auc": std_auc,
        "fold_aucs": fold_aucs,
        "holdout_auc": holdout_auc,
        "holdout_brier": holdout_brier,
        "calibration_buckets": calibration_rows,
        "stratified_auc": strat_rows,
        "yz_overnight_rank": yz_on_rank,
        "structural_in_top10": structural_in_top10,
        "feature_importance_top20": [(f, float(g)) for f, g in top20.items()],
        "decision": decision,
    },
    "version": "flow_predictor_v3_structural_2026-04-27",
}
with open(MODEL_OUT, "wb") as f:
    pickle.dump(model_data, f)
print(f"\n  Saved: {MODEL_OUT}")

# Also dump metrics as JSON for easy reading
metrics_json = MODEL_OUT.replace(".pkl", "_metrics.json")
with open(metrics_json, "w") as f:
    json.dump({k: v for k, v in model_data["metrics"].items()
               if k not in ("calibration_buckets", "stratified_auc",
                            "feature_importance_top20")}, f, indent=2, default=str)
print(f"  Metrics: {metrics_json}")

print(f"\nDone in {time.time() - t0:.1f}s")
