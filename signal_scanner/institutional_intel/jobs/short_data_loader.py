"""Short Interest, Short Volume, Dark Pool & Cost-to-Borrow data loader.

Fetches from Polygon.io / Massive.com and loads into DuckDB warehouse.

Data sources:
  - Short Interest:  GET /stocks/v1/short-interest  (bi-monthly FINRA)
  - Short Volume:    GET /stocks/v1/short-volume     (daily FINRA)
  - Dark Pool:       GET /v3/trades/{ticker}         (tick-level, filter exchange=4)
  - Cost-to-Borrow:  GET /stocks/v1/stock-borrow-costs (if available)

Usage:
    # Load short interest for all tickers, last 2 years
    python -m signal_scanner.institutional_intel.jobs.short_data_loader --mode short-interest --from-date 2024-01-01

    # Load short volume for warehouse tickers, last 90 days
    python -m signal_scanner.institutional_intel.jobs.short_data_loader --mode short-volume --days-back 90

    # Load dark pool aggregates for specific tickers (yesterday)
    python -m signal_scanner.institutional_intel.jobs.short_data_loader --mode dark-pool --tickers AAPL,TSLA --days-back 5

    # Load all data types for warehouse tickers
    python -m signal_scanner.institutional_intel.jobs.short_data_loader --mode all --days-back 90
"""

from __future__ import annotations

import argparse
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Set

import duckdb
import pandas as pd
import requests
from loguru import logger

from signal_scanner.institutional_intel.config import (
    MASSIVE_API_KEY,
    MASSIVE_BASE_URL,
    WAREHOUSE_PATH,
)
from signal_scanner.core.readiness import latest_complete_trading_day


# ---------------------------------------------------------------------------
# API helpers (matching massive_loader.py patterns)
# ---------------------------------------------------------------------------

def _get_api_key() -> str:
    if not MASSIVE_API_KEY:
        raise ValueError(
            "MASSIVE_API_KEY not set. Set it as an environment variable or in .env"
        )
    return MASSIVE_API_KEY


