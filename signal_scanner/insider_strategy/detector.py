"""Daily Director-cluster detector — finds today's new tradeable clusters.

Mirrors the backtest definition (research/insider_strategy_backtest.py):
   * Open-market BUYS (transaction_code='P', direction='BUY')
   * >=2 distinct insiders inc. >=1 Director in trailing 30-day window
   * Cluster known at MAX(transaction_date) of the window + 2 SEC-lag days
   * Liquidity floor: price >= $5 and 20-day avg dollar volume >= $1M
   * Regime gate: SPY (or composite) above its 200-day SMA at known_date
   * Dedupe: skip tickers we've entered in the last 60 days
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import List, Dict, Any

import duckdb
from loguru import logger

from signal_scanner.institutional_intel.config import safe_duckdb_connect

CLUSTER_WINDOW_DAYS = 30
SEC_LAG_DAYS = 2
MIN_INSIDERS = 2
MIN_DIRECTORS = 1
PRICE_FLOOR = 5.0
ADV_FLOOR_DOLLARS = 1_000_000
DEDUPE_DAYS = 60
ATR_WINDOW = 14


def detect_new_clusters(as_of: date = None, lookback_days: int = 3
                        ) -> List[Dict[str, Any]]:
    """Return Director clusters that crystallised in the last `lookback_days`.

    Each result is a dict with: ticker, cluster_date, known_date, n_insiders,
    n_directors, n_officers, total_value, avg_buy_price, current_price, atr14,
    adv_20d, in_window (always True for fresh).
    """
    if as_of is None:
        as_of = date.today()
    earliest_cluster = as_of - timedelta(days=lookback_days + CLUSTER_WINDOW_DAYS)

    con = safe_duckdb_connect(read_only=True)
    if con is None:
        logger.warning("[INSIDER-DET] warehouse busy, skipping detection")
        return []

    try:
        rows = con.execute(f"""
            WITH buys AS (
                SELECT ticker, insider_name, insider_role, transaction_date,
                       shares, price
                FROM fact_form4_transactions
                WHERE transaction_code = 'P' AND upper(direction) = 'BUY'
                  AND ticker IS NOT NULL AND ticker <> ''
                  AND ticker NOT IN ('NONE', 'N/A', '--', '?', 'NULL')
                  AND LENGTH(ticker) BETWEEN 1 AND 6
                  AND transaction_date BETWEEN DATE '{earliest_cluster}' AND DATE '{as_of}'
                  AND shares > 0 AND price > 0
            ),
            clustered AS (
                SELECT b.ticker, b.transaction_date AS cluster_date,
                       COUNT(DISTINCT b2.insider_name) AS n_insiders,
                       COUNT(DISTINCT CASE WHEN b2.insider_role ILIKE '%director%'
                                           THEN b2.insider_name END) AS n_directors,
                       COUNT(DISTINCT CASE WHEN b2.insider_role ILIKE '%officer%'
                                           THEN b2.insider_name END) AS n_officers,
                       SUM(b2.shares * b2.price) AS total_value,
                       SUM(b2.shares) AS total_shares,
                       AVG(b2.price) AS avg_buy_price
                FROM buys b JOIN buys b2
                  ON b2.ticker = b.ticker
                 AND b2.transaction_date BETWEEN b.transaction_date - INTERVAL '{CLUSTER_WINDOW_DAYS}' DAY
                                             AND b.transaction_date
                GROUP BY b.ticker, b.transaction_date
                HAVING COUNT(DISTINCT b2.insider_name) >= {MIN_INSIDERS}
                   AND MAX(CASE WHEN b2.insider_role ILIKE '%director%' THEN 1 ELSE 0 END) >= {MIN_DIRECTORS}
            ),
            -- Keep only the LATEST cluster crystallisation date per ticker, and only
            -- those whose known_date (=+2d) falls in our recent lookback window.
            latest AS (
                SELECT ticker, MAX(cluster_date) AS cluster_date
                FROM clustered
                GROUP BY ticker
            )
            SELECT c.ticker, c.cluster_date, c.n_insiders, c.n_directors,
                   c.n_officers, c.total_value, c.total_shares, c.avg_buy_price
            FROM clustered c
            JOIN latest l ON l.ticker = c.ticker AND l.cluster_date = c.cluster_date
            WHERE c.cluster_date + INTERVAL '{SEC_LAG_DAYS}' DAY
                  BETWEEN DATE '{as_of - timedelta(days=lookback_days)}' AND DATE '{as_of}'
            ORDER BY c.cluster_date DESC, c.n_directors DESC
        """).fetchall()
    finally:
        try:
            con.close()
        except Exception:
            pass

    if not rows:
        return []

    # Enrich each with current price + ATR(14) + ADV(20d) — uses fresh DuckDB conn
    con = safe_duckdb_connect(read_only=True)
    if con is None:
        return []
    enriched = []
    try:
        for r in rows:
            (ticker, cluster_date, n_ins, n_dir, n_off,
             total_value, total_shares, avg_buy) = r
            known_date = cluster_date + timedelta(days=SEC_LAG_DAYS)

            # Pull last ~30 bars for current price + ATR + ADV
            px = con.execute(f"""
                SELECT trade_date, open, high, low, close, volume
                FROM fact_daily_prices
                WHERE ticker='{ticker}'
                  AND trade_date <= DATE '{as_of}'
                ORDER BY trade_date DESC LIMIT 35
            """).fetchall()
            if len(px) < 20:
                continue
            px = list(reversed(px))  # chronological
            current_price = float(px[-1][4])
            if current_price < PRICE_FLOOR:
                continue

            # ATR(14) on last 14+1 bars
            tr_vals = []
            for i in range(max(0, len(px) - 14), len(px)):
                if i == 0:
                    tr_vals.append(float(px[i][2] - px[i][3]))
                else:
                    h, l, pc = float(px[i][2]), float(px[i][3]), float(px[i - 1][4])
                    tr_vals.append(max(h - l, abs(h - pc), abs(l - pc)))
            atr14 = sum(tr_vals) / len(tr_vals) if tr_vals else current_price * 0.02

            # ADV (20d avg dollar volume)
            recent = px[-20:]
            adv_20d = sum(float(b[4]) * float(b[5]) for b in recent) / max(len(recent), 1)
            if adv_20d < ADV_FLOOR_DOLLARS:
                continue

            enriched.append({
                "ticker": ticker,
                "cluster_date": str(cluster_date),
                "known_date": str(known_date),
                "n_insiders": int(n_ins),
                "n_directors": int(n_dir),
                "n_officers": int(n_off),
                "total_value": float(total_value or 0),
                "total_shares": int(total_shares or 0),
                "avg_buy_price": round(float(avg_buy or 0), 4),
                "current_price": round(current_price, 4),
                "atr14": round(atr14, 4),
                "adv_20d": round(adv_20d, 0),
            })
    finally:
        try:
            con.close()
        except Exception:
            pass

    return enriched


def regime_allows_long(as_of: date = None) -> tuple:
    """Return (allowed, current_price, sma200) using SPY (or composite) at as_of.

    Same rule as the backtest's sensitivity 2: long-allowed iff close > 200-SMA.
    """
    if as_of is None:
        as_of = date.today()

    con = safe_duckdb_connect(read_only=True)
    if con is None:
        return (True, None, None)  # don't block on DB fault — fail-open
    try:
        # SPY first
        rows = con.execute(f"""
            SELECT trade_date, close FROM fact_daily_prices
            WHERE ticker='SPY' AND trade_date <= DATE '{as_of}'
            ORDER BY trade_date DESC LIMIT 220
        """).fetchall()
        if len(rows) < 200:
            # Composite fallback (SPY data ends 2024 per memory)
            rows = con.execute(f"""
                SELECT trade_date, AVG(close) FROM fact_daily_prices
                WHERE ticker IN ('AAPL','MSFT','NVDA','GOOGL','AMZN')
                  AND trade_date <= DATE '{as_of}'
                GROUP BY trade_date
                ORDER BY trade_date DESC LIMIT 220
            """).fetchall()
    finally:
        try:
            con.close()
        except Exception:
            pass

    if len(rows) < 200:
        return (True, None, None)
    rows = list(reversed(rows))
    closes = [float(r[1]) for r in rows]
    current = closes[-1]
    sma200 = sum(closes[-200:]) / 200
    return (current > sma200, current, sma200)
