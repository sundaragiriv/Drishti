"""Predictive Labels — 3d/5d forward return computation.

Computes forward-looking labels for the predictive intelligence model.
Point-in-time safe: labels are computed from future prices that would
NOT be available at prediction time.

Labels:
  fwd_return_3d  — % return from close_t to close_t+3
  fwd_return_5d  — % return from close_t to close_t+5
  fwd_direction  — 1 if fwd_return_5d > 0, 0 if <= 0, NULL if no future price
  fwd_magnitude  — abs(fwd_return_5d), NULL if no future price

Persisted to: fact_predictive_labels (DuckDB)

Usage:
    python -m signal_scanner.institutional_intel.intelligence.predictive_labels --compute
    python -m signal_scanner.institutional_intel.intelligence.predictive_labels --verify
"""

from __future__ import annotations

import argparse
from datetime import datetime
from typing import Dict

from loguru import logger


CREATE_LABELS_TABLE = """
CREATE TABLE IF NOT EXISTS fact_predictive_labels (
    ticker          VARCHAR NOT NULL,
    trade_date      DATE    NOT NULL,
    close_price     DOUBLE  NOT NULL,
    -- Forward returns (computed from future closes)
    fwd_close_3d    DOUBLE,
    fwd_close_5d    DOUBLE,
    fwd_return_3d   DOUBLE,          -- (close_t+3 - close_t) / close_t
    fwd_return_5d   DOUBLE,          -- (close_t+5 - close_t) / close_t
    fwd_direction   INTEGER,         -- 1 if fwd_return_5d > 0, else 0
    fwd_magnitude   DOUBLE,          -- abs(fwd_return_5d)
    -- SPY benchmark for alpha computation
    spy_return_3d   DOUBLE,
    spy_return_5d   DOUBLE,
    fwd_alpha_3d    DOUBLE,          -- fwd_return_3d - spy_return_3d
    fwd_alpha_5d    DOUBLE,          -- fwd_return_5d - spy_return_5d
    -- Meta
    computed_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (ticker, trade_date)
);
"""


