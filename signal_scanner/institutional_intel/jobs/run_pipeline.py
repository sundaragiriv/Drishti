"""End-to-end institutional intelligence pipeline.

Stages:
  1. bootstrap   — ensure schema and directories exist
  2. ingest      — load data into fact tables (bulk or raw EDGAR)
  3. dim_issuer  — populate CUSIP→ticker dimension + backfill tickers
  4. aggregate   — build quarterly snapshots and QoQ diffs

Default mode uses SEC pre-parsed bulk datasets (fast, minutes).
Use --mode=edgar for raw EDGAR download+parse (slow, hours).

Usage:
    # Fast bulk load (recommended)
    python -m signal_scanner.institutional_intel.jobs.run_pipeline
    python -m signal_scanner.institutional_intel.jobs.run_pipeline --from-year 2023

    # Raw EDGAR mode (for incremental daily updates)
    python -m signal_scanner.institutional_intel.jobs.run_pipeline --mode edgar --from-date 2024-01-01

    # Single stage
    python -m signal_scanner.institutional_intel.jobs.run_pipeline --stage dim_issuer
    python -m signal_scanner.institutional_intel.jobs.run_pipeline --stage aggregate
"""

import argparse
import os
import threading
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import duckdb
from loguru import logger


# ---------------------------------------------------------------------------
# Pipeline watchdog — auto-terminate if stuck
# ---------------------------------------------------------------------------

def _start_watchdog(max_minutes: int) -> threading.Timer:
    """Start a daemon timer that terminates the pipeline if it exceeds max_minutes."""
    def _timeout():
        logger.error(
            "PIPELINE TIMEOUT: exceeded {}m max runtime — force-terminating (PID {})",
            max_minutes, os.getpid(),
        )
        os._exit(1)

    timer = threading.Timer(max_minutes * 60, _timeout)
    timer.daemon = True
    timer.start()
    logger.info("Pipeline watchdog: will auto-terminate after {}m", max_minutes)
    return timer

from signal_scanner.institutional_intel.config import (
    WAREHOUSE_PATH,
    InstitutionalIntelConfig,
)
from signal_scanner.institutional_intel.ingest.sec_client import SecClient
from signal_scanner.institutional_intel.jobs.aggregate import run_aggregation
from signal_scanner.institutional_intel.warehouse.db import init_warehouse


# ---------------------------------------------------------------------------
# dim_issuer population
# ---------------------------------------------------------------------------

def _load_sec_cik_ticker_map(client: SecClient) -> Dict[str, str]:
    """Fetch SEC company_tickers_exchange.json → {cik: ticker}."""
    url = "https://www.sec.gov/files/company_tickers_exchange.json"
    try:
        payload = client.get_json(url)
    except Exception as exc:
        logger.warning("Failed to fetch SEC ticker map: {}", exc)
        return {}

    data = payload.get("data", [])
    cik_to_ticker: Dict[str, str] = {}
    for row in data:
        if not isinstance(row, list) or len(row) < 3:
            continue
        cik_raw = str(row[0]).strip().lstrip("0") or "0"
        ticker = str(row[2] or "").strip().upper()
        if ticker and cik_raw:
            cik_to_ticker[cik_raw] = ticker
    return cik_to_ticker


