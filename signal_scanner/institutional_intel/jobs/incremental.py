"""Phase A incremental refresh runner."""

import argparse
from datetime import date, timedelta

from loguru import logger

from signal_scanner.institutional_intel.config import InstitutionalIntelConfig
from signal_scanner.institutional_intel.ingest.download_loop import run_download_loop
from signal_scanner.institutional_intel.warehouse.db import init_warehouse
from signal_scanner.institutional_intel.warehouse.ops import (
    finish_ingestion_run,
    get_max_manifest_filing_date,
    start_ingestion_run,
)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Institutional intelligence incremental refresh")
    p.add_argument(
        "--metadata-only",
        action="store_true",
        help="Only save filing metadata stubs (no full filing bodies)",
    )
    p.add_argument(
        "--workers",
        type=int,
        default=3,
        help="Concurrent workers for filing downloads (recommended 3-6)",
    )
    p.add_argument(
        "--universe-file",
        default="",
        help="Optional symbol universe file (insider forms 3/4/5 filtered via ticker->CIK map)",
    )
    p.add_argument(
        "--user-agent",
        default="",
        help="Override SEC User-Agent header",
    )
    p.add_argument(
        "--progress-every",
        type=int,
        default=50,
        help="Log progress every N saved filings",
    )
    return p.parse_args()


def main() -> None:
    args = parse_args()
    init_warehouse()
    latest = get_max_manifest_filing_date()
    cfg = InstitutionalIntelConfig()
    if latest:
        start_d = date.fromisoformat(latest) + timedelta(days=1)
    else:
        start_d = date.fromisoformat(cfg.backfill_start_date)
    end_d = date.today()
    if start_d > end_d:
        logger.info("Incremental run skipped: manifest already up to date")
        return

    forms = ["13F-HR", "13F-HR/A", "4", "3", "5"]
    run_id = start_ingestion_run(source="SEC_EDGAR", job_name="incremental", parser_version="phase-a")
    try:
        stats = run_download_loop(
            start_date=start_d,
            end_date=end_d,
            forms=forms,
            max_filings=0,
            force=False,
            metadata_only=bool(args.metadata_only),
            workers=max(1, int(args.workers)),
            universe_file=str(args.universe_file or ""),
            user_agent=str(args.user_agent or ""),
            progress_every=max(1, int(args.progress_every)),
        )
        finish_ingestion_run(
            run_id=run_id,
            status="SUCCESS",
            rows_ingested=int(stats["files_written"]),
            rows_failed=int(stats["errors"]),
            notes=(
                f"quarters_scanned={stats['quarters_scanned']} "
                f"filings_seen={stats['filings_seen']} "
                f"insider_skipped_by_universe={stats.get('insider_skipped_by_universe', 0)}"
            ),
        )
        logger.info(
            "Incremental complete | from={} | to={} | saved={} | errors={} | insider_skipped={}",
            start_d,
            end_d,
            stats["files_written"],
            stats["errors"],
            stats.get("insider_skipped_by_universe", 0),
        )
    except Exception as ex:
        finish_ingestion_run(
            run_id=run_id,
            status="FAILED",
            rows_ingested=0,
            rows_failed=1,
            notes=str(ex),
        )
        raise


if __name__ == "__main__":
    main()
