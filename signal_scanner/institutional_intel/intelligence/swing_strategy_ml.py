"""Swing Strategy ML — LightGBM per-strategy models for swing trade quality.

Trains one LightGBM model per strategy (SQUEEZE, MEAN_REV, GAP_DRIFT,
INSIDER_BREAKOUT) to predict probability of hitting 2R target.

Joins ``fact_swing_features`` with ``swing_backtest_results`` to build
labeled training data. Produces calibration reports showing per-bucket
hit rates — the decision metric for which strategies to pursue live.

Usage:
    python -m signal_scanner.institutional_intel.intelligence.swing_strategy_ml \
        --train --strategy ALL \
        --quarters-train 2023-Q4,2024-Q1,2024-Q2 \
        --quarters-val 2024-Q3,2024-Q4

    python -m signal_scanner.institutional_intel.intelligence.swing_strategy_ml \
        --report --strategy ALL
"""

from __future__ import annotations

import argparse
import pickle
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import duckdb
import lightgbm as lgb
import numpy as np
import pandas as pd
from loguru import logger
from sklearn.metrics import roc_auc_score, log_loss

from signal_scanner.institutional_intel.config import WAREHOUSE_PATH

# ---------------------------------------------------------------------------
# Paths & Constants
# ---------------------------------------------------------------------------

_MODELS_DIR = WAREHOUSE_PATH.parent / "models"
ALL_STRATEGIES = [
    "SQUEEZE", "MEAN_REV", "GAP_DRIFT", "INSIDER_BREAKOUT",
    "GAP_DRIFT_FILTERED", "MEAN_REV_FILTERED", "CANDLE_REVERSAL",
]

QUARTERS_TRAIN_DEFAULT = ["2023-Q4", "2024-Q1", "2024-Q2"]
QUARTERS_VAL_DEFAULT = ["2024-Q3", "2024-Q4"]

MIN_SAMPLES = 100  # Skip training if fewer entries

# ---------------------------------------------------------------------------
# Feature definitions
# ---------------------------------------------------------------------------

FEATURES_PRICE = [
    "close", "sma_10", "sma_20", "sma_50", "sma_200",
    "price_vs_sma200_pct", "price_vs_sma50_pct", "pct_from_52w_high",
]
FEATURES_MOMENTUM = [
    "rsi_14", "rsi_2", "roc_5", "roc_10", "roc_20", "ret_vs_spy_20d",
]
FEATURES_VOLATILITY = [
    "atr_20", "bb_width_pct", "squeeze_on",
]
FEATURES_VOLUME = [
    "volume_ratio_20d", "obv_slope_10d", "volume_trend_5d",
]
FEATURES_SETUP = [
    "consecutive_down_days", "rsi2_below_10", "gap_pct_from_prev",
    "volume_surge_3x", "days_since_insider_cluster", "price_vs_20d_high_pct",
]
FEATURES_TREND = [
    "ema_20_slope", "adx_14", "plus_di_minus_di", "linreg_slope_12d",
]
FEATURES_INTEL_NUMERIC = [
    "conviction_score", "int_squeeze_score", "int_short_squeeze_score",
    "int_days_to_cover", "insider_effect_score", "trend_score",
    "institutional_pressure", "expected_value",
]
FEATURES_INTEL_BINARY = ["insider_cluster_detected"]
FEATURES_CANDLE = [
    "hammer", "inv_hammer", "engulfing_bull", "engulfing_bear",
    "doji", "morning_star", "evening_star", "three_white_soldiers",
    "piercing_line", "dark_cloud_cover",
]
FEATURES_TIMING = ["quarter_month", "day_of_week"]
FEATURES_CATEGORICAL = ["accum_phase", "sector"]
DERIVED_FEATURES = ["atr_pct", "r_unit_pct", "squeeze_x_conviction"]

ALL_FEATURES = (
    FEATURES_PRICE + FEATURES_MOMENTUM + FEATURES_VOLATILITY +
    FEATURES_VOLUME + FEATURES_SETUP + FEATURES_TREND +
    FEATURES_INTEL_NUMERIC + FEATURES_INTEL_BINARY +
    FEATURES_CANDLE + FEATURES_TIMING +
    FEATURES_CATEGORICAL + DERIVED_FEATURES
)

