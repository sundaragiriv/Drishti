"""Daily 13F-HR incremental refresh from SEC EDGAR.

Fetches the SEC daily filing index for the last N days, downloads new
13F-HR / 13F-HR/A filings, parses the information table, and inserts
into fact_13f_positions.

This bridges the gap between quarterly bulk loads:
  - Amendments (13F-HR/A) arrive continuously throughout the year
  - New funds crossing $100M AUM file their initial 13F mid-quarter
  - Late filers submit after the quarterly deadline

Quarterly reconciliation:
  When SEC publishes the bulk ZIP (~2 months after quarter end), run:
    python -m signal_scanner.institutional_intel.jobs.run_pipeline
      --mode bulk --from-year {year}
  This uses ON CONFLICT DO NOTHING, so daily-ingested records survive.

Usage:
    python -m signal_scanner.institutional_intel.jobs.daily_13f_refresh
    python -m signal_scanner.institutional_intel.jobs.daily_13f_refresh --days 10
"""

from __future__ import annotations

import argparse
import re
import tempfile
from datetime import date, datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

import duckdb
from loguru import logger

from signal_scanner.institutional_intel.config import safe_duckdb_connect
from signal_scanner.institutional_intel.ingest.sec_client import SecClient
from signal_scanner.institutional_intel.ingest.sec_index import (
    FilingIndexEntry,
    fetch_daily_index,
)
from signal_scanner.institutional_intel.parsers.form13f_parser import (
    parse_13f_information_table,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_13F_INGEST_TYPES = {"13F-HR", "13F-HR/A"}

_F13_COLS = [
    "filing_accession_no", "manager_cik", "manager_name", "report_period",
    "filed_at", "issuer_name", "cusip", "ticker", "class_title",
    "value_usd_thousands", "shares", "put_call", "discretion",
    "source_path", "ingested_at",
]

# Regex patterns for SGML header extraction
_CIK_RE = re.compile(r"CENTRAL INDEX KEY:\s*(\d+)")
_NAME_RE = re.compile(r"COMPANY CONFORMED NAME:\s*(.+)")
_PERIOD_RE = re.compile(r"PERIOD OF REPORT:\s*(\d{8})")
_FILED_RE = re.compile(r"FILED AS OF DATE:\s*(\d{8})")

# Match any <XML>...</XML> block (may contain informationTable)
_XML_BLOCK_RE = re.compile(r"<XML>(.*?)</XML>", re.DOTALL | re.IGNORECASE)

# Fallback: directly find <informationTable> in raw text
_INFO_TABLE_RE = re.compile(
    r"<informationTable.*?</informationTable>",
    re.DOTALL | re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Dedup check
# ---------------------------------------------------------------------------

def _filter_new_accessions(
    conn: duckdb.DuckDBPyConnection,
    candidates: List[str],
) -> Set[str]:
    """Return subset of accession numbers NOT already in fact_13f_positions."""
    already: Set[str] = set()
    chunk = 500
    for i in range(0, len(candidates), chunk):
        batch = candidates[i : i + chunk]
        placeholders = ", ".join(["?"] * len(batch))
        rows = conn.execute(
            f"""
            SELECT DISTINCT filing_accession_no
            FROM fact_13f_positions
            WHERE filing_accession_no IN ({placeholders})
            """,
            batch,
        ).fetchall()
        already.update(r[0] for r in rows if r[0])
    return set(candidates) - already


# ---------------------------------------------------------------------------
# SGML parsing
# ---------------------------------------------------------------------------

def _parse_header(sgml: str) -> Dict[str, str]:
    """Extract manager metadata from the SEC-HEADER section of an SGML filing."""
    # Restrict search to first 20 KB to avoid scanning the whole filing body
    header_snippet = sgml[:20_000]

    m_cik = _CIK_RE.search(header_snippet)
    m_name = _NAME_RE.search(header_snippet)
    m_period = _PERIOD_RE.search(header_snippet)
    m_filed = _FILED_RE.search(header_snippet)

    def _yyyymmdd_to_iso(raw: Optional[re.Match]) -> str:
        if not raw:
            return ""
        d = raw.group(1)
        return f"{d[:4]}-{d[4:6]}-{d[6:]}"

    return {
        "manager_cik": m_cik.group(1) if m_cik else "",
        "manager_name": (m_name.group(1).strip() if m_name else ""),
        "report_period": _yyyymmdd_to_iso(m_period),
        "filed_at": _yyyymmdd_to_iso(m_filed),
    }


def _extract_info_table_xml(sgml: str) -> str:
    """Find the informationTable XML block embedded in the SGML wrapper.

    The 13F SGML file may contain multiple <XML>…</XML> blocks:
      - Block 1: edgarSubmission (cover page)
      - Block 2: informationTable (the positions we want)

    We iterate all blocks and return the first that contains 'informationTable'.
    """
    for m in _XML_BLOCK_RE.finditer(sgml):
        content = m.group(1).strip()
        if "informationTable" in content or "infoTable" in content:
            return content

    # Fallback: some filings embed the table without an <XML> wrapper tag
    m_direct = _INFO_TABLE_RE.search(sgml)
    if m_direct:
        return m_direct.group(0)

    return ""


# ---------------------------------------------------------------------------
# Download + parse
# ---------------------------------------------------------------------------

def _download_and_parse(
    client: SecClient,
    entry: FilingIndexEntry,
) -> Tuple[List[Dict], str]:
    """Download a 13F-HR filing and parse its information table.

    Returns (rows, error_message). error_message is empty on success.
    """
    try:
        sgml = client.get_text(entry.filing_url)
    except Exception as exc:
        return [], f"Download failed: {exc}"

    # Extract manager metadata from SGML header
    header = _parse_header(sgml)
    if not header["report_period"]:
        return [], "Could not extract PERIOD OF REPORT from SGML header"

    # Extract the informationTable XML
    xml_text = _extract_info_table_xml(sgml)
    if not xml_text:
        # 13F-NT (notification of late filing) has no table — skip silently
        return [], ""

    # Write to temp file and parse
    try:
        tmp = tempfile.NamedTemporaryFile(
            suffix=".xml", delete=False, mode="w", encoding="utf-8",
        )
        tmp.write(xml_text)
        tmp.close()

        context = {
            "accession_no": entry.accession_no,
            "manager_cik": header["manager_cik"],
            "manager_name": header["manager_name"],
            "report_period": header["report_period"],
            "filed_at": header["filed_at"],
        }
        rows = parse_13f_information_table(Path(tmp.name), context)
        Path(tmp.name).unlink(missing_ok=True)

        # Tag source
        for r in rows:
            r["source_path"] = f"daily:{entry.filing_url}"

        return rows, ""
    except Exception as exc:
        return [], f"Parse error: {exc}"


# ---------------------------------------------------------------------------
# DB write
# ---------------------------------------------------------------------------

def _insert_rows(conn: duckdb.DuckDBPyConnection, rows: List[Dict]) -> int:
    """Batch insert 13F position rows. Uses temp-table DELETE+INSERT to
    handle amendments: if a 13F-HR/A replaces positions from an earlier
    filing with the same accession, the DELETE pass removes the old rows
    before re-inserting.
    """
    if not rows:
        return 0

    placeholders = ", ".join(["?"] * len(_F13_COLS))
    sql = (
        f"INSERT INTO fact_13f_positions ({', '.join(_F13_COLS)}) "
        f"VALUES ({placeholders})"
    )
    values = [tuple(r.get(c) for c in _F13_COLS) for r in rows]

    # Idempotent: remove any existing rows for this accession before inserting
    accession = rows[0].get("filing_accession_no", "")
    if accession:
        conn.execute(
            "DELETE FROM fact_13f_positions WHERE filing_accession_no = ?",
            [accession],
        )

    conn.executemany(sql, values)
    return len(values)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def refresh_daily_13f(lookback_days: int = 5) -> Dict[str, int]:
    """Fetch and ingest recent 13F-HR / 13F-HR/A filings from SEC EDGAR.

    Args:
        lookback_days: Number of calendar days to look back in daily index.

    Returns:
        Stats dict: days_checked, filings_found, filings_new,
                    rows_inserted, errors.
    """
    conn = safe_duckdb_connect(read_only=False)
    if conn is None:
        logger.warning("[13F daily] DuckDB locked — skipping daily 13F refresh")
        return {
            "days_checked": 0, "filings_found": 0, "filings_new": 0,
            "rows_inserted": 0, "errors": 0, "skipped": "db_locked",
        }

    try:
        client = SecClient()
        today = date.today()
        start = today - timedelta(days=lookback_days)

        all_entries: List[FilingIndexEntry] = []
        days_checked = 0

        for offset in range(lookback_days + 1):
            d = start + timedelta(days=offset)
            if d.weekday() >= 5:  # skip weekends
                continue
            entries = fetch_daily_index(client, d)
            f13_entries = [e for e in entries if e.form_type in _13F_INGEST_TYPES]
            all_entries.extend(f13_entries)
            days_checked += 1

        # Deduplicate by accession_no (each entry in daily index = one filing)
        seen: Set[str] = set()
        unique_entries: List[FilingIndexEntry] = []
        for e in all_entries:
            if e.accession_no not in seen:
                seen.add(e.accession_no)
                unique_entries.append(e)

        logger.info(
            "[13F daily] {} unique 13F-HR filings from {} index entries across {} days",
            len(unique_entries), len(all_entries), days_checked,
        )

        if not unique_entries:
            return {
                "days_checked": days_checked, "filings_found": 0,
                "filings_new": 0, "rows_inserted": 0, "errors": 0,
            }

        # Filter out already-ingested accessions
        candidate_accessions = [e.accession_no for e in unique_entries]
        new_accessions = _filter_new_accessions(conn, candidate_accessions)
        new_entries = [e for e in unique_entries if e.accession_no in new_accessions]

        logger.info(
            "[13F daily] {} new filings to process (skipping {} already ingested)",
            len(new_entries), len(unique_entries) - len(new_entries),
        )

        if not new_entries:
            return {
                "days_checked": days_checked, "filings_found": len(all_entries),
                "filings_new": 0, "rows_inserted": 0, "errors": 0,
            }

        # Download, parse, batch insert
        row_buffer: List[Dict] = []
        total_rows = 0
        total_errors = 0

        for i, entry in enumerate(new_entries):
            rows, err = _download_and_parse(client, entry)
            if err:
                total_errors += 1
                if total_errors <= 20:
                    logger.warning("[13F daily] {} — {}", entry.accession_no, err)
                continue

            if not rows:
                continue  # 13F-NT or empty table — not an error

            row_buffer.extend(rows)

            # Flush every 2000 rows (13Fs can have thousands of holdings per filing)
            if len(row_buffer) >= 2000:
                for r in row_buffer:
                    total_rows += _insert_rows(conn, [r])
                row_buffer.clear()
                logger.info(
                    "[13F daily] progress {}/{} filings | {} rows",
                    i + 1, len(new_entries), total_rows,
                )

        # Final flush — batch by accession to keep DELETE+INSERT idempotent
        if row_buffer:
            from itertools import groupby
            row_buffer.sort(key=lambda r: r.get("filing_accession_no", ""))
            for acc, grp in groupby(row_buffer, key=lambda r: r.get("filing_accession_no", "")):
                grp_rows = list(grp)
                total_rows += _insert_rows(conn, grp_rows)

        stats = {
            "days_checked": days_checked,
            "filings_found": len(all_entries),
            "filings_new": len(new_entries),
            "rows_inserted": total_rows,
            "errors": total_errors,
        }
        logger.info("[13F daily] refresh complete: {}", stats)
        return stats

    finally:
        conn.close()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    p = argparse.ArgumentParser(
        description="Fetch recent 13F-HR filings from SEC EDGAR (daily incremental)",
    )
    p.add_argument(
        "--days", type=int, default=5,
        help="Number of days to look back (default: 5)",
    )
    args = p.parse_args()

    stats = refresh_daily_13f(lookback_days=args.days)
    print(f"[13F daily] {stats}")


if __name__ == "__main__":
    main()
