"""ML Signal Classifier v2 — Enhanced with Form 4 Insider + Price Momentum.

Adds 7 new features over v1:
  Form 4 (60-day window, open-market only, no lookahead):
    f4_open_buy_count_60d    — # open-market purchases by insiders
    f4_distinct_insiders_60d — # distinct insiders buying open-market
    f4_officer_buy_count_60d — # Officer-role open-market buyers
    f4_net_dollar_log        — log-signed net insider dollar flow
    f4_buy_pressure_60d      — buys / (buys+sells) ratio

  Price (as of entry date, no lookahead):
    price_momentum_90d       — 90-day price return at entry
    price_above_200sma       — 1 if price > 200-day SMA

Model: XGBoost with stronger regularisation (depth=3, lambda=2.0)
       to reduce overfitting gap seen in v1 (0.725 train vs 0.533 val).

Train:    2020-Q2 → 2022-Q4  (same as v1)
Validate: 2023-Q1 → 2023-Q4  (held-out)
Save:     data/models/ml_signal_v2.pkl

Usage:
    python -m signal_scanner.institutional_intel.intelligence.ml_signal_v2 --train
    python -m signal_scanner.institutional_intel.intelligence.ml_signal_v2 --validate
    python -m signal_scanner.institutional_intel.intelligence.ml_signal_v2 --score --quarter 2025-Q1 --write
"""

from __future__ import annotations

import argparse
import math
import pickle
from datetime import date, timedelta
from pathlib import Path
from typing import List, Optional

import duckdb
import numpy as np
import pandas as pd
from loguru import logger

from signal_scanner.institutional_intel.config import WAREHOUSE_PATH
from signal_scanner.institutional_intel.intelligence.ml_signal import (
    PHASE_ENCODE,
    FEATURE_COLS as FEATURE_COLS_V1,
    _all_quarters_between,
    _ensure_ml_score_column,
)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
_MODELS_DIR = WAREHOUSE_PATH.parents[1] / "models"
_MODEL_PATH_V2 = _MODELS_DIR / "ml_signal_v2.pkl"

# ---------------------------------------------------------------------------
# Feature columns — v1 baseline + 7 new signals
# ---------------------------------------------------------------------------
FEATURE_COLS_V2: List[str] = FEATURE_COLS_V1 + [
    # Form 4 open-market insider activity (60-day window)
    "f4_open_buy_count_60d",
    "f4_distinct_insiders_60d",
    "f4_officer_buy_count_60d",
    "f4_net_dollar_log",
    "f4_buy_pressure_60d",
    # Price momentum
    "price_momentum_90d",
    "price_above_200sma",
]


# ---------------------------------------------------------------------------
# Form 4 feature SQL helpers
# ---------------------------------------------------------------------------

_F4_CTE = """
    f4_clean AS (
        -- Open-market transactions only (P=purchase, S=sale), realistic dates
        SELECT
            ticker,
            transaction_date,
            direction,
            shares * COALESCE(NULLIF(price, 0), 1.0) AS dollar_value,
            insider_name,
            insider_role
        FROM fact_form4_transactions
        WHERE transaction_code IN ('P', 'S')
          AND transaction_date BETWEEN '2016-01-01' AND '2026-12-31'
          AND ticker IS NOT NULL AND ticker != ''
          AND shares > 0
    ),
    f4_agg AS (
        SELECT
            d.ticker,
            d.ref_date,
            COUNT(CASE WHEN f.direction = 'BUY' THEN 1 END)
                AS f4_open_buy_count_60d,
            COUNT(DISTINCT CASE WHEN f.direction = 'BUY' THEN f.insider_name END)
                AS f4_distinct_insiders_60d,
            COUNT(CASE WHEN f.direction = 'BUY'
                        AND UPPER(f.insider_role) LIKE '%OFFICER%' THEN 1 END)
                AS f4_officer_buy_count_60d,
            SUM(CASE WHEN f.direction = 'BUY'  THEN  f.dollar_value
                     WHEN f.direction = 'SELL' THEN -f.dollar_value
                     ELSE 0 END)
                AS f4_net_dollar_raw,
            COUNT(CASE WHEN f.direction = 'BUY' THEN 1 END) * 1.0
                / NULLIF(COUNT(*), 0)
                AS f4_buy_pressure_60d
        FROM ref_dates d
        LEFT JOIN f4_clean f
            ON  f.ticker = d.ticker
            AND f.transaction_date >= d.ref_date - INTERVAL '60 days'
            AND f.transaction_date <= d.ref_date
        GROUP BY d.ticker, d.ref_date
    )
"""

