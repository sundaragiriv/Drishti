"""Predictive Feature Join Engine — point-in-time safe feature assembly.

Joins daily technical features (fact_swing_features) with labels
(fact_predictive_labels) to create the training dataset for the
forward return predictor.

Point-in-time rules:
  - Daily technicals: use trade_date = prediction_date (available at close)
  - Institutional features: from fact_swing_features (already point-in-time
    mapped by the swing feature engine to the latest SETTLED quarter)
  - Short/dark pool: from fact_swing_features (snapshotted at feature time)
  - Existing ML model outputs: EXCLUDED from v1 (see PREDICTIVE_AI_SAFEGUARDS.md)
  - Form 4 insider features: from fact_swing_features (days_since_insider_cluster)

Usage:
    python -m signal_scanner.institutional_intel.intelligence.predictive_features --build
    python -m signal_scanner.institutional_intel.intelligence.predictive_features --stats
"""

from __future__ import annotations

import argparse
from typing import Dict, List

from loguru import logger


# Feature families (from fact_swing_features)
# All are knowable at close of trade_date — no future leakage.
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

FEATURES_TREND = [
    "ema_20_slope", "adx_14", "plus_di_minus_di", "linreg_slope_12d",
]

FEATURES_SETUP = [
    "consecutive_down_days", "rsi2_below_10", "gap_pct_from_prev",
    "volume_surge_3x", "days_since_insider_cluster", "price_vs_20d_high_pct",
]

FEATURES_CANDLE = [
    "hammer", "inv_hammer", "engulfing_bull", "engulfing_bear",
    "doji", "morning_star", "evening_star", "three_white_soldiers",
    "piercing_line", "dark_cloud_cover",
]

FEATURES_INTEL = [
    "conviction_score", "insider_cluster_detected",
    "insider_effect_score", "trend_score", "institutional_pressure",
    "expected_value",
    # Short/dark pool (point-in-time: snapshotted when features were computed)
    "int_squeeze_score", "int_short_squeeze_score", "int_days_to_cover",
    "short_volume_ratio_avg", "dark_pool_pct_avg",
]

FEATURES_CALENDAR = [
    "quarter_month", "day_of_week",
]

FEATURES_CATEGORICAL = [
    "accum_phase", "sector",
]

# Interconnected stock features (from fact_interconnected_features)
FEATURES_INTERCONNECTED = [
    "peer_avg_ret_5d", "peer_avg_ret_20d", "peer_momentum_spread",
    "peer_count", "sector_breadth_20d", "sector_avg_ret_5d",
    "sector_avg_ret_20d", "peers_in_accum", "peers_with_insider",
    "peer_avg_conviction",
]

# Explicitly EXCLUDED from v1 (leakage risk — see PREDICTIVE_AI_SAFEGUARDS.md)
EXCLUDED_V1 = [
    # "ml_score_v2",        — trained on overlapping period
    # "swing_ml_prob",      — trained on overlapping period
    # "strategy_ml_prob",   — trained on overlapping period
]

ALL_NUMERIC_FEATURES = (
    FEATURES_PRICE + FEATURES_MOMENTUM + FEATURES_VOLATILITY +
    FEATURES_VOLUME + FEATURES_TREND + FEATURES_SETUP +
    FEATURES_CANDLE + FEATURES_INTEL + FEATURES_CALENDAR +
    FEATURES_INTERCONNECTED
)

ALL_FEATURES = ALL_NUMERIC_FEATURES + FEATURES_CATEGORICAL


CREATE_TRAINING_TABLE = """
CREATE TABLE IF NOT EXISTS predictive_training_data (
    ticker          VARCHAR NOT NULL,
    trade_date      DATE    NOT NULL,
    -- Labels (from fact_predictive_labels)
    fwd_return_3d   DOUBLE,
    fwd_return_5d   DOUBLE,
    fwd_direction   INTEGER,
    fwd_magnitude   DOUBLE,
    fwd_alpha_5d    DOUBLE,
    -- Features (all columns from fact_swing_features, point-in-time safe)
    {feature_columns}
    -- Meta
    report_quarter  VARCHAR,
    computed_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (ticker, trade_date)
);
"""


def _settled_quarter(trade_date_str: str) -> str:
    """Return the latest quarter whose 13F filings are available on trade_date.

    13F filing deadline: 45 days after quarter end.
    So on Jan 1 2024, only Q2 2023 filings are guaranteed (filed by Aug 14 2023).
    Q3 2023 filings (deadline Nov 14 2023) are also available by Jan 1.

    Conservative rule: quarter is settled if trade_date >= quarter_end + 45 days.
    """
    from datetime import datetime, timedelta
    td = datetime.strptime(trade_date_str[:10], "%Y-%m-%d")

    # Check quarters from newest to oldest
    quarters = []
    for year in range(td.year, td.year - 2, -1):
        for qend, qlabel in [
            (f"{year}-12-31", f"{year}-Q4"),
            (f"{year}-09-30", f"{year}-Q3"),
            (f"{year}-06-30", f"{year}-Q2"),
            (f"{year}-03-31", f"{year}-Q1"),
        ]:
            quarters.append((datetime.strptime(qend, "%Y-%m-%d"), qlabel))

    for qend_dt, qlabel in sorted(quarters, reverse=True):
        if td >= qend_dt + timedelta(days=45):
            return qlabel
    return quarters[-1][1]  # fallback to oldest


