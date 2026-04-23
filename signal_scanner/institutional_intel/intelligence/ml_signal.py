"""Phase 6: XGBoost ML Signal Classifier.

Trains a binary classifier to predict whether a stock will beat SPY
in the next 90 days (alpha_90d > 0) based on institutional intelligence features.

Feature Matrix (from intelligence_scores + agg_qoq_changes + agg_sector_rotation):
    conviction_score, accum_phase_encoded, accum_phase_quarters, accum_strength_score,
    tier1_manager_count, insider_cluster_detected, insider_net_buy_count, ceo_cfo_buying,
    cascade_stage, copycat_score, divergence_active, divergence_magnitude,
    manager_quality_score, insider_score, count_up_streak, inst_count_change_pct,
    value_change_pct, avg_price_change_pct, avg_volume_change_pct,
    sector_flow_pct, sector_inflow_streak

Target: alpha_90d > 0  (binary: beat SPY in 90 days)

Train:    backtest_results quarters 2020-Q2 → 2023-Q4
Validate: 2024-Q2 → 2024-Q3  (out-of-sample, skips contaminated 2024-Q1)
Model:    XGBoost, saved to data/models/ml_signal_v1.pkl

Usage:
    python -m signal_scanner.institutional_intel.intelligence.ml_signal --train
    python -m signal_scanner.institutional_intel.intelligence.ml_signal --validate
    python -m signal_scanner.institutional_intel.intelligence.ml_signal --score --quarter 2025-Q2
    python -m signal_scanner.institutional_intel.intelligence.ml_signal --score --quarter 2025-Q2 --write
"""

from __future__ import annotations

import argparse
import pickle
from pathlib import Path
from typing import List, Optional, Tuple

import duckdb
import numpy as np
import pandas as pd
from loguru import logger

from signal_scanner.institutional_intel.config import WAREHOUSE_PATH

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
_MODELS_DIR = WAREHOUSE_PATH.parents[1] / "models"
_MODEL_PATH = _MODELS_DIR / "ml_signal_v1.pkl"

# ---------------------------------------------------------------------------
# Phase encoding map
# ---------------------------------------------------------------------------
PHASE_ENCODE: dict[str, int] = {
    "ACTIVE_ACCUM":  5,
    "LATE_ACCUM":    4,
    "EARLY_ACCUM":   3,
    "EXPANSION":     2,
    "DORMANT":       1,
    "DISTRIBUTION":  0,
    "DECLINE":       0,
}

# Feature column names (order matters for model)
FEATURE_COLS: List[str] = [
    "conviction_score",
    "accum_phase_encoded",
    "accum_phase_quarters",
    "accum_strength_score",
    "tier1_manager_count",
    "insider_cluster_detected",
    "insider_net_buy_count",
    "ceo_cfo_buying",
    "cascade_stage",
    "copycat_score",
    "divergence_active",
    "divergence_magnitude",
    "manager_quality_score",
    "insider_score",
    "count_up_streak",
    "inst_count_change_pct",
    "value_change_pct",
    "avg_price_change_pct",
    "avg_volume_change_pct",
    "sector_flow_pct",
    "sector_inflow_streak",
]


# ---------------------------------------------------------------------------
# Feature extraction helpers
# ---------------------------------------------------------------------------

def _all_quarters_between(start_q: str, end_q: str) -> List[str]:
    """Return quarter strings from start to end inclusive."""
    quarters: List[str] = []
    year, qnum = int(start_q.split("-Q")[0]), int(start_q.split("-Q")[1])
    end_year, end_qnum = int(end_q.split("-Q")[0]), int(end_q.split("-Q")[1])
    while (year, qnum) <= (end_year, end_qnum):
        quarters.append(f"{year}-Q{qnum}")
        qnum += 1
        if qnum > 4:
            qnum, year = 1, year + 1
    return quarters


