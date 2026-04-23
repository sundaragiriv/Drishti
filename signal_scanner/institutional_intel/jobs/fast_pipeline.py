"""Run a fast, staged SEC ingestion pipeline for Quant-Bridge universes.

Phase 1 (default): insider forms (3/4/5), universe-filtered, metadata-first.
Phase 2 (optional): institutional 13F metadata pass.
"""

from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path

from loguru import logger

from signal_scanner.institutional_intel.ingest.download_loop import run_download_loop
from signal_scanner.institutional_intel.warehouse.db import init_warehouse
from signal_scanner.institutional_intel.warehouse.ops import (
    finish_ingestion_run,
    start_ingestion_run,
)


def parse_args() -> argparse.Namespace:
    default_universe = (
        Path(__file__).resolve().parents[2] / "watchlists" / "universe_master.txt"
    )
    p = argparse.ArgumentParser(description="Fast staged SEC metadata pipeline")
    p.add_argument("--from-date", default="2020-01-01")
    p.add_argument("--to-date", default="")
    p.add_argument("--universe-file", default=str(default_universe))
    p.add_argument("--workers", type=int, default=6)
    p.add_argument("--rps", type=float, default=8.0)
    p.add_argument("--progress-every", type=int, default=500)
    p.add_argument("--user-agent", default="")
    p.add_argument(
        "--include-13f",
        action="store_true",
        help="Also run metadata pass for 13F-HR/13F-HR-A after insider phase",
    )
    p.add_argument(
        "--max-filings",
        type=int,
        default=0,
        help="Optional cap per phase (0=unlimited)",
    )
    return p.parse_args()


def _run_phase(
    *,
    name: str,
    forms: list[str],
    start_d: date,
    end_d: date,
    workers: int,
    rps: float,
    progress_every: int,
    user_agent: str,
    max_filings: int,
    universe_file: str,
    apply_universe_filter: bool,
) -> None:
    run_id = start_ingestion_run(source="SEC_EDGAR", job_name=name, parser_version="phase-a-fast")
    try:
        stats = run_download_loop(
            start_date=start_d,
            end_date=end_d,
            forms=forms,
            max_filings=max(0, int(max_filings)),
            force=False,
            user_agent=str(user_agent or ""),
            progress_every=max(1, int(progress_every)),
            metadata_only=True,
            workers=max(1, int(workers)),
            universe_file=(str(universe_file) if apply_universe_filter else ""),
            requests_per_second=max(0.1, float(rps)),
        )
        finish_ingestion_run(
            run_id=run_id,
            status="SUCCESS",
            rows_ingested=int(stats["files_written"]),
            rows_failed=int(stats["errors"]),
            notes=(
                f"forms={','.join(forms)} "
                f"quarters_scanned={stats['quarters_scanned']} "
                f"filings_seen={stats['filings_seen']} "
                f"insider_skipped_by_universe={stats.get('insider_skipped_by_universe', 0)}"
            ),
        )
        logger.info(
            "{} complete | forms={} | quarters={} | seen={} | saved={} | errors={} | insider_skipped={}",
            name,
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


def main() -> None:
    args = parse_args()
    init_warehouse()
    start_d = date.fromisoformat(args.from_date)
    end_d = date.fromisoformat(args.to_date) if args.to_date else date.today()

    logger.info(
        "Fast pipeline start | from={} | to={} | workers={} | rps={} | universe={} | include_13f={}",
        start_d,
        end_d,
        args.workers,
        args.rps,
        args.universe_file,
        bool(args.include_13f),
    )

    _run_phase(
        name="fast_insider_metadata",
        forms=["4", "3", "5"],
        start_d=start_d,
        end_d=end_d,
        workers=args.workers,
        rps=args.rps,
        progress_every=args.progress_every,
        user_agent=args.user_agent,
        max_filings=args.max_filings,
        universe_file=args.universe_file,
        apply_universe_filter=True,
    )

    if args.include_13f:
        _run_phase(
            name="fast_13f_metadata",
            forms=["13F-HR", "13F-HR/A"],
            start_d=start_d,
            end_d=end_d,
            workers=max(1, min(int(args.workers), 4)),
            rps=args.rps,
            progress_every=args.progress_every,
            user_agent=args.user_agent,
            max_filings=args.max_filings,
            universe_file=args.universe_file,
            apply_universe_filter=False,
        )

    logger.info("Fast pipeline finished")


if __name__ == "__main__":
    main()
