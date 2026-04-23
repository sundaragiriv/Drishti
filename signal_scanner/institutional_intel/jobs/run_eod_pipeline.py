"""EOD (End-of-Day) Pipeline Orchestrator.

Runs every trading day after market close (~4:30 PM ET).
Orchestrates all daily data refreshes, intelligence pipeline,
CUSIP fix, ML scoring, and cleanup in the correct order.

Usage:
    python -m signal_scanner.institutional_intel.jobs.run_eod_pipeline
    python -m signal_scanner.institutional_intel.jobs.run_eod_pipeline --skip-data   # only intelligence/ML
    python -m signal_scanner.institutional_intel.jobs.run_eod_pipeline --quarter 2025-Q4  # specific quarter
"""

import argparse
import subprocess
import sys
import time
from datetime import datetime

import duckdb
from loguru import logger

from signal_scanner.institutional_intel.config import WAREHOUSE_PATH, get_active_quarter, safe_duckdb_connect


_session_ref = None  # set in main() for _run() to update

def _run(step: str, args: list[str]) -> bool:
    """Run a subprocess step, log result, return success."""
    if _session_ref:
        _session_ref.set_active_job(step)
    logger.info("=" * 60)
    logger.info("STEP: {}", step)
    logger.info("=" * 60)
    result = subprocess.run(
        [sys.executable, "-m"] + args,
        capture_output=False,
    )
    if result.returncode != 0:
        logger.error("STEP FAILED: {} (exit code {})", step, result.returncode)
        if _session_ref:
            _session_ref.record_blocked_job(step, f"exit code {result.returncode}")
        return False
    logger.info("STEP OK: {}", step)
    if _session_ref:
        _session_ref.heartbeat()
    return True


def fix_cusip_ticker_mapping(conn: duckdb.DuckDBPyConnection) -> int:
    """Fill blank tickers in fact_13f_positions from dim_issuer CUSIP lookup.

    Returns number of rows updated.
    """
    logger.info("Fixing CUSIP->ticker mapping for blank-ticker rows...")
    conn.execute("""
        UPDATE fact_13f_positions f
        SET ticker = d.ticker
        FROM dim_issuer d
        WHERE f.cusip = d.cusip
          AND (f.ticker IS NULL OR f.ticker = '')
    """)
    remaining = conn.execute(
        "SELECT COUNT(*) FROM fact_13f_positions WHERE ticker IS NULL OR ticker = ''"
    ).fetchone()[0]
    logger.info("CUSIP fix complete. Remaining blank-ticker rows: {:,}", remaining)
    return remaining