def _extract_features(
    conn: duckdb.DuckDBPyConnection,
    quarters: List[str],
    require_target: bool = True,
) -> pd.DataFrame:
    """Build feature DataFrame for the given quarters.

    When require_target=True, joins with backtest_results and filters to
    rows where alpha_90d is known (training/validation mode).
    When require_target=False, reads from intelligence_scores only (scoring mode).
    """
    q_list = "'" + "','".join(quarters) + "'"

    if require_target:
        df = conn.execute(f"""
            SELECT
                br.ticker,
                br.signal_quarter                            AS report_quarter,
                -- Use alpha_90d when available; fall back to absolute return_90d
                COALESCE(br.alpha_90d, br.return_90d)        AS target_return,
                br.alpha_90d,
                br.return_90d,
                COALESCE(i.conviction_score,     0)          AS conviction_score,
                COALESCE(i.accum_phase,   'DORMANT')         AS accum_phase,
                COALESCE(i.accum_phase_quarters,  0)         AS accum_phase_quarters,
                COALESCE(i.accum_strength_score,  0)         AS accum_strength_score,
                COALESCE(i.tier1_manager_count,   0)         AS tier1_manager_count,
                CASE WHEN i.insider_cluster_detected THEN 1 ELSE 0 END
                                                             AS insider_cluster_detected,
                COALESCE(i.insider_net_buy_count,  0)        AS insider_net_buy_count,
                CASE WHEN i.ceo_cfo_buying THEN 1 ELSE 0 END AS ceo_cfo_buying,
                COALESCE(i.cascade_stage,          0)        AS cascade_stage,
                COALESCE(i.copycat_score,          0)        AS copycat_score,
                CASE WHEN i.divergence_active THEN 1 ELSE 0 END
                                                             AS divergence_active,
                COALESCE(i.divergence_magnitude,   0)        AS divergence_magnitude,
                COALESCE(i.manager_quality_score,  0)        AS manager_quality_score,
                COALESCE(i.insider_score,          0)        AS insider_score,
                COALESCE(q.count_up_streak,        0)        AS count_up_streak,
                COALESCE(q.inst_count_change_pct,  0)        AS inst_count_change_pct,
                COALESCE(q.value_change_pct,       0)        AS value_change_pct,
                COALESCE(q.avg_price_change_pct,   0)        AS avg_price_change_pct,
                COALESCE(q.avg_volume_change_pct,  0)        AS avg_volume_change_pct,
                COALESCE(sr.flow_pct,              0)        AS sector_flow_pct,
                COALESCE(sr.inflow_streak,         0)        AS sector_inflow_streak
            FROM backtest_results br
            JOIN intelligence_scores i
                ON br.ticker = i.ticker AND br.signal_quarter = i.report_quarter
            LEFT JOIN agg_qoq_changes q
                ON br.ticker = q.ticker AND br.signal_quarter = q.current_quarter
            LEFT JOIN agg_sector_rotation sr
                ON q.sector = sr.sector AND br.signal_quarter = sr.report_quarter
            WHERE br.signal_quarter IN ({q_list})
              AND (br.alpha_90d IS NOT NULL OR br.return_90d IS NOT NULL)
              AND COALESCE(i.data_quality_score, 100) >= 75
        """).fetchdf()
    else:
        df = conn.execute(f"""
            SELECT
                i.ticker,
                i.report_quarter,
                NULL::DOUBLE                                  AS alpha_90d,
                COALESCE(i.conviction_score,     0)          AS conviction_score,
                COALESCE(i.accum_phase,   'DORMANT')         AS accum_phase,
                COALESCE(i.accum_phase_quarters,  0)         AS accum_phase_quarters,
                COALESCE(i.accum_strength_score,  0)         AS accum_strength_score,
                COALESCE(i.tier1_manager_count,   0)         AS tier1_manager_count,
                CASE WHEN i.insider_cluster_detected THEN 1 ELSE 0 END
                                                             AS insider_cluster_detected,
                COALESCE(i.insider_net_buy_count,  0)        AS insider_net_buy_count,
                CASE WHEN i.ceo_cfo_buying THEN 1 ELSE 0 END AS ceo_cfo_buying,
                COALESCE(i.cascade_stage,          0)        AS cascade_stage,
                COALESCE(i.copycat_score,          0)        AS copycat_score,
                CASE WHEN i.divergence_active THEN 1 ELSE 0 END
                                                             AS divergence_active,
                COALESCE(i.divergence_magnitude,   0)        AS divergence_magnitude,
                COALESCE(i.manager_quality_score,  0)        AS manager_quality_score,
                COALESCE(i.insider_score,          0)        AS insider_score,
                COALESCE(q.count_up_streak,        0)        AS count_up_streak,
                COALESCE(q.inst_count_change_pct,  0)        AS inst_count_change_pct,
                COALESCE(q.value_change_pct,       0)        AS value_change_pct,
                COALESCE(q.avg_price_change_pct,   0)        AS avg_price_change_pct,
                COALESCE(q.avg_volume_change_pct,  0)        AS avg_volume_change_pct,
                COALESCE(sr.flow_pct,              0)        AS sector_flow_pct,
                COALESCE(sr.inflow_streak,         0)        AS sector_inflow_streak
            FROM intelligence_scores i
            LEFT JOIN agg_qoq_changes q
                ON i.ticker = q.ticker AND i.report_quarter = q.current_quarter
            LEFT JOIN agg_sector_rotation sr
                ON q.sector = sr.sector AND i.report_quarter = sr.report_quarter
            WHERE i.report_quarter IN ({q_list})
              AND COALESCE(i.data_quality_score, 100) >= 75
        """).fetchdf()

    if df.empty:
        return df

    # Encode phase → integer
    df["accum_phase_encoded"] = df["accum_phase"].map(PHASE_ENCODE).fillna(1).astype(int)

    # Clip outliers — price/volume pct changes can be extreme
    for col in ["avg_price_change_pct", "avg_volume_change_pct",
                "inst_count_change_pct", "value_change_pct", "sector_flow_pct"]:
        if col in df.columns:
            df[col] = df[col].clip(-200, 200)

    # Fill any remaining NaN with 0
    df[FEATURE_COLS] = df[FEATURE_COLS].fillna(0)

    return df


