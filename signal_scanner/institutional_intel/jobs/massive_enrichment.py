"""Massive Data Enrichment — exploit Polygon Stocks + Options Starter.

Warehouse enrichment from Polygon beyond basic daily OHLCV:
  - Stock snapshots (grouped daily bars for all tickers)
  - Corporate actions (splits + dividends for top tickers)
  - Reference data refresh (issuer name + sector from Polygon ticker details)
  - Related companies refresh (peer relationships for interconnected features)

Note: These tables enrich the warehouse for analytics and future feature
engineering. Not all are consumed by product surfaces yet.
Minute-bar history backfill is handled by existing massive_loader.py.

Role split:
  - IBKR = live execution data (real-time bars for strategies)
  - Massive = delayed intelligence / historical / analytics (15-min delay)

Usage:
    python -m signal_scanner.institutional_intel.jobs.massive_enrichment --snapshots
    python -m signal_scanner.institutional_intel.jobs.massive_enrichment --corporate-actions
    python -m signal_scanner.institutional_intel.jobs.massive_enrichment --reference-data
    python -m signal_scanner.institutional_intel.jobs.massive_enrichment --related
    python -m signal_scanner.institutional_intel.jobs.massive_enrichment --all
"""

from __future__ import annotations

import argparse
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, List

import requests
from loguru import logger

from signal_scanner.core.readiness import latest_complete_trading_day


POLYGON_BASE = "https://api.polygon.io"


def _get_api_key() -> str:
    key = os.environ.get("MASSIVE_API_KEY", "")
    if not key:
        try:
            env_path = os.path.join(os.path.dirname(__file__), "..", "..", "..", ".env")
            for line in open(env_path):
                if line.startswith("MASSIVE_API_KEY="):
                    key = line.split("=", 1)[1].strip()
                    break
        except Exception:
            pass
    return key


# ---------------------------------------------------------------------------
# E4: Stock Snapshots
# ---------------------------------------------------------------------------

def load_stock_snapshots(conn, tickers: List[str] = None, rps: float = 5.0) -> int:
    """Load grouped daily bars for all tickers via Polygon grouped daily endpoint.

    Stores OHLCV + VWAP in fact_stock_snapshots. One API call covers all tickers.
    This is warehouse enrichment — not yet consumed by product surfaces.
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS fact_stock_snapshots (
            ticker          VARCHAR NOT NULL,
            snapshot_date   DATE    NOT NULL,
            open            DOUBLE,
            high            DOUBLE,
            low             DOUBLE,
            close           DOUBLE,
            volume          BIGINT,
            vwap            DOUBLE,
            source          VARCHAR DEFAULT 'polygon_grouped_daily',
            snapshot_ts     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (ticker, snapshot_date)
        )
    """)

    api_key = _get_api_key()
    if not api_key:
        logger.error("No API key")
        return 0

    # Use grouped daily for efficiency (all tickers in one call)
    target_date = latest_complete_trading_day().strftime("%Y-%m-%d")
    r = requests.get(
        f"{POLYGON_BASE}/v2/aggs/grouped/locale/us/market/stocks/{target_date}",
        params={"apiKey": api_key, "adjusted": "true"},
        timeout=30,
    )
    if r.status_code != 200:
        # Fallback one more business day in case the latest session is delayed.
        from datetime import timedelta
        yesterday = (latest_complete_trading_day() - timedelta(days=1)).strftime("%Y-%m-%d")
        r = requests.get(
            f"{POLYGON_BASE}/v2/aggs/grouped/locale/us/market/stocks/{yesterday}",
            params={"apiKey": api_key, "adjusted": "true"},
            timeout=30,
        )
        target_date = yesterday

    if r.status_code != 200:
        logger.warning("Grouped daily snapshot failed: {}", r.status_code)
        return 0

    data = r.json()
    results = data.get("results", [])
    now = datetime.now(timezone.utc).isoformat()
    written = 0

    ticker_filter = set(tickers) if tickers else None

    for bar in results:
        ticker = bar.get("T", "")
        if ticker_filter and ticker not in ticker_filter:
            continue
        try:
            conn.execute("""
                INSERT INTO fact_stock_snapshots
                    (ticker, snapshot_date, open, high, low, close, volume, vwap, snapshot_ts)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (ticker, snapshot_date) DO UPDATE SET
                    open = excluded.open, high = excluded.high,
                    low = excluded.low, close = excluded.close,
                    volume = excluded.volume, vwap = excluded.vwap,
                    snapshot_ts = excluded.snapshot_ts
            """, [
                ticker, target_date,
                bar.get("o"), bar.get("h"), bar.get("l"), bar.get("c"),
                bar.get("v"), bar.get("vw"), now,
            ])
            written += 1
        except Exception:
            pass

    logger.info("Stock snapshots: {} tickers for {}", written, target_date)
    return written