def populate_dim_issuer() -> int:
    """Populate dim_issuer using multi-source ticker resolution — all in SQL.

    Sources:
      1. Form 4 data: ticker + issuer_cik + issuer_name (exact name match)
      2. SEC CIK→ticker map loaded into a temp table

    After populating dim_issuer, backfills ticker into fact_13f_positions.
    """
    conn = duckdb.connect(str(WAREHOUSE_PATH))
    try:
        # Step 1: Build a Form 4 name→ticker lookup table in DuckDB
        conn.execute("""
            CREATE OR REPLACE TEMP TABLE _f4_lookup AS
            SELECT DISTINCT
                UPPER(TRIM(issuer_name)) AS issuer_name_upper,
                FIRST(UPPER(TRIM(ticker))) AS ticker,
                FIRST(issuer_cik) AS issuer_cik
            FROM fact_form4_transactions
            WHERE ticker IS NOT NULL AND LENGTH(ticker) > 0
              AND issuer_name IS NOT NULL AND LENGTH(issuer_name) > 0
            GROUP BY UPPER(TRIM(issuer_name))
        """)
        f4_count = conn.execute("SELECT COUNT(*) FROM _f4_lookup").fetchone()[0]
        logger.info("Form 4 ticker lookup: {} unique issuer names", f4_count)

        # Step 2: Load SEC CIK→ticker map into a temp table
        client = SecClient()
        sec_map = _load_sec_cik_ticker_map(client)
        logger.info("SEC CIK→ticker map: {} entries", len(sec_map))

        if sec_map:
            # Create temp table from Python dict
            sec_rows = [(cik, ticker) for cik, ticker in sec_map.items()]
            conn.execute("CREATE OR REPLACE TEMP TABLE _sec_map (cik TEXT, ticker TEXT)")
            conn.executemany("INSERT INTO _sec_map VALUES (?, ?)", sec_rows)

        # Step 3: Insert all unique CUSIPs with name-matched tickers (bulk SQL)
        conn.execute("""
            INSERT INTO dim_issuer (issuer_key, issuer_cik, ticker, cusip, issuer_name, mapping_confidence)
            SELECT
                f13.cusip AS issuer_key,
                f4.issuer_cik,
                f4.ticker,
                f13.cusip,
                f13.issuer_name,
                CASE WHEN f4.ticker IS NOT NULL THEN 'HIGH' ELSE 'LOW' END
            FROM (
                SELECT DISTINCT cusip, FIRST(issuer_name) AS issuer_name
                FROM fact_13f_positions
                WHERE cusip IS NOT NULL AND LENGTH(cusip) > 0
                GROUP BY cusip
            ) f13
            LEFT JOIN _f4_lookup f4
                ON UPPER(TRIM(f13.issuer_name)) = f4.issuer_name_upper
            ON CONFLICT(issuer_key) DO UPDATE SET
                ticker = COALESCE(NULLIF(excluded.ticker, ''), dim_issuer.ticker),
                issuer_cik = COALESCE(NULLIF(excluded.issuer_cik, ''), dim_issuer.issuer_cik),
                issuer_name = COALESCE(NULLIF(excluded.issuer_name, ''), dim_issuer.issuer_name),
                mapping_confidence = CASE
                    WHEN excluded.ticker IS NOT NULL AND LENGTH(excluded.ticker) > 0 THEN 'HIGH'
                    ELSE dim_issuer.mapping_confidence
                END
        """)

        total = conn.execute("SELECT COUNT(*) FROM dim_issuer").fetchone()[0]
        resolved = conn.execute(
            "SELECT COUNT(*) FROM dim_issuer WHERE ticker IS NOT NULL AND LENGTH(ticker) > 0"
        ).fetchone()[0]
        logger.info(
            "dim_issuer: {} total, {} resolved with ticker ({:.1f}%)",
            total, resolved, (resolved / max(total, 1)) * 100,
        )

        # Step 4: Backfill tickers into fact_13f_positions
        backfilled = _backfill_tickers(conn)
        logger.info("Backfilled {} rows in fact_13f_positions with tickers", backfilled)

        # Step 5: Populate sector/industry via Massive.com or SIC codes
        sector_count = _populate_sectors(conn)
        logger.info("Sectors populated: {} tickers with sector data", sector_count)

        # Step 6: Propagate sectors to agg tables
        conn.execute("""
            UPDATE agg_quarterly_holdings aq
            SET sector = di.sector
            FROM dim_issuer di
            WHERE aq.ticker = di.ticker
              AND di.sector IS NOT NULL AND di.sector != ''
              AND (aq.sector IS NULL OR aq.sector = '')
        """)
        conn.execute("""
            UPDATE agg_qoq_changes qc
            SET sector = di.sector
            FROM dim_issuer di
            WHERE qc.ticker = di.ticker
              AND di.sector IS NOT NULL AND di.sector != ''
              AND (qc.sector IS NULL OR qc.sector = '')
        """)

        return total
    finally:
        conn.close()


