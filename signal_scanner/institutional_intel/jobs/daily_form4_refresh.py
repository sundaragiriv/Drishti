"""Daily Form 4 insider transaction refresh from SEC EDGAR.

Fetches the SEC daily filing index for the last N days, downloads new
Form 3/4/5 filings, parses them, and inserts into fact_form4_transactions.

This bridges the gap between quarterly bulk loads — insider transactions
are available on EDGAR within hours of filing but bulk ZIPs lag by months.

Usage:
    python -m signal_scanner.institutional_intel.jobs.daily_form4_refresh
    python -m signal_scanner.institutional_intel.jobs.daily_form4_refresh --days 10
"""

import argparse
import re
import tempfile
from datetime import date, timedelta
from pathlib import Path
from typing import Dict, List, Set, Tuple

import duckdb
from loguru import logger

from signal_scanner.institutional_intel.config import (
    WAREHOUSE_PATH,
    safe_duckdb_connect,
)
from signal_scanner.institutional_intel.ingest.sec_client import SecClient
from signal_scanner.institutional_intel.ingest.sec_index import (
    FilingIndexEntry,
    fetch_daily_index,
)
from signal_scanner.institutional_intel.parsers.form4_parser import parse_form4

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_FORM4_TYPES = {"3", "4", "5", "3/A", "4/A", "5/A"}

_F4_COLS = [
    "filing_accession_no", "issuer_cik", "issuer_name", "ticker",
    "insider_name", "insider_role", "transaction_date", "transaction_code",
    "direction", "shares", "price", "ownership_after",
    "source_path", "ingested_at",
]

