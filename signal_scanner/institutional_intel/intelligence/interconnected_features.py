"""Interconnected Stocks Feature Family — peer/sector/leader-follower features.

Computes per ticker-day features based on related stocks' recent behavior:
  - Peer momentum: average 5d/20d return of related stocks
  - Peer momentum spread: ticker return minus peer average (leader vs laggard)
  - Sector breadth: % of sector stocks above their 20SMA
  - Cluster confirmation: how many peers are in accumulation / have insider clusters

All features are point-in-time safe (use only data up to trade_date).

Usage:
    python -m signal_scanner.institutional_intel.intelligence.interconnected_features --compute
"""

from __future__ import annotations

import argparse
from typing import Dict

from loguru import logger


CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS fact_interconnected_features (
    ticker              VARCHAR NOT NULL,
    trade_date          DATE    NOT NULL,
    -- Peer momentum (from dim_related_companies + fact_daily_prices)
    peer_avg_ret_5d     DOUBLE,     -- avg 5d return of related stocks
    peer_avg_ret_20d    DOUBLE,     -- avg 20d return of related stocks
    peer_momentum_spread DOUBLE,    -- ticker ret_5d minus peer_avg_ret_5d (leader/laggard)
    peer_count          INTEGER,    -- how many peers had price data
    -- Sector breadth (from dim_issuer + fact_daily_prices)
    sector_breadth_20d  DOUBLE,     -- % of sector stocks with close > SMA20
    sector_avg_ret_5d   DOUBLE,     -- avg 5d return for sector
    sector_avg_ret_20d  DOUBLE,     -- avg 20d return for sector
    sector_ticker_count INTEGER,    -- how many sector stocks had data
    -- Cluster confirmation (from intelligence_scores)
    peers_in_accum      INTEGER,    -- how many peers are in ACCUM phase
    peers_with_insider   INTEGER,   -- how many peers have insider clusters
    peer_avg_conviction DOUBLE,     -- avg conviction of peers
    -- Meta
    sector              VARCHAR,
    computed_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (ticker, trade_date)
);
"""


def compute_interconnected_features(
    conn,
    min_date: str = "2023-10-01",
    max_date: str = "2024-12-31",
) -> Dict[str, int]:
    """Compute interconnected stock features for all ticker-days.

    Uses:
      - dim_related_companies for peer relationships
      - fact_daily_prices for peer returns + sector breadth
      - dim_issuer for sector mapping
      - intelligence_scores for peer institutional state

    All lookups use data available on or before trade_date.
    """
    conn.execute(CREATE_TABLE)

    logger.info("Computing interconnected features: {} to {}", min_date, max_date)

    # Step 1: Peer momentum features
    logger.info("Step 1: Peer momentum features...")
    conn.execute("""
        INSERT INTO fact_interconnected_features
            (ticker, trade_date, peer_avg_ret_5d, peer_avg_ret_20d,
             peer_momentum_spread, peer_count, sector)
        SELECT
            t.ticker, t.trade_date,
            -- Peer avg 5d return
            AVG(peer_ret.ret_5d) as peer_avg_ret_5d,
            AVG(peer_ret.ret_20d) as peer_avg_ret_20d,
            -- Leader/laggard spread
            t_ret.ret_5d - AVG(peer_ret.ret_5d) as peer_momentum_spread,
            COUNT(peer_ret.ret_5d) as peer_count,
            iss.sector
        FROM (
            SELECT DISTINCT ticker, trade_date FROM fact_swing_features
            WHERE trade_date >= ? AND trade_date <= ?
        ) t
        JOIN dim_related_companies rc ON t.ticker = rc.ticker
        LEFT JOIN dim_issuer iss ON t.ticker = iss.ticker
        -- Peer returns (point-in-time: only data up to trade_date)
        LEFT JOIN (
            SELECT p.ticker, p.trade_date,
                   (p.close - LAG(p.close, 5) OVER (PARTITION BY p.ticker ORDER BY p.trade_date))
                   / NULLIF(LAG(p.close, 5) OVER (PARTITION BY p.ticker ORDER BY p.trade_date), 0) as ret_5d,
                   (p.close - LAG(p.close, 20) OVER (PARTITION BY p.ticker ORDER BY p.trade_date))
                   / NULLIF(LAG(p.close, 20) OVER (PARTITION BY p.ticker ORDER BY p.trade_date), 0) as ret_20d
            FROM fact_daily_prices p
            WHERE p.trade_date >= ? AND p.close > 0
        ) peer_ret ON rc.related_ticker = peer_ret.ticker AND t.trade_date = peer_ret.trade_date
        -- Ticker's own return for spread calc
        LEFT JOIN (
            SELECT p2.ticker, p2.trade_date,
                   (p2.close - LAG(p2.close, 5) OVER (PARTITION BY p2.ticker ORDER BY p2.trade_date))
                   / NULLIF(LAG(p2.close, 5) OVER (PARTITION BY p2.ticker ORDER BY p2.trade_date), 0) as ret_5d
            FROM fact_daily_prices p2
            WHERE p2.trade_date >= ? AND p2.close > 0
        ) t_ret ON t.ticker = t_ret.ticker AND t.trade_date = t_ret.trade_date
        GROUP BY t.ticker, t.trade_date, t_ret.ret_5d, iss.sector
        ON CONFLICT (ticker, trade_date) DO UPDATE SET
            peer_avg_ret_5d = excluded.peer_avg_ret_5d,
            peer_avg_ret_20d = excluded.peer_avg_ret_20d,
            peer_momentum_spread = excluded.peer_momentum_spread,
            peer_count = excluded.peer_count,
            sector = excluded.sector
    """, [min_date, max_date,
          # peer_ret needs lookback
          str(int(min_date[:4]) - 1) + min_date[4:],
          str(int(min_date[:4]) - 1) + min_date[4:]])

    # Step 2: Sector breadth
    logger.info("Step 2: Sector breadth features...")
    conn.execute("""
        UPDATE fact_interconnected_features f SET
            sector_breadth_20d = sector_stats.breadth,
            sector_avg_ret_5d = sector_stats.avg_ret_5d,
            sector_avg_ret_20d = sector_stats.avg_ret_20d,
            sector_ticker_count = sector_stats.cnt
        FROM (
            SELECT iss.sector, p.trade_date,
                   AVG(CASE WHEN sf.close > sf.sma_20 THEN 1.0 ELSE 0.0 END) as breadth,
                   AVG(sf.roc_5 / 100.0) as avg_ret_5d,
                   AVG(sf.roc_20 / 100.0) as avg_ret_20d,
                   COUNT(*) as cnt
            FROM fact_swing_features sf
            JOIN dim_issuer iss ON sf.ticker = iss.ticker
            JOIN fact_daily_prices p ON sf.ticker = p.ticker AND sf.trade_date = p.trade_date
            WHERE sf.trade_date >= ? AND sf.trade_date <= ?
              AND iss.sector IS NOT NULL AND iss.sector != ''
            GROUP BY iss.sector, p.trade_date
        ) sector_stats
        WHERE f.sector = sector_stats.sector
          AND f.trade_date = sector_stats.trade_date
    """, [min_date, max_date])

    # Step 3: Peer institutional state (cluster confirmation)
    # Point-in-time safe: map each trade_date to its settled quarter
    logger.info("Step 3: Peer institutional confirmation (point-in-time)...")
    from signal_scanner.institutional_intel.intelligence.predictive_features import _settled_quarter
    from collections import defaultdict

    # Get all distinct trade_dates in the feature table
    feat_dates = conn.execute("""
        SELECT DISTINCT trade_date FROM fact_interconnected_features
        WHERE trade_date >= ? AND trade_date <= ?
        ORDER BY trade_date
    """, [min_date, max_date]).fetchall()

    # Group dates by settled quarter
    sq_groups = defaultdict(list)
    for (d,) in feat_dates:
        sq = _settled_quarter(str(d))
        sq_groups[sq].append(str(d))

    for sq, dates in sq_groups.items():
        d_min = dates[0]
        d_max = dates[-1]
        conn.execute("""
            UPDATE fact_interconnected_features f SET
                peers_in_accum = peer_intel.accum_count,
                peers_with_insider = peer_intel.insider_count,
                peer_avg_conviction = peer_intel.avg_conv
            FROM (
                SELECT rc.ticker,
                       SUM(CASE WHEN iscore.accum_phase IN ('ACTIVE_ACCUM','LATE_ACCUM','EARLY_ACCUM') THEN 1 ELSE 0 END) as accum_count,
                       SUM(CASE WHEN iscore.insider_cluster_detected THEN 1 ELSE 0 END) as insider_count,
                       AVG(iscore.conviction_score) as avg_conv
                FROM dim_related_companies rc
                JOIN intelligence_scores iscore
                    ON rc.related_ticker = iscore.ticker
                    AND iscore.report_quarter = ?
                GROUP BY rc.ticker
            ) peer_intel
            WHERE f.ticker = peer_intel.ticker
              AND f.trade_date >= ? AND f.trade_date <= ?
        """, [sq, d_min, d_max])
        logger.debug("  Peer intel for {} ({} to {}): settled quarter {}", len(dates), d_min, d_max, sq)

    # Count results
    total = conn.execute(
        "SELECT COUNT(*) FROM fact_interconnected_features WHERE trade_date >= ?", [min_date]
    ).fetchone()[0]
    with_peers = conn.execute(
        "SELECT COUNT(*) FROM fact_interconnected_features WHERE peer_count > 0 AND trade_date >= ?", [min_date]
    ).fetchone()[0]

    logger.info("Interconnected features: {} total, {} with peer data", total, with_peers)
    return {"total": total, "with_peers": with_peers}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Compute interconnected stock features")
    parser.add_argument("--compute", action="store_true")
    parser.add_argument("--min-date", default="2023-10-01")
    parser.add_argument("--max-date", default="2024-12-31")
    args = parser.parse_args()

    from signal_scanner.institutional_intel.config import safe_duckdb_connect

    if args.compute:
        conn = safe_duckdb_connect(read_only=False)
        if conn:
            result = compute_interconnected_features(conn, args.min_date, args.max_date)
            print(f"Result: {result}")
            conn.close()
