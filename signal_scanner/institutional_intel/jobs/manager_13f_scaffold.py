"""Scaffold for manager-centric 13F flow.

Purpose:
- Keep 13F handling manager-first (correct model) instead of ticker-first.
- Build manager inventory and prep for downstream holdings extraction.
"""

from pathlib import Path

import duckdb
from loguru import logger

from signal_scanner.institutional_intel.config import WAREHOUSE_PATH


def main() -> None:
    out_dir = Path("data") / "meta"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_csv = out_dir / "manager_13f_inventory.csv"

    con = duckdb.connect(str(WAREHOUSE_PATH))
    try:
        rows = con.execute(
            """
            SELECT cik AS manager_cik,
                   COUNT(*) AS filing_count,
                   MIN(filing_date) AS first_filing_date,
                   MAX(filing_date) AS latest_filing_date
            FROM raw_file_manifest
            WHERE form_type IN ('13F-HR', '13F-HR/A')
            GROUP BY 1
            ORDER BY filing_count DESC
            """
        ).fetchdf()
    finally:
        con.close()

    rows.to_csv(out_csv, index=False)
    logger.info(
        "13F manager scaffold output | managers={} | csv={}",
        len(rows),
        out_csv,
    )
    logger.info(
        "Next steps: parse information table rows and populate fact_13f_positions by manager/report_period"
    )


if __name__ == "__main__":
    main()