_PRICE_CTE = """
    price_with_sma AS (
        SELECT
            ticker,
            trade_date,
            close,
            AVG(close) OVER (
                PARTITION BY ticker
                ORDER BY trade_date
                ROWS BETWEEN 199 PRECEDING AND CURRENT ROW
            ) AS sma200
        FROM fact_daily_prices
        WHERE ticker IS NOT NULL AND close IS NOT NULL AND close > 0
    ),
    momentum_agg AS (
        SELECT
            d.ticker,
            d.ref_date,
            p_now.close                                             AS price_now,
            p_now.sma200                                            AS sma200_now,
            CASE WHEN p_now.close > p_now.sma200 THEN 1.0 ELSE 0.0 END
                                                                    AS price_above_200sma,
            CASE WHEN p_90.close IS NOT NULL AND p_90.close > 0
                 THEN (p_now.close - p_90.close) / p_90.close * 100
                 ELSE 0.0 END                                       AS price_momentum_90d
        FROM ref_dates d
        -- Closest price on or before entry date
        LEFT JOIN price_with_sma p_now
            ON p_now.ticker = d.ticker
            AND p_now.trade_date = (
                SELECT MAX(trade_date) FROM fact_daily_prices
                WHERE ticker = d.ticker AND trade_date <= d.ref_date
            )
        -- Closest price ~90 days prior
        LEFT JOIN price_with_sma p_90
            ON p_90.ticker = d.ticker
            AND p_90.trade_date = (
                SELECT MAX(trade_date) FROM fact_daily_prices
                WHERE ticker = d.ticker
                  AND trade_date <= d.ref_date - INTERVAL '85 days'
            )
    )
"""


def _log_signed(x: Optional[float]) -> float:
    """log1p of absolute value, preserving sign. Returns 0 for None/NaN."""
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return 0.0
    return math.copysign(math.log1p(abs(x)), x)


def _apply_v2_transforms(df: pd.DataFrame) -> pd.DataFrame:
    """Apply encoding and clipping to all feature columns."""
    df["accum_phase_encoded"] = df["accum_phase"].map(PHASE_ENCODE).fillna(1).astype(int)

    # Clip outliers on percentage columns
    pct_cols = [
        "avg_price_change_pct", "avg_volume_change_pct",
        "inst_count_change_pct", "value_change_pct", "sector_flow_pct",
        "price_momentum_90d",
    ]
    for col in pct_cols:
        if col in df.columns:
            df[col] = df[col].clip(-200, 200)

    # Log-sign transform on net insider dollar flow
    if "f4_net_dollar_raw" in df.columns:
        df["f4_net_dollar_log"] = df["f4_net_dollar_raw"].apply(_log_signed)
        df.drop(columns=["f4_net_dollar_raw"], inplace=True)

    # Ensure all feature columns exist
    for col in FEATURE_COLS_V2:
        if col not in df.columns:
            df[col] = 0.0

    df[FEATURE_COLS_V2] = df[FEATURE_COLS_V2].fillna(0)
    return df


# ---------------------------------------------------------------------------
# Feature extraction — TRAINING mode (backtest rows with entry_date)
# ---------------------------------------------------------------------------