def _backfill_tickers(conn: duckdb.DuckDBPyConnection) -> int:
    """Update fact_13f_positions.ticker from dim_issuer where missing."""
    conn.execute("""
        UPDATE fact_13f_positions
        SET ticker = di.ticker
        FROM dim_issuer di
        WHERE fact_13f_positions.cusip = di.cusip
          AND di.ticker IS NOT NULL AND di.ticker != ''
          AND (fact_13f_positions.ticker IS NULL OR fact_13f_positions.ticker = '')
    """)
    # Count rows that now have tickers
    row = conn.execute("""
        SELECT COUNT(*) FROM fact_13f_positions
        WHERE ticker IS NOT NULL AND ticker != ''
    """).fetchone()
    return row[0] if row else 0


# ---------------------------------------------------------------------------
# Sector classification
# ---------------------------------------------------------------------------

# SIC code → GICS-like sector mapping (first 2 digits)
_SIC_SECTOR_MAP = {
    range(100, 1000): "Agriculture",
    range(1000, 1500): "Mining",
    range(1500, 1800): "Construction",
    range(2000, 4000): "Manufacturing",
    range(4000, 4900): "Transportation",
    range(4900, 5000): "Utilities",
    range(5000, 5200): "Wholesale Trade",
    range(5200, 6000): "Retail Trade",
    range(6000, 6800): "Financial Services",
    range(6800, 7000): "Real Estate",
    range(7000, 9000): "Services",
    range(9000, 10000): "Public Administration",
}


def _sic_to_sector(sic_code: str) -> str:
    """Map SIC code to sector name."""
    try:
        sic = int(sic_code)
    except (ValueError, TypeError):
        return ""
    for sic_range, sector in _SIC_SECTOR_MAP.items():
        if sic in sic_range:
            return sector
    return ""


