"""Polygon 1-minute bar loader for ORB Edge backtesting.

Fetches 1-minute OHLCV bars from Polygon.io and stores them in the
DuckDB warehouse table ``fact_intraday_bars``.  Bars are cached — once a
ticker×day is loaded, subsequent runs skip it.

Usage:
    # Specific tickers and date range
    python -m signal_scanner.institutional_intel.jobs.intraday_loader \
        --tickers AAPL,PLTR,NVDA --from 2024-10-01 --to 2024-12-31

    # Auto-load qualifying tickers from intelligence_scores
    python -m signal_scanner.institutional_intel.jobs.intraday_loader \
        --from-intelligence --quarters 2024-Q3,2024-Q4,2025-Q1
"""

from __future__ import annotations

import argparse
import calendar
import time
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Optional, Set, Tuple
from zoneinfo import ZoneInfo

import duckdb
import pandas as pd
import requests
from loguru import logger

from signal_scanner.institutional_intel.config import (
    MASSIVE_API_KEY,
    MASSIVE_BASE_URL,
    WAREHOUSE_PATH,
)

ET = ZoneInfo("America/New_York")

# Rate limiting — 4 requests/sec (conservative for paid tier)
_MIN_REQUEST_INTERVAL = 0.25


# ---------------------------------------------------------------------------
# API helpers (mirrors massive_loader.py)
# ---------------------------------------------------------------------------

def _get_api_key() -> str:
    if not MASSIVE_API_KEY:
        raise ValueError(
            "MASSIVE_API_KEY not set. Set it as an environment variable or in .env"
        )
    return MASSIVE_API_KEY


def _api_get(url: str, params: Optional[Dict] = None) -> Dict:
    api_key = _get_api_key()
    params = params or {}
    params["apiKey"] = api_key
    resp = requests.get(url, params=params, timeout=60)
    resp.raise_for_status()
    return resp.json()


def _ts_to_et(unix_ms: int) -> datetime:
    """Convert Polygon Unix-ms timestamp to US/Eastern datetime."""
    return datetime.fromtimestamp(unix_ms / 1000, tz=timezone.utc).astimezone(ET)


# ---------------------------------------------------------------------------
# DuckDB table
# ---------------------------------------------------------------------------