def main() -> None:
    parser = argparse.ArgumentParser(description="EOD pipeline orchestrator")
    parser.add_argument("--skip-data", action="store_true",
                        help="Skip data refresh steps (form4, 13F, 8K, short, ctb)")
    parser.add_argument("--quarter", default=None,
                        help="Force intelligence quarter (default: auto from get_active_quarter)")
    parser.add_argument("--days-back", type=int, default=7,
                        help="Days back for incremental data refresh (default: 7)")
    args = parser.parse_args()

    # ---- Session protection ----
    from signal_scanner.core.session import SessionRegistry, SessionMode, SessionPhase
    session = SessionRegistry()
    if not session.acquire(SessionMode.EOD_REFRESH, owner="eod_pipeline"):
        logger.error("=" * 60)
        logger.error(session.refusal_message())
        logger.error("=" * 60)
        sys.exit(1)
    session.set_phase(SessionPhase.RUNNING)
    session.start_background_heartbeat(interval_seconds=30)
    global _session_ref
    _session_ref = session

    start = datetime.now()
    logger.info("=" * 60)
    logger.info("EOD PIPELINE START: {}", start.strftime("%Y-%m-%d %H:%M:%S"))
    logger.info("=" * 60)

    failures = []

    # ── DATA REFRESH STEPS ──────────────────────────────────────────────────
    if not args.skip_data:
        # 1. Cost-to-borrow (yfinance)
        if not _run("Cost-to-Borrow (yfinance)", [
            "signal_scanner.institutional_intel.jobs.short_data_loader",
            "--mode", "ctb",
        ]):
            failures.append("cost-to-borrow")

        # 2. Short interest + volume (FINRA)
        if not _run("Short Interest + Volume (FINRA)", [
            "signal_scanner.institutional_intel.jobs.short_data_loader",
            "--mode", "all", "--days-back", "14",
        ]):
            failures.append("short-data")

        # 3. Form 4 insider transactions
        if not _run("Form 4 Insider Transactions", [
            "signal_scanner.institutional_intel.jobs.daily_form4_refresh",
            "--days", str(args.days_back),
        ]):
            failures.append("form4")

        # 4. Daily 13F incremental
        if not _run("Daily 13F Incremental", [
            "signal_scanner.institutional_intel.jobs.daily_13f_refresh",
            "--days", str(args.days_back),
        ]):
            failures.append("13f-incremental")

        # 5. 8-K material events
        if not _run("8-K Material Events", [
            "signal_scanner.institutional_intel.jobs.daily_8k_refresh",
            "--days", str(args.days_back),
        ]):
            failures.append("8k")

        # 6. Dark pool derivation (from FINRA short volume)
        if not _run("Dark Pool Derivation", [
            "signal_scanner.institutional_intel.jobs.short_data_loader",
            "--mode", "dark-pool", "--days-back", "14",
        ]):
            failures.append("dark-pool")

        # 7. Options contract snapshot (Polygon Options Starter — top 30 tickers)
        if not _run("Options Snapshot (Polygon)", [
            "signal_scanner.institutional_intel.jobs.options_snapshot_loader",
            "--universe", "--top", "30",
        ]):
            failures.append("options-snapshot")

    # ── CUSIP → TICKER FIX ─────────────────────────────────────────────────
    # Must run before aggregation so new 13F rows get ticker symbols.
    logger.info("Fixing CUSIP->ticker mapping...")
    try:
        conn = duckdb.connect(str(WAREHOUSE_PATH))
        fix_cusip_ticker_mapping(conn)
        conn.close()
    except Exception as e:
        logger.error("CUSIP fix failed: {}", e)
        failures.append("cusip-fix")

    # ── DETERMINE ACTIVE QUARTER ────────────────────────────────────────────
    conn_ro = safe_duckdb_connect(read_only=True)
    if conn_ro:
        quarter = args.quarter or get_active_quarter(conn_ro)
        conn_ro.close()
    else:
        quarter = args.quarter or "2025-Q4"
    logger.info("Intelligence quarter: {}", quarter)

    # ── AGGREGATION STAGE ────────────────────────────────────────────────────
    # Rebuild quarterly snapshot for the active quarter with latest data.
    logger.info("Rebuilding quarterly snapshot for {}...", quarter)
    try:
        from signal_scanner.institutional_intel.reports.qoq_engine import (
            build_quarterly_snapshots,
            compute_qoq_changes,
        )
        build_quarterly_snapshots(quarters=[quarter])
        compute_qoq_changes(quarters=[quarter])
        logger.info("Aggregation + QoQ complete for {}", quarter)
    except Exception as e:
        logger.error("Aggregation failed: {}", e)
        failures.append("aggregation")

    # ── INTELLIGENCE PIPELINE ────────────────────────────────────────────────
    if not _run(f"Intelligence Pipeline ({quarter})", [
        "signal_scanner.institutional_intel.jobs.run_pipeline",
        "--stage", "intelligence",
        "--intelligence-quarter", quarter,
        "--max-runtime", "30",
    ]):
        failures.append("intelligence")

    # ── SQUEEZE SCORES ───────────────────────────────────────────────────────
    if not _run("Squeeze Scores", [
        "signal_scanner.institutional_intel.intelligence.squeeze_detector",
        "--score",
    ]):
        failures.append("squeeze")

    # ── DATA CLEANUP ─────────────────────────────────────────────────────────
    if not _run("Data Cleanup", [
        "signal_scanner.institutional_intel.jobs.data_cleanup",
    ]):
        failures.append("data-cleanup")

    # ── ML SCORING ───────────────────────────────────────────────────────────
    # Apply existing trained model to latest intelligence data.
    if not _run(f"ML v2 Scoring ({quarter})", [
        "signal_scanner.institutional_intel.intelligence.ml_signal_v2",
        "--score", "--write", "--quarter", quarter,
    ]):
        failures.append("ml-scoring")

    # ── HMM REGIME MODEL ─────────────────────────────────────────────────────
    # Refit daily HMM on latest price data. Fast (<5s), no harm in daily refit.
    if not _run("HMM Regime Fit", [
        "signal_scanner.institutional_intel.intelligence.regime_hmm",
        "--fit-save", "--fallback", "AAPL,MSFT,NVDA,GOOGL,AMZN",
    ]):
        failures.append("hmm-regime")

    # ── IDEA HOUSEKEEPING ──────────────────────────────────────────────────
    try:
        from signal_scanner.database.db_manager import DatabaseManager as DBManager
        eod_db = DBManager()
        eod_db.init_db()

        # 1. Expire stale ideas (>5 days old)
        hk = eod_db.idea_ledger.daily_housekeeping()
        logger.info("Idea housekeeping: expired={}", hk.get("expired", 0))

        # 2. Revalidate — invalidate ideas whose symbol dropped from universe
        wh_conn = safe_duckdb_connect(read_only=True)
        if wh_conn:
            try:
                valid_tickers = set(
                    r[0] for r in wh_conn.execute(
                        "SELECT DISTINCT ticker FROM intelligence_scores "
                        "WHERE report_quarter = ? AND data_quality_score >= 50",
                        [quarter],
                    ).fetchall()
                )
                rv = eod_db.idea_ledger.revalidate_ideas(valid_tickers)
                logger.info("Idea revalidation: invalidated={}", rv.get("invalidated", 0))
            finally:
                wh_conn.close()

        if _session_ref:
            _session_ref.heartbeat()
    except Exception as e:
        logger.warning("Idea housekeeping failed: {}", e)

    # ── MASSIVE DATA ENRICHMENT ──────────────────────────────────────────
    if not _run("Massive Enrichment (snapshots + corp actions + reference)", [
        "signal_scanner.institutional_intel.jobs.massive_enrichment",
        "--all",
    ]):
        failures.append("massive-enrichment")

    # ── SESSION ARCHIVER ────────────────────────────────────────────────────
    try:
        from signal_scanner.core.session_archiver import archive_session
        from signal_scanner.core.live_bar_store import LiveBarStore
        store = LiveBarStore()
        ar = archive_session(store)
        logger.info("Session archive: {} bars, {} signals from {} symbols",
                     ar.get("bars_archived", 0), ar.get("signals_archived", 0),
                     ar.get("symbols", 0))
        if _session_ref:
            _session_ref.heartbeat()
    except Exception as e:
        logger.warning("Session archiver failed: {}", e)

    # ── HEALTH CHECK ─────────────────────────────────────────────────────────
    _run("Health Check", ["signal_scanner.daily_health_check"])

    # ── SUMMARY ──────────────────────────────────────────────────────────────
    elapsed = (datetime.now() - start).total_seconds() / 60
    logger.info("=" * 60)
    if failures:
        logger.warning("EOD PIPELINE COMPLETE WITH FAILURES in {:.1f}m", elapsed)
        logger.warning("Failed steps: {}", ", ".join(failures))
    else:
        logger.info("EOD PIPELINE COMPLETE (all steps OK) in {:.1f}m", elapsed)
    logger.info("=" * 60)

    session.release()
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