def _populate_sectors(conn: duckdb.DuckDBPyConnection) -> int:
    """Populate dim_issuer.sector using Massive.com/Polygon API or yfinance fallback.

    Strategy:
      1. Massive.com/Polygon REST API (if MASSIVE_API_KEY set)
      2. yfinance concurrent lookup (free, no key needed)
      Focuses on active tickers (present in agg_qoq_changes 2024+) first
      for speed, then fills remaining dim_issuer tickers.
    """
    import time
    from concurrent.futures import ThreadPoolExecutor, as_completed

    import requests

    from signal_scanner.institutional_intel.config import (
        MASSIVE_API_KEY,
        MASSIVE_BASE_URL,
    )

    def _valid_ticker(t: str) -> bool:
        """Accept only clean exchange symbols (1-5 uppercase letters/dots)."""
        import re
        return bool(t and re.match(r'^[A-Z][A-Z0-9.]{0,4}$', t.strip()))

    # Priority order: active tickers from recent quarters first, then all remaining
    active = conn.execute("""
        SELECT DISTINCT q.ticker
        FROM agg_qoq_changes q
        JOIN dim_issuer di ON q.ticker = di.ticker
        WHERE q.current_quarter >= '2024-Q1'
          AND (di.sector IS NULL OR di.sector = '')
        ORDER BY q.ticker
    """).fetchall()
    active_set = {r[0] for r in active if _valid_ticker(r[0])}

    all_missing = conn.execute(
        "SELECT DISTINCT ticker FROM dim_issuer "
        "WHERE ticker IS NOT NULL AND ticker != '' "
        "AND (sector IS NULL OR sector = '') "
        "ORDER BY ticker"
    ).fetchall()
    all_missing_valid = [r[0] for r in all_missing if _valid_ticker(r[0])]

    # Active tickers first, then the rest
    tickers = list(active_set) + [t for t in all_missing_valid if t not in active_set]

    if not tickers:
        return 0

    logger.info(
        "Populating sectors: {} active + {} remaining = {} total tickers",
        len(active_set), len(all_missing_valid) - len(active_set), len(tickers),
    )
    sector_map: dict = {}  # ticker → sector string

    # ── Strategy 1: Massive.com / Polygon API ─────────────────────────
    if MASSIVE_API_KEY:
        logger.info("Using Massive.com/Polygon API for sector lookup...")
        for i, ticker in enumerate(tickers):
            try:
                resp = requests.get(
                    f"{MASSIVE_BASE_URL}/v3/reference/tickers/{ticker}",
                    params={"apiKey": MASSIVE_API_KEY},
                    timeout=10,
                )
                if resp.status_code == 200:
                    data = resp.json().get("results", {})
                    sic_code = str(data.get("sic_code", ""))
                    # Always prefer the broad SIC range mapping; fall back to raw description
                    # only when no numeric SIC code is available
                    sector = _sic_to_sector(sic_code)
                    if not sector:
                        # Normalize raw SIC descriptions to broad categories
                        raw = (data.get("sic_description", "") or "").title()
                        sector = raw if len(raw) <= 40 else raw[:37] + "..."
                    if sector:
                        sector_map[ticker] = sector
                elif resp.status_code == 429:
                    logger.warning("Massive API rate limited at ticker {}, stopping", i)
                    break
                time.sleep(0.25)  # 4 rps
            except Exception:
                pass
            if (i + 1) % 200 == 0:
                logger.info("API sector lookup: {}/{}", i + 1, len(tickers))

    # ── Strategy 2: yfinance concurrent (free fallback) ───────────────
    remaining_tickers = [t for t in tickers if t not in sector_map]
    if remaining_tickers:
        try:
            import yfinance as yf

            def _fetch_yf_sector(ticker: str):
                try:
                    info = yf.Ticker(ticker).info
                    return ticker, (info.get("sector") or "")
                except Exception:
                    return ticker, ""

            logger.info(
                "yfinance sector lookup for {} tickers (20 concurrent workers)...",
                len(remaining_tickers),
            )
            batch_size = 200
            for batch_start in range(0, len(remaining_tickers), batch_size):
                batch = remaining_tickers[batch_start: batch_start + batch_size]
                with ThreadPoolExecutor(max_workers=20) as executor:
                    futures = {executor.submit(_fetch_yf_sector, t): t for t in batch}
                    for future in as_completed(futures):
                        ticker, sector = future.result()
                        if sector:
                            sector_map[ticker] = sector
                found = sum(1 for t in batch if t in sector_map)
                logger.info(
                    "yfinance batch {}-{}: {}/{} found sectors",
                    batch_start + 1, batch_start + len(batch), found, len(batch),
                )

        except ImportError:
            logger.warning(
                "yfinance not installed — sector lookup skipped. "
                "Run: pip install yfinance"
            )

    # ── Batch-write all results ────────────────────────────────────────
    if sector_map:
        rows = [(sector, ticker) for ticker, sector in sector_map.items()]
        conn.executemany(
            "UPDATE dim_issuer SET sector = ? "
            "WHERE ticker = ? AND (sector IS NULL OR sector = '')",
            rows,
        )
        logger.info("Wrote sector data for {} tickers", len(rows))

    total = conn.execute(
        "SELECT COUNT(*) FROM dim_issuer "
        "WHERE sector IS NOT NULL AND sector != ''"
    ).fetchone()[0]

    return total


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    default_universe = str(
        Path(__file__).resolve().parents[2] / "watchlists" / "universe_master.txt"
    )

    p = argparse.ArgumentParser(
        description="End-to-end SEC institutional intelligence pipeline"
    )

    # Mode selection
    p.add_argument(
        "--mode", default="bulk", choices=["bulk", "edgar"],
        help="Ingestion mode: 'bulk' (fast, SEC pre-parsed datasets) or "
             "'edgar' (raw EDGAR download+parse). Default: bulk",
    )

    # Bulk mode args
    p.add_argument(
        "--from-year", type=int, default=2024,
        help="[bulk] Start year (default: 2024)",
    )
    p.add_argument(
        "--to-year", type=int, default=0,
        help="[bulk] End year (default: current year)",
    )

    # Edgar mode args
    p.add_argument(
        "--from-date", default="2024-01-01",
        help="[edgar] Start date for downloads (default: 2024-01-01)",
    )
    p.add_argument(
        "--to-date", default="",
        help="[edgar] End date for downloads (default: today)",
    )
    p.add_argument(
        "--forms", default="13F-HR,13F-HR/A,4,3,5",
        help="[edgar] Comma-separated form types to download",
    )
    p.add_argument(
        "--max-filings", type=int, default=0,
        help="[edgar] Cap downloads (0=unlimited)",
    )
    p.add_argument(
        "--parse-limit", type=int, default=0,
        help="[edgar] Cap filings to parse (0=unlimited)",
    )
    p.add_argument("--workers", type=int, default=4, help="[edgar] Download threads")
    p.add_argument("--rps", type=float, default=8.0, help="[edgar] Requests per second")
    p.add_argument("--universe-file", default=default_universe)
    p.add_argument("--user-agent", default="")

    # Stage control
    p.add_argument(
        "--skip-ingest", action="store_true",
        help="Skip ingestion stage (use existing fact table data)",
    )
    p.add_argument(
        "--skip-aggregate", action="store_true",
        help="Skip aggregation stage",
    )
    p.add_argument(
        "--skip-prices", action="store_true",
        help="Skip price data loading from Massive.com",
    )
    p.add_argument(
        "--price-days-back", type=int, default=730,
        help="Days of price history to load (default: 730 = 2 years)",
    )
    p.add_argument(
        "--stage",
        default="all",
        choices=["all", "ingest", "dim_issuer", "prices", "aggregate", "intelligence"],
        help="Run a single stage instead of all",
    )
    p.add_argument(
        "--skip-intelligence", action="store_true",
        help="Skip intelligence classification stage",
    )
    p.add_argument(
        "--intelligence-quarter", default="",
        help="Run intelligence for specific quarter (e.g. 2025-Q4). Default: latest.",
    )
    p.add_argument(
        "--max-runtime", type=int, default=30,
        help="Max runtime in minutes before auto-termination (default: 30)",
    )
    return p.parse_args()