# ---------------------------------------------------------------------------
# E6: Corporate Actions
# ---------------------------------------------------------------------------

def load_corporate_actions(conn, tickers: List[str] = None, rps: float = 3.0) -> Dict[str, int]:
    """Load splits and dividends from Polygon reference endpoints."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS fact_corporate_actions (
            ticker          VARCHAR NOT NULL,
            action_type     VARCHAR NOT NULL,       -- SPLIT or DIVIDEND
            execution_date  DATE,
            record_date     DATE,
            -- Split fields
            split_from      DOUBLE,
            split_to        DOUBLE,
            -- Dividend fields
            cash_amount     DOUBLE,
            dividend_type   VARCHAR,
            frequency       INTEGER,
            -- Meta
            source          VARCHAR DEFAULT 'polygon',
            ingested_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (ticker, action_type, execution_date)
        )
    """)

    api_key = _get_api_key()
    if not api_key:
        return {"splits": 0, "dividends": 0}

    if tickers is None:
        # Get top tickers from intelligence
        rows = conn.execute("""
            SELECT DISTINCT ticker FROM intelligence_scores
            WHERE report_quarter = (SELECT MAX(report_quarter) FROM intelligence_scores
                                    WHERE data_quality_score >= 75)
            AND conviction_score >= 50
            ORDER BY conviction_score DESC LIMIT 200
        """).fetchall()
        tickers = [r[0] for r in rows]

    splits_loaded = 0
    divs_loaded = 0

    for i, ticker in enumerate(tickers):
        # Splits
        try:
            r = requests.get(
                f"{POLYGON_BASE}/v3/reference/splits",
                params={"apiKey": api_key, "ticker": ticker, "limit": 10},
                timeout=10,
            )
            if r.status_code == 200:
                for s in r.json().get("results", []):
                    try:
                        conn.execute("""
                            INSERT INTO fact_corporate_actions
                                (ticker, action_type, execution_date, split_from, split_to)
                            VALUES (?, 'SPLIT', ?, ?, ?)
                            ON CONFLICT DO NOTHING
                        """, [ticker, s.get("execution_date"), s.get("split_from"), s.get("split_to")])
                        splits_loaded += 1
                    except Exception:
                        pass
        except Exception:
            pass

        # Dividends
        try:
            r = requests.get(
                f"{POLYGON_BASE}/v3/reference/dividends",
                params={"apiKey": api_key, "ticker": ticker, "limit": 10},
                timeout=10,
            )
            if r.status_code == 200:
                for d in r.json().get("results", []):
                    try:
                        conn.execute("""
                            INSERT INTO fact_corporate_actions
                                (ticker, action_type, execution_date, record_date,
                                 cash_amount, dividend_type, frequency)
                            VALUES (?, 'DIVIDEND', ?, ?, ?, ?, ?)
                            ON CONFLICT DO NOTHING
                        """, [
                            ticker, d.get("ex_dividend_date"), d.get("record_date"),
                            d.get("cash_amount"), d.get("dividend_type"), d.get("frequency"),
                        ])
                        divs_loaded += 1
                    except Exception:
                        pass
        except Exception:
            pass

        if i < len(tickers) - 1:
            time.sleep(1.0 / rps)

        if (i + 1) % 50 == 0:
            logger.info("  Corporate actions: {}/{} tickers processed", i + 1, len(tickers))

    logger.info("Corporate actions: {} splits, {} dividends from {} tickers",
                splits_loaded, divs_loaded, len(tickers))
    return {"splits": splits_loaded, "dividends": divs_loaded}


# ---------------------------------------------------------------------------
# E6: Reference Data Hygiene
# ---------------------------------------------------------------------------