def build_training_dataset(
    conn,
    min_date: str = "2023-10-01",
    max_date: str = "2024-12-31",
) -> Dict[str, int]:
    """Build the joined training dataset with point-in-time safe intel mapping.

    For each trade_date:
    - Daily technicals (price/volume/momentum/candles): from same trade_date
    - Institutional intelligence (conviction/phase/squeeze/pressure):
      from the SETTLED quarter (latest quarter with 13F deadline passed)

    This prevents leakage from same-quarter intelligence being used
    before it was publicly available.

    Args:
        conn: DuckDB write connection
        min_date: earliest date to include
        max_date: latest date to include

    Returns: dict with row counts.
    """
    # Build feature column definitions for CREATE TABLE
    feature_cols = []
    for f in ALL_NUMERIC_FEATURES:
        feature_cols.append(f"    {f} DOUBLE,")
    for f in FEATURES_CATEGORICAL:
        feature_cols.append(f"    {f} VARCHAR,")

    create_sql = CREATE_TRAINING_TABLE.format(
        feature_columns="\n".join(feature_cols)
    )
    conn.execute(create_sql)

    # Technical features (no leakage — same trade_date)
    tech_features = (FEATURES_PRICE + FEATURES_MOMENTUM + FEATURES_VOLATILITY +
                     FEATURES_VOLUME + FEATURES_TREND + FEATURES_SETUP +
                     FEATURES_CANDLE + FEATURES_CALENDAR)

    # Intel features (must come from settled quarter)
    intel_features = FEATURES_INTEL + FEATURES_CATEGORICAL

    logger.info("Building training dataset: {} to {}", min_date, max_date)
    logger.info("Tech features: {} | Intel features: {} (point-in-time mapped)",
                len(tech_features), len(intel_features))

    # Build mapping: trade_date range → settled quarter
    # Get all distinct trade dates in range
    dates = conn.execute("""
        SELECT DISTINCT trade_date FROM fact_swing_features
        WHERE trade_date >= ? AND trade_date <= ?
        ORDER BY trade_date
    """, [min_date, max_date]).fetchall()
    dates = [str(r[0]) for r in dates]

    # Group dates by settled quarter
    from collections import defaultdict
    quarter_groups = defaultdict(list)
    for d in dates:
        sq = _settled_quarter(d)
        quarter_groups[sq].append(d)

    logger.info("Settled quarter mapping: {}", {q: f"{len(ds)} days" for q, ds in quarter_groups.items()})

    total_inserted = 0
    for settled_q, q_dates in quarter_groups.items():
        if not q_dates:
            continue

        q_min = q_dates[0]
        q_max = q_dates[-1]

        tech_select = ", ".join([f"sf_tech.{f}" for f in tech_features])
        intel_select = ", ".join([f"sf_intel.{f}" for f in intel_features])
        inter_select = ", ".join([f"ic.{f}" for f in FEATURES_INTERCONNECTED])

        # Join: labels × tech features (same date) × intel (settled quarter) × interconnected (same date)
        conn.execute(f"""
            INSERT INTO predictive_training_data
                (ticker, trade_date,
                 fwd_return_3d, fwd_return_5d, fwd_direction, fwd_magnitude, fwd_alpha_5d,
                 {', '.join(tech_features)},
                 {', '.join(intel_features)},
                 {', '.join(FEATURES_INTERCONNECTED)},
                 report_quarter)
            SELECT
                pl.ticker, pl.trade_date,
                pl.fwd_return_3d, pl.fwd_return_5d, pl.fwd_direction, pl.fwd_magnitude, pl.fwd_alpha_5d,
                {tech_select},
                {intel_select},
                {inter_select},
                ?
            FROM fact_predictive_labels pl
            INNER JOIN fact_swing_features sf_tech
                ON pl.ticker = sf_tech.ticker AND pl.trade_date = sf_tech.trade_date
            LEFT JOIN fact_swing_features sf_intel
                ON pl.ticker = sf_intel.ticker
                AND sf_intel.report_quarter = ?
                AND sf_intel.trade_date = (
                    SELECT MAX(trade_date) FROM fact_swing_features
                    WHERE ticker = pl.ticker AND report_quarter = ?
                )
            LEFT JOIN fact_interconnected_features ic
                ON pl.ticker = ic.ticker AND pl.trade_date = ic.trade_date
            WHERE pl.trade_date >= ? AND pl.trade_date <= ?
              AND pl.fwd_return_5d IS NOT NULL
              AND sf_tech.close IS NOT NULL AND sf_tech.close > 0
            ON CONFLICT (ticker, trade_date) DO UPDATE SET
                fwd_return_3d = excluded.fwd_return_3d,
                fwd_return_5d = excluded.fwd_return_5d,
                fwd_direction = excluded.fwd_direction,
                fwd_magnitude = excluded.fwd_magnitude,
                fwd_alpha_5d = excluded.fwd_alpha_5d,
                {', '.join(f'{f} = excluded.{f}' for f in intel_features)},
                {', '.join(f'{f} = excluded.{f}' for f in FEATURES_INTERCONNECTED)},
                report_quarter = excluded.report_quarter
        """, [settled_q, settled_q, settled_q, q_min, q_max])

        count = conn.execute(
            "SELECT COUNT(*) FROM predictive_training_data WHERE trade_date >= ? AND trade_date <= ?",
            [q_min, q_max],
        ).fetchone()[0]
        total_inserted += count
        logger.info("  {} ({} to {}): {} rows (intel from {})", settled_q, q_min, q_max, count, settled_q)

    # Count results
    total = conn.execute(
        "SELECT COUNT(*) FROM predictive_training_data WHERE trade_date >= ? AND trade_date <= ?",
        [min_date, max_date],
    ).fetchone()[0]
    tickers = conn.execute(
        "SELECT COUNT(DISTINCT ticker) FROM predictive_training_data WHERE trade_date >= ? AND trade_date <= ?",
        [min_date, max_date],
    ).fetchone()[0]

    logger.info("Training dataset: {} rows, {} tickers", total, tickers)
    return {"total": total, "tickers": tickers}


