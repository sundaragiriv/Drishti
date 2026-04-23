"""Daily Form 8-K material event refresh from SEC EDGAR.

Fetches the SEC daily filing index for the last N days, downloads new
Form 8-K filings, extracts material event item codes from the SGML header,
and stores structured records in fact_form8k_events.

8-K items we track:
  1.01  Entry into Material Definitive Agreement  (M&A / deals)
  1.05  Material Cybersecurity Incidents
  2.01  Completion of Acquisition or Disposition
  2.02  Results of Operations and Financial Condition  (earnings)
  2.04  Triggering Events Under Debt Instruments
  5.02  Departure/Election of Directors or Officers  (C-suite changes)
  5.03  Amendments to Articles of Incorporation
  7.01  Regulation FD Disclosure
  8.01  Other Events

The SGML header for 8-K filings contains ITEM INFORMATION: fields (text
descriptions of each item). We map these descriptions to numeric codes via
keyword matching — no need to download the full filing body for basic
classification.

Usage:
    python -m signal_scanner.institutional_intel.jobs.daily_8k_refresh
    python -m signal_scanner.institutional_intel.jobs.daily_8k_refresh --days 10
    python -m signal_scanner.institutional_intel.jobs.daily_8k_refresh --ticker-lookup
"""

from __future__ import annotations

import argparse
import re
from datetime import date, datetime, timezone, timedelta
from typing import Dict, List, Optional, Set, Tuple

import duckdb
from loguru import logger