# ---------------------------------------------------------------------------
# Model training
# ---------------------------------------------------------------------------

def train_model(
    conn: duckdb.DuckDBPyConnection,
    train_start: str = "2020-Q2",
    train_end: str = "2022-Q4",
    # Held-out validation: latest 4 quarters of backtest range
    val_start: str = "2023-Q1",
    val_end: str = "2023-Q4",
) -> dict:
    """Train XGBoost on training quarters, validate on held-out set.

    Temporal split: train 2020-Q2 → 2022-Q4, validate 2023-Q1 → 2023-Q4.
    Returns a dict with model, feature importances, and evaluation metrics.
    """
    try:
        import xgboost as xgb
    except ImportError as e:
        raise ImportError(
            "xgboost is required: pip install xgboost"
        ) from e

    train_quarters = _all_quarters_between(train_start, train_end)
    # Skip contaminated quarter
    train_quarters = [q for q in train_quarters if q not in ("2024-Q1", "2025-Q3")]
    val_quarters = _all_quarters_between(val_start, val_end)
    val_quarters = [q for q in val_quarters if q not in ("2024-Q1", "2025-Q3")]

    logger.info("Extracting training features: {} quarters", len(train_quarters))
    train_df = _extract_features(conn, train_quarters, require_target=True)
    logger.info("Training set: {} rows", len(train_df))

    logger.info("Extracting validation features: {} quarters", len(val_quarters))
    val_df = _extract_features(conn, val_quarters, require_target=True)
    logger.info("Validation set: {} rows", len(val_df))

    if train_df.empty:
        raise RuntimeError(
            "No training data found. Run backtest --run first to populate backtest_results."
        )

    X_train = train_df[FEATURE_COLS].values.astype(np.float32)
    # Use alpha_90d when available, else absolute return_90d
    target_col = "target_return"
    using_alpha = train_df["alpha_90d"].notna().any()
    label = "alpha_90d" if using_alpha else "return_90d"
    logger.info("Target: {} > 0  (using {} rows with valid data)", label, len(train_df))
    y_train = (train_df[target_col] > 0).astype(int).values

    # Class balance
    pos_rate = y_train.mean()
    scale_pos = (1 - pos_rate) / max(pos_rate, 0.01)
    logger.info("Training class balance: {:.1f}% positive ({} > 0)", pos_rate * 100, label)

    model = xgb.XGBClassifier(
        n_estimators=200,
        max_depth=4,
        learning_rate=0.05,
        subsample=0.8,
        colsample_bytree=0.8,
        min_child_weight=5,
        scale_pos_weight=scale_pos,
        eval_metric="auc",
        random_state=42,
        n_jobs=-1,
        verbosity=0,
    )

    if not val_df.empty:
        X_val = val_df[FEATURE_COLS].values.astype(np.float32)
        y_val = (val_df[target_col] > 0).astype(int).values
        model.fit(
            X_train, y_train,
            eval_set=[(X_val, y_val)],
            verbose=False,
        )
    else:
        logger.warning("No validation data available — training without eval set")
        model.fit(X_train, y_train)

    # --- Evaluation ---
    from sklearn.metrics import roc_auc_score, accuracy_score

    train_proba = model.predict_proba(X_train)[:, 1]
    train_auc = roc_auc_score(y_train, train_proba)
    train_acc = accuracy_score(y_train, (train_proba >= 0.5).astype(int))
    logger.info("Train  AUC={:.3f}  Acc={:.1f}%", train_auc, train_acc * 100)

    val_metrics: dict = {}
    if not val_df.empty:
        val_proba = model.predict_proba(X_val)[:, 1]
        val_auc = roc_auc_score(y_val, val_proba)
        val_acc = accuracy_score(y_val, (val_proba >= 0.5).astype(int))
        logger.info("Val    AUC={:.3f}  Acc={:.1f}%  n={}", val_auc, val_acc * 100, len(y_val))
        val_metrics = {"val_auc": val_auc, "val_accuracy": val_acc, "val_n": len(y_val)}

    # Feature importance
    importance = dict(zip(FEATURE_COLS, model.feature_importances_.tolist()))
    top5 = sorted(importance.items(), key=lambda x: x[1], reverse=True)[:5]
    logger.info("Top features: {}", ", ".join(f"{k}={v:.3f}" for k, v in top5))

    # Save model + metadata
    _MODELS_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "model": model,
        "feature_cols": FEATURE_COLS,
        "phase_encode": PHASE_ENCODE,
        "train_quarters": train_quarters,
        "val_quarters": val_quarters,
        "train_n": len(train_df),
        "train_auc": train_auc,
        "train_accuracy": train_acc,
        **val_metrics,
        "feature_importance": importance,
    }
    with open(_MODEL_PATH, "wb") as f:
        pickle.dump(payload, f)
    logger.info("Model saved to {}", _MODEL_PATH)

    return payload


