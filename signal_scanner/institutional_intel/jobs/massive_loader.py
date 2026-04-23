"""Massive.com (Polygon) daily price data loader.

Fetches daily OHLCV data and loads into fact_daily_prices in the warehouse.

Two modes:
  - grouped: Fetch ALL tickers for each date (efficient for bulk backfill)
  - ticker:  Fetch date range for specific tickers (efficient for targeted refresh)

Usage:
    # Bulk backfill: all tickers, last 2 years
    python -m signal_scanner.institutional_intel.jobs.massive_loader --from-date 2023-01-01

    # Refresh warehouse tickers only, last 90 days
    python -m signal_scanner.institutional_intel.jobs.massive_loader --warehouse-tickers --days-back 90

    # Specific tickers
    python -m signal_scanner.institutional_intel.jobs.massive_loader --tickers AAPL,MSFT,GOOGL --from-date 2024-01-01
"""

import argparse
import time
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Optional, Set

import duckdb
import requests
from loguru import logger

from signal_scanner.institutional_intel.config import (
    MASSIVE_API_KEY,
    MASSIVE_BASE_URL,
    WAREHOUSE_PATH,
)


# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------

def _get_api_key() -> str:
    """Get Massive/Polygon API key."""
    if not MASSIVE_API_KEY:
        raise ValueError(
            "MASSIVE_API_KEY not set. Set it as an environment variable or in .env"
        )
    return MASSIVE_API_KEY