from signal_scanner.institutional_intel.config import safe_duckdb_connect
from signal_scanner.institutional_intel.ingest.sec_client import SecClient
from signal_scanner.institutional_intel.ingest.sec_index import (
    FilingIndexEntry,
    fetch_daily_index,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_8K_FORM_TYPES = {"8-K", "8-K/A"}

# Keyword → 8-K item code mapping (case-insensitive keyword search)
_ITEM_KEYWORD_MAP: List[Tuple[str, str]] = [
    ("material definitive agreement",       "1.01"),
    ("entry into",                          "1.01"),
    ("material cybersecurity",              "1.05"),
    ("completion of acquisition",           "2.01"),
    ("results of operations",               "2.02"),
    ("financial condition",                 "2.02"),
    ("triggering events",                   "2.04"),
    ("departure of directors",              "5.02"),
    ("election of directors",               "5.02"),
    ("appointment",                         "5.02"),
    ("officer",                             "5.02"),
    ("articles of incorporation",           "5.03"),
    ("regulation fd",                       "7.01"),
    ("other events",                        "8.01"),
    ("financial statements",                "9.01"),
]

# Regex patterns for SGML header fields
_CIK_RE = re.compile(r"CENTRAL INDEX KEY:\s*(\d+)")
_NAME_RE = re.compile(r"COMPANY CONFORMED NAME:\s*(.+)")
_FILED_RE = re.compile(r"FILED AS OF DATE:\s*(\d{8})")
_PERIOD_RE = re.compile(r"CONFORMED PERIOD OF REPORT:\s*(\d{8})")
_ITEM_RE = re.compile(r"ITEM INFORMATION:\s*(.+)", re.IGNORECASE)


_F8K_COLS = [
    "filing_accession_no", "filer_cik", "company_name", "ticker",
    "filed_date", "report_date", "event_items",
    "has_earnings", "has_acquisition", "has_officer_change", "has_cyber_incident",
    "source_url", "ingested_at",
]


# ---------------------------------------------------------------------------
# Item code extraction
# ---------------------------------------------------------------------------

def _extract_items_from_header(sgml_header: str) -> List[str]:
    """Extract 8-K item descriptions from SGML header and map to codes."""
    item_texts = _ITEM_RE.findall(sgml_header)
    codes: Set[str] = set()

    for item_text in item_texts:
        item_lower = item_text.lower()
        # Check if it's already a numeric code like "Item 2.02"
        m = re.search(r"\b(\d+\.\d+)\b", item_text)
        if m:
            codes.add(m.group(1))
            continue
        # Keyword match
        for keyword, code in _ITEM_KEYWORD_MAP:
            if keyword in item_lower:
                codes.add(code)
                break

    return sorted(codes)


def _parse_sgml_header(sgml: str) -> Dict:
    """Extract filing metadata from the first 20KB of an 8-K SGML file."""
    snippet = sgml[:20_000]

    m_cik = _CIK_RE.search(snippet)
    m_name = _NAME_RE.search(snippet)
    m_filed = _FILED_RE.search(snippet)
    m_period = _PERIOD_RE.search(snippet)

    def _to_iso(m: Optional[re.Match]) -> str:
        if not m:
            return ""
        d = m.group(1)
        return f"{d[:4]}-{d[4:6]}-{d[6:]}"

    items = _extract_items_from_header(snippet)

    return {
        "filer_cik": m_cik.group(1) if m_cik else "",
        "company_name": m_name.group(1).strip() if m_name else "",
        "filed_date": _to_iso(m_filed),
        "report_date": _to_iso(m_period),
        "event_items": ",".join(items),
        "has_earnings": "2.02" in items,
        "has_acquisition": ("1.01" in items or "2.01" in items),
        "has_officer_change": "5.02" in items,
        "has_cyber_incident": "1.05" in items,
    }


# ---------------------------------------------------------------------------
# CIK → ticker lookup (best-effort from dim_issuer)
# ---------------------------------------------------------------------------

def _build_cik_ticker_map(conn: duckdb.DuckDBPyConnection) -> Dict[str, str]:
    """Build a CIK → ticker dict from dim_issuer for fast lookup."""
    try:
        rows = conn.execute(
            "SELECT issuer_cik, ticker FROM dim_issuer WHERE issuer_cik IS NOT NULL AND ticker IS NOT NULL"
        ).fetchall()
        return {str(r[0]).lstrip("0"): r[1] for r in rows if r[0] and r[1]}
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Dedup
# ---------------------------------------------------------------------------

def _filter_new_accessions(
    conn: duckdb.DuckDBPyConnection,
    candidates: List[str],
) -> Set[str]:
    """Return subset of accession numbers NOT already in fact_form8k_events."""
    already: Set[str] = set()
    chunk_size = 500
    for i in range(0, len(candidates), chunk_size):
        batch = candidates[i : i + chunk_size]
        placeholders = ", ".join(["?"] * len(batch))
        rows = conn.execute(
            f"""
            SELECT filing_accession_no FROM fact_form8k_events
            WHERE filing_accession_no IN ({placeholders})
            """,
            batch,
        ).fetchall()
        already.update(r[0] for r in rows if r[0])
    return set(candidates) - already


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def refresh_daily_8k(lookback_days: int = 5) -> Dict[str, int]:
    """Fetch and ingest recent 8-K filings from SEC EDGAR.

    Only the SGML header is downloaded and parsed — the full filing body
    is NOT required for item classification. This keeps the refresh fast
    even for days with 500+ 8-K filings.

    Args:
        lookback_days: Number of calendar days to look back.

    Returns:
        Stats dict.
    """
    conn = safe_duckdb_connect(read_only=False)
    if conn is None:
        logger.warning("[8-K daily] DuckDB locked — skipping daily 8-K refresh")
        return {
            "days_checked": 0, "filings_found": 0, "filings_new": 0,
            "rows_inserted": 0, "errors": 0, "skipped": "db_locked",
        }

    try:
        cik_to_ticker = _build_cik_ticker_map(conn)
        logger.debug("[8-K daily] CIK→ticker map: {} entries", len(cik_to_ticker))

        client = SecClient()
        today = date.today()
        start = today - timedelta(days=lookback_days)

        all_entries: List[FilingIndexEntry] = []
        days_checked = 0

        for offset in range(lookback_days + 1):
            d = start + timedelta(days=offset)
            if d.weekday() >= 5:
                continue
            entries = fetch_daily_index(client, d)
            k8_entries = [e for e in entries if e.form_type in _8K_FORM_TYPES]
            all_entries.extend(k8_entries)
            days_checked += 1

        # Deduplicate by accession_no
        seen: Set[str] = set()
        unique_entries: List[FilingIndexEntry] = []
        for e in all_entries:
            if e.accession_no not in seen:
                seen.add(e.accession_no)
                unique_entries.append(e)

        logger.info(
            "[8-K daily] {} unique 8-K filings from {} entries across {} days",
            len(unique_entries), len(all_entries), days_checked,
        )

        if not unique_entries:
            return {
                "days_checked": days_checked, "filings_found": 0,
                "filings_new": 0, "rows_inserted": 0, "errors": 0,
            }

        # Filter already-ingested
        candidate_accessions = [e.accession_no for e in unique_entries]
        new_accessions = _filter_new_accessions(conn, candidate_accessions)
        new_entries = [e for e in unique_entries if e.accession_no in new_accessions]

        logger.info(
            "[8-K daily] {} new filings to process (skipping {} already ingested)",
            len(new_entries), len(unique_entries) - len(new_entries),
        )

        if not new_entries:
            return {
                "days_checked": days_checked, "filings_found": len(all_entries),
                "filings_new": 0, "rows_inserted": 0, "errors": 0,
            }

        now_iso = datetime.now(timezone.utc).isoformat()
        rows_to_insert: List[tuple] = []
        total_errors = 0

        for i, entry in enumerate(new_entries):
            try:
                sgml = client.get_text(entry.filing_url)
            except Exception as exc:
                total_errors += 1
                if total_errors <= 20:
                    logger.warning("[8-K daily] download {} — {}", entry.accession_no, exc)
                continue

            try:
                meta = _parse_sgml_header(sgml)
            except Exception as exc:
                total_errors += 1
                logger.debug("[8-K daily] parse {} — {}", entry.accession_no, exc)
                continue

            # Best-effort ticker lookup
            cik_normalized = meta["filer_cik"].lstrip("0")
            ticker = cik_to_ticker.get(cik_normalized, "")
            if not ticker:
                # Company index entry may have the ticker directly
                ticker = ""

            rows_to_insert.append((
                entry.accession_no,
                meta["filer_cik"],
                meta["company_name"],
                ticker,
                meta["filed_date"] or None,
                meta["report_date"] or None,
                meta["event_items"],
                meta["has_earnings"],
                meta["has_acquisition"],
                meta["has_officer_change"],
                meta["has_cyber_incident"],
                entry.filing_url,
                now_iso,
            ))

            if (i + 1) % 100 == 0:
                logger.info(
                    "[8-K daily] processed {}/{} filings | {} rows buffered",
                    i + 1, len(new_entries), len(rows_to_insert),
                )

        total_rows = 0
        if rows_to_insert:
            placeholders = ", ".join(["?"] * len(_F8K_COLS))
            sql = (
                f"INSERT OR IGNORE INTO fact_form8k_events "
                f"({', '.join(_F8K_COLS)}) VALUES ({placeholders})"
            )
            try:
                conn.executemany(sql, rows_to_insert)
                total_rows = len(rows_to_insert)
            except Exception as exc:
                # DuckDB doesn't support INSERT OR IGNORE — use temp table pattern
                logger.debug("[8-K daily] Falling back to temp-table upsert: {}", exc)
                conn.execute(
                    "CREATE TEMP TABLE IF NOT EXISTS _8k_load AS "
                    "SELECT * FROM fact_form8k_events LIMIT 0"
                )
                conn.executemany(
                    f"INSERT INTO _8k_load ({', '.join(_F8K_COLS)}) VALUES ({placeholders})",
                    rows_to_insert,
                )
                conn.execute("""
                    DELETE FROM fact_form8k_events
                    WHERE filing_accession_no IN (SELECT filing_accession_no FROM _8k_load)
                """)
                conn.execute("""
                    INSERT INTO fact_form8k_events
                    SELECT filing_accession_no, filer_cik, company_name, ticker,
                           filed_date::DATE, report_date::DATE, event_items,
                           has_earnings, has_acquisition, has_officer_change,
                           has_cyber_incident, source_url, ingested_at::TIMESTAMP
                    FROM _8k_load
                """)
                total_rows = len(rows_to_insert)

        stats = {
            "days_checked": days_checked,
            "filings_found": len(all_entries),
            "filings_new": len(new_entries),
            "rows_inserted": total_rows,
            "errors": total_errors,
            "earnings_events": sum(1 for r in rows_to_insert if r[7]),
            "acquisition_events": sum(1 for r in rows_to_insert if r[8]),
            "officer_changes": sum(1 for r in rows_to_insert if r[9]),
        }
        logger.info("[8-K daily] refresh complete: {}", stats)
        return stats

    finally:
        conn.close()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    p = argparse.ArgumentParser(
        description="Fetch recent 8-K material event filings from SEC EDGAR",
    )
    p.add_argument(
        "--days", type=int, default=5,
        help="Number of days to look back (default: 5)",
    )
    args = p.parse_args()

    stats = refresh_daily_8k(lookback_days=args.days)
    print(f"[8-K daily] {stats}")


if __name__ == "__main__":
    main()