def _extract_features_v2_train(
    conn: duckdb.DuckDBPyConnection,
    quarters: List[str],
) -> pd.DataFrame:
    """Build training feature DataFrame including Form 4 + momentum.

    Uses backtest_results.entry_date as the reference date for all
    forward-looking-safe features (no lookahead bias).
    """
    q_list = "'" + "','".join(quarters) + "'"

    logger.info("Building v2 training features for {} quarters (includes Form 4 + momentum)...", len(quarters))

    df = conn.execute(f"""
        WITH ref_dates AS (
            -- One row per (ticker, entry_date) from backtest
            SELECT DISTINCT ticker, entry_date AS ref_date
            FROM backtest_results
            WHERE signal_quarter IN ({q_list})
        ),
        {_F4_CTE},
        {_PRICE_CTE}
        SELECT
            br.ticker,
            br.signal_quarter                            AS report_quarter,
            br.entry_date,
            COALESCE(br.alpha_90d, br.return_90d)        AS target_return,
            br.alpha_90d,
            br.return_90d,
            -- v1 features
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
            COALESCE(sr.inflow_streak,         0)        AS sector_inflow_streak,
            -- v2 Form 4 features (60-day window ending at entry_date)
            COALESCE(f4.f4_open_buy_count_60d,    0)     AS f4_open_buy_count_60d,
            COALESCE(f4.f4_distinct_insiders_60d, 0)     AS f4_distinct_insiders_60d,
            COALESCE(f4.f4_officer_buy_count_60d, 0)     AS f4_officer_buy_count_60d,
            COALESCE(f4.f4_net_dollar_raw,        0)     AS f4_net_dollar_raw,
            COALESCE(f4.f4_buy_pressure_60d,      0.5)   AS f4_buy_pressure_60d,
            -- v2 Price momentum features
            COALESCE(m.price_momentum_90d,        0)     AS price_momentum_90d,
            COALESCE(m.price_above_200sma,        0)     AS price_above_200sma
        FROM backtest_results br
        JOIN intelligence_scores i
            ON br.ticker = i.ticker AND br.signal_quarter = i.report_quarter
        LEFT JOIN agg_qoq_changes q
            ON br.ticker = q.ticker AND br.signal_quarter = q.current_quarter
        LEFT JOIN agg_sector_rotation sr
            ON q.sector = sr.sector AND br.signal_quarter = sr.report_quarter
        LEFT JOIN f4_agg f4
            ON f4.ticker = br.ticker AND f4.ref_date = br.entry_date
        LEFT JOIN momentum_agg m
            ON m.ticker = br.ticker AND m.ref_date = br.entry_date
        WHERE br.signal_quarter IN ({q_list})
          AND (br.alpha_90d IS NOT NULL OR br.return_90d IS NOT NULL)
          AND COALESCE(i.data_quality_score, 100) >= 75
    """).fetchdf()

    if df.empty:
        return df

    return _apply_v2_transforms(df)


# ---------------------------------------------------------------------------
# Feature extraction — SCORING mode (current quarter, reference = today)
# ---------------------------------------------------------------------------

def _extract_features_v2_score(
    conn: duckdb.DuckDBPyConnection,
    quarters: List[str],
    ref_date: Optional[date] = None,
) -> pd.DataFrame:
    """Build scoring features for production use.

    ref_date: the 'today' date for Form 4 and price lookbacks.
              Defaults to yesterday (most recent complete trading day).
    """
    if ref_date is None:
        ref_date = date.today() - timedelta(days=1)

    q_list = "'" + "','".join(quarters) + "'"
    ref_str = ref_date.isoformat()

    logger.info("Building v2 scoring features for {} (ref_date={})", quarters, ref_str)

    df = conn.execute(f"""
        WITH ref_dates AS (
            -- One row per ticker using today as the reference date
            SELECT ticker, DATE '{ref_str}' AS ref_date
            FROM intelligence_scores
            WHERE report_quarter IN ({q_list})
              AND COALESCE(data_quality_score, 100) >= 75
        ),
        {_F4_CTE},
        {_PRICE_CTE}
        SELECT
            i.ticker,
            i.report_quarter,
            -- v1 features
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
            COALESCE(sr.inflow_streak,         0)        AS sector_inflow_streak,
            -- v2 Form 4 features (60-day window ending today)
            COALESCE(f4.f4_open_buy_count_60d,    0)     AS f4_open_buy_count_60d,
            COALESCE(f4.f4_distinct_insiders_60d, 0)     AS f4_distinct_insiders_60d,
            COALESCE(f4.f4_officer_buy_count_60d, 0)     AS f4_officer_buy_count_60d,
            COALESCE(f4.f4_net_dollar_raw,        0)     AS f4_net_dollar_raw,
            COALESCE(f4.f4_buy_pressure_60d,      0.5)   AS f4_buy_pressure_60d,
            -- v2 Price momentum features
            COALESCE(m.price_momentum_90d,        0)     AS price_momentum_90d,
            COALESCE(m.price_above_200sma,        0)     AS price_above_200sma
        FROM intelligence_scores i
        LEFT JOIN agg_qoq_changes q
            ON i.ticker = q.ticker AND i.report_quarter = q.current_quarter
        LEFT JOIN agg_sector_rotation sr
            ON q.sector = sr.sector AND i.report_quarter = sr.report_quarter
        LEFT JOIN f4_agg f4
            ON f4.ticker = i.ticker
            AND f4.ref_date = DATE '{ref_str}'
        LEFT JOIN momentum_agg m
            ON m.ticker = i.ticker
            AND m.ref_date = DATE '{ref_str}'
        WHERE i.report_quarter IN ({q_list})
          AND COALESCE(i.data_quality_score, 100) >= 75
    """).fetchdf()

    if df.empty:
        return df

    return _apply_v2_transforms(df)