def _ensure_table(conn: duckdb.DuckDBPyConnection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS fact_intraday_bars (
            ticker       TEXT NOT NULL,
            bar_time     TIMESTAMP NOT NULL,
            open         DOUBLE,
            high         DOUBLE,
            low          DOUBLE,
            close        DOUBLE,
            volume       BIGINT,
            vwap         DOUBLE,
            transactions INTEGER,
            PRIMARY KEY (ticker, bar_time)
        )
    """)


def _existing_dates(conn: duckdb.DuckDBPyConnection, ticker: str) -> Set[date]:
    """Return set of dates already loaded for this ticker."""
    rows = conn.execute("""
        SELECT DISTINCT CAST(bar_time AS DATE) AS d
        FROM fact_intraday_bars
        WHERE ticker = ?
    """, [ticker]).fetchall()
    return {r[0] for r in rows}


# ---------------------------------------------------------------------------
# Polygon fetcher
# ---------------------------------------------------------------------------

def _fetch_minute_bars(
    ticker: str,
    from_date: date,
    to_date: date,
) -> List[Dict]:
    """Fetch 1-minute bars from Polygon for a ticker and date range.

    Handles pagination via next_url if results exceed 50K.
    Returns list of dicts ready for DataFrame conversion.
    """
    all_rows: List[Dict] = []
    url = (
        f"{MASSIVE_BASE_URL}/v2/aggs/ticker/{ticker}/range/1/minute/"
        f"{from_date.isoformat()}/{to_date.isoformat()}"
    )
    params = {"adjusted": "true", "sort": "asc", "limit": "50000"}

    while url:
        data = _api_get(url, params)
        results = data.get("results", [])

        for r in results:
            ts = r.get("t")
            if ts is None:
                continue
            bar_dt = _ts_to_et(ts)
            all_rows.append({
                "ticker": ticker,
                "bar_time": bar_dt.replace(tzinfo=None),  # store as naive ET
                "open": r.get("o"),
                "high": r.get("h"),
                "low": r.get("l"),
                "close": r.get("c"),
                "volume": int(r.get("v", 0)),
                "vwap": r.get("vw"),
                "transactions": int(r.get("n", 0)),
            })

        # Pagination
        next_url = data.get("next_url")
        if next_url:
            url = next_url
            params = {}  # next_url already has params except apiKey
            time.sleep(_MIN_REQUEST_INTERVAL)
        else:
            url = None

    return all_rows


def _month_chunks(from_date: date, to_date: date) -> List[Tuple[date, date]]:
    """Split a date range into month-sized chunks for efficient API calls."""
    chunks = []
    cursor = from_date
    while cursor <= to_date:
        _, last_day = calendar.monthrange(cursor.year, cursor.month)
        month_end = date(cursor.year, cursor.month, last_day)
        chunk_end = min(month_end, to_date)
        chunks.append((cursor, chunk_end))
        cursor = chunk_end + timedelta(days=1)
    return chunks


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def load_intraday_bars(
    conn: duckdb.DuckDBPyConnection,
    tickers: List[str],
    from_date: date,
    to_date: date,
) -> int:
    """Load 1-minute bars for the given tickers and date range.

    Skips dates already in the warehouse. Returns total rows inserted.
    """
    _ensure_table(conn)
    total_inserted = 0

    for i, ticker in enumerate(tickers):
        logger.info(
            "[{}/{}] Loading 1-min bars for {} ({} → {})",
            i + 1, len(tickers), ticker, from_date, to_date,
        )
        existing = _existing_dates(conn, ticker)

        for chunk_start, chunk_end in _month_chunks(from_date, to_date):
            # Quick check: if all trading days in this chunk are loaded, skip
            # (rough heuristic — if any date in chunk is missing, fetch the chunk)
            chunk_dates = {
                chunk_start + timedelta(days=d)
                for d in range((chunk_end - chunk_start).days + 1)
                if (chunk_start + timedelta(days=d)).weekday() < 5  # Mon-Fri
            }
            missing = chunk_dates - existing
            if not missing:
                logger.debug("  {} {}->{}: all loaded, skipping", ticker, chunk_start, chunk_end)
                continue

            logger.info(
                "  Fetching {} → {} ({} potential days missing)",
                chunk_start, chunk_end, len(missing),
            )
            try:
                rows = _fetch_minute_bars(ticker, chunk_start, chunk_end)
            except requests.HTTPError as e:
                logger.warning("  API error for {} {}->{}: {}", ticker, chunk_start, chunk_end, e)
                time.sleep(1)
                continue
            except Exception as e:
                logger.warning("  Unexpected error for {}: {}", ticker, e)
                continue

            if not rows:
                logger.debug("  No data returned for {} {}->{}", ticker, chunk_start, chunk_end)
                time.sleep(_MIN_REQUEST_INTERVAL)
                continue

            # Bulk insert — filter out rows that already exist
            df = pd.DataFrame(rows)
            df["bar_date"] = pd.to_datetime(df["bar_time"]).dt.date
            df = df[~df["bar_date"].isin(existing)]
            df = df.drop(columns=["bar_date"])

            if df.empty:
                time.sleep(_MIN_REQUEST_INTERVAL)
                continue

            n_before = conn.execute("SELECT COUNT(*) FROM fact_intraday_bars").fetchone()[0]
            conn.register("_intraday_temp", df)
            conn.execute("""
                INSERT OR IGNORE INTO fact_intraday_bars
                SELECT ticker, bar_time, open, high, low, close,
                       volume, vwap, transactions
                FROM _intraday_temp
            """)
            conn.unregister("_intraday_temp")
            n_after = conn.execute("SELECT COUNT(*) FROM fact_intraday_bars").fetchone()[0]
            inserted = n_after - n_before

            total_inserted += inserted
            existing.update(pd.to_datetime(df["bar_time"]).dt.date)
            logger.info("  Inserted {} bars for {} (total: {})", inserted, ticker, total_inserted)

            time.sleep(_MIN_REQUEST_INTERVAL)

    logger.info("Intraday load complete: {} total bars inserted", total_inserted)
    return total_inserted


# ---------------------------------------------------------------------------
# Intelligence-based ticker selection
# ---------------------------------------------------------------------------

def _quarter_end_date(quarter: str) -> date:
    """Return the last day of the quarter, e.g. '2024-Q3' → 2024-09-30."""
    year = int(quarter.split("-Q")[0])
    qnum = int(quarter.split("-Q")[1])
    end_month = {1: 3, 2: 6, 3: 9, 4: 12}[qnum]
    last_day = calendar.monthrange(year, end_month)[1]
    return date(year, end_month, last_day)


def _filing_date(quarter: str) -> date:
    """13F filing deadline: quarter_end + 45 days."""
    return _quarter_end_date(quarter) + timedelta(days=45)


def _next_quarter(quarter: str) -> str:
    year = int(quarter.split("-Q")[0])
    qnum = int(quarter.split("-Q")[1])
    qnum += 1
    if qnum > 4:
        qnum = 1
        year += 1
    return f"{year}-Q{qnum}"


def get_qualifying_tickers(
    conn: duckdb.DuckDBPyConnection,
    quarter: str,
    min_conviction: float = 55.0,
) -> List[str]:
    """Return tickers qualifying for ORB backtest from intelligence_scores."""
    rows = conn.execute("""
        SELECT DISTINCT i.ticker
        FROM intelligence_scores i
        WHERE i.report_quarter = ?
          AND i.conviction_score >= ?
          AND i.accum_phase IN ('ACTIVE_ACCUM', 'LATE_ACCUM', 'EARLY_ACCUM')
          AND i.swing_signal IN ('BUY', 'WATCH')
    """, [quarter, min_conviction]).fetchall()
    return [r[0] for r in rows]


def load_from_intelligence(
    conn: duckdb.DuckDBPyConnection,
    quarters: List[str],
    min_conviction: float = 55.0,
) -> int:
    """Load 1-min bars for all qualifying tickers across the given quarters.

    For each quarter, the trading window is: filing_date → next_quarter's filing_date.
    """
    total = 0
    for quarter in quarters:
        tickers = get_qualifying_tickers(conn, quarter, min_conviction)
        if not tickers:
            logger.warning("No qualifying tickers for {}", quarter)
            continue

        start = _filing_date(quarter)
        end = _filing_date(_next_quarter(quarter))
        logger.info(
            "Quarter {}: {} qualifying tickers, window {} → {}",
            quarter, len(tickers), start, end,
        )
        total += load_intraday_bars(conn, tickers, start, end)

    return total


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Load 1-minute bars from Polygon for ORB Edge backtesting"
    )
    parser.add_argument(
        "--tickers", type=str, default=None,
        help="Comma-separated tickers (e.g. AAPL,PLTR,NVDA)",
    )
    parser.add_argument("--from", dest="from_date", type=str, default=None)
    parser.add_argument("--to", dest="to_date", type=str, default=None)
    parser.add_argument(
        "--from-intelligence", action="store_true",
        help="Auto-load qualifying tickers from intelligence_scores",
    )
    parser.add_argument(
        "--quarters", type=str, default=None,
        help="Comma-separated quarters for --from-intelligence (e.g. 2024-Q3,2024-Q4)",
    )
    parser.add_argument(
        "--min-conviction", type=float, default=55.0,
        help="Minimum conviction score for qualifying tickers (default: 55)",
    )
    args = parser.parse_args()

    conn = duckdb.connect(str(WAREHOUSE_PATH))

    try:
        if args.from_intelligence:
            if not args.quarters:
                parser.error("--from-intelligence requires --quarters")
            quarters = [q.strip() for q in args.quarters.split(",")]
            total = load_from_intelligence(conn, quarters, args.min_conviction)
        else:
            if not args.tickers or not args.from_date or not args.to_date:
                parser.error("Provide --tickers, --from, and --to")
            tickers = [t.strip().upper() for t in args.tickers.split(",")]
            from_d = date.fromisoformat(args.from_date)
            to_d = date.fromisoformat(args.to_date)
            total = load_intraday_bars(conn, tickers, from_d, to_d)

        logger.info("Done. Total bars inserted: {}", total)
    finally:
        conn.close()


if __name__ == "__main__":
    from signal_scanner.utils.logger import setup_logger
    setup_logger()
    main()