def main() -> None:
    args = parse_args()
    run_single = args.stage != "all"

    # Start watchdog timer
    watchdog = _start_watchdog(args.max_runtime)

    # ── Stage 1: Bootstrap ──────────────────────────────────────────────
    logger.info("=" * 60)
    logger.info("STAGE 1: BOOTSTRAP")
    logger.info("=" * 60)
    init_warehouse()

    # ── Stage 2: Ingest ─────────────────────────────────────────────────
    if (not run_single and not args.skip_ingest) or args.stage == "ingest":
        logger.info("=" * 60)
        logger.info("STAGE 2: INGEST (mode={})", args.mode)
        logger.info("=" * 60)

        if args.mode == "bulk":
            from signal_scanner.institutional_intel.jobs.bulk_load import (
                bulk_load_13f,
                bulk_load_insider,
            )
            to_year = args.to_year or date.today().year
            stats_13f = bulk_load_13f(args.from_year, to_year)
            stats_insider = bulk_load_insider(args.from_year, to_year)
            logger.info("Bulk ingest complete — 13F: {} | Insider: {}", stats_13f, stats_insider)

        else:  # edgar mode
            from signal_scanner.institutional_intel.ingest.download_loop import (
                run_download_loop,
            )
            from signal_scanner.institutional_intel.jobs.parse_filings import (
                parse_13f_filings,
                parse_form4_filings,
            )
            start_d = date.fromisoformat(args.from_date)
            end_d = date.fromisoformat(args.to_date) if args.to_date else date.today()
            forms = [f.strip().upper() for f in args.forms.split(",") if f.strip()]

            dl_stats = run_download_loop(
                start_date=start_d,
                end_date=end_d,
                forms=forms,
                max_filings=args.max_filings,
                force=False,
                user_agent=args.user_agent or "",
                metadata_only=False,
                workers=args.workers,
                universe_file=args.universe_file,
                requests_per_second=args.rps,
            )
            logger.info("Download complete: {}", dl_stats)

            stats_13f = parse_13f_filings(limit=args.parse_limit)
            stats_f4 = parse_form4_filings(limit=args.parse_limit)
            logger.info("Parse complete — 13F: {} | Form4: {}", stats_13f, stats_f4)

    # ── Stage 3: dim_issuer ─────────────────────────────────────────────
    if (not run_single) or args.stage == "dim_issuer":
        logger.info("=" * 60)
        logger.info("STAGE 3: DIM_ISSUER + TICKER BACKFILL")
        logger.info("=" * 60)
        count = populate_dim_issuer()
        logger.info("dim_issuer populated: {} rows", count)

    # ── Stage 4: Load prices from Massive.com ─────────────────────────
    if (not run_single and not args.skip_prices) or args.stage == "prices":
        logger.info("=" * 60)
        logger.info("STAGE 4: LOAD PRICES (Massive.com)")
        logger.info("=" * 60)
        try:
            from signal_scanner.institutional_intel.config import MASSIVE_API_KEY
            if MASSIVE_API_KEY:
                from signal_scanner.institutional_intel.jobs.massive_loader import (
                    refresh_warehouse_prices,
                )
                price_stats = refresh_warehouse_prices(
                    days_back=args.price_days_back, rps=4.0,
                )
                logger.info("Price loading complete: {}", price_stats)
            else:
                logger.warning(
                    "MASSIVE_API_KEY not set — skipping price data. "
                    "Set it to enable price/volume enrichment in reports."
                )
        except Exception as exc:
            logger.warning("Price loading failed (non-fatal): {}", exc)

    # ── Stage 5: Aggregate ──────────────────────────────────────────────
    if (not run_single and not args.skip_aggregate) or args.stage == "aggregate":
        logger.info("=" * 60)
        logger.info("STAGE 5: AGGREGATE")
        logger.info("=" * 60)
        stats = run_aggregation()
        logger.info("Aggregation complete: {}", stats)

    # ── Stage 6: Intelligence Classification ────────────────────────────
    if (not run_single and not args.skip_intelligence) or args.stage == "intelligence":
        logger.info("=" * 60)
        logger.info("STAGE 6: INTELLIGENCE CLASSIFICATION")
        logger.info("=" * 60)
        try:
            import duckdb as _duckdb
            from signal_scanner.institutional_intel.intelligence.phase_classifier import (
                run_phase_classification,
            )
            from signal_scanner.institutional_intel.intelligence.cascade_detector import (
                update_cascade_in_intelligence,
            )
            from signal_scanner.institutional_intel.intelligence.divergence_scanner import (
                update_divergence_in_intelligence,
            )
            from signal_scanner.institutional_intel.intelligence.manager_quality import (
                build_manager_tiers, update_manager_quality_in_intelligence,
            )
            from signal_scanner.institutional_intel.intelligence.insider_intelligence import (
                update_insider_in_intelligence,
            )
            from signal_scanner.institutional_intel.intelligence.insider_outcome_engine import (
                update_insider_effect_in_intelligence,
            )
            from signal_scanner.institutional_intel.intelligence.conviction_score import (
                update_conviction_in_intelligence,
            )
            from signal_scanner.institutional_intel.intelligence.sector_rotation import (
                compute_sector_rotation,
            )
            from signal_scanner.institutional_intel.intelligence.distribution_detector import (
                update_distribution_in_intelligence,
            )
            from signal_scanner.institutional_intel.intelligence.trading_signals import (
                update_trading_signals_in_intelligence,
            )

            intel_quarter = (args.intelligence_quarter or "").strip() or None
            _conn = _duckdb.connect(str(WAREHOUSE_PATH))
            try:
                # Auto-detect latest available quarter if not specified
                if not intel_quarter:
                    row = _conn.execute(
                        "SELECT MAX(current_quarter) FROM agg_qoq_changes"
                    ).fetchone()
                    intel_quarter = row[0] if row and row[0] else None
                if not intel_quarter:
                    logger.error("No quarter data found in agg_qoq_changes — run aggregate stage first.")
                    raise SystemExit(1)
                logger.info("Intelligence classification for quarter={}", intel_quarter)

                # Step 6a: Phase classification (Model 1+2)
                n_phase = run_phase_classification(_conn, intel_quarter)
                logger.info("Phase classification: {} tickers", n_phase)

                # Step 6b: Cascade detection (Model 3)
                n_casc = update_cascade_in_intelligence(_conn, intel_quarter)
                logger.info("Cascade detection updated: {} tickers", n_casc)

                # Step 6c: Divergence scan (Model 4)
                n_div = update_divergence_in_intelligence(_conn, intel_quarter)
                logger.info("Divergence scan updated: {} tickers", n_div)

                # Step 6d: Manager tiers + quality (Models 5+6)
                build_manager_tiers(_conn)
                n_mgr = update_manager_quality_in_intelligence(_conn, intel_quarter)
                logger.info("Manager quality updated: {} tickers", n_mgr)

                # Step 6e: Insider intelligence (Model 7)
                n_ins = update_insider_in_intelligence(_conn, intel_quarter)
                logger.info("Insider intelligence updated: {} tickers", n_ins)

                # Step 6e2: Insider outcome engine — pressure, effect, trend scores
                n_ioe = update_insider_effect_in_intelligence(_conn, intel_quarter)
                logger.info("Insider effect/pressure updated: {} tickers", n_ioe)

                # Step 6f: Sector rotation (Macro)
                n_sec = compute_sector_rotation(_conn, intel_quarter)
                logger.info("Sector rotation computed: {} rows", n_sec)

                # Step 6g: Conviction score (Model 8) — must run after 6a-6f
                n_conv = update_conviction_in_intelligence(_conn, intel_quarter)
                logger.info("Conviction scores computed: {} tickers", n_conv)

                # Step 6h: Distribution warnings (Model 9)
                n_dist = update_distribution_in_intelligence(_conn, intel_quarter)
                logger.info("Distribution warnings updated: {} tickers", n_dist)

                # Step 6i: Trading signals (Model 10)
                n_sig = update_trading_signals_in_intelligence(_conn, intel_quarter)
                logger.info("Trading signals generated: {} tickers", n_sig)

                # Step 6j: SHORT Conviction Score (Model 11) — parallel to LONG conviction
                from signal_scanner.institutional_intel.intelligence.short_conviction_engine import (
                    update_short_conviction_in_intelligence,
                )
                n_short_conv = update_short_conviction_in_intelligence(_conn, intel_quarter)
                logger.info("SHORT conviction scores computed: {} tickers", n_short_conv)

                # Step 6k: Expectancy calibration (Model 12) — runs after signals
                from signal_scanner.institutional_intel.intelligence.expectancy_engine import (
                    calibrate_expectancy,
                    apply_expectancy_to_quarter,
                )
                calibrate_expectancy(_conn)
                n_ev = apply_expectancy_to_quarter(_conn, intel_quarter)
                logger.info("Expectancy applied: {} tickers", n_ev)

            finally:
                _conn.close()

            logger.info(
                "Intelligence stage complete — phases:{} cascade:{} conviction:{} signals:{} ev:{}",
                n_phase, n_casc, n_conv, n_sig, n_ev,
            )
        except Exception as exc:
            logger.error("Intelligence classification failed: {}", exc)
            raise

    watchdog.cancel()
    logger.info("=" * 60)
    logger.info("PIPELINE COMPLETE")
    logger.info("=" * 60)


if __name__ == "__main__":
    main()
