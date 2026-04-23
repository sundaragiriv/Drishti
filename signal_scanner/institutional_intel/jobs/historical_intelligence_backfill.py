"""Historical Intelligence Backfill — Score all unscored quarters.

Loops over every quarter present in agg_qoq_changes that is NOT yet in
intelligence_scores and runs the full 9-stage intelligence pipeline for
each quarter, in chronological order.

Quarter coverage:
    2006-Q4  to  2015-Q4  → PARTIAL (13F only, no Massive price data)
    2016-Q1  to  2025-Q3  → FULL    (13F + Massive daily prices)
    2025-Q4               → SKIP    (already scored)

Usage:
    # Score everything that's missing
    python -m signal_scanner.institutional_intel.jobs.historical_intelligence_backfill

    # Dry-run: show which quarters would be processed
    python -m signal_scanner.institutional_intel.jobs.historical_intelligence_backfill --dry-run

    # Score only a specific range
    python -m signal_scanner.institutional_intel.jobs.historical_intelligence_backfill \\
        --from-quarter 2016-Q1 --to-quarter 2020-Q4

    # Only quarters with full price data (2016+)
    python -m signal_scanner.institutional_intel.jobs.historical_intelligence_backfill --full-only

    # Resume after a failure (skips quarters that already have scores)
    python -m signal_scanner.institutional_intel.jobs.historical_intelligence_backfill --resume
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime
from typing import List, Optional

import duckdb
from loguru import logger

from signal_scanner.institutional_intel.config import WAREHOUSE_PATH


# ---------------------------------------------------------------------------
# Quarter helpers
# ---------------------------------------------------------------------------

def _quarter_sort_key(q: str) -> tuple[int, int]:
    """Return (year, quarter_num) for chronological sorting.
    Input: '2016-Q1', '2025-Q4', etc.
    """
    try:
        year_str, q_str = q.split("-Q")
        return int(year_str), int(q_str)
    except Exception:
        return (0, 0)


def _parse_quarter_arg(value: str) -> Optional[str]:
    """Validate and normalise a quarter arg like '2016-Q1'. Returns None if blank."""
    if not value:
        return None
    value = value.strip().upper()
    try:
        year_str, q_str = value.split("-Q")
        year, qnum = int(year_str), int(q_str)
        if not (1 <= qnum <= 4):
            raise ValueError
        return f"{year}-Q{qnum}"
    except Exception:
        raise argparse.ArgumentTypeError(
            f"Invalid quarter format '{value}'. Use YYYY-QN, e.g. 2016-Q1"
        )


# ---------------------------------------------------------------------------
# Quarter discovery
# ---------------------------------------------------------------------------

def get_pending_quarters(
    conn: duckdb.DuckDBPyConnection,
    from_quarter: Optional[str] = None,
    to_quarter: Optional[str] = None,
    full_only: bool = False,
    resume: bool = True,
) -> List[str]:
    """Return chronologically-sorted list of quarters to process.

    Args:
        from_quarter: Only process quarters >= this value (e.g. '2016-Q1').
        to_quarter:   Only process quarters <= this value.
        full_only:    If True, only include quarters with Massive price data (2016+).
        resume:       If True, skip quarters already present in intelligence_scores.
                      If False, re-score everything in range (overwrites).
    """
    # All quarters in agg_qoq_changes
    all_quarters_raw = conn.execute(
        "SELECT DISTINCT current_quarter FROM agg_qoq_changes WHERE current_quarter IS NOT NULL"
    ).fetchall()
    all_quarters = sorted(
        {r[0] for r in all_quarters_raw if r[0]},
        key=_quarter_sort_key,
    )

    # Already scored
    scored_raw = conn.execute(
        "SELECT DISTINCT report_quarter FROM intelligence_scores WHERE report_quarter IS NOT NULL"
    ).fetchall()
    already_scored = {r[0] for r in scored_raw}

    result = []
    for q in all_quarters:
        # Range filters
        if from_quarter and _quarter_sort_key(q) < _quarter_sort_key(from_quarter):
            continue
        if to_quarter and _quarter_sort_key(q) > _quarter_sort_key(to_quarter):
            continue

        # Full-only: only quarters where we have Massive price data (2016+)
        if full_only:
            try:
                year = int(q.split("-Q")[0])
                if year < 2016:
                    continue
            except Exception:
                continue

        # Resume: skip already scored
        if resume and q in already_scored:
            continue

        result.append(q)

    return result


# ---------------------------------------------------------------------------
# Per-quarter intelligence run
# ---------------------------------------------------------------------------

def run_intelligence_for_quarter(quarter: str) -> dict:
    """Run the full 9-stage intelligence pipeline for one quarter.

    Opens and closes its own DuckDB connection so the massive_loader price
    fetch (running concurrently) can acquire write access between quarters.

    Returns a dict of stage counts and timing info.
    """
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
        update_manager_quality_in_intelligence,
    )
    from signal_scanner.institutional_intel.intelligence.insider_intelligence import (
        update_insider_in_intelligence,
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

    t0 = time.perf_counter()
    stats = {"quarter": quarter, "error": None}

    # Retry opening the DB up to 10 times (price loader may briefly hold it)
    conn = None
    for attempt in range(10):
        try:
            conn = duckdb.connect(str(WAREHOUSE_PATH))
            break
        except duckdb.IOException:
            if attempt < 9:
                wait = 3 * (attempt + 1)
                logger.info(
                    "  DB locked (attempt {}/10), waiting {}s...", attempt + 1, wait
                )
                time.sleep(wait)
            else:
                raise

    try:
        # Step 6a — Phase classification (creates rows in intelligence_scores)
        n_phase = run_phase_classification(conn, quarter)
        stats["n_phase"] = n_phase

        # Step 6b — Cascade detection
        n_casc = update_cascade_in_intelligence(conn, quarter)
        stats["n_cascade"] = n_casc

        # Step 6c — Divergence scan (graceful fallback if no price data)
        n_div = update_divergence_in_intelligence(conn, quarter)
        stats["n_divergence"] = n_div

        # Step 6d — Manager quality (tiers already built in preflight)
        n_mgr = update_manager_quality_in_intelligence(conn, quarter)
        stats["n_manager"] = n_mgr

        # Step 6e — Insider intelligence
        n_ins = update_insider_in_intelligence(conn, quarter)
        stats["n_insider"] = n_ins

        # Step 6f — Sector rotation
        n_sec = compute_sector_rotation(conn, quarter)
        stats["n_sector"] = n_sec

        # Step 6g — Conviction score (must be after 6a-6f)
        n_conv = update_conviction_in_intelligence(conn, quarter)
        stats["n_conviction"] = n_conv

        # Step 6h — Distribution warnings
        n_dist = update_distribution_in_intelligence(conn, quarter)
        stats["n_distribution"] = n_dist

        # Step 6i — Trading signals
        n_sig = update_trading_signals_in_intelligence(conn, quarter)
        stats["n_signals"] = n_sig

        # Checkpoint to flush WAL
        try:
            conn.execute("CHECKPOINT")
        except Exception:
            pass

    finally:
        conn.close()

    stats["elapsed_s"] = round(time.perf_counter() - t0, 1)
    return stats


# ---------------------------------------------------------------------------
# Main backfill loop
# ---------------------------------------------------------------------------

def _open_db_with_retry(max_attempts: int = 10) -> duckdb.DuckDBPyConnection:
    """Open DuckDB write connection, retrying if price loader briefly holds the lock."""
    for attempt in range(max_attempts):
        try:
            return duckdb.connect(str(WAREHOUSE_PATH))
        except duckdb.IOException:
            if attempt < max_attempts - 1:
                wait = 3 * (attempt + 1)
                logger.info("DB locked (attempt {}/{}), waiting {}s...", attempt + 1, max_attempts, wait)
                time.sleep(wait)
            else:
                raise


def run_backfill(
    from_quarter: Optional[str] = None,
    to_quarter: Optional[str] = None,
    full_only: bool = False,
    resume: bool = True,
    dry_run: bool = False,
) -> None:
    """Discover and score all pending quarters."""

    # Discovery: use read-only connection so price loader isn't blocked
    try:
        conn_ro = duckdb.connect(str(WAREHOUSE_PATH), read_only=True)
        try:
            pending = get_pending_quarters(
                conn_ro,
                from_quarter=from_quarter,
                to_quarter=to_quarter,
                full_only=full_only,
                resume=resume,
            )
        finally:
            conn_ro.close()
    except Exception as exc:
        logger.error("Failed to discover pending quarters: {}", exc)
        sys.exit(1)

    total = len(pending)

    if total == 0:
        logger.info("Nothing to backfill — all quarters in range are already scored.")
        return

    # Identify which are FULL (price data available) vs PARTIAL
    full_quarters = [q for q in pending if _quarter_sort_key(q)[0] >= 2016]
    partial_quarters = [q for q in pending if _quarter_sort_key(q)[0] < 2016]

    logger.info(
        "Backfill plan: {} quarters total  ({} FULL with prices, {} PARTIAL 13F-only)",
        total, len(full_quarters), len(partial_quarters),
    )
    if from_quarter or to_quarter:
        logger.info(
            "  Range filter: {} → {}",
            from_quarter or "earliest",
            to_quarter or "latest",
        )
    if full_only:
        logger.info("  --full-only: partial (pre-2016) quarters skipped")

    logger.info("  Quarters to process:")
    for i, q in enumerate(pending, 1):
        kind = "FULL" if _quarter_sort_key(q)[0] >= 2016 else "PARTIAL"
        logger.info("    {:3d}. {}  [{}]", i, q, kind)

    if dry_run:
        logger.info("DRY RUN — no changes written.")
        return

    # PRE-FLIGHT: Build manager tiers once (global across all quarters)
    logger.info("=" * 60)
    logger.info("PRE-FLIGHT: Building manager tiers...")
    logger.info("=" * 60)
    try:
        from signal_scanner.institutional_intel.intelligence.manager_quality import (
            build_manager_tiers,
        )
        conn_preflight = _open_db_with_retry()
        try:
            build_manager_tiers(conn_preflight)
        finally:
            conn_preflight.close()
        logger.info("Manager tiers built.")
    except Exception as exc:
        logger.warning("Manager tiers build failed (non-fatal): {}", exc)

    # Loop over quarters — each opens its own connection
    succeeded, failed = 0, 0
    session_start = time.perf_counter()

    for i, quarter in enumerate(pending, 1):
        year = _quarter_sort_key(quarter)[0]
        kind = "FULL" if year >= 2016 else "PARTIAL"
        logger.info("=" * 60)
        logger.info("QUARTER {}/{}: {}  [{}]", i, total, quarter, kind)
        logger.info("=" * 60)

        try:
            stats = run_intelligence_for_quarter(quarter)
            logger.info(
                "  OK {}  phase={} cascade={} conviction={} signals={}  ({:.1f}s)",
                quarter,
                stats.get("n_phase", 0),
                stats.get("n_cascade", 0),
                stats.get("n_conviction", 0),
                stats.get("n_signals", 0),
                stats.get("elapsed_s", 0),
            )
            succeeded += 1

        except Exception as exc:
            logger.error("  FAILED {} : {}", quarter, exc)
            failed += 1
            # Continue to next quarter rather than aborting
            continue

    total_elapsed = time.perf_counter() - session_start
    logger.info("=" * 60)
    logger.info(
        "BACKFILL COMPLETE: {}/{} quarters scored  ({} failed)  total={:.0f}s",
        succeeded, total, failed, total_elapsed,
    )
    if failed:
        logger.warning("  {} quarters failed — re-run with --resume to retry.", failed)
    logger.info("=" * 60)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Historical intelligence backfill — score all unscored quarters",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument(
        "--from-quarter",
        default="",
        metavar="YYYY-QN",
        help="Only process quarters >= this value (e.g. 2016-Q1)",
    )
    p.add_argument(
        "--to-quarter",
        default="",
        metavar="YYYY-QN",
        help="Only process quarters <= this value (e.g. 2025-Q3)",
    )
    p.add_argument(
        "--full-only",
        action="store_true",
        default=False,
        help="Only score quarters with Massive price data (2016-Q1 onward)",
    )
    p.add_argument(
        "--no-resume",
        action="store_true",
        default=False,
        help="Re-score quarters that already have intelligence_scores rows (overwrites)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Print quarters that would be processed without making changes",
    )
    return p.parse_args()


def main() -> None:
    args = parse_args()

    # Validate quarter args
    from_q = _parse_quarter_arg(args.from_quarter) if args.from_quarter else None
    to_q = _parse_quarter_arg(args.to_quarter) if args.to_quarter else None

    run_backfill(
        from_quarter=from_q,
        to_quarter=to_q,
        full_only=args.full_only,
        resume=not args.no_resume,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    main()