def get_training_stats(conn) -> Dict:
    """Get stats about the training dataset."""
    row = conn.execute("""
        SELECT COUNT(*) as total,
               COUNT(DISTINCT ticker) as tickers,
               MIN(trade_date) as min_date,
               MAX(trade_date) as max_date,
               ROUND(AVG(fwd_return_5d) * 100, 3) as avg_5d_pct,
               ROUND(AVG(CASE WHEN fwd_direction = 1 THEN 1.0 ELSE 0.0 END) * 100, 1) as pct_positive,
               COUNT(CASE WHEN conviction_score IS NOT NULL THEN 1 END) as has_conviction,
               COUNT(CASE WHEN rsi_14 IS NOT NULL THEN 1 END) as has_rsi,
               COUNT(CASE WHEN accum_phase IS NOT NULL THEN 1 END) as has_phase
        FROM predictive_training_data
    """).fetchone()

    return {
        "total_rows": row[0],
        "tickers": row[1],
        "min_date": str(row[2]),
        "max_date": str(row[3]),
        "avg_5d_return_pct": row[4],
        "pct_positive_direction": row[5],
        "has_conviction": row[6],
        "has_rsi": row[7],
        "has_phase": row[8],
        "feature_count": len(ALL_FEATURES),
        "excluded_v1": EXCLUDED_V1,
    }


def get_temporal_splits(conn) -> Dict:
    """Get recommended train/val/test split dates."""
    dates = conn.execute("""
        SELECT MIN(trade_date), MAX(trade_date),
               COUNT(DISTINCT trade_date) as trading_days
        FROM predictive_training_data
    """).fetchone()

    # Temporal split: ~60% train, ~20% val, ~20% test
    # Strictly non-overlapping: train ends before val starts, val ends before test starts
    all_dates = conn.execute("""
        SELECT DISTINCT trade_date FROM predictive_training_data ORDER BY trade_date
    """).fetchall()
    all_dates = [str(r[0]) for r in all_dates]
    n = len(all_dates)

    train_end_idx = int(n * 0.6)
    val_start_idx = train_end_idx + 1
    val_end_idx = int(n * 0.8)
    test_start_idx = val_end_idx + 1

    return {
        "total_dates": n,
        "train": {"start": all_dates[0], "end": all_dates[train_end_idx]},
        "val": {"start": all_dates[val_start_idx], "end": all_dates[val_end_idx]},
        "test": {"start": all_dates[test_start_idx], "end": all_dates[-1]},
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Predictive feature join engine")
    parser.add_argument("--build", action="store_true", help="Build training dataset")
    parser.add_argument("--stats", action="store_true", help="Show dataset stats")
    parser.add_argument("--splits", action="store_true", help="Show train/val/test splits")
    parser.add_argument("--min-date", default="2023-10-01")
    parser.add_argument("--max-date", default="2024-12-31")
    args = parser.parse_args()

    from signal_scanner.institutional_intel.config import safe_duckdb_connect

    if args.build:
        conn = safe_duckdb_connect(read_only=False)
        if conn:
            result = build_training_dataset(conn, args.min_date, args.max_date)
            print(f"Built: {result}")
            conn.close()

    if args.stats:
        conn = safe_duckdb_connect(read_only=True)
        if conn:
            stats = get_training_stats(conn)
            print("Training dataset stats:")
            for k, v in stats.items():
                print(f"  {k}: {v}")
            conn.close()

    if args.splits:
        conn = safe_duckdb_connect(read_only=True)
        if conn:
            splits = get_temporal_splits(conn)
            print("Temporal splits:")
            for k, v in splits.items():
                print(f"  {k}: {v}")
            conn.close()