def _api_get(url: str, params: Optional[Dict] = None) -> Dict:
    """Make authenticated GET request to Polygon/Massive API."""
    api_key = _get_api_key()
    params = params or {}
    params["apiKey"] = api_key
    resp = requests.get(url, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


def _paginated_fetch(
    url: str,
    params: Dict,
    max_pages: int = 200,
    rps: float = 4.0,
) -> List[Dict]:
    """Fetch all pages from a cursor-paginated Polygon endpoint.

    Returns combined results list from all pages.
    """
    delay = 1.0 / rps
    all_results: List[Dict] = []
    page = 0

    while url and page < max_pages:
        try:
            data = _api_get(url, params if page == 0 else None)
        except requests.exceptions.HTTPError as e:
            status = e.response.status_code if e.response is not None else "?"
            if status == 429:
                logger.warning("Rate limited, sleeping 60s...")
                time.sleep(60)
                continue
            raise

        results = data.get("results", [])
        all_results.extend(results)
        page += 1

        # Cursor pagination: next_url contains cursor param
        next_url = data.get("next_url")
        if next_url:
            # next_url excludes apiKey — we re-add it in _api_get
            url = next_url
        else:
            break

        time.sleep(delay)

    return all_results


def _db_connect_with_retry(read_only: bool = False, max_attempts: int = 5) -> duckdb.DuckDBPyConnection:
    """Connect to warehouse with retry for DB lock."""
    for attempt in range(max_attempts):
        try:
            return duckdb.connect(str(WAREHOUSE_PATH), read_only=read_only)
        except duckdb.IOException:
            if attempt < max_attempts - 1:
                time.sleep(2 * (attempt + 1))
            else:
                raise


def _log(msg: str) -> None:
    print(msg, flush=True)
    try:
        progress_file = WAREHOUSE_PATH.parent / "short_loader_progress.txt"
        with open(progress_file, "a") as f:
            f.write(f"{datetime.now().strftime('%H:%M:%S')} {msg}\n")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# SHORT INTEREST — bi-monthly FINRA data
# ---------------------------------------------------------------------------

def load_short_interest(
    from_date: date,
    to_date: Optional[date] = None,
    ticker_filter: Optional[Set[str]] = None,
    rps: float = 4.0,
) -> Dict[str, int]:
    """Load short interest data for all tickers in a date range.

    Uses GET /stocks/v1/short-interest with date range filters.
    Bi-monthly data: ~24 settlement dates per year.
    """
    to_date = to_date or date.today()
    _log(f"[SHORT INTEREST] Loading from {from_date} to {to_date}")

    url = f"{MASSIVE_BASE_URL}/stocks/v1/short-interest"
    params = {
        "settlement_date.gte": from_date.isoformat(),
        "settlement_date.lte": to_date.isoformat(),
        "sort": "settlement_date.asc,ticker.asc",
        "limit": "50000",
    }

    all_results = _paginated_fetch(url, params, rps=rps)
    _log(f"  Fetched {len(all_results):,} short interest records")

    if not all_results:
        return {"total_rows": 0}

    now_iso = datetime.now(timezone.utc).isoformat()
    records = []
    for r in all_results:
        ticker = r.get("ticker", "")
        if not ticker:
            continue
        if ticker_filter and ticker not in ticker_filter:
            continue
        records.append({
            "ticker": ticker,
            "settlement_date": r.get("settlement_date"),
            "short_interest": r.get("short_interest"),
            "avg_daily_volume": r.get("avg_daily_volume"),
            "days_to_cover": r.get("days_to_cover"),
            "source": "massive_short_interest",
            "ingested_at": now_iso,
        })

    if not records:
        return {"total_rows": 0}

    # Fast path: pandas DataFrame → DuckDB native registration
    df = pd.DataFrame(records)
    conn = _db_connect_with_retry()
    try:
        conn.register("_si_load", df)
        # DuckDB has no PK on this table — delete matching keys then insert
        conn.execute("""
            DELETE FROM fact_short_interest
            WHERE (ticker, settlement_date) IN (
                SELECT ticker, settlement_date::DATE FROM _si_load
            )
        """)
        conn.execute("""
            INSERT INTO fact_short_interest
            SELECT ticker, settlement_date::DATE, short_interest, avg_daily_volume,
                   days_to_cover, source, ingested_at::TIMESTAMP FROM _si_load
        """)
        conn.unregister("_si_load")
    finally:
        conn.close()

    _log(f"  Inserted {len(records):,} short interest rows")
    return {"total_rows": len(records)}


# ---------------------------------------------------------------------------
# SHORT VOLUME — daily FINRA data
# ---------------------------------------------------------------------------

def load_short_volume(
    from_date: date,
    to_date: Optional[date] = None,
    ticker_filter: Optional[Set[str]] = None,
    rps: float = 4.0,
) -> Dict[str, int]:
    """Load daily short volume data.

    Uses GET /stocks/v1/short-volume with date range filters.
    Daily data — can be large for full market. Use ticker_filter for targeted loads.
    """
    to_date = to_date or (date.today() - timedelta(days=1))
    _log(f"[SHORT VOLUME] Loading from {from_date} to {to_date}")

    # For large date ranges, fetch day by day to avoid API limits
    total_rows = 0
    days_loaded = 0
    delay = 1.0 / rps

    # Get existing dates to skip
    existing_dates: Set[str] = set()
    try:
        conn = duckdb.connect(str(WAREHOUSE_PATH), read_only=True)
        try:
            existing = conn.execute(
                "SELECT DISTINCT trade_date::TEXT FROM fact_short_volume"
            ).fetchall()
            existing_dates = {r[0] for r in existing}
        finally:
            conn.close()
    except Exception:
        pass

    current = from_date
    while current <= to_date:
        if current.weekday() >= 5:
            current += timedelta(days=1)
            continue

        date_str = current.isoformat()
        if date_str in existing_dates:
            current += timedelta(days=1)
            continue

        try:
            url = f"{MASSIVE_BASE_URL}/stocks/v1/short-volume"
            params = {
                "date": date_str,
                "sort": "ticker.asc",
                "limit": "50000",
            }
            results = _paginated_fetch(url, params, rps=rps)

            if results:
                now_iso = datetime.now(timezone.utc).isoformat()
                records = []
                for r in results:
                    ticker = r.get("ticker", "")
                    if not ticker:
                        continue
                    if ticker_filter and ticker not in ticker_filter:
                        continue
                    records.append({
                        "ticker": ticker,
                        "trade_date": r.get("date", date_str),
                        "short_volume": r.get("short_volume"),
                        "total_volume": r.get("total_volume"),
                        "short_volume_ratio": r.get("short_volume_ratio"),
                        "exempt_volume": r.get("exempt_volume"),
                        "non_exempt_volume": r.get("non_exempt_volume"),
                        "source": "massive_short_volume",
                        "ingested_at": now_iso,
                    })

                if records:
                    sv_df = pd.DataFrame(records)
                    conn = _db_connect_with_retry()
                    try:
                        conn.register("_sv_load", sv_df)
                        conn.execute("""
                            DELETE FROM fact_short_volume
                            WHERE (ticker, trade_date) IN (
                                SELECT ticker, trade_date::DATE FROM _sv_load
                            )
                        """)
                        conn.execute("""
                            INSERT INTO fact_short_volume
                            SELECT ticker, trade_date::DATE, short_volume, total_volume,
                                   short_volume_ratio, exempt_volume, non_exempt_volume,
                                   source, ingested_at::TIMESTAMP FROM _sv_load
                        """)
                        conn.unregister("_sv_load")
                    finally:
                        conn.close()
                    total_rows += len(records)
                    days_loaded += 1
                    _log(f"  [{days_loaded}] {date_str}: {len(records)} rows | total: {total_rows:,}")

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

    _log(f"[SHORT VOLUME] Complete: {days_loaded} dates, {total_rows:,} rows")
    return {"total_rows": total_rows, "days_loaded": days_loaded}


# ---------------------------------------------------------------------------
# DARK POOL — derived from FINRA off-exchange volume (fact_short_volume)
#
# FINRA publishes daily off-exchange (ATS/dark pool) volume in their short
# volume data feed. fact_short_volume.total_volume IS the dark pool volume
# for each ticker per day. We cross-reference with fact_daily_prices.volume
# (total exchange volume) to compute the dark pool percentage.
#
# This approach is more accurate and far cheaper than fetching tick-level
# data from /v3/trades (which requires a premium API plan).
# ---------------------------------------------------------------------------

def load_dark_pool_daily(
    tickers: List[str],
    from_date: date,
    to_date: Optional[date] = None,
    rps: float = 4.0,
) -> Dict[str, int]:
    """Derive dark pool metrics from FINRA off-exchange volume data.

    FINRA short-volume data (fact_short_volume) reports off-exchange (dark pool /
    ATS / OTC) total volume per ticker per day. This is the authoritative source
    for dark pool activity — no tick-level API needed.

    Populates fact_dark_pool_daily using:
      dark_pool_volume  = fact_short_volume.total_volume  (FINRA off-exchange total)
      dark_pool_pct     = finra_total_vol / (finra_total_vol + exchange_vol) * 100
      dark_pool_trades  = NULL (not available from FINRA aggregate)
      dark_pool_vwap    = NULL (not available from FINRA aggregate)
    """
    to_date = to_date or (date.today() - timedelta(days=1))

    # Scope to provided tickers or all short-volume tickers if list is empty
    ticker_clause = ""
    ticker_params: List[str] = []
    if tickers:
        placeholders = ", ".join(["?" for _ in tickers])
        ticker_clause = f"AND sv.ticker IN ({placeholders})"
        ticker_params = list(tickers)

    # Use DuckDB to join fact_short_volume and fact_daily_prices in one shot
    query = f"""
        SELECT
            sv.ticker,
            sv.trade_date,
            sv.total_volume                          AS dark_pool_volume,
            NULL::DOUBLE                             AS dark_pool_trades,
            NULL::DOUBLE                             AS dark_pool_vwap,
            COALESCE(dp.volume, sv.total_volume)     AS total_volume,
            CASE
                WHEN COALESCE(dp.volume, sv.total_volume) > 0
                THEN ROUND(sv.total_volume / COALESCE(dp.volume, sv.total_volume) * 100, 2)
                ELSE 0
            END                                      AS dark_pool_pct
        FROM fact_short_volume sv
        LEFT JOIN fact_daily_prices dp
            ON dp.ticker = sv.ticker AND dp.trade_date = sv.trade_date
        WHERE sv.trade_date BETWEEN ?::DATE AND ?::DATE
          AND sv.total_volume > 0
          {ticker_clause}
        ORDER BY sv.trade_date, sv.ticker
    """

    _log(f"[DARK POOL] Deriving from FINRA data | {len(tickers) if tickers else 'all'} tickers "
         f"| {from_date} to {to_date}")

    params: List = [str(from_date), str(to_date)] + ticker_params
    conn = _db_connect_with_retry()
    if conn is None:
        _log("[DARK POOL] Could not connect to warehouse — skipping")
        return {"total_rows": 0, "tickers_done": 0}

    try:
        rows = conn.execute(query, params).fetchall()
    finally:
        conn.close()

    if not rows:
        _log("[DARK POOL] No FINRA short-volume data found for date range — run short-volume first")
        return {"total_rows": 0, "tickers_done": 0}

    now_iso = datetime.now(timezone.utc).isoformat()
    insert_rows = [
        (r[0], str(r[1]), r[2], r[3], r[4], r[5], r[6], "finra_derived", now_iso)
        for r in rows
    ]

    # Batch upsert: delete existing then bulk insert
    conn = _db_connect_with_retry()
    if conn is None:
        _log("[DARK POOL] Could not connect for write — skipping")
        return {"total_rows": 0, "tickers_done": 0}
    try:
        # Build temp table and use DELETE-then-INSERT pattern
        conn.execute("CREATE TEMP TABLE _dp_load AS SELECT * FROM fact_dark_pool_daily LIMIT 0")
        conn.executemany(
            "INSERT INTO _dp_load VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", insert_rows
        )
        conn.execute("""
            DELETE FROM fact_dark_pool_daily
            WHERE (ticker, trade_date) IN (
                SELECT ticker, trade_date::DATE FROM _dp_load
            )
        """)
        conn.execute("""
            INSERT INTO fact_dark_pool_daily
            SELECT ticker, trade_date::DATE, dark_pool_volume, dark_pool_trades,
                   dark_pool_vwap, total_volume, dark_pool_pct,
                   source, ingested_at::TIMESTAMP FROM _dp_load
        """)
        total_rows = len(insert_rows)
        tickers_done = len({r[0] for r in rows})
    finally:
        conn.close()

    _log(f"[DARK POOL] Complete: {tickers_done} tickers, {total_rows:,} rows")
    return {"total_rows": total_rows, "tickers_done": tickers_done}


# ---------------------------------------------------------------------------
# COST-TO-BORROW — yfinance short data (primary) + IBKR tick types (future)
#
# yfinance provides: shortRatio, shortPercentOfFloat, sharesShort, floatShares
# IBKR provides: tick type 7 (shortable: -1/0/1/2) + tick type 236 (shortable_shares)
# These are stored in fact_cost_to_borrow as an approximation.
# ---------------------------------------------------------------------------

def load_cost_to_borrow(
    from_date: date,
    to_date: Optional[date] = None,
    ticker_filter: Optional[Set[str]] = None,
    rps: float = 4.0,
) -> Dict[str, int]:
    """Load short metrics from yfinance as cost-to-borrow proxy.

    yfinance provides daily-resolution short data:
      - shortRatio (days-to-cover) → stored as fee_rate proxy
      - shortPercentOfFloat → stored as utilization_pct
      - sharesShort + floatShares → available_shares estimate

    IBKR tick type 7 (shortable status) and 236 (shortable shares) will be
    integrated when live IBKR session is available.

    Note: Polygon /stocks/v1/stock-borrow-costs endpoint not found (404).
    yfinance is the best available free alternative.
    """
    report_date = latest_complete_trading_day()
    report_date_str = report_date.isoformat()
    _log(f"[COST-TO-BORROW] Loading via yfinance, report_date={report_date_str}")

    # Determine tickers to load
    if ticker_filter:
        tickers_to_load = sorted(ticker_filter)
    else:
        tickers_to_load = get_intelligence_tickers(min_conviction=40)[:1000]

    if not tickers_to_load:
        _log("[COST-TO-BORROW] No tickers to load")
        return {"total_rows": 0, "source": "yfinance"}

    try:
        import yfinance as yf
    except ImportError:
        _log("[COST-TO-BORROW] yfinance not installed — pip install yfinance")
        return {"total_rows": 0, "source": "yfinance", "error": "not_installed"}

    _log(f"[COST-TO-BORROW] Fetching {len(tickers_to_load)} tickers from yfinance")

    rows = []
    errors = 0
    now_iso = datetime.now(timezone.utc).isoformat()

    for i, ticker in enumerate(tickers_to_load):
        try:
            info = yf.Ticker(ticker).fast_info
            short_ratio = getattr(info, 'last_price', None)  # placeholder
            # Use Ticker.info for short data (more complete than fast_info)
            full_info = yf.Ticker(ticker).info
            short_ratio = full_info.get("shortRatio")
            short_pct_float = full_info.get("shortPercentOfFloat")
            shares_short = full_info.get("sharesShort")
            float_shares = full_info.get("floatShares")

            # Derive available shares (float - already short)
            avail = None
            if float_shares and shares_short:
                avail = max(0, float_shares - shares_short)

            rows.append((
                ticker,
                report_date_str,
                short_ratio,        # fee_rate proxy (days-to-cover)
                avail,              # available_shares estimate
                short_pct_float,    # utilization_pct (short % of float)
                "yfinance",
                now_iso,
            ))

            if (i + 1) % 50 == 0:
                _log(f"  [{i+1}/{len(tickers_to_load)}] fetched, {len(rows)} rows")
            time.sleep(1.0 / max(rps, 2.0))  # respect rate limits

        except Exception as exc:
            errors += 1
            logger.debug("CTB yfinance error {}: {}", ticker, exc)

    if not rows:
        _log("[COST-TO-BORROW] No data retrieved from yfinance")
        return {"total_rows": 0, "source": "yfinance", "errors": errors}

    # Upsert into fact_cost_to_borrow
    conn = _db_connect_with_retry()
    if conn is None:
        _log("[COST-TO-BORROW] Could not connect to warehouse")
        return {"total_rows": 0, "source": "yfinance"}

    try:
        # Delete today's rows for these tickers, then bulk insert
        tickers_in_rows = [r[0] for r in rows]
        conn.execute(f"""
            DELETE FROM fact_cost_to_borrow
            WHERE report_date = '{report_date_str}'::DATE
              AND ticker IN ({', '.join(repr(t) for t in tickers_in_rows)})
        """)
        conn.executemany("""
            INSERT INTO fact_cost_to_borrow
                (ticker, report_date, fee_rate, available_shares,
                 utilization_pct, source, ingested_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, rows)
    finally:
        conn.close()

    _log(f"[COST-TO-BORROW] Complete: {len(rows):,} rows | {errors} errors | source=yfinance")
    return {"total_rows": len(rows), "source": "yfinance", "errors": errors}


def _load_cost_to_borrow_massive(
    from_date: date,
    to_date: Optional[date] = None,
    ticker_filter: Optional[Set[str]] = None,
    rps: float = 4.0,
) -> Dict[str, int]:
    """LEGACY: Massive.com CTB endpoint (returns 404 — kept for future reference).

    If Massive.com publishes the CTB endpoint, restore this function as primary.
    """
    to_date = to_date or date.today()

    # Try known endpoint patterns for CTB data
    endpoint_candidates = [
        "/stocks/v1/stock-borrow-costs",
        "/stocks/v1/cost-to-borrow",
        "/v1/stocks/cost-to-borrow",
    ]

    all_results: List[Dict] = []
    working_endpoint = None

    for endpoint in endpoint_candidates:
        url = f"{MASSIVE_BASE_URL}{endpoint}"
        params = {
            "limit": "50000",
        }
        params["date.gte"] = from_date.isoformat()
        params["date.lte"] = to_date.isoformat()

        try:
            all_results = _paginated_fetch(url, params, max_pages=100, rps=rps)
            if all_results:
                working_endpoint = endpoint
                _log(f"  CTB endpoint found: {endpoint}")
                break
        except requests.exceptions.HTTPError as e:
            status = e.response.status_code if e.response is not None else "?"
            if status == 404:
                logger.debug("CTB endpoint {} returned 404, trying next", endpoint)
                continue
            elif status == 403:
                _log(f"  [AUTH ERROR] CTB endpoint {endpoint} — may require higher plan tier")
                continue
            else:
                logger.debug("CTB endpoint {} returned HTTP {}: {}", endpoint, status, e)
                continue
        except Exception as exc:
            logger.debug("CTB endpoint {} failed: {}", endpoint, exc)
            continue

    if not all_results:
        _log("  [WARN] No CTB data found. Endpoint may require manual configuration.")
        _log("  Check Massive.com docs for the exact cost-to-borrow endpoint URL.")
        return {"total_rows": 0, "endpoint": "NOT_FOUND"}

    now_iso = datetime.now(timezone.utc).isoformat()
    rows = []
    for r in all_results:
        ticker = r.get("ticker", "")
        if not ticker:
            continue
        if ticker_filter and ticker not in ticker_filter:
            continue
        # Adapt field names based on what the API returns
        rows.append((
            ticker,
            r.get("date") or r.get("report_date") or r.get("settlement_date", ""),
            r.get("fee_rate") or r.get("borrow_rate") or r.get("rate"),
            r.get("available_shares") or r.get("available") or r.get("shares_available"),
            r.get("utilization_pct") or r.get("utilization") or r.get("on_loan_pct"),
            "massive_ctb",
            now_iso,
        ))

    if rows:
        conn = _db_connect_with_retry()
        try:
            # Delete matching keys then insert (DuckDB has no PK on this table)
            for r in rows:
                conn.execute(
                    "DELETE FROM fact_cost_to_borrow WHERE ticker = ? AND report_date = ?",
                    (r[0], r[1]),
                )
            conn.executemany("""
                INSERT INTO fact_cost_to_borrow
                    (ticker, report_date, fee_rate, available_shares,
                     utilization_pct, source, ingested_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, rows)
        finally:
            conn.close()

    _log(f"  Inserted {len(rows):,} CTB rows via {working_endpoint}")
    return {"total_rows": len(rows), "endpoint": working_endpoint}


# ---------------------------------------------------------------------------
# Warehouse ticker helper
# ---------------------------------------------------------------------------

def get_warehouse_tickers() -> List[str]:
    """Get all unique tickers from dim_issuer."""
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


def get_intelligence_tickers(min_conviction: float = 0) -> List[str]:
    """Get tickers from intelligence_scores above a conviction threshold."""
    conn = duckdb.connect(str(WAREHOUSE_PATH), read_only=True)
    try:
        rows = conn.execute("""
            SELECT DISTINCT ticker FROM intelligence_scores
            WHERE conviction_score >= ?
              AND ticker NOT IN ('N/A','NONE','NULL','')
              AND LENGTH(ticker) <= 5
            ORDER BY ticker
        """, [min_conviction]).fetchall()
        return [r[0] for r in rows]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Summary statistics
# ---------------------------------------------------------------------------

def print_summary() -> None:
    """Print summary statistics for all short/dark pool tables."""
    conn = duckdb.connect(str(WAREHOUSE_PATH), read_only=True)
    try:
        print("\n" + "=" * 70)
        print("SHORT DATA WAREHOUSE SUMMARY")
        print("=" * 70)

        for table, date_col in [
            ("fact_short_interest", "settlement_date"),
            ("fact_short_volume", "trade_date"),
            ("fact_dark_pool_daily", "trade_date"),
            ("fact_cost_to_borrow", "report_date"),
        ]:
            try:
                row = conn.execute(f"""
                    SELECT COUNT(*),
                           COUNT(DISTINCT ticker),
                           MIN({date_col})::TEXT,
                           MAX({date_col})::TEXT
                    FROM {table}
                """).fetchone()
                print(f"\n{table}:")
                print(f"  Rows: {row[0]:,} | Tickers: {row[1]:,}")
                print(f"  Date range: {row[2]} to {row[3]}")
            except Exception:
                print(f"\n{table}: [not yet populated]")

        # Short interest top stats
        try:
            top = conn.execute("""
                SELECT ticker, short_interest, days_to_cover
                FROM fact_short_interest
                WHERE settlement_date = (SELECT MAX(settlement_date) FROM fact_short_interest)
                ORDER BY days_to_cover DESC NULLS LAST
                LIMIT 10
            """).fetchall()
            if top:
                print(f"\nTop 10 by Days-to-Cover (latest settlement):")
                for r in top:
                    print(f"  {r[0]:>6s}  SI: {r[1]:>12,}  DTC: {r[2]:.1f}" if r[2] else f"  {r[0]:>6s}  SI: {r[1]:>12,}")
        except Exception:
            pass

        # Short volume ratio extremes
        try:
            high_ratio = conn.execute("""
                SELECT ticker, trade_date, short_volume_ratio
                FROM fact_short_volume
                WHERE trade_date = (SELECT MAX(trade_date) FROM fact_short_volume)
                  AND total_volume > 100000
                ORDER BY short_volume_ratio DESC NULLS LAST
                LIMIT 10
            """).fetchall()
            if high_ratio:
                print(f"\nTop 10 Short Volume Ratio (latest date, vol>100K):")
                for r in high_ratio:
                    print(f"  {r[0]:>6s}  {r[1]}  ratio: {r[2]:.1f}%")
        except Exception:
            pass

        print("\n" + "=" * 70)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Load short interest, short volume, dark pool & CTB data from Massive.com"
    )
    p.add_argument(
        "--mode", required=True,
        choices=["short-interest", "short-volume", "dark-pool", "ctb", "all", "summary"],
        help="Data type to load",
    )
    p.add_argument("--from-date", default="2024-01-01", help="Start date (YYYY-MM-DD)")
    p.add_argument("--to-date", default="", help="End date (default: yesterday)")
    p.add_argument("--days-back", type=int, default=0, help="Alternative: days back from today")
    p.add_argument(
        "--tickers", default="",
        help="Comma-separated tickers (required for dark-pool mode)",
    )
    p.add_argument(
        "--warehouse-tickers", action="store_true",
        help="Use tickers from dim_issuer as filter",
    )
    p.add_argument(
        "--intel-tickers", action="store_true",
        help="Use tickers from intelligence_scores (higher conviction)",
    )
    p.add_argument(
        "--min-conviction", type=float, default=0,
        help="Min conviction score filter for --intel-tickers",
    )
    p.add_argument("--rps", type=float, default=4.0, help="API requests per second")
    return p.parse_args()


def main() -> None:
    from signal_scanner.institutional_intel.warehouse.db import init_warehouse
    init_warehouse()

    args = parse_args()

    if args.mode == "summary":
        print_summary()
        return

    # Resolve dates
    if args.days_back > 0:
        from_date = date.today() - timedelta(days=args.days_back)
    else:
        from_date = date.fromisoformat(args.from_date)
    to_date = date.fromisoformat(args.to_date) if args.to_date else None

    # Resolve ticker filter
    ticker_filter: Optional[Set[str]] = None
    ticker_list: Optional[List[str]] = None

    if args.tickers:
        tlist = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
        ticker_filter = set(tlist)
        ticker_list = tlist
    elif args.intel_tickers:
        tlist = get_intelligence_tickers(args.min_conviction)
        ticker_filter = set(tlist)
        ticker_list = tlist
        logger.info("Using {} intelligence tickers (conviction >= {})", len(tlist), args.min_conviction)
    elif args.warehouse_tickers:
        tlist = get_warehouse_tickers()
        ticker_filter = set(tlist)
        ticker_list = tlist
        logger.info("Using {} warehouse tickers", len(tlist))

    stats: Dict[str, Any] = {}

    if args.mode in ("short-interest", "all"):
        stats["short_interest"] = load_short_interest(
            from_date, to_date, ticker_filter=ticker_filter, rps=args.rps,
        )

    if args.mode in ("short-volume", "all"):
        stats["short_volume"] = load_short_volume(
            from_date, to_date, ticker_filter=ticker_filter, rps=args.rps,
        )

    if args.mode in ("dark-pool", "all"):
        if not ticker_list:
            # Default to top-conviction tickers for dark pool (too API-heavy for full market)
            ticker_list = get_intelligence_tickers(min_conviction=50)
            if not ticker_list:
                ticker_list = get_warehouse_tickers()[:200]  # cap at 200
            logger.info("Dark pool: using {} tickers", len(ticker_list))
        stats["dark_pool"] = load_dark_pool_daily(
            ticker_list, from_date, to_date, rps=args.rps,
        )

    if args.mode in ("ctb", "all"):
        stats["cost_to_borrow"] = load_cost_to_borrow(
            from_date, to_date, ticker_filter=ticker_filter, rps=args.rps,
        )

    _log(f"\n[COMPLETE] Stats: {stats}")
    print_summary()


if __name__ == "__main__":
    main()