def _api_get(url: str, params: Optional[Dict] = None) -> Dict:
    """Make authenticated GET request to Massive/Polygon API."""
    api_key = _get_api_key()
    params = params or {}
    params["apiKey"] = api_key
    resp = requests.get(url, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------------
# Grouped daily load (all tickers for one date)
# ---------------------------------------------------------------------------

def _fetch_grouped_daily(trade_date: date, adjusted: bool = True) -> List[Dict]:
    """Fetch daily OHLCV for ALL tickers on a single date.

    Uses: GET /v2/aggs/grouped/locale/us/market/stocks/{date}
    Returns one row per ticker.
    """
    date_str = trade_date.isoformat()
    url = f"{MASSIVE_BASE_URL}/v2/aggs/grouped/locale/us/market/stocks/{date_str}"
    data = _api_get(url, {"adjusted": str(adjusted).lower()})

    results = data.get("results", [])
    now_iso = datetime.now(timezone.utc).isoformat()

    rows = []
    for r in results:
        ticker = r.get("T", "")
        if not ticker:
            continue
        rows.append({
            "ticker": ticker,
            "trade_date": date_str,
            "open": r.get("o"),
            "high": r.get("h"),
            "low": r.get("l"),
            "close": r.get("c"),
            "volume": int(r.get("v", 0)),
            "vwap": r.get("vw"),
            "transactions": r.get("n"),
            "source": "massive_grouped",
            "ingested_at": now_iso,
        })
    return rows


def load_grouped_daily(
    from_date: date,
    to_date: Optional[date] = None,
    ticker_filter: Optional[Set[str]] = None,
    rps: float = 4.0,
) -> Dict[str, int]:
    """Load daily prices for ALL tickers, date by date.

    Args:
        from_date: Start date.
        to_date: End date (default: yesterday).
        ticker_filter: If set, only insert rows for these tickers.
        rps: API requests per second (rate limit).

    Returns:
        Dict with total_rows and dates_loaded counts.
    """
    to_date = to_date or (date.today() - timedelta(days=1))
    total_rows = 0
    dates_loaded = 0
    delay = 1.0 / rps

    # Progress file for reliable monitoring on Windows
    progress_file = WAREHOUSE_PATH.parent / "price_loader_progress.txt"

    def _log(msg: str) -> None:
        print(msg, flush=True)
        try:
            with open(progress_file, "a") as f:
                f.write(f"{datetime.now().strftime('%H:%M:%S')} {msg}\n")
        except Exception:
            pass

    # Get dates already loaded to skip them — open/close quickly
    existing_dates: Set[str] = set()
    try:
        conn = duckdb.connect(str(WAREHOUSE_PATH), read_only=True)
        try:
            rows = conn.execute(
                "SELECT DISTINCT trade_date::TEXT FROM fact_daily_prices"
            ).fetchall()
            existing_dates = {r[0] for r in rows}
        finally:
            conn.close()
    except Exception:
        pass

    current = from_date
    while current <= to_date:
        # Skip weekends
        if current.weekday() >= 5:
            current += timedelta(days=1)
            continue

        date_str = current.isoformat()
        if date_str in existing_dates:
            current += timedelta(days=1)
            continue

        try:
            rows = _fetch_grouped_daily(current)
            if ticker_filter:
                rows = [r for r in rows if r["ticker"] in ticker_filter]

            if rows:
                # Open connection, insert, close — releases lock between batches.
                # Retry up to 5 times if another process holds the DB lock.
                inserted = False
                for attempt in range(5):
                    try:
                        conn = duckdb.connect(str(WAREHOUSE_PATH))
                        try:
                            _insert_price_rows(conn, rows)
                        finally:
                            conn.close()
                        inserted = True
                        break
                    except duckdb.IOException:
                        if attempt < 4:
                            time.sleep(2 * (attempt + 1))
                        else:
                            _log(f"  [DB LOCKED] {date_str}: giving up after 5 retries")
                if inserted:
                    total_rows += len(rows)
                    dates_loaded += 1
                    _log(f"  [{dates_loaded}] {date_str}: {len(rows)} rows | total: {total_rows:,}")

        except requests.exceptions.HTTPError as e:
            status = e.response.status_code if e.response is not None else "?"
            if status == 429:
                _log(f"  [RATE LIMITED] sleeping 60s at {date_str}")
                time.sleep(60)
                continue
            elif status == 403:
                _log(f"  [AUTH ERROR] API key unauthorized at {date_str}")
                break
            else:
                _log(f"  [HTTP {status}] {date_str}: {e}")
        except Exception as exc:
            _log(f"  [ERROR] {date_str}: {exc}")

        time.sleep(delay)
        current += timedelta(days=1)

    logger.info(
        "Grouped daily load complete: {} dates, {} rows",
        dates_loaded, total_rows,
    )
    _log(f"[COMPLETE] {dates_loaded} dates | {total_rows:,} total rows loaded")
    return {"total_rows": total_rows, "dates_loaded": dates_loaded}


# ---------------------------------------------------------------------------
# Per-ticker load (date range for specific tickers)
# ---------------------------------------------------------------------------

def _fetch_ticker_aggs(
    ticker: str, from_date: date, to_date: date, adjusted: bool = True
) -> List[Dict]:
    """Fetch daily OHLCV for a single ticker over a date range.

    Uses: GET /v2/aggs/ticker/{ticker}/range/1/day/{from}/{to}
    """
    url = (
        f"{MASSIVE_BASE_URL}/v2/aggs/ticker/{ticker}/range/1/day/"
        f"{from_date.isoformat()}/{to_date.isoformat()}"
    )
    data = _api_get(url, {"adjusted": str(adjusted).lower(), "limit": "50000"})

    results = data.get("results", [])
    now_iso = datetime.now(timezone.utc).isoformat()

    rows = []
    for r in results:
        ts_ms = r.get("t", 0)
        trade_dt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).date()
        rows.append({
            "ticker": ticker,
            "trade_date": trade_dt.isoformat(),
            "open": r.get("o"),
            "high": r.get("h"),
            "low": r.get("l"),
            "close": r.get("c"),
            "volume": int(r.get("v", 0)),
            "vwap": r.get("vw"),
            "transactions": r.get("n"),
            "source": "massive_ticker",
            "ingested_at": now_iso,
        })
    return rows


def load_ticker_prices(
    tickers: List[str],
    from_date: date,
    to_date: Optional[date] = None,
    rps: float = 4.0,
) -> Dict[str, int]:
    """Load daily prices for specific tickers.

    Args:
        tickers: List of ticker symbols.
        from_date: Start date.
        to_date: End date (default: yesterday).
        rps: API requests per second.

    Returns:
        Dict with total_rows and tickers_loaded counts.
    """
    to_date = to_date or (date.today() - timedelta(days=1))
    conn = duckdb.connect(str(WAREHOUSE_PATH))
    total_rows = 0
    tickers_loaded = 0
    delay = 1.0 / rps

    try:
        for i, ticker in enumerate(tickers):
            try:
                rows = _fetch_ticker_aggs(ticker, from_date, to_date)
                if rows:
                    _insert_price_rows(conn, rows)
                    total_rows += len(rows)
                    tickers_loaded += 1

                if (i + 1) % 50 == 0:
                    logger.info(
                        "Price loader: {}/{} tickers, {} rows",
                        i + 1, len(tickers), total_rows,
                    )
            except requests.exceptions.HTTPError as e:
                if e.response is not None and e.response.status_code == 429:
                    logger.warning("Rate limited on {}, sleeping 60s...", ticker)
                    time.sleep(60)
                    continue
                else:
                    logger.debug("HTTP error for {}: {}", ticker, e)
            except Exception as exc:
                logger.debug("Failed to load {}: {}", ticker, exc)

            time.sleep(delay)

        logger.info(
            "Ticker price load complete: {}/{} tickers, {} rows",
            tickers_loaded, len(tickers), total_rows,
        )
        return {"total_rows": total_rows, "tickers_loaded": tickers_loaded}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Warehouse ticker refresh
