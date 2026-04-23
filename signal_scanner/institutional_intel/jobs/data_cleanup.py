"""Data Quality Cleanup — intelligence_scores table.

Performs three cleanup operations:
    1. Removes junk/invalid tickers (N/A, NONE, multi-ticker strings, etc.)
    2. Adds data_quality_score column if missing
    3. Marks contaminated quarters (2024-Q1) with score 0.0
    4. Marks sparse quarters (< 500 tickers) with score 50.0
    5. All other quarters default to 100.0

Usage:
    python -m signal_scanner.institutional_intel.jobs.data_cleanup
    python -m signal_scanner.institutional_intel.jobs.data_cleanup --dry-run
"""

from __future__ import annotations

import argparse
import sys

import duckdb
from loguru import logger

from signal_scanner.institutional_intel.config import WAREHOUSE_PATH

# Quarters confirmed contaminated by SEC bulk data year-boundary gaps
# 2025-Q3 was removed — re-checked Mar 4 2026: 3,239 tickers, avg_inst=380 (healthy).
CONTAMINATED_QUARTERS = ("2024-Q1",)

# Min ticker count below which a quarter is considered sparse (early partial data)
# Set low — the most recent quarter always starts with early filers only.
# Contaminated quarters (quality=0) already handle truly bad data.
SPARSE_THRESHOLD = 200


def _is_valid_ticker(ticker: str) -> bool:
    """Return True if ticker looks like a real single stock ticker (1-5 uppercase alphanum)."""
    if not ticker or not ticker.strip():
        return False
    t = ticker.strip()
    if len(t) > 5:
        return False
    # Must not contain special characters that indicate multi-ticker or exchange-prefixed entries
    for ch in " ,;:/()[]{}\\|.":
        if ch in t:
            return False
    return True