# ---------------------------------------------------------------------------
# Validation summary
# ---------------------------------------------------------------------------

def validate_model(conn: duckdb.DuckDBPyConnection) -> None:
    """Print precision/recall/win-rate by ml_score bucket on the held-out validation set."""
    try:
        from sklearn.metrics import classification_report
    except ImportError:
        logger.warning("scikit-learn not available — install for full validation report")
        classification_report = None  # type: ignore[assignment]

    payload = _load_model()
    model = payload["model"]
    val_quarters = payload.get("val_quarters") or ["2023-Q1", "2023-Q2", "2023-Q3", "2023-Q4"]

    df = _extract_features(conn, val_quarters, require_target=True)
    if df.empty:
        print("No validation data. Run backtest --run first.")
        return

    X = df[FEATURE_COLS].values.astype(np.float32)
    using_alpha = df["alpha_90d"].notna().any()
    target_col = "alpha_90d" if using_alpha else "return_90d"
    perf_col = "target_return"
    y = (df[perf_col] > 0).astype(int).values
    proba = model.predict_proba(X)[:, 1]
    df["ml_score"] = (proba * 100).round(1)
    df["target"] = y

    target_label = "alpha_90d>0 vs SPY" if using_alpha else "return_90d>0 (no SPY benchmark)"
    print(f"\n{'='*65}")
    print(f"  ML SIGNAL VALIDATION  ({len(df):,} signals, {len(val_quarters)} quarters)")
    print(f"  Target: {target_label}")
    print(f"{'='*65}")

    if classification_report is not None:
        print("\nClassification Report (threshold=0.5):")
        preds = (proba >= 0.5).astype(int)
        class_names = ["SPY-lag", "Beat-SPY"] if using_alpha else ["Negative", "Positive"]
        print(classification_report(y, preds, target_names=class_names))

    win_label = "alpha_90d > 0 = win vs SPY" if using_alpha else "return_90d > 0 = positive return"
    print(f"\nWin Rate by ML Score Bucket ({win_label}):")
    bins = [0, 30, 50, 65, 80, 101]
    labels = ["0-30", "30-50", "50-65", "65-80", "80-100"]
    df["bucket"] = pd.cut(df["ml_score"], bins=bins, labels=labels, right=False)
    for label in labels:
        subset = df[df["bucket"] == label]
        if subset.empty:
            continue
        win_rate = subset["target"].mean() * 100
        avg_ret = subset[perf_col].mean()
        print(f"  {label:10s}  n={len(subset):5d}  WinRate={win_rate:5.1f}%  AvgReturn={avg_ret:+.2f}%")

    overall_wr = df["target"].mean() * 100
    print(f"\n  Overall baseline win rate: {overall_wr:.1f}%")
    print(f"{'='*65}\n")


# ---------------------------------------------------------------------------
# Scoring (inference)
# ---------------------------------------------------------------------------

def _load_model() -> dict:
    """Load trained model payload from disk."""
    if not _MODEL_PATH.exists():
        raise FileNotFoundError(
            f"Model not found at {_MODEL_PATH}. Run --train first."
        )
    with open(_MODEL_PATH, "rb") as f:
        return pickle.load(f)