# XML extraction regexes (same patterns as parse_filings.py)
_XML_PROLOG_RE = re.compile(
    r"<\?xml.*?\?>.*?</ownershipDocument>",
    re.DOTALL | re.IGNORECASE,
)
_OD_FALLBACK_RE = re.compile(
    r"<ownershipDocument.*?</ownershipDocument>",
    re.DOTALL | re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------

def _filter_new_accessions(
    conn: duckdb.DuckDBPyConnection,
    candidates: List[str],
) -> Set[str]:
    """Return subset of candidate accession numbers NOT already in the DB.

    Uses a chunked IN query to efficiently check against the full table.
    """
    already_ingested: Set[str] = set()
    chunk_size = 500
    for i in range(0, len(candidates), chunk_size):
        chunk = candidates[i:i + chunk_size]
        placeholders = ", ".join(["?"] * len(chunk))
        rows = conn.execute(
            f"""
            SELECT DISTINCT filing_accession_no
            FROM fact_form4_transactions
            WHERE filing_accession_no IN ({placeholders})
            """,
            chunk,
        ).fetchall()
        already_ingested.update(r[0] for r in rows if r[0])
    return set(candidates) - already_ingested


def _extract_xml_from_sgml(raw: str) -> str:
    """Extract ownershipDocument XML from SGML wrapper text."""
    m = _XML_PROLOG_RE.search(raw)
    if m:
        return m.group(0)
    if "<ownershipDocument" in raw:
        m2 = _OD_FALLBACK_RE.search(raw)
        if m2:
            return m2.group(0)
    return raw  # Last resort: try parsing the raw text as-is


def _download_and_parse(
    client: SecClient,
    entry: FilingIndexEntry,
) -> Tuple[List[Dict], str]:
    """Download a single Form 4 filing and parse it.

    Returns (rows, error_message). error_message is empty on success.
    """
    try:
        raw = client.get_text(entry.filing_url)
    except Exception as exc:
        return [], f"Download failed: {exc}"

    xml_text = _extract_xml_from_sgml(raw)

    context = {"accession_no": entry.accession_no}
    try:
        tmp = tempfile.NamedTemporaryFile(
            suffix=".xml", delete=False, mode="w", encoding="utf-8",
        )
        tmp.write(xml_text)
        tmp.close()
        rows = parse_form4(Path(tmp.name), context)
        Path(tmp.name).unlink(missing_ok=True)

        # Tag source as daily refresh
        for r in rows:
            r["source_path"] = f"daily:{entry.filing_url}"

        return rows, ""
    except Exception as exc:
        return [], f"Parse error: {exc}"


def _insert_rows(conn: duckdb.DuckDBPyConnection, rows: List[Dict]) -> int:
    """Batch insert parsed Form 4 rows into fact_form4_transactions."""
    if not rows:
        return 0
    placeholders = ", ".join(["?"] * len(_F4_COLS))
    sql = f"INSERT INTO fact_form4_transactions ({', '.join(_F4_COLS)}) VALUES ({placeholders})"
    values = [tuple(r.get(c) for c in _F4_COLS) for r in rows]
    conn.executemany(sql, values)
    return len(values)


def refresh_daily_form4(lookback_days: int = 5) -> Dict[str, int]:
    """Fetch and ingest recent Form 3/4/5 filings from SEC EDGAR.

    Args:
        lookback_days: Number of days back to check for new filings.

    Returns:
        Dict with stats: days_checked, filings_found, filings_new,
        rows_inserted, errors.
    """
    conn = safe_duckdb_connect(read_only=False)
    if conn is None:
        logger.warning("DuckDB locked — skipping daily Form 4 refresh")
        return {"days_checked": 0, "filings_found": 0, "filings_new": 0,
                "rows_inserted": 0, "errors": 0, "skipped": "db_locked"}

    try:
        client = SecClient()

        # Collect Form 4 entries from daily indexes
        today = date.today()
        start = today - timedelta(days=lookback_days)
        all_entries: List[FilingIndexEntry] = []
        days_checked = 0

        for offset in range(lookback_days + 1):
            d = start + timedelta(days=offset)
            if d.weekday() >= 5:  # Skip weekends
                continue
            entries = fetch_daily_index(client, d)
            form4_entries = [e for e in entries if e.form_type in _FORM4_TYPES]
            all_entries.extend(form4_entries)
            days_checked += 1

        # Deduplicate entries by accession_no (SEC lists multiple entries
        # per filing — one per reporting owner — but the filing content is identical)
        seen_acc: Set[str] = set()
        unique_entries: List[FilingIndexEntry] = []
        for e in all_entries:
            if e.accession_no not in seen_acc:
                seen_acc.add(e.accession_no)
                unique_entries.append(e)

        logger.info(
            "Daily Form 4: {} unique filings from {} index entries across {} business days",
            len(unique_entries), len(all_entries), days_checked,
        )

        # Filter out already-ingested (checks full table efficiently)
        candidate_accessions = [e.accession_no for e in unique_entries]
        new_accessions = _filter_new_accessions(conn, candidate_accessions)
        new_entries = [e for e in unique_entries if e.accession_no in new_accessions]
        logger.info(
            "Daily Form 4: {} new filings to process (skipping {} already ingested)",
            len(new_entries), len(all_entries) - len(new_entries),
        )

        if not new_entries:
            return {"days_checked": days_checked, "filings_found": len(all_entries),
                    "filings_new": 0, "rows_inserted": 0, "errors": 0}

        # Download, parse, and batch insert
        row_buffer: List[Dict] = []
        total_rows = 0
        total_errors = 0

        for i, entry in enumerate(new_entries):
            rows, err = _download_and_parse(client, entry)
            if err:
                total_errors += 1
                if total_errors <= 20:
                    logger.warning(
                        "Form4 daily error [{}]: {}",
                        entry.accession_no, err,
                    )
                continue

            if not rows:
                continue

            row_buffer.extend(rows)

            # Batch commit every 500 rows
            if len(row_buffer) >= 500:
                inserted = _insert_rows(conn, row_buffer)
                total_rows += inserted
                row_buffer.clear()
                logger.info(
                    "Daily Form 4 progress: {}/{} filings | {} rows",
                    i + 1, len(new_entries), total_rows,
                )

        # Final flush
        if row_buffer:
            inserted = _insert_rows(conn, row_buffer)
            total_rows += inserted

        stats = {
            "days_checked": days_checked,
            "filings_found": len(all_entries),
            "filings_new": len(new_entries),
            "rows_inserted": total_rows,
            "errors": total_errors,
        }
        logger.info("Daily Form 4 refresh complete: {}", stats)
        return stats

    finally:
        conn.close()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    p = argparse.ArgumentParser(
        description="Fetch recent Form 4 insider filings from SEC EDGAR",
    )
    p.add_argument(
        "--days", type=int, default=5,
        help="Number of days to look back (default: 5)",
    )
    args = p.parse_args()

    stats = refresh_daily_form4(lookback_days=args.days)
    print(f"Daily Form 4 refresh: {stats}")


if __name__ == "__main__":
    main()