def run_cleanup(dry_run: bool = False) -> None:
    """Run all data quality cleanup steps."""
    logger.info("Opening DuckDB warehouse: {}", WAREHOUSE_PATH)
    conn = duckdb.connect(str(WAREHOUSE_PATH))

    try:
        # ---------------------------------------------------------------
        # Step 1: Count junk tickers before cleanup
        # ---------------------------------------------------------------
        junk_count = conn.execute("""
            SELECT COUNT(*)
            FROM intelligence_scores
            WHERE ticker IN ('N/A','NONE','NULL','')
               OR LENGTH(ticker) > 5
               OR ticker LIKE '% %'
               OR ticker LIKE '%,%'
               OR ticker LIKE '%:%'
               OR ticker LIKE '%;%'
               OR ticker LIKE '%/%'
               OR ticker LIKE '%(%'
        """).fetchone()[0]

        logger.info("Junk ticker rows found: {}", junk_count)

        if not dry_run and junk_count > 0:
            deleted = conn.execute("""
                DELETE FROM intelligence_scores
                WHERE ticker IN ('N/A','NONE','NULL','')
                   OR LENGTH(ticker) > 5
                   OR ticker LIKE '% %'
                   OR ticker LIKE '%,%'
                   OR ticker LIKE '%:%'
                   OR ticker LIKE '%;%'
                   OR ticker LIKE '%/%'
                   OR ticker LIKE '%(%'
            """).rowcount
            logger.info("  Deleted {} junk ticker rows.", deleted)
        elif dry_run:
            logger.info("  DRY RUN — would delete {} junk ticker rows.", junk_count)

        # ---------------------------------------------------------------
        # Step 2: Add data_quality_score column if missing
        # ---------------------------------------------------------------
        existing_cols = {
            row[0] for row in conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name='intelligence_scores'"
            ).fetchall()
        }

        if "data_quality_score" not in existing_cols:
            if not dry_run:
                conn.execute(
                    "ALTER TABLE intelligence_scores "
                    "ADD COLUMN data_quality_score REAL DEFAULT 100.0"
                )
                logger.info("  Added data_quality_score column (DEFAULT 100.0).")
            else:
                logger.info("  DRY RUN — would add data_quality_score column.")
        else:
            logger.info("  data_quality_score column already exists.")

        # ---------------------------------------------------------------
        # Step 3: Reset all quality scores to 100.0 (idempotent baseline)
        # Steps 4+5 will re-apply penalties on top. This ensures threshold
        # changes (e.g. SPARSE_THRESHOLD) take effect on re-runs.
        # ---------------------------------------------------------------
        if not dry_run:
            conn.execute("""
                UPDATE intelligence_scores
                SET data_quality_score = 100.0
                WHERE data_quality_score IS NULL OR data_quality_score != 0.0
            """)
            logger.info("  Reset all non-contaminated quality scores to 100.0.")

        # ---------------------------------------------------------------
        # Step 4: Mark contaminated quarters
        # ---------------------------------------------------------------
        for q in CONTAMINATED_QUARTERS:
            cnt = conn.execute(
                "SELECT COUNT(*) FROM intelligence_scores WHERE report_quarter = ?", [q]
            ).fetchone()[0]
            logger.info("Contaminated quarter {}: {} rows → marking data_quality_score=0.0", q, cnt)
            if not dry_run and cnt:
                conn.execute(
                    "UPDATE intelligence_scores SET data_quality_score = 0.0 "
                    "WHERE report_quarter = ?",
                    [q]
                )

        # ---------------------------------------------------------------
        # Step 5: Mark sparse quarters
        # ---------------------------------------------------------------
        quarter_counts = conn.execute("""
            SELECT report_quarter, COUNT(*) as cnt
            FROM intelligence_scores
            WHERE data_quality_score > 0   -- don't double-penalise contaminated
            GROUP BY report_quarter
            ORDER BY cnt
        """).fetchall()

        sparse_quarters = [(q, cnt) for q, cnt in quarter_counts if cnt < SPARSE_THRESHOLD]
        for q, cnt in sparse_quarters:
            logger.info("Sparse quarter {}: {} tickers → marking data_quality_score=50.0", q, cnt)
            if not dry_run:
                conn.execute(
                    "UPDATE intelligence_scores SET data_quality_score = 50.0 "
                    "WHERE report_quarter = ?",
                    [q]
                )

        # ---------------------------------------------------------------
        # Summary
        # ---------------------------------------------------------------
        if not dry_run:
            conn.execute("CHECKPOINT")
            summary = conn.execute("""
                SELECT
                    COUNT(*) as total_rows,
                    COUNT(DISTINCT ticker) as unique_tickers,
                    COUNT(DISTINCT report_quarter) as quarters,
                    SUM(CASE WHEN data_quality_score = 0.0 THEN 1 ELSE 0 END) as contaminated,
                    SUM(CASE WHEN data_quality_score = 50.0 THEN 1 ELSE 0 END) as sparse,
                    SUM(CASE WHEN data_quality_score >= 75.0 THEN 1 ELSE 0 END) as clean
                FROM intelligence_scores
            """).fetchone()
            logger.info("=== CLEANUP COMPLETE ===")
            logger.info("  Total rows       : {}", summary[0])
            logger.info("  Unique tickers   : {}", summary[1])
            logger.info("  Quarters         : {}", summary[2])
            logger.info("  Contaminated rows: {}", summary[3])
            logger.info("  Sparse rows      : {}", summary[4])
            logger.info("  Clean rows (>=75): {}", summary[5])

            # List clean quarters for reference
            clean_quarters = conn.execute("""
                SELECT report_quarter, COUNT(*) as tickers, AVG(data_quality_score) as avg_quality
                FROM intelligence_scores
                WHERE data_quality_score >= 75
                GROUP BY report_quarter
                ORDER BY report_quarter DESC
            """).fetchall()
            logger.info("  Clean quarters:")
            for q, cnt, score in clean_quarters:
                logger.info("    {} — {} tickers (quality={:.0f})", q, cnt, score)
        else:
            logger.info("DRY RUN complete — no changes written.")

    finally:
        conn.close()


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Clean junk tickers and mark contaminated quarters")
    p.add_argument("--dry-run", action="store_true", default=False,
                   help="Show what would be changed without modifying the DB")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    run_cleanup(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