# ---------------------------------------------------------------------------
# Training
# ---------------------------------------------------------------------------

def train_model_v2(
    conn: duckdb.DuckDBPyConnection,
    train_start: str = "2020-Q2",
    train_end: str = "2022-Q4",
    val_start: str = "2023-Q1",
    val_end: str = "2023-Q4",
) -> dict:
    """Train XGBoost v2 with Form 4 + momentum features.

    Key changes vs v1:
    - 28 features (21 original + 7 new)
    - max_depth=3, reg_lambda=2.0, reg_alpha=0.1 (reduce overfitting)
    - early_stopping_rounds=30
    """
    try:
        import xgboost as xgb
    except ImportError as e:
        raise ImportError("xgboost is required: pip install xgboost") from e

    train_quarters = _all_quarters_between(train_start, train_end)
    train_quarters = [q for q in train_quarters if q not in ("2024-Q1", "2025-Q3")]
    val_quarters = _all_quarters_between(val_start, val_end)
    val_quarters = [q for q in val_quarters if q not in ("2024-Q1", "2025-Q3")]

    logger.info("Extracting v2 training features: {} quarters", len(train_quarters))
    train_df = _extract_features_v2_train(conn, train_quarters)
    logger.info("Training set: {} rows  ({} features)", len(train_df), len(FEATURE_COLS_V2))

    if train_df.empty:
        raise RuntimeError("No training data. Run backtest --run first.")

    logger.info("Extracting v2 validation features: {} quarters", len(val_quarters))
    val_df = _extract_features_v2_train(conn, val_quarters)
    logger.info("Validation set: {} rows", len(val_df))

    X_train = train_df[FEATURE_COLS_V2].values.astype(np.float32)
    y_train = (train_df["target_return"] > 0).astype(int).values

    pos_rate = y_train.mean()
    scale_pos = (1 - pos_rate) / max(pos_rate, 0.01)
    logger.info("Training class balance: {:.1f}% positive (alpha_90d > 0)", pos_rate * 100)

    # Tuned regularisation: balance overfitting control vs score spread.
    # v2.1: relaxed from v2.0 (depth=3/lambda=2) which compressed scores
    # into 30-65 range — need wider spread for Triple Lock gating.
    model = xgb.XGBClassifier(
        n_estimators=600,
        max_depth=4,            # depth=4 allows sparse F4 features to split
        learning_rate=0.03,
        subsample=0.75,
        colsample_bytree=0.75,
        min_child_weight=5,     # allow splits on sparser F4 features (4.5% coverage)
        reg_lambda=1.0,         # relaxed from 2.0 for wider score spread
        reg_alpha=0.1,
        scale_pos_weight=scale_pos,
        eval_metric="auc",
        early_stopping_rounds=40,
        random_state=42,
        n_jobs=-1,
        verbosity=0,
    )

    if not val_df.empty:
        X_val = val_df[FEATURE_COLS_V2].values.astype(np.float32)
        y_val = (val_df["target_return"] > 0).astype(int).values
        model.fit(X_train, y_train, eval_set=[(X_val, y_val)], verbose=False)
    else:
        logger.warning("No validation data — training without early stopping")
        model.set_params(early_stopping_rounds=None)
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
    importance = dict(zip(FEATURE_COLS_V2, model.feature_importances_.tolist()))
    top8 = sorted(importance.items(), key=lambda x: x[1], reverse=True)[:8]
    logger.info("Top features: {}", ", ".join(f"{k}={v:.3f}" for k, v in top8))

    _MODELS_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "model": model,
        "feature_cols": FEATURE_COLS_V2,
        "phase_encode": PHASE_ENCODE,
        "train_quarters": train_quarters,
        "val_quarters": val_quarters,
        "train_n": len(train_df),
        "train_auc": train_auc,
        "train_accuracy": train_acc,
        "version": "v2",
        **val_metrics,
        "feature_importance": importance,
    }
    with open(_MODEL_PATH_V2, "wb") as f:
        pickle.dump(payload, f)
    logger.info("ML v2 model saved to {}", _MODEL_PATH_V2)
    return payload


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate_model_v2(conn: duckdb.DuckDBPyConnection) -> None:
    """Print v2 win-rate by ml_score_v2 bucket on held-out validation set."""
    try:
        from sklearn.metrics import classification_report
    except ImportError:
        classification_report = None  # type: ignore[assignment]

    payload = _load_model_v2()
    model = payload["model"]
    val_quarters = payload.get("val_quarters") or ["2023-Q1", "2023-Q2", "2023-Q3", "2023-Q4"]

    df = _extract_features_v2_train(conn, val_quarters)
    if df.empty:
        print("No validation data.")
        return

    X = df[FEATURE_COLS_V2].values.astype(np.float32)
    y = (df["target_return"] > 0).astype(int).values
    proba = model.predict_proba(X)[:, 1]
    # Percentile rank per quarter (same logic as production scoring)
    df["ml_score_v2"] = (
        pd.Series(proba).rank(pct=True).values * 100
    ).round(1)
    df["target"] = y

    using_alpha = df["alpha_90d"].notna().any()
    target_label = "alpha_90d>0 vs SPY" if using_alpha else "return_90d>0"

    from sklearn.metrics import roc_auc_score
    val_auc = roc_auc_score(y, proba)

    print(f"\n{'='*65}")
    print(f"  ML SIGNAL v2 VALIDATION  ({len(df):,} signals, {len(val_quarters)} quarters)")
    print(f"  Target: {target_label}")
    print(f"  Features: {len(FEATURE_COLS_V2)} (v1={len(FEATURE_COLS_V1)} + 7 new)")
    print(f"  Val AUC (raw proba): {val_auc:.3f}")
    print(f"  Scoring: cross-sectional percentile rank (0-100)")
    print(f"{'='*65}")

    if classification_report is not None:
        preds = (proba >= 0.5).astype(int)
        class_names = ["SPY-lag", "Beat-SPY"] if using_alpha else ["Negative", "Positive"]
        print("\nClassification Report (threshold=0.5 on raw proba):")
        print(classification_report(y, preds, target_names=class_names))

    print(f"\nWin Rate by ML v2 Percentile Bucket ({target_label}):")
    bins = [0, 20, 40, 60, 70, 80, 90, 101]
    labels = ["0-20", "20-40", "40-60", "60-70", "70-80", "80-90", "90-100"]
    df["bucket"] = pd.cut(df["ml_score_v2"], bins=bins, labels=labels, right=False)
    for label in labels:
        subset = df[df["bucket"] == label]
        if subset.empty:
            continue
        win_rate = subset["target"].mean() * 100
        avg_ret = subset["target_return"].mean()
        print(f"  {label:10s}  n={len(subset):5d}  WinRate={win_rate:5.1f}%  AvgReturn={avg_ret:+.2f}%")

    overall_wr = df["target"].mean() * 100
    print(f"\n  Overall baseline win rate: {overall_wr:.1f}%")

    # Triple Lock performance
    if "f4_distinct_insiders_60d" in df.columns:
        triple = df[
            (df["conviction_score"] > 70) &
            (df["ml_score_v2"] > 70) &
            (df["f4_distinct_insiders_60d"] >= 1) &
            (df["accum_phase"].isin(["EARLY_ACCUM", "ACTIVE_ACCUM", "LATE_ACCUM"]))
        ]
        if not triple.empty:
            tl_wr = triple["target"].mean() * 100
            tl_ret = triple["target_return"].mean()
            print(f"\n  TRIPLE LOCK subset: n={len(triple)}  WinRate={tl_wr:.1f}%  AvgReturn={tl_ret:+.2f}%")
        else:
            print("\n  TRIPLE LOCK subset: no observations in validation set")

        # Also show top-percentile-only (>= 80th)
        top_pct = df[df["ml_score_v2"] >= 80]
        if not top_pct.empty:
            tp_wr = top_pct["target"].mean() * 100
            tp_ret = top_pct["target_return"].mean()
            print(f"  TOP 20% (pctl>=80): n={len(top_pct)}  WinRate={tp_wr:.1f}%  AvgReturn={tp_ret:+.2f}%")

        # Top 10%
        top10 = df[df["ml_score_v2"] >= 90]
        if not top10.empty:
            t10_wr = top10["target"].mean() * 100
            t10_ret = top10["target_return"].mean()
            print(f"  TOP 10% (pctl>=90): n={len(top10)}  WinRate={t10_wr:.1f}%  AvgReturn={t10_ret:+.2f}%")

    print(f"{'='*65}\n")