# Phase encoding for LightGBM categorical
PHASE_ENCODE = {
    "EARLY_ACCUM": 0, "ACTIVE_ACCUM": 1, "LATE_ACCUM": 2,
    "DISTRIBUTION": 3, "DECLINE": 4, "DORMANT": 5,
}


# ---------------------------------------------------------------------------
# DuckDB table
# ---------------------------------------------------------------------------

def _ensure_tables(conn: duckdb.DuckDBPyConnection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS swing_strategy_ml_predictions (
            ticker          TEXT NOT NULL,
            trade_date      DATE NOT NULL,
            strategy        TEXT NOT NULL,
            ml_prob_2r      DOUBLE,
            ml_percentile   DOUBLE,
            actual_hit_2r   BOOLEAN,
            actual_hit_stop BOOLEAN,
            max_favorable_r DOUBLE,
            hold_days       INTEGER,
            exit_type       TEXT,
            model_version   TEXT,
            computed_at     TIMESTAMP,
            PRIMARY KEY (ticker, trade_date, strategy)
        )
    """)


# ---------------------------------------------------------------------------
# Data loading & preparation
# ---------------------------------------------------------------------------

def load_training_data(
    strategy: str,
    quarters: List[str],
    conn: duckdb.DuckDBPyConnection,
) -> pd.DataFrame:
    """Load features + backtest outcomes joined for a strategy.

    Returns DataFrame with features + target columns.
    """
    q_list = "'" + "','".join(quarters) + "'"

    df = conn.execute(f"""
        SELECT
            f.*,
            b.entry_triggered,
            b.entry_price,
            b.stop_price,
            b.r_unit,
            b.hit_1r,
            b.hit_2r,
            b.hit_stop,
            b.max_favorable_r AS bt_max_favorable_r,
            b.max_adverse_r AS bt_max_adverse_r,
            b.hold_days AS bt_hold_days,
            b.exit_type AS bt_exit_type,
            b.exit_r AS bt_exit_r
        FROM fact_swing_features f
        INNER JOIN swing_backtest_results b
            ON f.ticker = b.ticker
            AND f.trade_date = b.trade_date
        WHERE b.strategy = '{strategy}'
          AND b.entry_triggered = TRUE
          AND b.report_quarter IN ({q_list})
          AND f.close IS NOT NULL
          AND f.close > 0
    """).fetchdf()

    logger.info("  {} training data: {} rows for quarters {}",
                strategy, len(df), quarters)
    return df


def prepare_features(
    df: pd.DataFrame,
) -> Tuple[pd.DataFrame, pd.Series, List[str]]:
    """Prepare feature matrix and target from raw joined data.

    Returns (X, y, feature_cols).
    """
    # Derive features
    if "atr_20" in df.columns and "close" in df.columns:
        df["atr_pct"] = df["atr_20"] / df["close"].replace(0, np.nan) * 100
    else:
        df["atr_pct"] = 0.0

    if "r_unit" in df.columns and "entry_price" in df.columns:
        df["r_unit_pct"] = df["r_unit"] / df["entry_price"].replace(0, np.nan) * 100
    else:
        df["r_unit_pct"] = 0.0

    if "int_squeeze_score" in df.columns and "conviction_score" in df.columns:
        df["squeeze_x_conviction"] = (
            df["int_squeeze_score"].fillna(0) * df["conviction_score"].fillna(0) / 100
        )
    else:
        df["squeeze_x_conviction"] = 0.0

    # Encode categoricals
    df["accum_phase"] = df["accum_phase"].map(PHASE_ENCODE).fillna(5).astype(int)

    # Sector encoding (simple integer)
    if "sector" in df.columns:
        sectors = sorted(df["sector"].dropna().unique())
        sector_map = {s: i for i, s in enumerate(sectors)}
        df["sector"] = df["sector"].map(sector_map).fillna(-1).astype(int)

    # Boolean -> int (fill NA first)
    bool_cols = FEATURES_INTEL_BINARY + FEATURES_CANDLE + ["squeeze_on", "rsi2_below_10", "volume_surge_3x"]
    for col in bool_cols:
        if col in df.columns:
            df[col] = df[col].fillna(False).astype(int)

    # Select feature columns
    feature_cols = [c for c in ALL_FEATURES if c in df.columns]

    X = df[feature_cols].copy()

    # Fill NaN with -999 for LightGBM (it handles missing natively but
    # we keep consistent)
    for col in feature_cols:
        if col not in FEATURES_CATEGORICAL:
            X[col] = X[col].fillna(np.nan)  # LightGBM handles NaN

    y = df["hit_2r"].astype(int)

    return X, y, feature_cols


# ---------------------------------------------------------------------------
# Training
# ---------------------------------------------------------------------------

def train_model(
    strategy: str,
    conn: duckdb.DuckDBPyConnection,
    quarters_train: Optional[List[str]] = None,
    quarters_val: Optional[List[str]] = None,
) -> Tuple[Optional[lgb.LGBMClassifier], Dict[str, Any]]:
    """Train LightGBM for one strategy.

    Returns (model, metrics) or (None, {}) if insufficient data.
    """
    quarters_train = quarters_train or QUARTERS_TRAIN_DEFAULT
    quarters_val = quarters_val or QUARTERS_VAL_DEFAULT

    logger.info("Training {} — train={}, val={}", strategy, quarters_train, quarters_val)

    df_train = load_training_data(strategy, quarters_train, conn)
    df_val = load_training_data(strategy, quarters_val, conn)

    if len(df_train) < MIN_SAMPLES:
        logger.warning("  {} — only {} train samples (need {}). Skipping.",
                       strategy, len(df_train), MIN_SAMPLES)
        return None, {}

    X_train, y_train, feature_cols = prepare_features(df_train)
    X_val, y_val, _ = prepare_features(df_val)
    # Ensure val has same columns
    for col in feature_cols:
        if col not in X_val.columns:
            X_val[col] = np.nan
    X_val = X_val[feature_cols]

    pos_rate = y_train.mean()
    scale = (1 - pos_rate) / pos_rate if pos_rate > 0 else 1.0

    logger.info("  Train: {:,} rows, {:.1%} positive (scale_pos_weight={:.2f})",
                len(X_train), pos_rate, scale)
    if len(df_val) > 0:
        logger.info("  Val: {:,} rows, {:.1%} positive", len(X_val), y_val.mean())

    params = {
        "objective": "binary",
        "metric": "auc",
        "n_estimators": 800,
        "max_depth": 5,
        "num_leaves": 31,
        "learning_rate": 0.03,
        "min_child_samples": 50,
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

    # Categorical feature indices
    cat_indices = [feature_cols.index(c) for c in FEATURES_CATEGORICAL if c in feature_cols]

    callbacks = [lgb.log_evaluation(100)]
    eval_set = []
    if len(X_val) > 0:
        eval_set = [(X_val, y_val)]
        callbacks.append(lgb.early_stopping(50, verbose=True))

    model.fit(
        X_train, y_train,
        eval_set=eval_set or None,
        categorical_feature=cat_indices if cat_indices else "auto",
        callbacks=callbacks,
    )

    # Compute metrics
    proba_train = model.predict_proba(X_train)[:, 1]
    metrics = {
        "strategy": strategy,
        "train_size": len(X_train),
        "train_pos_rate": float(pos_rate),
        "train_auc": float(roc_auc_score(y_train, proba_train)),
        "n_estimators_used": model.best_iteration_ or model.n_estimators,
        "feature_cols": feature_cols,
    }

    if len(X_val) > 0 and len(y_val.unique()) > 1:
        proba_val = model.predict_proba(X_val)[:, 1]
        metrics["val_size"] = len(X_val)
        metrics["val_pos_rate"] = float(y_val.mean())
        metrics["val_auc"] = float(roc_auc_score(y_val, proba_val))
        metrics["val_logloss"] = float(log_loss(y_val, proba_val))
    elif len(X_val) > 0:
        metrics["val_size"] = len(X_val)
        metrics["val_pos_rate"] = float(y_val.mean())
        metrics["val_auc"] = None
        logger.warning("  Val set has only one class — AUC not computed")

    logger.info("  Train AUC={:.4f}", metrics["train_auc"])
    if metrics.get("val_auc"):
        logger.info("  Val AUC={:.4f}", metrics["val_auc"])

    # Feature importance
    imp = pd.Series(model.feature_importances_, index=feature_cols).sort_values(ascending=False)
    metrics["top_features"] = imp.head(15).to_dict()
    logger.info("  Top 5 features: {}", list(imp.head(5).index))

    return model, metrics


# ---------------------------------------------------------------------------
# Calibration
# ---------------------------------------------------------------------------

def calibration_report(
    model: lgb.LGBMClassifier,
    df_val: pd.DataFrame,
    strategy: str,
    feature_cols: List[str],
) -> pd.DataFrame:
    """Generate calibration table: predicted probability buckets vs actual hit rates."""
    X_val, y_val, _ = prepare_features(df_val)
    for col in feature_cols:
        if col not in X_val.columns:
            X_val[col] = np.nan
    X_val = X_val[feature_cols]

    proba = model.predict_proba(X_val)[:, 1]

    # Percentile buckets
    percentiles = np.percentile(proba, [20, 40, 60, 80, 90, 95])
    labels = ["0-20%", "20-40%", "40-60%", "60-80%", "80-90%", "90-95%", "Top 5%"]

    def _bucket(p):
        if p <= percentiles[0]:
            return labels[0]
        elif p <= percentiles[1]:
            return labels[1]
        elif p <= percentiles[2]:
            return labels[2]
        elif p <= percentiles[3]:
            return labels[3]
        elif p <= percentiles[4]:
            return labels[4]
        elif p <= percentiles[5]:
            return labels[5]
        else:
            return labels[6]

    buckets = pd.Series([_bucket(p) for p in proba])

    report_rows = []
    for label in labels:
        mask = buckets == label
        n = mask.sum()
        if n == 0:
            continue
        actual_rate = float(y_val[mask].mean())
        avg_prob = float(proba[mask].mean())
        avg_mfe = float(df_val.loc[mask.values, "bt_max_favorable_r"].mean()) if "bt_max_favorable_r" in df_val.columns else None
        avg_exit_r = float(df_val.loc[mask.values, "bt_exit_r"].mean()) if "bt_exit_r" in df_val.columns else None

        report_rows.append({
            "strategy": strategy,
            "bucket": label,
            "n_trades": int(n),
            "avg_ml_prob": round(avg_prob, 4),
            "actual_2r_rate": round(actual_rate, 4),
            "avg_max_fav_r": round(avg_mfe, 2) if avg_mfe is not None else None,
            "avg_exit_r": round(avg_exit_r, 2) if avg_exit_r is not None else None,
        })

    return pd.DataFrame(report_rows)


# ---------------------------------------------------------------------------
# Model persistence
# ---------------------------------------------------------------------------

def _model_path(strategy: str) -> Path:
    return _MODELS_DIR / f"swing_strategy_ml_{strategy.lower()}.pkl"


def save_model(model: lgb.LGBMClassifier, metrics: Dict[str, Any],
               strategy: str) -> Path:
    _MODELS_DIR.mkdir(parents=True, exist_ok=True)
    path = _model_path(strategy)
    with open(path, "wb") as f:
        pickle.dump({"model": model, "metrics": metrics}, f)
    logger.info("Saved {} model to {}", strategy, path)
    return path


def load_model(strategy: str) -> Tuple[lgb.LGBMClassifier, Dict[str, Any]]:
    path = _model_path(strategy)
    with open(path, "rb") as f:
        data = pickle.load(f)
    return data["model"], data["metrics"]


# ---------------------------------------------------------------------------
# Scoring & writing predictions
# ---------------------------------------------------------------------------

def score_and_write(
    model: lgb.LGBMClassifier,
    strategy: str,
    quarters: List[str],
    conn: duckdb.DuckDBPyConnection,
    model_version: str = "v1",
) -> int:
    """Score all triggered entries for a strategy and write predictions."""
    _ensure_tables(conn)
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")

    df = load_training_data(strategy, quarters, conn)
    if df.empty:
        logger.warning("  No data to score for {} in {}", strategy, quarters)
        return 0

    metrics = load_model(strategy)[1]
    feature_cols = metrics.get("feature_cols", [c for c in ALL_FEATURES if c in df.columns])

    X, y, _ = prepare_features(df)
    for col in feature_cols:
        if col not in X.columns:
            X[col] = np.nan
    X = X[feature_cols]

    proba = model.predict_proba(X)[:, 1]

    # Percentile within this scoring set
    from scipy.stats import percentileofscore
    percentiles = np.array([percentileofscore(proba, p) for p in proba])

    preds = pd.DataFrame({
        "ticker": df["ticker"].values,
        "trade_date": df["trade_date"].values,
        "strategy": strategy,
        "ml_prob_2r": np.round(proba, 6),
        "ml_percentile": np.round(percentiles, 2),
        "actual_hit_2r": y.values.astype(bool),
        "actual_hit_stop": df["hit_stop"].values if "hit_stop" in df.columns else False,
        "max_favorable_r": df["bt_max_favorable_r"].values if "bt_max_favorable_r" in df.columns else None,
        "hold_days": df["bt_hold_days"].values if "bt_hold_days" in df.columns else None,
        "exit_type": df["bt_exit_type"].values if "bt_exit_type" in df.columns else None,
        "model_version": model_version,
        "computed_at": now_iso,
    })

    conn.register("_swing_ml_temp", preds)
    conn.execute(f"""
        DELETE FROM swing_strategy_ml_predictions
        WHERE strategy = '{strategy}'
    """)
    conn.execute("INSERT OR REPLACE INTO swing_strategy_ml_predictions SELECT * FROM _swing_ml_temp")
    conn.unregister("_swing_ml_temp")

    logger.info("  Wrote {:,} predictions for {} ({})", len(preds), strategy, quarters)
    return len(preds)


# ---------------------------------------------------------------------------
# Full pipeline
# ---------------------------------------------------------------------------

def run_train_pipeline(
    conn: duckdb.DuckDBPyConnection,
    strategies: List[str],
    quarters_train: Optional[List[str]] = None,
    quarters_val: Optional[List[str]] = None,
) -> Dict[str, Dict]:
    """Train all strategies and produce calibration reports."""
    all_metrics = {}

    for strategy in strategies:
        logger.info("\n{'='*50}")
        logger.info("TRAINING: {}", strategy)

        model, metrics = train_model(strategy, conn, quarters_train, quarters_val)

        if model is None:
            logger.warning("  Skipped {} — insufficient data", strategy)
            continue

        # Save model
        save_model(model, metrics, strategy)

        # Calibration report
        q_val = quarters_val or QUARTERS_VAL_DEFAULT
        df_val = load_training_data(strategy, q_val, conn)
        if len(df_val) > 0:
            feature_cols = metrics.get("feature_cols", [])
            cal = calibration_report(model, df_val, strategy, feature_cols)
            print(f"\n  CALIBRATION — {strategy}")
            print(f"  {'Bucket':<12} {'N':>6} {'ML Prob':>8} {'Actual 2R':>10} "
                  f"{'Avg MFE':>8} {'Avg R':>7}")
            print(f"  {'-'*12} {'-'*6} {'-'*8} {'-'*10} {'-'*8} {'-'*7}")
            for _, row in cal.iterrows():
                print(f"  {row['bucket']:<12} {row['n_trades']:>6} "
                      f"{row['avg_ml_prob']:>8.4f} {row['actual_2r_rate']:>9.1%} "
                      f"{row['avg_max_fav_r']:>8.2f} {row['avg_exit_r']:>7.2f}")

        # Score validation set
        if len(df_val) > 0:
            score_and_write(model, strategy, q_val, conn)

        all_metrics[strategy] = metrics

    # Summary
    print(f"\n{'='*60}")
    print(f"  TRAINING SUMMARY")
    print(f"{'='*60}")
    print(f"  {'Strategy':<20} {'Train':>6} {'Val':>6} {'Train AUC':>10} {'Val AUC':>10}")
    print(f"  {'-'*20} {'-'*6} {'-'*6} {'-'*10} {'-'*10}")
    for strat, m in all_metrics.items():
        t_auc = f"{m['train_auc']:.4f}" if m.get("train_auc") else "N/A"
        v_auc = f"{m['val_auc']:.4f}" if m.get("val_auc") else "N/A"
        print(f"  {strat:<20} {m.get('train_size', 0):>6,} {m.get('val_size', 0):>6,} "
              f"{t_auc:>10} {v_auc:>10}")
    print(f"{'='*60}")

    return all_metrics


def print_report(conn: duckdb.DuckDBPyConnection,
                 strategies: List[str]) -> None:
    """Print stored prediction statistics."""
    print(f"\n{'='*60}")
    print(f"  SWING STRATEGY ML — PREDICTION REPORT")
    print(f"{'='*60}")

    for strategy in strategies:
        df = conn.execute("""
            SELECT
                COUNT(*) AS n,
                AVG(ml_prob_2r) AS avg_prob,
                SUM(CASE WHEN actual_hit_2r THEN 1 ELSE 0 END) AS actual_2r,
                SUM(CASE WHEN actual_hit_stop THEN 1 ELSE 0 END) AS actual_stop,
                AVG(max_favorable_r) AS avg_mfe
            FROM swing_strategy_ml_predictions
            WHERE strategy = ?
        """, [strategy]).fetchone()

        if df[0] == 0:
            print(f"\n  {strategy}: No predictions found")
            continue

        n, avg_p, h2r, stp, mfe = df
        print(f"\n  {strategy}:")
        print(f"    Predictions: {n:,}")
        print(f"    Avg ML prob: {avg_p:.4f}")
        print(f"    Actual 2R: {h2r}/{n} ({h2r/n*100:.1f}%)")
        print(f"    Stopped: {stp}/{n} ({stp/n*100:.1f}%)")
        print(f"    Avg MFE: {mfe:.2f}R")

        # Top quintile
        top = conn.execute("""
            SELECT COUNT(*) AS n,
                   SUM(CASE WHEN actual_hit_2r THEN 1 ELSE 0 END) AS h2r
            FROM swing_strategy_ml_predictions
            WHERE strategy = ? AND ml_percentile >= 80
        """, [strategy]).fetchone()
        if top[0] > 0:
            print(f"    Top 20% (ml_pct>=80): {top[1]}/{top[0]} = {top[1]/top[0]*100:.1f}% hit 2R")

    print(f"\n{'='*60}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Swing Strategy ML — per-strategy LightGBM models"
    )
    parser.add_argument("--train", action="store_true", help="Train models")
    parser.add_argument("--report", action="store_true", help="Print prediction report")
    parser.add_argument(
        "--strategy", type=str, default="ALL",
        help="Strategy: SQUEEZE|MEAN_REV|GAP_DRIFT|INSIDER_BREAKOUT|ALL",
    )
    parser.add_argument(
        "--quarters-train", type=str, default=None,
        help="Training quarters (comma-separated)",
    )
    parser.add_argument(
        "--quarters-val", type=str, default=None,
        help="Validation quarters (comma-separated)",
    )
    args = parser.parse_args()

    strategies = ALL_STRATEGIES if args.strategy == "ALL" else [args.strategy.upper()]

    conn = duckdb.connect(str(WAREHOUSE_PATH))
    try:
        _ensure_tables(conn)

        if args.train:
            qt = [q.strip() for q in args.quarters_train.split(",")] if args.quarters_train else None
            qv = [q.strip() for q in args.quarters_val.split(",")] if args.quarters_val else None
            run_train_pipeline(conn, strategies, qt, qv)

        if args.report:
            print_report(conn, strategies)

        if not args.train and not args.report:
            parser.print_help()
    finally:
        conn.close()


if __name__ == "__main__":
    from signal_scanner.utils.logger import setup_logger
    setup_logger()
    main()
