"""Run aggregation engine: build quarterly snapshots and compute QoQ changes.

Reads fact_13f_positions, writes to agg_quarterly_holdings, agg_qoq_changes,
and agg_sector_quarterly.

Usage:
    python -m signal_scanner.institutional_intel.jobs.aggregate
    python -m signal_scanner.institutional_intel.jobs.aggregate --quarters "2024-Q3,2024-Q4"
"""

import argparse
from typing import Dict, List, Optional

from loguru import logger

from signal_scanner.institutional_intel.reports.qoq_engine import (
    build_quarterly_snapshots,
    compute_qoq_changes,
)
from signal_scanner.institutional_intel.warehouse.db import init_warehouse
from signal_scanner.institutional_intel.warehouse.ops import (
    finish_ingestion_run,
    start_ingestion_run,
)


def run_aggregation(quarters: Optional[List[str]] = None) -> Dict[str, int]:
    """Run full aggregation pipeline.

    Args:
        quarters: Optional list of quarters to rebuild (e.g. ['2024-Q3']).
                  If None, rebuilds all quarters found in fact data.

    Returns:
        Dict with snapshot_rows and qoq_rows counts.
    """
    logger.info("Building quarterly snapshots...")
    snapshot_rows = build_quarterly_snapshots(quarters=quarters)

    logger.info("Computing QoQ changes...")
    qoq_rows = compute_qoq_changes(quarters=quarters)

    return {
        "snapshot_rows": snapshot_rows,
        "qoq_rows": qoq_rows,
    }


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Aggregate fact tables into quarterly reports"
    )
    p.add_argument(
        "--quarters",
        default="",
        help="Comma-separated quarters to rebuild (e.g. '2024-Q3,2024-Q4'). Empty = all.",
    )
    return p.parse_args()


def main() -> None:
    args = parse_args()
    init_warehouse()

    quarters = [q.strip() for q in args.quarters.split(",") if q.strip()] or None

    run_id = start_ingestion_run(
        source="SEC", job_name="aggregate", parser_version="phase-b"
    )

    try:
        stats = run_aggregation(quarters=quarters)
        finish_ingestion_run(
            run_id=run_id,
            status="COMPLETED",
            rows_ingested=stats["snapshot_rows"] + stats["qoq_rows"],
            rows_failed=0,
            notes=f"snapshots={stats['snapshot_rows']} qoq={stats['qoq_rows']}",
        )
        logger.info(
            "Aggregation complete | snapshots={} | qoq_rows={}",
            stats["snapshot_rows"],
            stats["qoq_rows"],
        )
    except Exception as exc:
        finish_ingestion_run(
            run_id=run_id,
            status="FAILED",
            rows_ingested=0,
            rows_failed=1,
            notes=str(exc),
        )
        raise


if __name__ == "__main__":
    main()