# ---------------------------------------------------------------------------
# Scoring (inference)
# ---------------------------------------------------------------------------

def _load_model_v2() -> dict:
    if not _MODEL_PATH_V2.exists():
        raise FileNotFoundError(
            f"v2 model not found at {_MODEL_PATH_V2}. Run --train first."
        )
    with open(_MODEL_PATH_V2, "rb") as f:
        return pickle.load(f)


def score_quarter_v2(
    conn: duckdb.DuckDBPyConnection,
    quarter: str,
    write_to_db: bool = False,
    ref_date: Optional[date] = None,
) -> pd.DataFrame:
    """Score all tickers in a quarter using v2 model.

    If write_to_db=True, writes ml_score_v2 column to intelligence_scores.
    Also computes Triple Lock flag and writes inst_f4_distinct_60d column.
    """
    payload = _load_model_v2()
    model = payload["model"]
    expected_features = payload.get("feature_cols", FEATURE_COLS_V2)

    df = _extract_features_v2_score(conn, [quarter], ref_date=ref_date)
    if df.empty:
        logger.warning("No data for quarter={}", quarter)
        return pd.DataFrame(columns=["ticker", "ml_score_v2", "triple_lock"])

    X = df[expected_features].values.astype(np.float32)
    proba = model.predict_proba(X)[:, 1]
    # Cross-sectional percentile rank within quarter (0-100 spread).
    # Standard quant technique: preserves model ranking while making
    # absolute thresholds meaningful (top 30% → score > 70).
    df["ml_score_v2"] = (
        pd.Series(proba).rank(pct=True).values * 100
    ).round(1)

    # Triple Lock flag — all three independent signals converge:
    #   1. Institutional conviction > 70  (13F accumulation strength)
    #   2. ML v2 percentile > 70         (top 30% model confidence)
    #   3. Form 4 insider buying >= 1    (real-money insider confirmation)
    #   + must be in accumulation phase
    df["triple_lock"] = (
        (df["conviction_score"] > 70) &
        (df["ml_score_v2"] > 70) &
        (df["f4_distinct_insiders_60d"] >= 1) &
        (df["accum_phase"].isin(["EARLY_ACCUM", "ACTIVE_ACCUM", "LATE_ACCUM"]))
    ).astype(int)

    result = df[["ticker", "report_quarter", "ml_score_v2", "triple_lock",
                 "f4_distinct_insiders_60d", "f4_officer_buy_count_60d",
                 "price_momentum_90d", "price_above_200sma"]].copy()
    result = result.sort_values("ml_score_v2", ascending=False).reset_index(drop=True)

    triple_count = result["triple_lock"].sum()
    logger.info(
        "v2 scores: {} tickers in {}, {} Triple Lock",
        len(result), quarter, triple_count,
    )

    if triple_count > 0:
        tl = result[result["triple_lock"] == 1].head(15)
        logger.info("Top Triple Lock tickers:")
        for _, r in tl.iterrows():
            logger.info(
                "  {:6s}  ml_v2={:.1f}  f4_insiders={:.0f}  momentum={:.1f}%",
                str(r["ticker"]), r["ml_score_v2"],
                r["f4_distinct_insiders_60d"], r["price_momentum_90d"],
            )

    if write_to_db:
        _ensure_ml_score_v2_columns(conn)
        rows = [
            (
                float(r["ml_score_v2"]),
                int(r["triple_lock"]),
                float(r["f4_distinct_insiders_60d"]),
                float(r["price_momentum_90d"]),
                float(r.get("price_above_200sma", -1)),
                r["ticker"], quarter,
            )
            for _, r in result.iterrows()
        ]
        conn.executemany(
            """UPDATE intelligence_scores
               SET ml_score_v2 = ?,
                   triple_lock = ?,
                   inst_f4_distinct_60d = ?,
                   price_momentum_90d = ?,
                   price_above_200sma = ?
               WHERE ticker = ? AND report_quarter = ?""",
            rows,
        )
        logger.info("Wrote ml_score_v2 + triple_lock for {} tickers in {}", len(rows), quarter)

    return result