def refresh_reference_data(conn, tickers: List[str] = None, rps: float = 5.0) -> int:
    """Refresh ticker details (sector, name, type, market cap) from Polygon."""
    api_key = _get_api_key()
    if not api_key:
        return 0

    if tickers is None:
        rows = conn.execute("""
            SELECT DISTINCT ticker FROM intelligence_scores
            WHERE report_quarter = (SELECT MAX(report_quarter) FROM intelligence_scores
                                    WHERE data_quality_score >= 75)
            AND conviction_score >= 50
            ORDER BY conviction_score DESC LIMIT 200
        """).fetchall()
        tickers = [r[0] for r in rows]

    updated = 0
    for i, ticker in enumerate(tickers):
        try:
            r = requests.get(
                f"{POLYGON_BASE}/v3/reference/tickers/{ticker}",
                params={"apiKey": api_key},
                timeout=10,
            )
            if r.status_code == 200:
                result = r.json().get("results", {})
                name = result.get("name", "")
                sector = result.get("sic_description", "")
                market_cap = result.get("market_cap")
                ticker_type = result.get("type", "")

                if name or sector:
                    conn.execute("""
                        UPDATE dim_issuer SET
                            issuer_name = COALESCE(?, issuer_name),
                            sector = CASE WHEN ? != '' THEN ? ELSE sector END
                        WHERE ticker = ?
                    """, [name, sector, sector, ticker])
                    updated += 1
        except Exception:
            pass

        if i < len(tickers) - 1:
            time.sleep(1.0 / rps)

        if (i + 1) % 50 == 0:
            logger.info("  Reference data: {}/{} tickers", i + 1, len(tickers))

    logger.info("Reference data: {} tickers updated", updated)
    return updated


# ---------------------------------------------------------------------------
# E3: Related Companies Refresh
# ---------------------------------------------------------------------------

def refresh_related_companies(conn, tickers: List[str] = None, rps: float = 3.0) -> int:
    """Refresh dim_related_companies from Polygon related-companies endpoint."""
    api_key = _get_api_key()
    if not api_key:
        return 0

    if tickers is None:
        rows = conn.execute("""
            SELECT DISTINCT ticker FROM intelligence_scores
            WHERE report_quarter = (SELECT MAX(report_quarter) FROM intelligence_scores
                                    WHERE data_quality_score >= 75)
            AND conviction_score >= 60
            ORDER BY conviction_score DESC LIMIT 100
        """).fetchall()
        tickers = [r[0] for r in rows]

    now = datetime.now(timezone.utc).isoformat()
    inserted = 0

    for i, ticker in enumerate(tickers):
        try:
            r = requests.get(
                f"{POLYGON_BASE}/v1/related-companies/{ticker}",
                params={"apiKey": api_key},
                timeout=10,
            )
            if r.status_code == 200:
                for rel in r.json().get("results", []):
                    rel_ticker = rel.get("ticker", "")
                    if not rel_ticker:
                        continue
                    try:
                        conn.execute("""
                            INSERT INTO dim_related_companies
                                (ticker, related_ticker, relationship_type, source, ingested_at)
                            VALUES (?, ?, 'polygon_related', 'polygon', ?)
                            ON CONFLICT DO NOTHING
                        """, [ticker, rel_ticker, now])
                        inserted += 1
                    except Exception:
                        pass
        except Exception:
            pass

        if i < len(tickers) - 1:
            time.sleep(1.0 / rps)

    logger.info("Related companies: {} new relationships from {} tickers", inserted, len(tickers))
    return inserted


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Massive data enrichment")
    parser.add_argument("--snapshots", action="store_true", help="Load stock snapshots")
    parser.add_argument("--corporate-actions", action="store_true", help="Load splits + dividends")
    parser.add_argument("--reference-data", action="store_true", help="Refresh ticker details")
    parser.add_argument("--related", action="store_true", help="Refresh related companies")
    parser.add_argument("--all", action="store_true", help="Run all enrichment")
    args = parser.parse_args()

    from signal_scanner.institutional_intel.config import safe_duckdb_connect

    conn = safe_duckdb_connect(read_only=False)
    if not conn:
        logger.error("Cannot connect to DuckDB")
        exit(1)

    try:
        if args.snapshots or args.all:
            load_stock_snapshots(conn)
        if args.corporate_actions or args.all:
            load_corporate_actions(conn)
        if args.reference_data or args.all:
            refresh_reference_data(conn)
        if args.related or args.all:
            refresh_related_companies(conn)
    finally:
        conn.close()
