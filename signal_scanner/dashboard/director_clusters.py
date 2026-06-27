"""Recent Director-cluster watchlist — the validated insider-swing edge.

Backtest in this repo (research/pond_trigger_backtest.py --pond-alone) showed:
  - All clusters (>=2 insiders, 30d):     +5.17% / 55.0% win at 60d vs baseline 2.86% / 52.0%
  - Director-involved:                    +5.93% / 55.8% win at 60d  *** strongest ***
  - Edge appears at 40-60d (slow drift), NOT same-day; this is a hold, not a trigger.

So the surface is a *watchlist* of names where Directors recently clustered, with
hold-window status — i.e. "smart money is buying these; here is where we are
inside the 60-day window." Point-in-time correct (transaction_date + 2d SEC lag).
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import List, Dict, Any

import duckdb

from signal_scanner.institutional_intel.config import safe_duckdb_connect

CLUSTER_WINDOW_DAYS = 30      # >=2 distinct insiders within trailing 30d
SEC_LAG_DAYS = 2              # cluster becomes "knowable" at last buy + 2d
THESIS_HOLD_DAYS = 60         # tradeable drift window per backtest
LOOKBACK_DAYS_VISIBLE = 60    # show clusters from last N days (active + recent past)
MIN_DIRECTORS = 1             # at least 1 director in the cluster (the edge driver)
MIN_INSIDERS = 2              # cluster threshold


def get_recent_director_clusters(limit: int = 12) -> List[Dict[str, Any]]:
    """Return up-to-`limit` recently-formed Director clusters with current state.

    Each row: ticker, cluster_date, knowable_date, days_since, days_remaining,
              n_insiders, n_directors, avg_buy_price, total_value,
              current_price, return_since_cluster_pct.
    """
    con = safe_duckdb_connect(read_only=True)
    if con is None:
        return []
    try:
        return _query(con, limit)
    finally:
        try:
            con.close()
        except Exception:
            pass


def _query(con: duckdb.DuckDBPyConnection, limit: int) -> List[Dict[str, Any]]:
    cutoff = (date.today() - timedelta(days=LOOKBACK_DAYS_VISIBLE)).isoformat()
    today = date.today().isoformat()
    rows = con.execute(f"""
        WITH buys AS (
            SELECT ticker, insider_name, insider_role, transaction_date,
                   shares, price
            FROM fact_form4_transactions
            WHERE transaction_code = 'P' AND upper(direction) = 'BUY'
              AND ticker IS NOT NULL AND ticker <> ''
              AND ticker NOT IN ('NONE', 'N/A', '--', '?', 'NULL')
              AND LENGTH(ticker) BETWEEN 1 AND 6
              AND transaction_date BETWEEN DATE '{cutoff}' - INTERVAL '{CLUSTER_WINDOW_DAYS}' DAY
                                       AND DATE '{today}'
              AND shares > 0 AND price > 0
        ),
        cluster_seed AS (
            -- For each ticker, find the day where the cluster crystallises
            -- (the day where the rolling-30d distinct-insider count first hits >= MIN_INSIDERS
            -- AND includes a Director, looking only at the LATEST such cluster per ticker).
            SELECT b.ticker, MAX(b.transaction_date) AS cluster_date
            FROM buys b JOIN buys b2
              ON b2.ticker = b.ticker
             AND b2.transaction_date BETWEEN b.transaction_date - INTERVAL '{CLUSTER_WINDOW_DAYS}' DAY
                                         AND b.transaction_date
            GROUP BY b.ticker, b.transaction_date
            HAVING COUNT(DISTINCT b2.insider_name) >= {MIN_INSIDERS}
               AND MAX(CASE WHEN b2.insider_role ILIKE '%director%' THEN 1 ELSE 0 END) >= {MIN_DIRECTORS}
        ),
        cluster_latest AS (
            SELECT ticker, MAX(cluster_date) AS cluster_date FROM cluster_seed
            GROUP BY ticker
        ),
        cluster_detail AS (
            SELECT cl.ticker, cl.cluster_date,
                   COUNT(DISTINCT b.insider_name) AS n_insiders,
                   COUNT(DISTINCT CASE WHEN b.insider_role ILIKE '%director%' THEN b.insider_name END) AS n_directors,
                   SUM(b.shares * b.price) AS total_value,
                   SUM(b.shares) AS total_shares,
                   AVG(b.price) AS avg_buy_price
            FROM cluster_latest cl
            JOIN buys b
              ON b.ticker = cl.ticker
             AND b.transaction_date BETWEEN cl.cluster_date - INTERVAL '{CLUSTER_WINDOW_DAYS}' DAY
                                         AND cl.cluster_date
            GROUP BY cl.ticker, cl.cluster_date
        ),
        latest_px AS (
            SELECT ticker, close, trade_date
            FROM (
                SELECT ticker, close, trade_date,
                       ROW_NUMBER() OVER (PARTITION BY ticker ORDER BY trade_date DESC) AS rn
                FROM fact_daily_prices
                WHERE trade_date >= DATE '{cutoff}' - INTERVAL '10' DAY
            ) WHERE rn = 1
        )
        SELECT d.ticker,
               d.cluster_date,
               d.n_insiders,
               d.n_directors,
               COALESCE(d.total_value, 0)   AS total_value,
               COALESCE(d.total_shares, 0)  AS total_shares,
               COALESCE(d.avg_buy_price, 0) AS avg_buy_price,
               COALESCE(p.close, 0)         AS current_price
        FROM cluster_detail d
        LEFT JOIN latest_px p ON p.ticker = d.ticker
        WHERE d.n_directors >= {MIN_DIRECTORS}
          AND COALESCE(p.close, 0) > 0
        ORDER BY d.cluster_date DESC, d.n_directors DESC, d.n_insiders DESC
        LIMIT {int(limit) * 2}
    """).fetchall()

    today_d = date.today()
    out: List[Dict[str, Any]] = []
    for r in rows:
        (ticker, cluster_date, n_ins, n_dir,
         total_value, total_shares, avg_buy, curr_px) = r
        knowable_date = cluster_date + timedelta(days=SEC_LAG_DAYS)
        days_since_knowable = (today_d - knowable_date).days
        days_remaining = THESIS_HOLD_DAYS - days_since_knowable
        if days_remaining < -7:
            continue  # window long expired
        ret_pct = ((float(curr_px) / float(avg_buy)) - 1.0) * 100 if avg_buy else 0.0
        out.append({
            "ticker": ticker,
            "cluster_date": str(cluster_date),
            "knowable_date": str(knowable_date),
            "n_insiders": int(n_ins),
            "n_directors": int(n_dir),
            "total_value": float(total_value or 0),
            "total_shares": int(total_shares or 0),
            "avg_buy_price": round(float(avg_buy or 0), 2),
            "current_price": round(float(curr_px or 0), 2),
            "return_since_cluster_pct": round(ret_pct, 2),
            "days_since_knowable": int(days_since_knowable),
            "days_remaining": int(days_remaining),
            "in_window": 0 <= days_since_knowable <= THESIS_HOLD_DAYS,
        })
        if len(out) >= limit:
            break
    return out


def get_cluster_tickers_in_window() -> set:
    """Return the set of tickers currently inside an active Director cluster window.

    Used by Sniper Board to tag rows with a 'Director cluster' badge.
    """
    return {c["ticker"] for c in get_recent_director_clusters(limit=200)
            if c["in_window"]}
