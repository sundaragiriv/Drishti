"""Intraday ML — LightGBM models to predict intraday trade quality.

Joins ``fact_intraday_features`` with ``strategy_backtest_results`` to train
per-strategy classifiers that predict which entries will hit 2R before stop.

Target: hit_2r (binary) — did price reach 2R before stop?
Features: ~30 intraday features (available by 10:00 AM) + intelligence context.
Goal: Filter 91K MOMENTUM_IGN entries to top ~5K with 55–60%+ 2R hit rate.

Design choices:
  - Only features available BEFORE entry time (no day_high, eod_close, etc.)
  - Temporal split: Q1+Q2 train → Q3 validate (no future leakage)
  - LightGBM for native categorical + NULL handling
  - Calibration report: precision at top percentile buckets

Usage:
    # Train per-strategy (Q1+Q2 train, Q3 validate)
    python -m signal_scanner.institutional_intel.intelligence.intraday_ml \\
        --train --strategy MOMENTUM_IGN

    # Train all strategies
    python -m ... --train --strategy ALL

    # Score all entries and write predictions
    python -m ... --score --strategy ALL

    # Print calibration report
    python -m ... --report --strategy ALL
"""

from __future__ import annotations

import argparse
import json
import pickle
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import duckdb
import lightgbm as lgb
import numpy as np
import pandas as pd
from loguru import logger

from signal_scanner.institutional_intel.config import WAREHOUSE_PATH

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
_MODELS_DIR = WAREHOUSE_PATH.parent / "models"

ALL_STRATEGIES = ["VWAP_MR", "FPB", "MOMENTUM_IGN", "ORB_V2"]

# Default temporal split
QUARTERS_TRAIN = ["2024-Q1", "2024-Q2"]
QUARTERS_VAL = ["2024-Q3"]

# ---------------------------------------------------------------------------
# Feature definitions — ONLY features available before typical entry time
# ---------------------------------------------------------------------------

# Pre-open / daily context
FEATURES_PREOPEN = [
    "prev_close",
    "gap_pct",
    "atr_20d",
]

# Opening range (9:30-9:45)
FEATURES_OR = [
    "open_930",
    "or_high",
    "or_low",
    "or_range",
    "or_volume",
    "avg_or_volume_20d",
    "volume_ratio",
    "or_range_vs_atr",
]

# Snapshots by 10:00 AM
FEATURES_BY_1000 = [
    "vwap_at_1000",
    "price_vs_vwap_1000",
    "rel_volume_1000",
    # NOTE: first_30min_vol_pct EXCLUDED — uses total_rth_volume (future data)
    "first_30min_range_pct",
    "ret_5min_0945",
    "ret_15min_1000",
    "ret_30min_1000",
    "ret_vs_spy_1000",
    "consolidation_bars",
]

# Breakout flags (detected 9:45+, before most entries)
FEATURES_BREAKOUT = [
    "or_breakout",
    "or_breakdown",
]

# Intelligence context (always available, frozen at compute time)
FEATURES_INTEL_NUMERIC = [
    "conviction_score",
    "expected_value",
    "squeeze_score",
    "short_squeeze_score",
    "tier1_count",
]

FEATURES_INTEL_BINARY = [
    "insider_cluster",
]

# 5-min candlestick + volume pattern features
FEATURES_CANDLE_5M = [
    "candle_hammer_count_5m",
    "candle_engulf_bull_count_5m",
    "candle_doji_count_5m",
    "candle_reversal_near_vwap",
]
FEATURES_VOLUME_5M = [
    "volume_spike_count_5m",
    "volume_spike_near_vwap",
    "max_bar_volume_ratio_5m",
    "volume_climax_reversal",
]

# Categorical features — LightGBM handles natively
FEATURES_CATEGORICAL = [
    "accum_phase",
    "swing_signal",
    "sector",
]

# Combined feature list (order matters for column indexing)
ALL_FEATURES = (
    FEATURES_PREOPEN
    + FEATURES_OR
    + FEATURES_BY_1000
    + FEATURES_BREAKOUT
    + FEATURES_INTEL_NUMERIC
    + FEATURES_INTEL_BINARY
    + FEATURES_CANDLE_5M
    + FEATURES_VOLUME_5M
    + FEATURES_CATEGORICAL
)

