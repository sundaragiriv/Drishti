"""Swing Trade ML — LightGBM enriched with daily technical features.

Builds on ML v2 (28 features, XGBoost, AUC 0.560) by adding:
  - Daily technical indicators: RSI-14, SMA distance, ATR, Bollinger, volume ratio
  - Short-term momentum: 5d/10d/20d returns
  - 52-week position: distance from high/low
  - Squeeze data: squeeze_score, short_squeeze_score, expected_value
  - Sector as native categorical (LightGBM handles natively)

Target: alpha_90d > 0 (beat SPY in 90 days)
Train: 2020-Q3 → 2022-Q4  (same as ML v2)
Validate: 2023-Q1 → 2023-Q4 (held-out)

Usage:
    python -m signal_scanner.institutional_intel.intelligence.swing_ml --train
    python -m signal_scanner.institutional_intel.intelligence.swing_ml --report
    python -m signal_scanner.institutional_intel.intelligence.swing_ml --score --quarters 2025-Q3
"""

from __future__ import annotations

import argparse
import math
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
from signal_scanner.institutional_intel.intelligence.ml_signal import (
    PHASE_ENCODE,
    _all_quarters_between,
)

# ---------------------------------------------------------------------------
# Paths & Constants
# ---------------------------------------------------------------------------
_MODELS_DIR = WAREHOUSE_PATH.parent / "models"
_MODEL_PATH = _MODELS_DIR / "swing_ml_v3.pkl"

QUARTERS_TRAIN = _all_quarters_between("2020-Q3", "2022-Q4")
QUARTERS_VAL = _all_quarters_between("2023-Q1", "2023-Q4")

# ---------------------------------------------------------------------------
# Feature definitions
# ---------------------------------------------------------------------------

