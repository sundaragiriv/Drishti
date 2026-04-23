"""Phase A backfill runner."""

import argparse
from datetime import date

from loguru import logger

from signal_scanner.institutional_intel.config import InstitutionalIntelConfig
from signal_scanner.institutional_intel.ingest.download_loop import run_download_loop
from signal_scanner.institutional_intel.warehouse.db import init_warehouse
from signal_scanner.institutional_intel.warehouse.ops import (
    finish_ingestion_run,
    start_ingestion_run,
)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Institutional intelligence backfill")
    p.add_argument("--from-date", default=InstitutionalIntelConfig().backfill_start_date)
    p.add_argument("--to-date", default="")
    p.add_argument(
        "--forms",
        default="13F-HR,13F-HR/A,4,3,5",
        help="Comma-separated SEC forms",
    )
    p.add_argument(
        "--max-filings",
        type=int,
        default=0,
        help="Optional cap for testing (0 = unlimited)",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="Re-download filings even if raw file already exists",
    )
    p.add_argument(
        "--user-agent",
        default="",
        help="Override SEC User-Agent header (recommended: include real contact email)",
    )
    p.add_argument(
        "--progress-every",
        type=int,
        default=50,
        help="Log progress every N saved filings",
    )
    p.add_argument(
        "--metadata-only",
        action="store_true",
        help="Only save filing metadata stubs (faster first pass, no full filing bodies)",
    )
    p.add_argument(
        "--workers",
        type=int,
        default=6,
        help="Concurrent workers for filing downloads (recommended 3-6)",
    )
    p.add_argument(
        "--rps",
        type=float,
        default=8.0,
        help="Global SEC requests/second across workers (safe range: 5-9)",
    )
    p.add_argument(
        "--universe-file",
        default="",
        help="Optional symbol universe file (insider forms 3/4/5 filtered via ticker->CIK map)",
    )
    return p.parse_args()


def main() -> None:
    args = parse_args()
    init_warehouse()
    forms = [f.strip().upper() for f in args.forms.split(",") if f.strip()]
    start_d = date.fromisoformat(args.from_date)
    end_d = date.fromisoformat(args.to_date) if args.to_date else date.today()

    run_id = start_ingestion_run(source="SEC_EDGAR", job_name="backfill", parser_version="phase-a")
    try:
        stats = run_download_loop(
            start_date=start_d,
            end_date=end_d,
            forms=forms,
            max_filings=max(0, args.max_filings),
            force=bool(args.force),
            user_agent=str(args.user_agent or ""),
            progress_every=max(1, int(args.progress_every)),
            metadata_only=bool(args.metadata_only),
            workers=max(1, int(args.workers)),
            universe_file=str(args.universe_file or ""),
            requests_per_second=max(0.1, float(args.rps)),
        )
        finish_ingestion_run(
            run_id=run_id,
            status="SUCCESS",
            rows_ingested=int(stats["files_written"]),
            rows_failed=int(stats["errors"]),
            notes=(
                f"quarters_scanned={stats['quarters_scanned']} "
                f"filings_seen={stats['filings_seen']} "
                f"insider_skipped_by_universe={stats.get('insider_skipped_by_universe', 0)} "
                f"forms={','.join(forms)}"
            ),
        )
        logger.info(
            "Backfill complete | from={} | to={} | forms={} | quarters={} | seen={} | saved={} | errors={} | insider_skipped={}",
            start_d,
            end_d,
            forms,
            stats["quarters_scanned"],
            stats["filings_seen"],
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