def score_quarter(
    conn: duckdb.DuckDBPyConnection,
    quarter: str,
    write_to_db: bool = False,
) -> pd.DataFrame:
    """Score all tickers in a quarter. Returns DataFrame with ticker + ml_score.

    If write_to_db=True, updates intelligence_scores.ml_score in place.
    """
    payload = _load_model()
    model = payload["model"]
    expected_features = payload.get("feature_cols", FEATURE_COLS)

    df = _extract_features(conn, [quarter], require_target=False)
    if df.empty:
        logger.warning("No data for quarter={}", quarter)
        return pd.DataFrame(columns=["ticker", "ml_score"])

    X = df[expected_features].values.astype(np.float32)
    proba = model.predict_proba(X)[:, 1]
    df["ml_score"] = (proba * 100).round(1)

    result = df[["ticker", "report_quarter", "ml_score"]].copy()
    result = result.sort_values("ml_score", ascending=False).reset_index(drop=True)

    if write_to_db:
        _ensure_ml_score_column(conn)
        rows = [(float(r["ml_score"]), r["ticker"], quarter) for _, r in result.iterrows()]
        conn.executemany(
            "UPDATE intelligence_scores SET ml_score = ? "
            "WHERE ticker = ? AND report_quarter = ?",
            rows,
        )
        logger.info("Wrote ml_score for {} tickers in quarter={}", len(rows), quarter)

    return result


def _ensure_ml_score_column(conn: duckdb.DuckDBPyConnection) -> None:
    """Add ml_score column to intelligence_scores if it doesn't exist."""
    try:
        conn.execute("SELECT ml_score FROM intelligence_scores LIMIT 0")
    except duckdb.BinderException:
        conn.execute("ALTER TABLE intelligence_scores ADD COLUMN ml_score REAL DEFAULT 0.0")
        logger.info("Added ml_score column to intelligence_scores")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="ML Signal Classifier")
    parser.add_argument("--train",    action="store_true", help="Train model on historical data")
    parser.add_argument("--validate", action="store_true", help="Print validation report")
    parser.add_argument("--score",    action="store_true", help="Score a quarter")
    parser.add_argument("--quarter",  default=None,        help="Quarter to score (e.g. 2025-Q2)")
    parser.add_argument("--write",    action="store_true", help="Write ml_score back to DuckDB")
    parser.add_argument("--train-start", default="2020-Q2")
    parser.add_argument("--train-end",   default="2023-Q4")
    parser.add_argument("--val-start",   default="2024-Q2")
    parser.add_argument("--val-end",     default="2024-Q3")
    args = parser.parse_args()

    conn = duckdb.connect(str(WAREHOUSE_PATH))
    try:
        if args.train:
            result = train_model(
                conn,
                train_start=args.train_start,
                train_end=args.train_end,
                val_start=args.val_start,
                val_end=args.val_end,
            )
            print(f"\nTraining complete:")
            print(f"  Train  AUC={result['train_auc']:.3f}  Acc={result['train_accuracy']*100:.1f}%  n={result['train_n']:,}")
            if "val_auc" in result:
                print(f"  Val    AUC={result['val_auc']:.3f}  Acc={result['val_accuracy']*100:.1f}%  n={result['val_n']:,}")
            print(f"\nTop feature importances:")
            top10 = sorted(result["feature_importance"].items(), key=lambda x: x[1], reverse=True)[:10]
            for feat, imp in top10:
                bar = "█" * int(imp * 50)
                print(f"  {feat:35s}  {imp:.4f}  {bar}")

        if args.validate:
            validate_model(conn)

        if args.score:
            if not args.quarter:
                # Default to latest clean quarter
                row = conn.execute("""
                    SELECT report_quarter FROM intelligence_scores
                    WHERE COALESCE(data_quality_score, 100) >= 75
                    GROUP BY report_quarter HAVING COUNT(*) >= 500
                    ORDER BY report_quarter DESC LIMIT 1
                """).fetchone()
                args.quarter = row[0] if row else "2025-Q2"
                logger.info("No --quarter specified; using {}", args.quarter)

            df = score_quarter(conn, args.quarter, write_to_db=args.write)
            if df.empty:
                print(f"No data for quarter={args.quarter}")
            else:
                print(f"\nML Scores for {args.quarter}  ({len(df):,} tickers)")
                print(f"{'Rank':>4}  {'Ticker':6s}  {'ML Score':>8}")
                print("-" * 30)
                for i, (_, row) in enumerate(df.head(25).iterrows(), 1):
                    print(f"  {i:3d}  {str(row['ticker']):6s}  {row['ml_score']:8.1f}")
                if args.write:
                    print(f"\nml_score written to intelligence_scores for {args.quarter}")

        if not any([args.train, args.validate, args.score]):
            parser.print_help()
    finally:
        conn.close()


if __name__ == "__main__":
    main()