# V2 base intelligence features (from intelligence_scores + QoQ + sector rotation)
FEATURES_INTEL = [
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

# V2 Form4 + momentum features
FEATURES_F4 = [
    "f4_open_buy_count_60d",
    "f4_distinct_insiders_60d",
    "f4_officer_buy_count_60d",
    "f4_net_dollar_log",
    "f4_buy_pressure_60d",
    "price_momentum_90d",
    "price_above_200sma",
]

# NEW: Daily technical features computed from fact_daily_prices
FEATURES_DAILY_TECH = [
    "rsi_14",
    "price_vs_sma20_pct",
    "price_vs_sma50_pct",
    "price_vs_sma200_pct",
    "atr_20_pct",         # ATR as % of price
    "bb_position",        # 0=lower band, 1=upper band
    "vol_ratio_20_50",    # 20d avg vol / 50d avg vol
    "ret_5d",
    "ret_10d",
    "ret_20d",
    "pct_from_52w_high",
    "pct_from_52w_low",
    "consec_up_days",
    "consec_down_days",
]

# NEW: Squeeze features
FEATURES_SQUEEZE = [
    "squeeze_score",
    "short_squeeze_score",
    "expected_value",
]

# Categorical (LightGBM native)
FEATURES_CATEGORICAL = [
    "sector",
    "accum_phase",
]

ALL_FEATURES = (
    FEATURES_INTEL + FEATURES_F4 + FEATURES_DAILY_TECH
    + FEATURES_SQUEEZE + FEATURES_CATEGORICAL
)


# ---------------------------------------------------------------------------
# DuckDB table for swing predictions
# ---------------------------------------------------------------------------

def _ensure_tables(conn: duckdb.DuckDBPyConnection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS swing_ml_predictions (
            ticker          TEXT NOT NULL,
            report_quarter  TEXT NOT NULL,
            entry_date      DATE,
            ml_prob_alpha   DOUBLE,
            ml_percentile   DOUBLE,
            actual_alpha_90d DOUBLE,
            actual_return_90d DOUBLE,
            model_version   TEXT,
            computed_at     TIMESTAMP,
            PRIMARY KEY (ticker, report_quarter)
        )
    """)


# ---------------------------------------------------------------------------
# SQL for Form4 + momentum (reuse ML v2 CTEs)
# ---------------------------------------------------------------------------

_F4_CTE = """
    f4_clean AS (
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

_PRICE_FEATURES_CTE = """
    daily_enriched AS (
        SELECT
            ticker,
            trade_date,
            close,
            high,
            low,
            volume,
            -- SMAs
            AVG(close) OVER w20  AS sma_20,
            AVG(close) OVER w50  AS sma_50,
            AVG(close) OVER w200 AS sma_200,
            -- Bollinger
            STDDEV(close) OVER w20 AS bb_std_20,
            -- ATR (simplified as avg true range)
            AVG(high - low) OVER w20 AS atr_20,
            -- Volume averages
            AVG(volume) OVER w20 AS avg_vol_20,
            AVG(volume) OVER w50 AS avg_vol_50,
            -- Returns
            LAG(close, 5)  OVER wo AS close_5d_ago,
            LAG(close, 10) OVER wo AS close_10d_ago,
            LAG(close, 20) OVER wo AS close_20d_ago,
            LAG(close, 90) OVER wo AS close_90d_ago,
            -- 52-week extremes
            MAX(high) OVER w252 AS high_52w,
            MIN(low)  OVER w252 AS low_52w,
            -- Consecutive up/down (simple: compare to prior close)
            LAG(close, 1) OVER wo AS prev_close,
            -- For RSI: gains and losses (14-day window)
            close - LAG(close, 1) OVER wo AS daily_change
        FROM fact_daily_prices
        WHERE close IS NOT NULL AND close > 0 AND ticker IS NOT NULL
        WINDOW
            wo   AS (PARTITION BY ticker ORDER BY trade_date),
            w20  AS (PARTITION BY ticker ORDER BY trade_date ROWS 19 PRECEDING),
            w50  AS (PARTITION BY ticker ORDER BY trade_date ROWS 49 PRECEDING),
            w200 AS (PARTITION BY ticker ORDER BY trade_date ROWS 199 PRECEDING),
            w252 AS (PARTITION BY ticker ORDER BY trade_date ROWS 251 PRECEDING)
    ),
    price_features AS (
        SELECT
            d.ticker,
            d.ref_date,
            p.close AS price_now,
            p.sma_200,
            CASE WHEN p.close > p.sma_200 THEN 1.0 ELSE 0.0 END AS price_above_200sma,
            CASE WHEN p.close_90d_ago IS NOT NULL AND p.close_90d_ago > 0
                 THEN (p.close - p.close_90d_ago) / p.close_90d_ago * 100
                 ELSE 0.0 END AS price_momentum_90d,
            -- New technical features
            (p.close - p.sma_20)  / NULLIF(p.sma_20,  0) * 100 AS price_vs_sma20_pct,
            (p.close - p.sma_50)  / NULLIF(p.sma_50,  0) * 100 AS price_vs_sma50_pct,
            (p.close - p.sma_200) / NULLIF(p.sma_200, 0) * 100 AS price_vs_sma200_pct,
            p.atr_20 / NULLIF(p.close, 0) * 100 AS atr_20_pct,
            CASE WHEN p.bb_std_20 > 0
                 THEN (p.close - (p.sma_20 - 2 * p.bb_std_20))
                    / NULLIF(4 * p.bb_std_20, 0)
                 ELSE 0.5 END AS bb_position,
            CASE WHEN p.avg_vol_50 > 0
                 THEN p.avg_vol_20 / p.avg_vol_50
                 ELSE 1.0 END AS vol_ratio_20_50,
            CASE WHEN p.close_5d_ago > 0
                 THEN (p.close - p.close_5d_ago) / p.close_5d_ago * 100
                 ELSE 0.0 END AS ret_5d,
            CASE WHEN p.close_10d_ago > 0
                 THEN (p.close - p.close_10d_ago) / p.close_10d_ago * 100
                 ELSE 0.0 END AS ret_10d,
            CASE WHEN p.close_20d_ago > 0
                 THEN (p.close - p.close_20d_ago) / p.close_20d_ago * 100
                 ELSE 0.0 END AS ret_20d,
            CASE WHEN p.high_52w > 0
                 THEN (p.close - p.high_52w) / p.high_52w * 100
                 ELSE 0.0 END AS pct_from_52w_high,
            CASE WHEN p.low_52w > 0
                 THEN (p.close - p.low_52w) / p.low_52w * 100
                 ELSE 0.0 END AS pct_from_52w_low
        FROM ref_dates d
        LEFT JOIN daily_enriched p
            ON p.ticker = d.ticker
            AND p.trade_date = (
                SELECT MAX(trade_date) FROM fact_daily_prices fp
                WHERE fp.ticker = d.ticker AND fp.trade_date <= d.ref_date
            )
    )
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _log_signed(x) -> float:
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return 0.0
    return math.copysign(math.log1p(abs(x)), x)


def _compute_rsi_and_consec(
    df: pd.DataFrame,
    conn: duckdb.DuckDBPyConnection,
) -> Tuple[pd.Series, pd.Series, pd.Series]:
    """Vectorized RSI-14 + consecutive up/down days for all rows.

    Returns (rsi_14, consec_up, consec_down).
    Pre-computes RSI per ticker-date via DuckDB, then merges.
    """
    if len(df) == 0:
        empty = pd.Series(dtype=float)
        return empty, empty, empty

    # Register entry dates as a temp table for efficient join
    entries = df[["ticker", "entry_date"]].drop_duplicates()
    conn.register("_entry_dates", entries)

    # Compute RSI-14 + consecutive days via SQL window functions
    rsi_df = conn.execute("""
        WITH relevant_prices AS (
            -- Only load prices for tickers/dates we need
            SELECT p.ticker, p.trade_date, p.close,
                   p.close - LAG(p.close) OVER (
                       PARTITION BY p.ticker ORDER BY p.trade_date
                   ) AS daily_change
            FROM fact_daily_prices p
            WHERE p.ticker IN (SELECT DISTINCT ticker FROM _entry_dates)
              AND p.close IS NOT NULL AND p.close > 0
        ),
        with_gains AS (
            SELECT *,
                CASE WHEN daily_change > 0 THEN daily_change ELSE 0 END AS gain,
                CASE WHEN daily_change < 0 THEN -daily_change ELSE 0 END AS loss,
                -- Rolling 14-day avg gain/loss (Cutler's RSI = SMA-based)
                AVG(CASE WHEN daily_change > 0 THEN daily_change ELSE 0 END)
                    OVER (PARTITION BY ticker ORDER BY trade_date ROWS 13 PRECEDING)
                    AS avg_gain_14,
                AVG(CASE WHEN daily_change < 0 THEN -daily_change ELSE 0 END)
                    OVER (PARTITION BY ticker ORDER BY trade_date ROWS 13 PRECEDING)
                    AS avg_loss_14
            FROM relevant_prices
        ),
        rsi_daily AS (
            SELECT ticker, trade_date,
                CASE WHEN avg_loss_14 = 0 THEN 100.0
                     WHEN avg_gain_14 = 0 THEN 0.0
                     ELSE 100.0 - 100.0 / (1.0 + avg_gain_14 / avg_loss_14)
                END AS rsi_14,
                -- Consecutive up/down: count streak ending at this bar
                daily_change
            FROM with_gains
        )
        SELECT e.ticker, e.entry_date,
               r.rsi_14
        FROM _entry_dates e
        LEFT JOIN rsi_daily r
            ON r.ticker = e.ticker
            AND r.trade_date = (
                SELECT MAX(trade_date) FROM rsi_daily r2
                WHERE r2.ticker = e.ticker AND r2.trade_date <= e.entry_date
            )
    """).fetchdf()

    conn.unregister("_entry_dates")

    # Merge RSI back to original df
    rsi_merged = df[["ticker", "entry_date"]].merge(
        rsi_df, on=["ticker", "entry_date"], how="left"
    )
    rsi_series = rsi_merged["rsi_14"].fillna(50.0)

    # For consecutive days, use a simpler SQL approach
    # (consecutive streaks are hard in pure SQL, use a fast Python approach)
    entries2 = df[["ticker", "entry_date"]].drop_duplicates()
    tickers = entries2["ticker"].unique().tolist()
    min_date = entries2["entry_date"].min()

    # Batch load — one query, all tickers
    ticker_chunks = [tickers[i:i+500] for i in range(0, len(tickers), 500)]
    all_prices = []
    for chunk in ticker_chunks:
        tl = ", ".join(f"'{t}'" for t in chunk)
        p = conn.execute(f"""
            SELECT ticker, trade_date, close
            FROM fact_daily_prices
            WHERE ticker IN ({tl})
              AND trade_date >= '{min_date}'::DATE - INTERVAL '25 days'
              AND close IS NOT NULL AND close > 0
            ORDER BY ticker, trade_date
        """).fetchdf()
        all_prices.append(p)

    prices = pd.concat(all_prices, ignore_index=True) if all_prices else pd.DataFrame()

    if prices.empty:
        return rsi_series, pd.Series(0, index=df.index), pd.Series(0, index=df.index)

    prices["change"] = prices.groupby("ticker")["close"].diff()

    # Build a lookup: for each ticker, pre-sort changes
    ticker_changes = {}
    for ticker, grp in prices.groupby("ticker"):
        ticker_changes[ticker] = grp[["trade_date", "change"]].dropna().values

    up_vals = np.zeros(len(df))
    down_vals = np.zeros(len(df))

    for i, (_, row) in enumerate(df.iterrows()):
        tc = ticker_changes.get(row["ticker"])
        if tc is None or len(tc) == 0:
            continue
        # Filter to dates <= entry_date
        mask = tc[:, 0] <= row["entry_date"]
        changes = tc[mask, 1][-20:]  # last 20

        # Consecutive up from end
        up = 0
        for c in reversed(changes):
            if c > 0:
                up += 1
            else:
                break
        up_vals[i] = up

        # Consecutive down from end
        down = 0
        for c in reversed(changes):
            if c < 0:
                down += 1
            else:
                break
        down_vals[i] = down

    return rsi_series, pd.Series(up_vals, index=df.index), pd.Series(down_vals, index=df.index)


# ---------------------------------------------------------------------------
# Feature extraction
# ---------------------------------------------------------------------------

def extract_features(
    conn: duckdb.DuckDBPyConnection,
    quarters: List[str],
    require_target: bool = True,
) -> pd.DataFrame:
    """Build enriched feature DataFrame for swing trade ML.

    Joins backtest_results + intelligence_scores + QoQ changes + sector rotation
    + Form 4 + daily price technicals + squeeze data.
    """
    q_list = "'" + "','".join(quarters) + "'"

    logger.info(f"Extracting swing features for {len(quarters)} quarters...")

    if require_target:
        source_join = f"""
            FROM backtest_results br
            JOIN intelligence_scores i
                ON br.ticker = i.ticker AND br.signal_quarter = i.report_quarter
        """
        where_clause = f"""
            WHERE br.signal_quarter IN ({q_list})
              AND (br.alpha_90d IS NOT NULL OR br.return_90d IS NOT NULL)
              AND COALESCE(i.data_quality_score, 100) >= 75
        """
        target_cols = """
            br.entry_date,
            COALESCE(br.alpha_90d, br.return_90d) AS target_return,
            br.alpha_90d,
            br.return_90d,
            br.alpha_30d,
            br.alpha_60d,
            br.return_30d,
            br.return_60d,
        """
        ticker_col = "br.ticker"
        quarter_col = "br.signal_quarter AS report_quarter"
        ref_date_source = "br.entry_date"
    else:
        source_join = f"""
            FROM intelligence_scores i
        """
        where_clause = f"""
            WHERE i.report_quarter IN ({q_list})
              AND COALESCE(i.data_quality_score, 100) >= 75
        """
        target_cols = """
            CURRENT_DATE AS entry_date,
            NULL::DOUBLE AS target_return,
            NULL::DOUBLE AS alpha_90d,
            NULL::DOUBLE AS return_90d,
            NULL::DOUBLE AS alpha_30d,
            NULL::DOUBLE AS alpha_60d,
            NULL::DOUBLE AS return_30d,
            NULL::DOUBLE AS return_60d,
        """
        ticker_col = "i.ticker"
        quarter_col = "i.report_quarter"
        ref_date_source = "CURRENT_DATE"

    if require_target:
        # Training: ref_dates from backtest_results entry_date
        ref_dates_cte = f"""
            ref_dates AS (
                SELECT DISTINCT ticker, entry_date AS ref_date
                FROM backtest_results
                WHERE signal_quarter IN ({q_list})
            )
        """
        query = f"""
            WITH {ref_dates_cte},
            {_F4_CTE},
            {_PRICE_FEATURES_CTE}
            SELECT
                br.ticker,
                br.signal_quarter AS report_quarter,
                br.entry_date,
                COALESCE(br.alpha_90d, br.return_90d) AS target_return,
                br.alpha_90d, br.return_90d,
                br.alpha_30d, br.alpha_60d,
                br.return_30d, br.return_60d,
                -- Intelligence features
                COALESCE(i.conviction_score,     0)     AS conviction_score,
                COALESCE(i.accum_phase,   'DORMANT')    AS accum_phase,
                COALESCE(i.accum_phase_quarters,  0)    AS accum_phase_quarters,
                COALESCE(i.accum_strength_score,  0)    AS accum_strength_score,
                COALESCE(i.tier1_manager_count,   0)    AS tier1_manager_count,
                CASE WHEN i.insider_cluster_detected THEN 1 ELSE 0 END
                                                         AS insider_cluster_detected,
                COALESCE(i.insider_net_buy_count,  0)   AS insider_net_buy_count,
                CASE WHEN i.ceo_cfo_buying THEN 1 ELSE 0 END AS ceo_cfo_buying,
                COALESCE(i.cascade_stage,          0)   AS cascade_stage,
                COALESCE(i.copycat_score,          0)   AS copycat_score,
                CASE WHEN i.divergence_active THEN 1 ELSE 0 END
                                                         AS divergence_active,
                COALESCE(i.divergence_magnitude,   0)   AS divergence_magnitude,
                COALESCE(i.manager_quality_score,  0)   AS manager_quality_score,
                COALESCE(i.insider_score,          0)   AS insider_score,
                COALESCE(q2.count_up_streak,       0)   AS count_up_streak,
                COALESCE(q2.inst_count_change_pct, 0)   AS inst_count_change_pct,
                COALESCE(q2.value_change_pct,      0)   AS value_change_pct,
                COALESCE(q2.avg_price_change_pct,  0)   AS avg_price_change_pct,
                COALESCE(q2.avg_volume_change_pct, 0)   AS avg_volume_change_pct,
                COALESCE(sr.flow_pct,              0)   AS sector_flow_pct,
                COALESCE(sr.inflow_streak,         0)   AS sector_inflow_streak,
                -- Form 4 features
                COALESCE(f4.f4_open_buy_count_60d,    0)   AS f4_open_buy_count_60d,
                COALESCE(f4.f4_distinct_insiders_60d, 0)   AS f4_distinct_insiders_60d,
                COALESCE(f4.f4_officer_buy_count_60d, 0)   AS f4_officer_buy_count_60d,
                COALESCE(f4.f4_net_dollar_raw,        0)   AS f4_net_dollar_raw,
                COALESCE(f4.f4_buy_pressure_60d,      0.5) AS f4_buy_pressure_60d,
                COALESCE(pf.price_momentum_90d,       0)   AS price_momentum_90d,
                COALESCE(pf.price_above_200sma,       0)   AS price_above_200sma,
                -- Daily technical features
                COALESCE(pf.price_vs_sma20_pct,       0)   AS price_vs_sma20_pct,
                COALESCE(pf.price_vs_sma50_pct,       0)   AS price_vs_sma50_pct,
                COALESCE(pf.price_vs_sma200_pct,      0)   AS price_vs_sma200_pct,
                COALESCE(pf.atr_20_pct,               0)   AS atr_20_pct,
                COALESCE(pf.bb_position,              0.5) AS bb_position,
                COALESCE(pf.vol_ratio_20_50,          1.0) AS vol_ratio_20_50,
                COALESCE(pf.ret_5d,                   0)   AS ret_5d,
                COALESCE(pf.ret_10d,                  0)   AS ret_10d,
                COALESCE(pf.ret_20d,                  0)   AS ret_20d,
                COALESCE(pf.pct_from_52w_high,        0)   AS pct_from_52w_high,
                COALESCE(pf.pct_from_52w_low,         0)   AS pct_from_52w_low,
                -- Squeeze features
                COALESCE(i.squeeze_score,             0)   AS squeeze_score,
                COALESCE(i.short_squeeze_score,       0)   AS short_squeeze_score,
                COALESCE(i.expected_value,            0)   AS expected_value,
                -- Sector
                COALESCE(q2.sector, 'Unknown')             AS sector
            FROM backtest_results br
            JOIN intelligence_scores i
                ON br.ticker = i.ticker AND br.signal_quarter = i.report_quarter
            LEFT JOIN agg_qoq_changes q2
                ON br.ticker = q2.ticker AND br.signal_quarter = q2.current_quarter
            LEFT JOIN agg_sector_rotation sr
                ON q2.sector = sr.sector AND br.signal_quarter = sr.report_quarter
            LEFT JOIN f4_agg f4
                ON f4.ticker = br.ticker AND f4.ref_date = br.entry_date
            LEFT JOIN price_features pf
                ON pf.ticker = br.ticker AND pf.ref_date = br.entry_date
            WHERE br.signal_quarter IN ({q_list})
              AND (br.alpha_90d IS NOT NULL OR br.return_90d IS NOT NULL)
              AND COALESCE(i.data_quality_score, 100) >= 75
        """
    else:
        # Scoring: ref_dates from intelligence_scores with CURRENT_DATE
        ref_dates_cte = f"""
            ref_dates AS (
                SELECT DISTINCT ticker, CURRENT_DATE AS ref_date
                FROM intelligence_scores
                WHERE report_quarter IN ({q_list})
            )
        """
        query = f"""
            WITH {ref_dates_cte},
            {_F4_CTE},
            {_PRICE_FEATURES_CTE}
            SELECT
                i.ticker,
                i.report_quarter,
                CURRENT_DATE AS entry_date,
                NULL::DOUBLE AS target_return,
                NULL::DOUBLE AS alpha_90d, NULL::DOUBLE AS return_90d,
                NULL::DOUBLE AS alpha_30d, NULL::DOUBLE AS alpha_60d,
                NULL::DOUBLE AS return_30d, NULL::DOUBLE AS return_60d,
                COALESCE(i.conviction_score,     0)     AS conviction_score,
                COALESCE(i.accum_phase,   'DORMANT')    AS accum_phase,
                COALESCE(i.accum_phase_quarters,  0)    AS accum_phase_quarters,
                COALESCE(i.accum_strength_score,  0)    AS accum_strength_score,
                COALESCE(i.tier1_manager_count,   0)    AS tier1_manager_count,
                CASE WHEN i.insider_cluster_detected THEN 1 ELSE 0 END
                                                         AS insider_cluster_detected,
                COALESCE(i.insider_net_buy_count,  0)   AS insider_net_buy_count,
                CASE WHEN i.ceo_cfo_buying THEN 1 ELSE 0 END AS ceo_cfo_buying,
                COALESCE(i.cascade_stage,          0)   AS cascade_stage,
                COALESCE(i.copycat_score,          0)   AS copycat_score,
                CASE WHEN i.divergence_active THEN 1 ELSE 0 END
                                                         AS divergence_active,
                COALESCE(i.divergence_magnitude,   0)   AS divergence_magnitude,
                COALESCE(i.manager_quality_score,  0)   AS manager_quality_score,
                COALESCE(i.insider_score,          0)   AS insider_score,
                COALESCE(q2.count_up_streak,       0)   AS count_up_streak,
                COALESCE(q2.inst_count_change_pct, 0)   AS inst_count_change_pct,
                COALESCE(q2.value_change_pct,      0)   AS value_change_pct,
                COALESCE(q2.avg_price_change_pct,  0)   AS avg_price_change_pct,
                COALESCE(q2.avg_volume_change_pct, 0)   AS avg_volume_change_pct,
                COALESCE(sr.flow_pct,              0)   AS sector_flow_pct,
                COALESCE(sr.inflow_streak,         0)   AS sector_inflow_streak,
                COALESCE(f4.f4_open_buy_count_60d,    0)   AS f4_open_buy_count_60d,
                COALESCE(f4.f4_distinct_insiders_60d, 0)   AS f4_distinct_insiders_60d,
                COALESCE(f4.f4_officer_buy_count_60d, 0)   AS f4_officer_buy_count_60d,
                COALESCE(f4.f4_net_dollar_raw,        0)   AS f4_net_dollar_raw,
                COALESCE(f4.f4_buy_pressure_60d,      0.5) AS f4_buy_pressure_60d,
                COALESCE(pf.price_momentum_90d,       0)   AS price_momentum_90d,
                COALESCE(pf.price_above_200sma,       0)   AS price_above_200sma,
                COALESCE(pf.price_vs_sma20_pct,       0)   AS price_vs_sma20_pct,
                COALESCE(pf.price_vs_sma50_pct,       0)   AS price_vs_sma50_pct,
                COALESCE(pf.price_vs_sma200_pct,      0)   AS price_vs_sma200_pct,
                COALESCE(pf.atr_20_pct,               0)   AS atr_20_pct,
                COALESCE(pf.bb_position,              0.5) AS bb_position,
                COALESCE(pf.vol_ratio_20_50,          1.0) AS vol_ratio_20_50,
                COALESCE(pf.ret_5d,                   0)   AS ret_5d,
                COALESCE(pf.ret_10d,                  0)   AS ret_10d,
                COALESCE(pf.ret_20d,                  0)   AS ret_20d,
                COALESCE(pf.pct_from_52w_high,        0)   AS pct_from_52w_high,
                COALESCE(pf.pct_from_52w_low,         0)   AS pct_from_52w_low,
                COALESCE(i.squeeze_score,             0)   AS squeeze_score,
                COALESCE(i.short_squeeze_score,       0)   AS short_squeeze_score,
                COALESCE(i.expected_value,            0)   AS expected_value,
                COALESCE(q2.sector, 'Unknown')             AS sector
            FROM intelligence_scores i
            LEFT JOIN agg_qoq_changes q2
                ON i.ticker = q2.ticker AND i.report_quarter = q2.current_quarter
            LEFT JOIN agg_sector_rotation sr
                ON q2.sector = sr.sector AND i.report_quarter = sr.report_quarter
            LEFT JOIN f4_agg f4
                ON f4.ticker = i.ticker AND f4.ref_date = CURRENT_DATE
            LEFT JOIN price_features pf
                ON pf.ticker = i.ticker AND pf.ref_date = CURRENT_DATE
            WHERE i.report_quarter IN ({q_list})
              AND COALESCE(i.data_quality_score, 100) >= 75
        """

    df = conn.execute(query).fetchdf()
    logger.info(f"  SQL returned {len(df):,} rows")

    if df.empty:
        return df

    # --- Python-side feature computation ---

    # Log-sign transform for insider dollar flow
    if "f4_net_dollar_raw" in df.columns:
        df["f4_net_dollar_log"] = df["f4_net_dollar_raw"].apply(_log_signed)
        df.drop(columns=["f4_net_dollar_raw"], inplace=True, errors="ignore")

    # Phase encoding
    df["accum_phase_encoded"] = df["accum_phase"].map(PHASE_ENCODE).fillna(1).astype(int)

    # RSI-14 + consecutive up/down days (vectorized via DuckDB + numpy)
    logger.info("  Computing RSI-14 + consecutive days (vectorized)...")
    df["rsi_14"], df["consec_up_days"], df["consec_down_days"] = \
        _compute_rsi_and_consec(df, conn)

    # Clip outliers
    clip_cols = [
        "avg_price_change_pct", "avg_volume_change_pct",
        "inst_count_change_pct", "value_change_pct", "sector_flow_pct",
        "price_momentum_90d", "ret_5d", "ret_10d", "ret_20d",
        "pct_from_52w_high", "pct_from_52w_low",
        "price_vs_sma20_pct", "price_vs_sma50_pct", "price_vs_sma200_pct",
    ]
    for col in clip_cols:
        if col in df.columns:
            df[col] = df[col].clip(-300, 300)

    # Categorical columns for LightGBM
    for col in FEATURES_CATEGORICAL:
        if col in df.columns:
            df[col] = df[col].astype("category")

    # Fill NaN
    for col in ALL_FEATURES:
        if col in df.columns and col not in FEATURES_CATEGORICAL:
            df[col] = df[col].fillna(0)

    pos_rate = (df["target_return"] > 0).mean() if "target_return" in df.columns and df["target_return"].notna().any() else 0
    logger.info(f"  Final: {len(df):,} rows, {pos_rate:.1%} positive (alpha>0)")

    return df


# ---------------------------------------------------------------------------
# Model training
# ---------------------------------------------------------------------------

def prepare_features(
    df: pd.DataFrame,
    target_threshold: float = 5.0,
) -> Tuple[pd.DataFrame, pd.Series, List[str]]:
    """Build feature matrix X, target y, feature name list.

    target_threshold: minimum alpha_90d % to count as a "win".
        Default 5.0 = beat SPY by 5%+ in 90 days (big winners).
    """
    feature_cols = ALL_FEATURES
    y = (df["target_return"] > target_threshold).astype(int)
    X = df[feature_cols].copy()
    return X, y, feature_cols


def train_model(
    conn: duckdb.DuckDBPyConnection,
    quarters_train: Optional[List[str]] = None,
    quarters_val: Optional[List[str]] = None,
) -> Tuple[lgb.LGBMClassifier, Dict[str, Any]]:
    """Train LightGBM swing trade classifier."""
    quarters_train = quarters_train or QUARTERS_TRAIN
    quarters_val = quarters_val or QUARTERS_VAL

    logger.info(f"Training swing ML: train={quarters_train[0]}..{quarters_train[-1]}, "
                f"val={quarters_val[0]}..{quarters_val[-1]}")

    df_train = extract_features(conn, quarters_train, require_target=True)
    df_val = extract_features(conn, quarters_val, require_target=True)

    if len(df_train) < 100:
        logger.warning(f"Too few training samples: {len(df_train)}")
        return None, {}

    X_train, y_train, feature_cols = prepare_features(df_train)
    X_val, y_val, _ = prepare_features(df_val)

    pos_rate = y_train.mean()
    scale = (1 - pos_rate) / pos_rate if pos_rate > 0 else 1.0

    logger.info(f"  Train: {len(X_train):,} rows, {pos_rate:.1%} positive, "
                f"scale_pos_weight={scale:.2f}")

    params = {
        "objective": "binary",
        "metric": "auc",
        "n_estimators": 1500,
        "max_depth": 4,
        "num_leaves": 15,
        "learning_rate": 0.05,
        "min_child_samples": 30,
        "subsample": 0.7,
        "colsample_bytree": 0.6,
        "reg_alpha": 0.1,
        "reg_lambda": 0.5,
        "scale_pos_weight": scale,
        "random_state": 42,
        "verbose": -1,
        "n_jobs": -1,
    }

    model = lgb.LGBMClassifier(**params)
    cat_indices = [feature_cols.index(c) for c in FEATURES_CATEGORICAL]

    model.fit(
        X_train, y_train,
        eval_set=[(X_val, y_val)],
        eval_metric="auc",
        categorical_feature=cat_indices,
        callbacks=[
            lgb.early_stopping(50, verbose=True),
            lgb.log_evaluation(100),
        ],
    )

    from sklearn.metrics import roc_auc_score, log_loss

    proba_train = model.predict_proba(X_train)[:, 1]
    proba_val = model.predict_proba(X_val)[:, 1]

    metrics = {
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

    logger.info(f"  Swing ML: train AUC={metrics['train_auc']:.4f}, "
                f"val AUC={metrics['val_auc']:.4f} "
                f"(iters={metrics['n_estimators_used']})")

    # Feature importance
    imp = pd.Series(
        model.feature_importances_, index=feature_cols
    ).sort_values(ascending=False)
    metrics["top_features"] = imp.head(20).to_dict()

    # Also compute separate metrics for different targets
    if "alpha_30d" in df_val.columns:
        y_30 = (df_val["alpha_30d"] > 0).astype(int)
        mask_30 = df_val["alpha_30d"].notna()
        if mask_30.sum() > 0:
            metrics["val_auc_alpha30"] = float(
                roc_auc_score(y_30[mask_30], proba_val[mask_30])
            )

    return model, metrics


# ---------------------------------------------------------------------------
# Calibration
# ---------------------------------------------------------------------------

def calibration_report(
    model: lgb.LGBMClassifier,
    df_val: pd.DataFrame,
) -> pd.DataFrame:
    """Compute actual win rate at each predicted probability bucket."""
    X_val, y_val, _ = prepare_features(df_val)
    proba = model.predict_proba(X_val)[:, 1]

    results = df_val[["ticker", "report_quarter"]].copy()
    results["prob"] = proba
    results["actual_win"] = y_val.values
    results["alpha_90d"] = df_val["alpha_90d"].values
    results["return_90d"] = df_val["return_90d"].values
    results["percentile"] = results["prob"].rank(pct=True) * 100

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
        win_rate = subset["actual_win"].mean()
        avg_alpha = subset["alpha_90d"].mean()
        avg_return = subset["return_90d"].mean()
        avg_prob = subset["prob"].mean()

        rows.append({
            "bucket": label,
            "n": n,
            "avg_prob": avg_prob,
            "win_rate": win_rate,
            "avg_alpha_90d": avg_alpha,
            "avg_return_90d": avg_return,
        })

    return pd.DataFrame(rows)


def print_calibration(cal_df: pd.DataFrame, metrics: Dict[str, Any]) -> None:
    """Print formatted calibration report."""
    ml_v2_auc = 0.560  # baseline for comparison

    print()
    print("=" * 90)
    print("  SWING TRADE ML — CALIBRATION REPORT (LightGBM v3)")
    print(f"  Train AUC: {metrics.get('train_auc', 0):.4f}  |  "
          f"Val AUC: {metrics.get('val_auc', 0):.4f}  |  "
          f"ML v2 baseline: {ml_v2_auc:.4f}  |  "
          f"Iters: {metrics.get('n_estimators_used', 0)}")
    print(f"  Lift vs v2: {(metrics.get('val_auc', 0) - ml_v2_auc):.4f} AUC points")
    if "val_auc_alpha30" in metrics:
        print(f"  Val AUC (alpha_30d): {metrics['val_auc_alpha30']:.4f}")
    print("=" * 90)
    print()
    print(f"  {'Bucket':<15s} {'N':>7s} {'AvgProb':>8s} {'WinRate':>8s} "
          f"{'AvgAlpha':>9s} {'AvgRet':>8s}")
    print(f"  {'-'*60}")

    for _, row in cal_df.iterrows():
        alpha_str = f"{row['avg_alpha_90d']:+.1f}%" if pd.notna(row['avg_alpha_90d']) else "N/A"
        ret_str = f"{row['avg_return_90d']:+.1f}%" if pd.notna(row['avg_return_90d']) else "N/A"
        print(
            f"  {row['bucket']:<15s} {row['n']:>7,d} {row['avg_prob']:>7.1%} "
            f"{row['win_rate']:>8.1%} {alpha_str:>9s} {ret_str:>8s}"
        )

    print()

    # Feature importance
    if "top_features" in metrics:
        print("  TOP 20 FEATURES:")
        for i, (feat, imp) in enumerate(metrics["top_features"].items()):
            tag = ""
            if feat in FEATURES_DAILY_TECH:
                tag = " [NEW-TECH]"
            elif feat in FEATURES_SQUEEZE:
                tag = " [NEW-SQUEEZE]"
            elif feat in FEATURES_CATEGORICAL:
                tag = " [CAT]"
            print(f"    {i+1:2d}. {feat:<35s} {imp:>6.0f}{tag}")
        print()


# ---------------------------------------------------------------------------
# Model persistence
# ---------------------------------------------------------------------------

def save_model(model: lgb.LGBMClassifier, metrics: Dict[str, Any]) -> Path:
    _MODELS_DIR.mkdir(parents=True, exist_ok=True)
    with open(_MODEL_PATH, "wb") as f:
        pickle.dump({"model": model, "metrics": metrics}, f)
    logger.info(f"Saved model to {_MODEL_PATH}")
    return _MODEL_PATH


def load_model() -> Tuple[lgb.LGBMClassifier, Dict[str, Any]]:
    with open(_MODEL_PATH, "rb") as f:
        data = pickle.load(f)
    return data["model"], data["metrics"]


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def score_and_write(
    model: lgb.LGBMClassifier,
    quarters: List[str],
    conn: duckdb.DuckDBPyConnection,
) -> int:
    """Score current intelligence_scores and write predictions."""
    _ensure_tables(conn)

    df = extract_features(conn, quarters, require_target=False)
    if len(df) == 0:
        return 0

    X, _, _ = prepare_features(df)
    proba = model.predict_proba(X)[:, 1]
    ranks = pd.Series(proba).rank(pct=True) * 100

    now = datetime.now(timezone.utc)
    pred_df = pd.DataFrame({
        "ticker": df["ticker"].values,
        "report_quarter": df["report_quarter"].values,
        "entry_date": df["entry_date"].values if "entry_date" in df.columns else now.date(),
        "ml_prob_alpha": proba,
        "ml_percentile": ranks.values,
        "actual_alpha_90d": None,
        "actual_return_90d": None,
        "model_version": "swing_ml_v3",
        "computed_at": now,
    })

    conn.register("_swing_pred", pred_df)
    conn.execute("INSERT OR REPLACE INTO swing_ml_predictions SELECT * FROM _swing_pred")
    conn.unregister("_swing_pred")

    logger.info(f"Wrote {len(pred_df):,} swing predictions")
    return len(pred_df)


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

def run_train_pipeline(
    quarters_train: Optional[List[str]] = None,
    quarters_val: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Train, evaluate, save."""
    conn = duckdb.connect(str(WAREHOUSE_PATH), read_only=True)
    try:
        model, metrics = train_model(conn, quarters_train, quarters_val)
        if model is None:
            return {}

        df_val = extract_features(
            conn, quarters_val or QUARTERS_VAL, require_target=True
        )
        cal_df = calibration_report(model, df_val)
        print_calibration(cal_df, metrics)
        save_model(model, metrics)
        return metrics
    finally:
        conn.close()


def run_report_pipeline(quarters_val: Optional[List[str]] = None) -> None:
    conn = duckdb.connect(str(WAREHOUSE_PATH), read_only=True)
    try:
        model, metrics = load_model()
        df_val = extract_features(
            conn, quarters_val or QUARTERS_VAL, require_target=True
        )
        cal_df = calibration_report(model, df_val)
        print_calibration(cal_df, metrics)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Swing Trade ML — LightGBM enriched classifier"
    )
    parser.add_argument("--train", action="store_true")
    parser.add_argument("--report", action="store_true")
    parser.add_argument("--score", action="store_true")
    parser.add_argument("--quarters", default=None,
                        help="Quarters to score (comma-separated)")

    args = parser.parse_args()

    if args.train:
        run_train_pipeline()

    if args.report:
        run_report_pipeline()

    if args.score:
        quarters = (
            [q.strip() for q in args.quarters.split(",")]
            if args.quarters else ["2025-Q3"]
        )
        conn = duckdb.connect(str(WAREHOUSE_PATH))
        try:
            model, _ = load_model()
            score_and_write(model, quarters, conn)
        finally:
            conn.close()

    if not (args.train or args.report or args.score):
        parser.print_help()


if __name__ == "__main__":
    main()