def compute_labels(
    conn,
    tickers: list = None,
    min_date: str = "2020-01-01",
    max_date: str = None,
) -> Dict[str, int]:
    """Compute 3d/5d forward return labels for all ticker-days.

    Point-in-time safe: uses only future close prices for labels.
    Does NOT compute any features — labels only.

    Args:
        conn: DuckDB write connection
        tickers: optional filter (default: all tickers in fact_daily_prices)
        min_date: earliest date to compute labels for
        max_date: latest date (default: max available minus 5 days)

    Returns: dict with row counts.
    """
    conn.execute(CREATE_LABELS_TABLE)

    if max_date is None:
        # Leave 5 trading days buffer so we have forward prices
        max_row = conn.execute(
            "SELECT MAX(trade_date) FROM fact_daily_prices"
        ).fetchone()
        max_date = str(max_row[0]) if max_row[0] else "2026-03-01"

    logger.info("Computing predictive labels: {} to {}", min_date, max_date)

    # Step 1: Compute SPY forward returns (used for alpha computation)
    logger.info("Step 1: SPY forward returns...")
    spy_df = conn.execute("""
        SELECT trade_date, close,
               LEAD(close, 3) OVER (ORDER BY trade_date) as fwd_3,
               LEAD(close, 5) OVER (ORDER BY trade_date) as fwd_5
        FROM fact_daily_prices
        WHERE ticker = 'SPY' AND trade_date >= ? AND close > 0
        ORDER BY trade_date
    """, [min_date]).fetchdf()

    spy_returns = {}
    for _, row in spy_df.iterrows():
        d = str(row["trade_date"])
        r3 = round((row["fwd_3"] - row["close"]) / row["close"], 6) if row["fwd_3"] and row["close"] else None
        r5 = round((row["fwd_5"] - row["close"]) / row["close"], 6) if row["fwd_5"] and row["close"] else None
        spy_returns[d] = (r3, r5)
    logger.info("SPY returns: {} trading days", len(spy_returns))

    # Step 2: Compute forward returns for all tickers using window functions
    logger.info("Step 2: All-ticker forward returns (window function)...")
    conn.execute("""
        INSERT INTO fact_predictive_labels
            (ticker, trade_date, close_price,
             fwd_close_3d, fwd_close_5d,
             fwd_return_3d, fwd_return_5d,
             fwd_direction, fwd_magnitude)
        SELECT
            ticker, trade_date, close,
            LEAD(close, 3) OVER (PARTITION BY ticker ORDER BY trade_date) as fwd_close_3d,
            LEAD(close, 5) OVER (PARTITION BY ticker ORDER BY trade_date) as fwd_close_5d,
            ROUND((LEAD(close, 3) OVER (PARTITION BY ticker ORDER BY trade_date) - close)
                  / NULLIF(close, 0), 6),
            ROUND((LEAD(close, 5) OVER (PARTITION BY ticker ORDER BY trade_date) - close)
                  / NULLIF(close, 0), 6),
            CASE WHEN LEAD(close, 5) OVER (PARTITION BY ticker ORDER BY trade_date) IS NULL THEN NULL
                 WHEN LEAD(close, 5) OVER (PARTITION BY ticker ORDER BY trade_date) > close THEN 1
                 ELSE 0 END,
            CASE WHEN LEAD(close, 5) OVER (PARTITION BY ticker ORDER BY trade_date) IS NULL THEN NULL
                 ELSE ROUND(ABS((LEAD(close, 5) OVER (PARTITION BY ticker ORDER BY trade_date) - close)
                  / NULLIF(close, 0)), 6) END
        FROM fact_daily_prices
        WHERE trade_date >= ? AND trade_date <= ? AND close > 0
        ON CONFLICT (ticker, trade_date) DO UPDATE SET
            fwd_close_3d = excluded.fwd_close_3d,
            fwd_close_5d = excluded.fwd_close_5d,
            fwd_return_3d = excluded.fwd_return_3d,
            fwd_return_5d = excluded.fwd_return_5d,
            fwd_direction = excluded.fwd_direction,
            fwd_magnitude = excluded.fwd_magnitude,
            computed_at = excluded.computed_at
    """, [min_date, max_date])

    # Step 3: SPY alpha — batch update via self-join on SPY's own labels
    logger.info("Step 3: SPY alpha computation (batch)...")
    conn.execute("""
        UPDATE fact_predictive_labels SET
            spy_return_3d = spy.fwd_return_3d,
            spy_return_5d = spy.fwd_return_5d,
            fwd_alpha_3d = ROUND(fact_predictive_labels.fwd_return_3d - COALESCE(spy.fwd_return_3d, 0), 6),
            fwd_alpha_5d = ROUND(fact_predictive_labels.fwd_return_5d - COALESCE(spy.fwd_return_5d, 0), 6)
        FROM fact_predictive_labels spy
        WHERE spy.ticker = 'SPY'
          AND spy.trade_date = fact_predictive_labels.trade_date
          AND fact_predictive_labels.ticker != 'SPY'
    """)

    # Count results
    total = conn.execute(
        "SELECT COUNT(*) FROM fact_predictive_labels WHERE trade_date >= ?",
        [min_date],
    ).fetchone()[0]
    with_5d = conn.execute(
        "SELECT COUNT(*) FROM fact_predictive_labels WHERE fwd_return_5d IS NOT NULL AND trade_date >= ?",
        [min_date],
    ).fetchone()[0]

    logger.info("Labels computed: {} total rows, {} with 5d forward returns", total, with_5d)
    return {"total": total, "with_5d_return": with_5d}


def verify_labels(conn) -> Dict:
    """Verify label quality and distribution."""
    stats = {}

    row = conn.execute("""
        SELECT COUNT(*) as total,
               COUNT(CASE WHEN fwd_return_5d IS NOT NULL THEN 1 END) as has_5d,
               MIN(trade_date) as min_date,
               MAX(trade_date) as max_date,
               COUNT(DISTINCT ticker) as tickers,
               ROUND(AVG(fwd_return_5d) * 100, 3) as avg_5d_pct,
               ROUND(STDDEV(fwd_return_5d) * 100, 3) as std_5d_pct,
               ROUND(AVG(CASE WHEN fwd_direction = 1 THEN 1.0 ELSE 0.0 END) * 100, 1) as pct_positive
        FROM fact_predictive_labels
        WHERE fwd_return_5d IS NOT NULL
    """).fetchone()

    stats = {
        "total_rows": row[0],
        "rows_with_5d": row[1],
        "min_date": str(row[2]),
        "max_date": str(row[3]),
        "tickers": row[4],
        "avg_5d_return_pct": row[5],
        "std_5d_return_pct": row[6],
        "pct_positive_direction": row[7],
    }
    return stats


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Compute predictive labels")
    parser.add_argument("--compute", action="store_true", help="Compute labels")
    parser.add_argument("--verify", action="store_true", help="Verify label quality")
    parser.add_argument("--min-date", default="2020-01-01")
    parser.add_argument("--max-date", default=None)
    args = parser.parse_args()

    from signal_scanner.institutional_intel.config import safe_duckdb_connect

    if args.compute:
        conn = safe_duckdb_connect(read_only=False)
        if conn:
            result = compute_labels(conn, min_date=args.min_date, max_date=args.max_date)
            print(f"Labels: {result}")
            conn.close()

    if args.verify:
        conn = safe_duckdb_connect(read_only=True)
        if conn:
            stats = verify_labels(conn)
            print("Label verification:")
            for k, v in stats.items():
                print(f"  {k}: {v}")
            conn.close()