def _ensure_ml_score_v2_columns(conn: duckdb.DuckDBPyConnection) -> None:
    """Add v2 columns to intelligence_scores if they don't exist."""
    new_cols = [
        ("ml_score_v2",          "REAL DEFAULT 0.0"),
        ("triple_lock",           "BOOLEAN DEFAULT FALSE"),
        ("inst_f4_distinct_60d",  "REAL DEFAULT 0.0"),
        ("price_momentum_90d",    "REAL DEFAULT 0.0"),
        ("price_above_200sma",    "REAL DEFAULT -1"),
    ]
    for col, dtype in new_cols:
        try:
            conn.execute(f"SELECT {col} FROM intelligence_scores LIMIT 0")
        except duckdb.BinderException:
            conn.execute(
                f"ALTER TABLE intelligence_scores ADD COLUMN {col} {dtype}"
            )
            logger.info("Added column {} to intelligence_scores", col)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="ML Signal Classifier v2")
    parser.add_argument("--train",    action="store_true")
    parser.add_argument("--validate", action="store_true")
    parser.add_argument("--score",    action="store_true")
    parser.add_argument("--quarter",  default=None)
    parser.add_argument("--write",    action="store_true")
    parser.add_argument("--train-start", default="2020-Q2")
    parser.add_argument("--train-end",   default="2022-Q4")
    parser.add_argument("--val-start",   default="2023-Q1")
    parser.add_argument("--val-end",     default="2023-Q4")
    args = parser.parse_args()

    # Training and validation only read DuckDB → use read_only to avoid Windows lock conflicts.
    # Scoring with --write needs write access to update intelligence_scores columns.
    needs_write = args.score and args.write
    conn = duckdb.connect(str(WAREHOUSE_PATH), read_only=not needs_write)
    try:
        if args.train:
            result = train_model_v2(
                conn,
                train_start=args.train_start, train_end=args.train_end,
                val_start=args.val_start,     val_end=args.val_end,
            )
            print(f"\nv2 training complete: train_auc={result['train_auc']:.3f}  "
                  f"val_auc={result.get('val_auc', 'N/A')}")
        if args.validate:
            validate_model_v2(conn)
        if args.score:
            quarter = args.quarter
            if not quarter:
                row = conn.execute("""
                    SELECT report_quarter FROM intelligence_scores
                    WHERE COALESCE(data_quality_score, 100) >= 75
                    GROUP BY report_quarter HAVING COUNT(*) >= 500
                      AND SUM(CASE WHEN accum_phase IN
                          ('ACTIVE_ACCUM','LATE_ACCUM','EARLY_ACCUM') THEN 1 ELSE 0 END) >= 100
                    ORDER BY report_quarter DESC LIMIT 1
                """).fetchone()
                quarter = row[0] if row else None
            if quarter:
                df = score_quarter_v2(conn, quarter, write_to_db=args.write)
                print(f"\nTop 10 by ml_score_v2 in {quarter}:")
                print(df[["ticker", "ml_score_v2", "triple_lock",
                           "f4_distinct_insiders_60d", "price_momentum_90d"]].head(10).to_string(index=False))
            else:
                print("No clean quarter found.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