# Derived features computed during prepare_features
DERIVED_FEATURES = [
    "gap_vs_atr",           # |gap_pct| / (atr_20d / prev_close * 100)
    "or_range_pct",         # or_range / open_930 * 100
    "or_volume_log",        # log(or_volume + 1)
    "vwap_distance_abs",    # abs(price_vs_vwap_1000)
    "rsi_proxy",            # based on ret_30min return direction/magnitude
    "price_vs_or_mid",      # price at 10:00 vs OR midpoint (%)
]

TARGET_COL = "hit_2r"


# ---------------------------------------------------------------------------
# DuckDB predictions table
# ---------------------------------------------------------------------------

def _ensure_tables(conn: duckdb.DuckDBPyConnection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS intraday_ml_predictions (
            ticker          TEXT NOT NULL,
            trade_date      DATE NOT NULL,
            strategy        TEXT NOT NULL,
            ml_prob_2r      DOUBLE,
            ml_percentile   DOUBLE,
            actual_hit_2r   BOOLEAN,
            actual_hit_stop BOOLEAN,
            max_favorable_r DOUBLE,
            model_version   TEXT,
            computed_at     TIMESTAMP,
            PRIMARY KEY (ticker, trade_date, strategy)
        )
    """)


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def load_training_data(
    strategy: str,
    quarters: List[str],
    conn: duckdb.DuckDBPyConnection,
) -> pd.DataFrame:
    """Load features + outcomes for a strategy's entries."""
    quarter_list = ", ".join(f"'{q}'" for q in quarters)

    query = f"""
        SELECT
            f.ticker,
            f.trade_date,
            f.report_quarter,
            -- Pre-open
            f.prev_close, f.gap_pct, f.atr_20d,
            -- OR
            f.open_930, f.or_high, f.or_low, f.or_range,
            f.or_volume, f.avg_or_volume_20d, f.volume_ratio, f.or_range_vs_atr,
            -- By 10:00
            f.vwap_at_1000, f.price_vs_vwap_1000,
            f.rel_volume_1000, f.first_30min_vol_pct, f.first_30min_range_pct,
            f.ret_5min_0945, f.ret_15min_1000, f.ret_30min_1000,
            f.ret_vs_spy_1000,
            f.consolidation_bars,
            -- Breakout
            f.or_breakout, f.or_breakdown,
            -- Intelligence
            f.conviction_score, f.expected_value,
            f.squeeze_score, f.short_squeeze_score, f.tier1_count,
            f.insider_cluster,
            f.accum_phase, f.swing_signal, f.sector,
            -- 5-min candlestick + volume
            f.candle_hammer_count_5m, f.candle_engulf_bull_count_5m,
            f.candle_doji_count_5m, f.candle_reversal_near_vwap,
            f.volume_spike_count_5m, f.volume_spike_near_vwap,
            f.max_bar_volume_ratio_5m, f.volume_climax_reversal,
            -- Targets
            s.hit_2r,
            s.hit_stop,
            s.max_favorable_r,
            s.hit_1r,
            s.hit_3r,
            s.hit_4r,
            s.stop_distance_pct,
            s.entry_price
        FROM fact_intraday_features f
        JOIN strategy_backtest_results s
            ON f.ticker = s.ticker AND f.trade_date = s.trade_date
        WHERE s.strategy = '{strategy}'
          AND s.entry_triggered = TRUE
          AND f.report_quarter IN ({quarter_list})
          AND f.open_930 IS NOT NULL
    """

    df = conn.execute(query).fetchdf()
    logger.info(
        f"Loaded {len(df):,} entries for {strategy} "
        f"(quarters: {quarters}, 2R rate: {df['hit_2r'].mean():.1%})"
    )
    return df


# ---------------------------------------------------------------------------
# Feature preparation
# ---------------------------------------------------------------------------

def prepare_features(df: pd.DataFrame) -> Tuple[pd.DataFrame, pd.Series, List[str]]:
    """Build feature matrix X, target y, and feature name list.

    Adds derived features and encodes categoricals for LightGBM.
    """
    X = df.copy()

    # --- Derived features ---
    # Gap vs ATR (normalized gap magnitude)
    atr_pct = X["atr_20d"] / X["prev_close"].replace(0, np.nan) * 100
    X["gap_vs_atr"] = X["gap_pct"].abs() / atr_pct.replace(0, np.nan)

    # OR range as % of price
    X["or_range_pct"] = X["or_range"] / X["open_930"].replace(0, np.nan) * 100

    # Log volume (reduces skew)
    X["or_volume_log"] = np.log1p(X["or_volume"].fillna(0).clip(lower=0))

    # Absolute VWAP distance
    X["vwap_distance_abs"] = X["price_vs_vwap_1000"].abs()

    # RSI proxy from 30-min return (simple momentum signal)
    X["rsi_proxy"] = X["ret_30min_1000"].clip(-10, 10)

    # Price vs OR midpoint
    or_mid = (X["or_high"] + X["or_low"]) / 2
    X["price_vs_or_mid"] = (
        (X["vwap_at_1000"] - or_mid) / or_mid.replace(0, np.nan) * 100
    )

    # --- Categorical encoding for LightGBM ---
    for col in FEATURES_CATEGORICAL:
        X[col] = X[col].astype("category")

    # --- Boolean to int ---
    bool_cols = (FEATURES_BREAKOUT + FEATURES_INTEL_BINARY +
                 ["candle_reversal_near_vwap", "volume_spike_near_vwap", "volume_climax_reversal"])
    for col in bool_cols:
        if col in X.columns:
            X[col] = X[col].fillna(False).astype(float)

    # Feature columns
    feature_cols = ALL_FEATURES + DERIVED_FEATURES
    y = df[TARGET_COL].astype(int)

    return X[feature_cols], y, feature_cols


# ---------------------------------------------------------------------------
# Model training
# ---------------------------------------------------------------------------

def train_model(
    strategy: str,
    conn: duckdb.DuckDBPyConnection,
    quarters_train: Optional[List[str]] = None,
    quarters_val: Optional[List[str]] = None,
) -> Tuple[lgb.LGBMClassifier, Dict[str, Any]]:
    """Train a LightGBM classifier for a single strategy.

    Returns (model, metrics_dict).
    """
    quarters_train = quarters_train or QUARTERS_TRAIN
    quarters_val = quarters_val or QUARTERS_VAL

    logger.info(f"Training {strategy}: train={quarters_train}, val={quarters_val}")

    # Load data
    df_train = load_training_data(strategy, quarters_train, conn)
    df_val = load_training_data(strategy, quarters_val, conn)

    if len(df_train) < 100:
        logger.warning(f"Too few training samples for {strategy}: {len(df_train)}")
        return None, {}

    X_train, y_train, feature_cols = prepare_features(df_train)
    X_val, y_val, _ = prepare_features(df_val)

    # Class balance
    pos_rate = y_train.mean()
    scale = (1 - pos_rate) / pos_rate if pos_rate > 0 else 1.0
    logger.info(
        f"  Train: {len(X_train):,} rows, {pos_rate:.1%} positive, "
        f"scale_pos_weight={scale:.2f}"
    )

    # LightGBM parameters — tuned for this problem
    params = {
        "objective": "binary",
        "metric": "auc",
        "n_estimators": 800,
        "max_depth": 5,
        "num_leaves": 31,
        "learning_rate": 0.03,
        "min_child_samples": 100,
        "subsample": 0.8,
        "colsample_bytree": 0.8,
        "reg_alpha": 1.0,
        "reg_lambda": 2.0,
        "scale_pos_weight": scale,
        "random_state": 42,
        "verbose": -1,
        "n_jobs": -1,
    }

    model = lgb.LGBMClassifier(**params)

    # Identify categorical feature indices for LightGBM
    cat_indices = [feature_cols.index(c) for c in FEATURES_CATEGORICAL]

    model.fit(
        X_train,
        y_train,
        eval_set=[(X_val, y_val)],
        eval_metric="auc",
        categorical_feature=cat_indices,
        callbacks=[
            lgb.early_stopping(50, verbose=True),
            lgb.log_evaluation(100),
        ],
    )

    # Predictions
    proba_train = model.predict_proba(X_train)[:, 1]
    proba_val = model.predict_proba(X_val)[:, 1]

    # Metrics
    from sklearn.metrics import roc_auc_score, log_loss

    metrics = {
        "strategy": strategy,
        "train_size": len(X_train),
        "val_size": len(X_val),
        "train_pos_rate": float(y_train.mean()),
        "val_pos_rate": float(y_val.mean()),
        "train_auc": float(roc_auc_score(y_train, proba_train)),
        "val_auc": float(roc_auc_score(y_val, proba_val)),
        "train_logloss": float(log_loss(y_train, proba_train)),
        "val_logloss": float(log_loss(y_val, proba_val)),
        "n_estimators_used": model.best_iteration_ or model.n_estimators,
        "feature_cols": feature_cols,
    }

    logger.info(
        f"  {strategy}: train AUC={metrics['train_auc']:.4f}, "
        f"val AUC={metrics['val_auc']:.4f} "
        f"(iters={metrics['n_estimators_used']})"
    )

    # Feature importance
    imp = pd.Series(
        model.feature_importances_, index=feature_cols
    ).sort_values(ascending=False)
    metrics["top_features"] = imp.head(15).to_dict()
    logger.info(f"  Top features: {list(imp.head(10).index)}")

    return model, metrics


# ---------------------------------------------------------------------------
# Calibration analysis
# ---------------------------------------------------------------------------

def calibration_report(
    model: lgb.LGBMClassifier,
    df_val: pd.DataFrame,
    strategy: str,
) -> pd.DataFrame:
    """Compute actual 2R hit rate at each predicted probability bucket.

    This is the KEY output — shows whether the model can separate winners.
    """
    X_val, y_val, _ = prepare_features(df_val)
    proba = model.predict_proba(X_val)[:, 1]

    results = df_val[["ticker", "trade_date"]].copy()
    results["prob_2r"] = proba
    results["actual_2r"] = y_val.values
    results["actual_stop"] = df_val["hit_stop"].astype(int).values
    results["mfe"] = df_val["max_favorable_r"].values
    results["hit_1r"] = df_val["hit_1r"].astype(int).values

    # Percentile buckets
    results["percentile"] = results["prob_2r"].rank(pct=True) * 100

    buckets = [
        ("Top 1%", 99, 100),
        ("Top 2%", 98, 100),
        ("Top 5%", 95, 100),
        ("Top 10%", 90, 100),
        ("Top 20%", 80, 100),
        ("Top 30%", 70, 100),
        ("Top 50%", 50, 100),
        ("Bottom 50%", 0, 50),
        ("Bottom 20%", 0, 20),
        ("ALL", 0, 100),
    ]

    rows = []
    for label, pmin, pmax in buckets:
        mask = (results["percentile"] >= pmin) & (results["percentile"] <= pmax)
        subset = results[mask]
        if len(subset) == 0:
            continue

        n = len(subset)
        hit_2r = subset["actual_2r"].mean()
        hit_1r = subset["hit_1r"].mean()
        stop_rate = subset["actual_stop"].mean()
        avg_mfe = subset["mfe"].mean()
        avg_prob = subset["prob_2r"].mean()

        # Expected value at 1:2 R:R
        # If you risk 1R to make 2R: EV = hit_2r * 2 - (1-hit_2r) * 1
        # But some trades that don't hit 2R still hit 1R
        ev_2r = hit_2r * 2 - (1 - hit_2r) * 1

        rows.append({
            "bucket": label,
            "n": n,
            "avg_prob": avg_prob,
            "hit_2r": hit_2r,
            "hit_1r": hit_1r,
            "stop_rate": stop_rate,
            "avg_mfe": avg_mfe,
            "ev_at_2r": ev_2r,
        })

    return pd.DataFrame(rows)


def print_calibration(
    cal_df: pd.DataFrame,
    strategy: str,
    metrics: Dict[str, Any],
) -> None:
    """Print formatted calibration report."""
    print()
    print("=" * 90)
    print(f"  {strategy} — ML CALIBRATION REPORT")
    print(f"  Train AUC: {metrics.get('train_auc', 0):.4f}  |  "
          f"Val AUC: {metrics.get('val_auc', 0):.4f}  |  "
          f"Iters: {metrics.get('n_estimators_used', 0)}")
    print("=" * 90)
    print()
    print(f"  {'Bucket':<15s} {'N':>7s} {'AvgProb':>8s} {'2R Hit':>7s} "
          f"{'1R Hit':>7s} {'StopR':>7s} {'AvgMFE':>7s} {'EV@2R':>7s}")
    print(f"  {'-'*74}")

    for _, row in cal_df.iterrows():
        ev_str = f"{row['ev_at_2r']:+.2f}R"
        print(
            f"  {row['bucket']:<15s} {row['n']:>7,d} {row['avg_prob']:>7.1%} "
            f"{row['hit_2r']:>7.1%} {row['hit_1r']:>7.1%} "
            f"{row['stop_rate']:>7.1%} {row['avg_mfe']:>+6.1f}R "
            f"{ev_str:>7s}"
        )

    print()

    # Feature importance
    if "top_features" in metrics:
        print(f"  TOP FEATURES:")
        for i, (feat, imp) in enumerate(metrics["top_features"].items()):
            print(f"    {i+1:2d}. {feat:<30s} {imp:>6.0f}")
        print()


# ---------------------------------------------------------------------------
# Scoring — write predictions back to DuckDB
# ---------------------------------------------------------------------------

def score_and_write(
    model: lgb.LGBMClassifier,
    strategy: str,
    quarters: List[str],
    conn: duckdb.DuckDBPyConnection,
    model_version: str = "v1",
) -> int:
    """Score all entries for a strategy and write predictions to DuckDB."""
    _ensure_tables(conn)

    df = load_training_data(strategy, quarters, conn)
    if len(df) == 0:
        return 0

    X, y, _ = prepare_features(df)
    proba = model.predict_proba(X)[:, 1]

    # Percentile within this strategy
    ranks = pd.Series(proba).rank(pct=True) * 100

    now = datetime.now(timezone.utc)
    pred_df = pd.DataFrame({
        "ticker": df["ticker"].values,
        "trade_date": df["trade_date"].values,
        "strategy": strategy,
        "ml_prob_2r": proba,
        "ml_percentile": ranks.values,
        "actual_hit_2r": df["hit_2r"].values,
        "actual_hit_stop": df["hit_stop"].values,
        "max_favorable_r": df["max_favorable_r"].values,
        "model_version": model_version,
        "computed_at": now,
    })

    conn.register("_pred_batch", pred_df)
    conn.execute("""
        INSERT OR REPLACE INTO intraday_ml_predictions
        SELECT * FROM _pred_batch
    """)
    conn.unregister("_pred_batch")

    logger.info(f"Wrote {len(pred_df):,} predictions for {strategy}")
    return len(pred_df)


# ---------------------------------------------------------------------------
# Model persistence
# ---------------------------------------------------------------------------

def _model_path(strategy: str) -> Path:
    return _MODELS_DIR / f"intraday_ml_{strategy.lower()}.pkl"


def save_model(
    model: lgb.LGBMClassifier,
    metrics: Dict[str, Any],
    strategy: str,
) -> Path:
    _MODELS_DIR.mkdir(parents=True, exist_ok=True)
    path = _model_path(strategy)
    with open(path, "wb") as f:
        pickle.dump({"model": model, "metrics": metrics}, f)
    logger.info(f"Saved model to {path}")
    return path


def load_model(strategy: str) -> Tuple[lgb.LGBMClassifier, Dict[str, Any]]:
    path = _model_path(strategy)
    with open(path, "rb") as f:
        data = pickle.load(f)
    return data["model"], data["metrics"]


# ---------------------------------------------------------------------------
# Full pipeline
# ---------------------------------------------------------------------------

def run_train_pipeline(
    strategies: List[str],
    quarters_train: Optional[List[str]] = None,
    quarters_val: Optional[List[str]] = None,
) -> Dict[str, Dict[str, Any]]:
    """Train and evaluate models for all specified strategies."""
    quarters_train = quarters_train or QUARTERS_TRAIN
    quarters_val = quarters_val or QUARTERS_VAL

    conn = duckdb.connect(str(WAREHOUSE_PATH), read_only=True)
    all_metrics = {}

    try:
        for strategy in strategies:
            print(f"\n{'='*60}")
            print(f"  TRAINING: {strategy}")
            print(f"{'='*60}")

            model, metrics = train_model(
                strategy, conn, quarters_train, quarters_val
            )
            if model is None:
                continue

            # Calibration on validation set
            df_val = load_training_data(strategy, quarters_val, conn)
            cal_df = calibration_report(model, df_val, strategy)
            print_calibration(cal_df, strategy, metrics)

            # Save
            save_model(model, metrics, strategy)
            all_metrics[strategy] = metrics

    finally:
        conn.close()

    # Summary comparison
    if len(all_metrics) > 1:
        _print_comparison(all_metrics)

    return all_metrics


def run_score_pipeline(
    strategies: List[str],
    quarters: Optional[List[str]] = None,
) -> None:
    """Score all entries and write predictions to DuckDB."""
    quarters = quarters or QUARTERS_TRAIN + QUARTERS_VAL

    conn = duckdb.connect(str(WAREHOUSE_PATH))
    _ensure_tables(conn)

    try:
        for strategy in strategies:
            model, metrics = load_model(strategy)
            n = score_and_write(model, strategy, quarters, conn)
            logger.info(f"Scored {n:,} entries for {strategy}")
    finally:
        conn.close()


def run_report_pipeline(
    strategies: List[str],
    quarters_val: Optional[List[str]] = None,
) -> None:
    """Load saved models and print calibration reports."""
    quarters_val = quarters_val or QUARTERS_VAL

    conn = duckdb.connect(str(WAREHOUSE_PATH), read_only=True)

    try:
        for strategy in strategies:
            model, metrics = load_model(strategy)
            df_val = load_training_data(strategy, quarters_val, conn)
            cal_df = calibration_report(model, df_val, strategy)
            print_calibration(cal_df, strategy, metrics)
    finally:
        conn.close()


def _print_comparison(all_metrics: Dict[str, Dict[str, Any]]) -> None:
    """Print side-by-side AUC comparison."""
    print()
    print("=" * 70)
    print("  MODEL COMPARISON")
    print("=" * 70)
    print(f"  {'Strategy':<18s} {'Train':>8s} {'Val':>8s} "
          f"{'TrainAUC':>9s} {'ValAUC':>8s} {'Iters':>6s}")
    print(f"  {'-'*60}")

    for strat, m in sorted(all_metrics.items()):
        print(
            f"  {strat:<18s} {m['train_size']:>8,d} {m['val_size']:>8,d} "
            f"{m['train_auc']:>8.4f} {m['val_auc']:>8.4f} "
            f"{m['n_estimators_used']:>6d}"
        )
    print()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Intraday ML — LightGBM trade quality predictor"
    )
    parser.add_argument("--train", action="store_true",
                        help="Train models")
    parser.add_argument("--score", action="store_true",
                        help="Score entries and write to DuckDB")
    parser.add_argument("--report", action="store_true",
                        help="Print calibration report from saved models")
    parser.add_argument("--strategy", default="ALL",
                        help="Strategy name or ALL (default: ALL)")
    parser.add_argument("--quarters-train", default="2024-Q1,2024-Q2",
                        help="Training quarters (default: 2024-Q1,2024-Q2)")
    parser.add_argument("--quarter-val", default="2024-Q3",
                        help="Validation quarter (default: 2024-Q3)")
    parser.add_argument("--quarters", default=None,
                        help="Quarters for scoring (default: all)")

    args = parser.parse_args()

    strategies = ALL_STRATEGIES if args.strategy == "ALL" else [args.strategy]
    q_train = [q.strip() for q in args.quarters_train.split(",")]
    q_val = [q.strip() for q in args.quarter_val.split(",")]

    if args.train:
        run_train_pipeline(strategies, q_train, q_val)

    if args.score:
        quarters = (
            [q.strip() for q in args.quarters.split(",")]
            if args.quarters
            else q_train + q_val
        )
        run_score_pipeline(strategies, quarters)

    if args.report:
        run_report_pipeline(strategies, q_val)

    if not (args.train or args.score or args.report):
        parser.print_help()


if __name__ == "__main__":
    main()
