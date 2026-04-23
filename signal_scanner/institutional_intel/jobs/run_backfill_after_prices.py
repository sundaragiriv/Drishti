"""Launcher: wait for price loader to finish, then immediately run backfill.

Run this in a background terminal. It will poll the price loader progress file
and start the historical intelligence backfill automatically once prices are done.

Usage:
    python -m signal_scanner.institutional_intel.jobs.run_backfill_after_prices

    # Only score FULL quarters (2016+) after prices load
    python -m signal_scanner.institutional_intel.jobs.run_backfill_after_prices --full-only

    # Score from a specific quarter onward after prices
    python -m signal_scanner.institutional_intel.jobs.run_backfill_after_prices --from-quarter 2016-Q1
"""

import argparse
import subprocess
import sys
import time
from pathlib import Path

from loguru import logger

WAREHOUSE_DIR = Path(__file__).resolve().parents[3] / "data" / "warehouse"
PROGRESS_FILE = WAREHOUSE_DIR / "price_loader_progress.txt"

POLL_INTERVAL_S = 60        # how often to check DB availability
DB_STABLE_CHECKS = 3        # number of consecutive successful opens before starting
DB_CHECK_GAP_S = 5          # seconds between stability checks


def _can_open_db_readonly() -> bool:
    """Try to open DuckDB read-only. Returns True if successful."""
    import duckdb as _duckdb
    try:
        c = _duckdb.connect(str(WAREHOUSE_DIR / "sec_intel.duckdb"), read_only=True)
        c.close()
        return True
    except Exception:
        return False


def _last_progress_line() -> str:
    if not PROGRESS_FILE.exists():
        return "(no progress file yet)"
    try:
        lines = [l for l in PROGRESS_FILE.read_text(encoding="utf-8").splitlines() if l.strip()]
        return lines[-1] if lines else "(empty)"
    except Exception:
        return "(unreadable)"


def wait_for_price_loader() -> None:
    """Block until DuckDB becomes stably available (price loader no longer holding it).

    We require DB_STABLE_CHECKS consecutive successful opens, spaced DB_CHECK_GAP_S
    seconds apart, before proceeding — this ensures we're not just catching the brief
    gap between per-date inserts in the price loader.
    """
    logger.info("Waiting for price loader to release the DuckDB file...")
    logger.info("Polling every {}s. Need {} stable reads to confirm.", POLL_INTERVAL_S, DB_STABLE_CHECKS)
    waited_s = 0
    while True:
        if _can_open_db_readonly():
            # Perform stability check: confirm it stays open for DB_STABLE_CHECKS consecutive tries
            stable_count = 1
            for _ in range(DB_STABLE_CHECKS - 1):
                time.sleep(DB_CHECK_GAP_S)
                if _can_open_db_readonly():
                    stable_count += 1
                else:
                    stable_count = 0
                    break
            if stable_count >= DB_STABLE_CHECKS:
                logger.info(
                    "DuckDB available for {} consecutive checks ({:.0f}s stable) — "
                    "price loader appears complete. Starting backfill...",
                    stable_count, DB_STABLE_CHECKS * DB_CHECK_GAP_S,
                )
                return
            else:
                logger.info("  DB opened but not stable (loader still active). Continuing to wait...")
        else:
            last = _last_progress_line()
            logger.info("  DB locked ({}s elapsed) | last: {}", waited_s, last)
        time.sleep(POLL_INTERVAL_S)
        waited_s += POLL_INTERVAL_S


def run_backfill(args: argparse.Namespace) -> int:
    """Invoke the backfill module as a subprocess."""
    cmd = [sys.executable, "-m",
           "signal_scanner.institutional_intel.jobs.historical_intelligence_backfill"]
    if args.from_quarter:
        cmd += ["--from-quarter", args.from_quarter]
    if args.to_quarter:
        cmd += ["--to-quarter", args.to_quarter]
    if args.full_only:
        cmd.append("--full-only")

    logger.info("Running: {}", " ".join(cmd))
    result = subprocess.run(cmd)
    return result.returncode


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Wait for price loader then run backfill")
    p.add_argument("--from-quarter", default="", metavar="YYYY-QN")
    p.add_argument("--to-quarter",   default="", metavar="YYYY-QN")
    p.add_argument("--full-only", action="store_true", default=False,
                   help="Only score quarters with price data (2016+)")
    p.add_argument("--no-wait", action="store_true", default=False,
                   help="Skip the wait check and run backfill immediately")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    if not args.no_wait:
        wait_for_price_loader()
    rc = run_backfill(args)
    sys.exit(rc)


if __name__ == "__main__":
    main()