# ---------------------------------------------------------------------------

def get_warehouse_tickers() -> List[str]:
    """Get all unique tickers from dim_issuer that have been resolved."""
    conn = duckdb.connect(str(WAREHOUSE_PATH), read_only=True)
    try:
        rows = conn.execute(
            "SELECT DISTINCT ticker FROM dim_issuer "
            "WHERE ticker IS NOT NULL AND ticker != '' "
            "ORDER BY ticker"
        ).fetchall()
        return [r[0] for r in rows]
    finally:
        conn.close()


def refresh_warehouse_prices(days_back: int = 90, rps: float = 4.0) -> Dict[str, int]:
    """Refresh prices for all tickers in the warehouse.

    Uses grouped daily endpoint (more efficient than per-ticker).
    Only fetches dates not already in fact_daily_prices.
    """
    from_date = date.today() - timedelta(days=days_back)
    tickers = set(get_warehouse_tickers())
    logger.info("Refreshing prices for {} warehouse tickers from {}", len(tickers), from_date)
    return load_grouped_daily(from_date=from_date, ticker_filter=tickers, rps=rps)


# ---------------------------------------------------------------------------
# Insert helper
# ---------------------------------------------------------------------------

def _insert_price_rows(conn: duckdb.DuckDBPyConnection, rows: List[Dict]) -> int:
    """Batch insert price rows into fact_daily_prices (upsert)."""
    if not rows:
        return 0

    values = [
        (
            r["ticker"], r["trade_date"], r.get("open"), r.get("high"),
            r.get("low"), r.get("close"), r.get("volume"), r.get("vwap"),
            r.get("transactions"), r.get("source", "massive"), r["ingested_at"],
        )
        for r in rows
    ]
    conn.executemany(
        """
        INSERT OR REPLACE INTO fact_daily_prices
            (ticker, trade_date, open, high, low, close, volume, vwap,
             transactions, source, ingested_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        values,
    )
    return len(values)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Load daily OHLCV prices from Massive.com into warehouse"
    )
    p.add_argument(
        "--mode", default="grouped", choices=["grouped", "ticker"],
        help="'grouped' fetches all tickers per date (fast bulk). "
             "'ticker' fetches date range per ticker. Default: grouped",
    )
    p.add_argument("--from-date", default="2023-01-01", help="Start date (YYYY-MM-DD)")
    p.add_argument("--to-date", default="", help="End date (default: yesterday)")
    p.add_argument("--days-back", type=int, default=0, help="Alternative: days back from today")
    p.add_argument(
        "--tickers", default="",
        help="Comma-separated tickers (for ticker mode)",
    )
    p.add_argument(
        "--warehouse-tickers", action="store_true",
        help="Use tickers from dim_issuer (grouped mode filters, ticker mode fetches)",
    )
    p.add_argument("--rps", type=float, default=4.0, help="Requests per second")
    return p.parse_args()


def main() -> None:
    from signal_scanner.institutional_intel.warehouse.db import init_warehouse
    init_warehouse()

    args = parse_args()

    if args.days_back > 0:
        from_date = date.today() - timedelta(days=args.days_back)
    else:
        from_date = date.fromisoformat(args.from_date)

    to_date = date.fromisoformat(args.to_date) if args.to_date else None

    if args.mode == "grouped":
        ticker_filter = None
        if args.warehouse_tickers:
            ticker_filter = set(get_warehouse_tickers())
            logger.info("Filtering to {} warehouse tickers", len(ticker_filter))
        stats = load_grouped_daily(
            from_date=from_date, to_date=to_date,
            ticker_filter=ticker_filter, rps=args.rps,
        )
    else:
        if args.warehouse_tickers:
            tickers = get_warehouse_tickers()
        elif args.tickers:
            tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
        else:
            logger.error("Ticker mode requires --tickers or --warehouse-tickers")
            return
        stats = load_ticker_prices(
            tickers=tickers, from_date=from_date, to_date=to_date, rps=args.rps,
        )

    logger.info("Price loading complete: {}", stats)


if __name__ == "__main__":
    main()
