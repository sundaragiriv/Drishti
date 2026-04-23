"""Parse downloaded SEC filings into fact tables.

Reads raw_file_manifest for unparsed filings, processes them in batches,
and inserts rows into fact_13f_positions and fact_form4_transactions.

Usage:
    python -m signal_scanner.institutional_intel.jobs.parse_filings --forms all
    python -m signal_scanner.institutional_intel.jobs.parse_filings --forms 13f --limit 100
    python -m signal_scanner.institutional_intel.jobs.parse_filings --forms form4
"""

import argparse
import re
import tempfile
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

import duckdb
from loguru import logger

from signal_scanner.institutional_intel.config import (
    WAREHOUSE_PATH,
    InstitutionalIntelConfig,
)
from signal_scanner.institutional_intel.ingest.sec_client import SecClient
from signal_scanner.institutional_intel.parsers.form13f_parser import (
    parse_13f_information_table,
)
from signal_scanner.institutional_intel.parsers.form4_parser import parse_form4
from signal_scanner.institutional_intel.warehouse.db import init_warehouse
from signal_scanner.institutional_intel.warehouse.ops import (
    finish_ingestion_run,
    start_ingestion_run,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
BATCH_SIZE = 500
FORM4_FORMS = {"3", "4", "5"}
FORM13F_FORMS = {"13F-HR", "13F-HR/A"}

# Regex to locate the information-table document inside a 13F SGML wrapper
_INFOTABLE_DOC_RE = re.compile(
    r"<DOCUMENT>\s*<TYPE>\s*INFORMATION\s+TABLE"
    r".*?<TEXT>(.*?)</TEXT>",
    re.DOTALL | re.IGNORECASE,
)

# SGML header fields
_PERIOD_RE = re.compile(
    r"CONFORMED\s+PERIOD\s+OF\s+REPORT:\s*(\d{8})", re.IGNORECASE
)
_COMPANY_NAME_RE = re.compile(
    r"COMPANY\s+CONFORMED\s+NAME:\s*(.+)", re.IGNORECASE
)
_CIK_RE = re.compile(
    r"CENTRAL\s+INDEX\s+KEY:\s*(\d+)", re.IGNORECASE
)


# ---------------------------------------------------------------------------
# Helpers — query manifest
# ---------------------------------------------------------------------------

def _get_unparsed_filings(
    conn: duckdb.DuckDBPyConnection,
    form_types: Set[str],
    fact_table: str,
    limit: int = 0,
) -> List[Dict]:
    """Return manifest rows for filings not yet present in *fact_table*."""
    quoted_forms = ", ".join(f"'{f}'" for f in form_types)
    sql = f"""
        SELECT m.accession_no, m.form_type, m.cik, m.filing_date,
               m.local_path, m.source_url
        FROM raw_file_manifest m
        WHERE m.form_type IN ({quoted_forms})
          AND m.local_path NOT LIKE 'manifest://%%'
          AND m.accession_no NOT IN (
              SELECT DISTINCT filing_accession_no FROM {fact_table}
          )
        ORDER BY m.filing_date
    """
    if limit > 0:
        sql += f" LIMIT {limit}"

    rows = conn.execute(sql).fetchall()
    cols = ["accession_no", "form_type", "cik", "filing_date",
            "local_path", "source_url"]
    return [dict(zip(cols, r)) for r in rows]


# ---------------------------------------------------------------------------
# 13F SGML helpers
# ---------------------------------------------------------------------------

def _extract_infotable_xml_from_sgml(sgml_text: str) -> Optional[str]:
    """Extract the information-table XML from a 13F SGML wrapper."""
    match = _INFOTABLE_DOC_RE.search(sgml_text)
    if not match:
        return None

    raw = match.group(1).strip()
    # Strip optional <XML>…</XML> wrapper
    raw = re.sub(r"^\s*<XML>\s*", "", raw, flags=re.IGNORECASE)
    raw = re.sub(r"\s*</XML>\s*$", "", raw, flags=re.IGNORECASE)

    # Quick sanity check
    if "informationTable" not in raw and "infoTable" not in raw and "<?xml" not in raw[:300]:
        return None
    return raw.strip()


def _extract_sgml_field(sgml_text: str, regex: re.Pattern) -> Optional[str]:
    m = regex.search(sgml_text)
    return m.group(1).strip() if m else None


def _normalize_period(raw_period: str) -> str:
    """Convert YYYYMMDD → YYYY-MM-DD."""
    if len(raw_period) == 8 and raw_period.isdigit():
        return f"{raw_period[:4]}-{raw_period[4:6]}-{raw_period[6:8]}"
    return raw_period


def _build_13f_context(filing: Dict, sgml_text: str) -> Dict[str, str]:
    """Build the context dict expected by parse_13f_information_table()."""
    period_raw = _extract_sgml_field(sgml_text, _PERIOD_RE) or ""
    report_period = _normalize_period(period_raw) if period_raw else (filing.get("filing_date") or "")

    manager_name = _extract_sgml_field(sgml_text, _COMPANY_NAME_RE) or ""
    manager_cik = _extract_sgml_field(sgml_text, _CIK_RE) or filing.get("cik", "")

    return {
        "accession_no": filing["accession_no"],
        "manager_cik": manager_cik,
        "manager_name": manager_name,
        "report_period": report_period,
        "filed_at": filing.get("filing_date") or "",
    }


def _fetch_infotable_url_from_index(
    client: SecClient,
    cik: str,
    accession_no: str,
) -> Optional[str]:
    """Fallback: query SEC filing-index JSON to find the info-table URL."""
    acc_nodashes = accession_no.replace("-", "")
    index_url = (
        f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc_nodashes}/"
        f"{accession_no}-index.json"
    )
    try:
        data = client.get_json(index_url)
    except Exception:
        return None

    items = data.get("directory", {}).get("item", [])
    for item in items:
        name = (item.get("name") or "").lower()
        # information table documents are typically named *infotable*.xml
        if name.endswith(".xml") and ("infotable" in name or "information" in name):
            return (
                f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc_nodashes}/{item['name']}"
            )
    # Second pass — any .xml that isn't the primary doc
    for item in items:
        name = (item.get("name") or "").lower()
        if name.endswith(".xml") and "primary" not in name:
            return (
                f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc_nodashes}/{item['name']}"
            )
    return None


# ---------------------------------------------------------------------------
# Single-filing parsers
# ---------------------------------------------------------------------------

def _parse_single_13f(
    filing: Dict,
    client: Optional[SecClient],
) -> Tuple[List[Dict], Optional[str]]:
    """Parse one 13F filing into position rows."""
    local_path = Path(filing["local_path"])
    if not local_path.exists():
        return [], f"File not found: {local_path}"

    try:
        sgml_text = local_path.read_text(encoding="utf-8", errors="ignore")
    except Exception as exc:
        return [], f"Read error: {exc}"

    context = _build_13f_context(filing, sgml_text)

    # Strategy 1: extract XML inline from the SGML wrapper
    xml_text = _extract_infotable_xml_from_sgml(sgml_text)

    # Strategy 2: fallback — fetch from SEC filing index
    if xml_text is None and client is not None:
        url = _fetch_infotable_url_from_index(
            client, filing.get("cik", ""), filing["accession_no"]
        )
        if url:
            try:
                xml_text = client.get_text(url)
            except Exception as exc:
                return [], f"Fallback fetch failed: {exc}"

    if xml_text is None:
        return [], "No information table found"

    # Write XML to a temp file so the parser can read it via Path
    try:
        tmp = tempfile.NamedTemporaryFile(
            suffix=".xml", delete=False, mode="w", encoding="utf-8"
        )
        tmp.write(xml_text)
        tmp.close()
        rows = parse_13f_information_table(Path(tmp.name), context)
        Path(tmp.name).unlink(missing_ok=True)
        return rows, None
    except Exception as exc:
        return [], f"Parse error: {exc}"


def _parse_single_form4(filing: Dict) -> Tuple[List[Dict], Optional[str]]:
    """Parse one Form 3/4/5 filing into transaction rows."""
    local_path = Path(filing["local_path"])
    if not local_path.exists():
        return [], f"File not found: {local_path}"

    # Form 4 raw text is SGML too — extract the XML document
    try:
        raw = local_path.read_text(encoding="utf-8", errors="ignore")
    except Exception as exc:
        return [], f"Read error: {exc}"

    # Try to find the XML inside SGML wrapper
    xml_text = raw
    xml_match = re.search(
        r"<\?xml.*?\?>.*?</ownershipDocument>",
        raw,
        re.DOTALL | re.IGNORECASE,
    )
    if xml_match:
        xml_text = xml_match.group(0)
    elif "<ownershipDocument" in raw:
        # No <?xml?> prolog but still has the root element
        od_match = re.search(
            r"<ownershipDocument.*?</ownershipDocument>",
            raw,
            re.DOTALL | re.IGNORECASE,
        )
        if od_match:
            xml_text = od_match.group(0)

    context = {"accession_no": filing["accession_no"]}

    try:
        tmp = tempfile.NamedTemporaryFile(
            suffix=".xml", delete=False, mode="w", encoding="utf-8"
        )
        tmp.write(xml_text)
        tmp.close()
        rows = parse_form4(Path(tmp.name), context)
        Path(tmp.name).unlink(missing_ok=True)
        return rows, None
    except Exception as exc:
        return [], f"Parse error: {exc}"


# ---------------------------------------------------------------------------
# Batch insert helpers
# ---------------------------------------------------------------------------

_13F_COLS = [
    "filing_accession_no", "manager_cik", "manager_name", "report_period",
    "filed_at", "issuer_name", "cusip", "ticker", "class_title",
    "value_usd_thousands", "shares", "put_call", "discretion",
    "source_path", "ingested_at",
]

_F4_COLS = [
    "filing_accession_no", "issuer_cik", "issuer_name", "ticker",
    "insider_name", "insider_role", "transaction_date", "transaction_code",
    "direction", "shares", "price", "ownership_after",
    "source_path", "ingested_at",
]


def _insert_13f_rows(conn: duckdb.DuckDBPyConnection, rows: List[Dict]) -> int:
    if not rows:
        return 0
    placeholders = ", ".join(["?"] * len(_13F_COLS))
    sql = f"INSERT INTO fact_13f_positions ({', '.join(_13F_COLS)}) VALUES ({placeholders})"
    values = [tuple(r.get(c) for c in _13F_COLS) for r in rows]
    conn.executemany(sql, values)
    return len(values)


def _insert_form4_rows(conn: duckdb.DuckDBPyConnection, rows: List[Dict]) -> int:
    if not rows:
        return 0
    placeholders = ", ".join(["?"] * len(_F4_COLS))
    sql = f"INSERT INTO fact_form4_transactions ({', '.join(_F4_COLS)}) VALUES ({placeholders})"
    values = [tuple(r.get(c) for c in _F4_COLS) for r in rows]
    conn.executemany(sql, values)
    return len(values)


def _upsert_dim_manager(conn: duckdb.DuckDBPyConnection, rows: List[Dict]) -> None:
    """Upsert unique (manager_cik, manager_name) pairs into dim_manager_13f."""
    seen = {}
    for r in rows:
        cik = r.get("manager_cik")
        name = r.get("manager_name")
        if cik and cik not in seen:
            seen[cik] = name or ""

    if not seen:
        return
    for cik, name in seen.items():
        conn.execute(
            """INSERT INTO dim_manager_13f (manager_cik, manager_name)
               VALUES (?, ?)
               ON CONFLICT(manager_cik) DO UPDATE
               SET manager_name = COALESCE(NULLIF(excluded.manager_name, ''), dim_manager_13f.manager_name)
            """,
            [cik, name],
        )


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------

def parse_13f_filings(
    limit: int = 0,
    batch_size: int = BATCH_SIZE,
    fetch_missing_xml: bool = True,
) -> Dict[str, int]:
    """Parse all unparsed 13F filings into fact_13f_positions."""
    conn = duckdb.connect(str(WAREHOUSE_PATH))
    client = SecClient() if fetch_missing_xml else None
    total_parsed = 0
    total_rows = 0
    total_errors = 0
    total_skipped = 0

    try:
        filings = _get_unparsed_filings(
            conn, FORM13F_FORMS, "fact_13f_positions", limit=limit
        )
        logger.info("Found {} unparsed 13F filings", len(filings))

        row_buffer: List[Dict] = []

        for i, filing in enumerate(filings):
            rows, err = _parse_single_13f(filing, client)
            if err:
                total_errors += 1
                if total_errors <= 20:
                    logger.warning(
                        "13F parse error [{}]: {}", filing["accession_no"], err
                    )
                continue

            if not rows:
                total_skipped += 1
                continue

            row_buffer.extend(rows)
            total_parsed += 1

            # Batch commit
            if len(row_buffer) >= batch_size * 10:
                inserted = _insert_13f_rows(conn, row_buffer)
                _upsert_dim_manager(conn, row_buffer)
                total_rows += inserted
                row_buffer.clear()
                logger.info(
                    "13F progress: {}/{} filings | {} rows",
                    i + 1, len(filings), total_rows,
                )

        # Final flush
        if row_buffer:
            inserted = _insert_13f_rows(conn, row_buffer)
            _upsert_dim_manager(conn, row_buffer)
            total_rows += inserted

        logger.info(
            "13F parsing complete | parsed={} rows={} errors={} skipped={}",
            total_parsed, total_rows, total_errors, total_skipped,
        )
        return {
            "parsed": total_parsed,
            "rows_inserted": total_rows,
            "errors": total_errors,
            "skipped": total_skipped,
        }
    finally:
        conn.close()


def parse_form4_filings(
    limit: int = 0,
    batch_size: int = BATCH_SIZE,
) -> Dict[str, int]:
    """Parse all unparsed Form 3/4/5 filings into fact_form4_transactions."""
    conn = duckdb.connect(str(WAREHOUSE_PATH))
    total_parsed = 0
    total_rows = 0
    total_errors = 0
    total_skipped = 0

    try:
        filings = _get_unparsed_filings(
            conn, FORM4_FORMS, "fact_form4_transactions", limit=limit
        )
        logger.info("Found {} unparsed Form 3/4/5 filings", len(filings))

        row_buffer: List[Dict] = []

        for i, filing in enumerate(filings):
            rows, err = _parse_single_form4(filing)
            if err:
                total_errors += 1
                if total_errors <= 20:
                    logger.warning(
                        "Form4 parse error [{}]: {}", filing["accession_no"], err
                    )
                continue

            if not rows:
                total_skipped += 1
                continue

            row_buffer.extend(rows)
            total_parsed += 1

            # Batch commit
            if len(row_buffer) >= batch_size * 10:
                inserted = _insert_form4_rows(conn, row_buffer)
                total_rows += inserted
                row_buffer.clear()
                logger.info(
                    "Form4 progress: {}/{} filings | {} rows",
                    i + 1, len(filings), total_rows,
                )

        # Final flush
        if row_buffer:
            inserted = _insert_form4_rows(conn, row_buffer)
            total_rows += inserted

        logger.info(
            "Form4 parsing complete | parsed={} rows={} errors={} skipped={}",
            total_parsed, total_rows, total_errors, total_skipped,
        )
        return {
            "parsed": total_parsed,
            "rows_inserted": total_rows,
            "errors": total_errors,
            "skipped": total_skipped,
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Parse downloaded SEC filings into DuckDB fact tables"
    )
    p.add_argument(
        "--forms", default="all", choices=["all", "13f", "form4"],
        help="Which filing types to parse (default: all)",
    )
    p.add_argument("--limit", type=int, default=0, help="Max filings to parse (0=unlimited)")
    p.add_argument("--batch-size", type=int, default=500, help="DB commit batch size")
    p.add_argument(
        "--no-fetch-xml", action="store_true",
        help="Don't fetch missing 13F info-table XMLs from SEC (skip those filings)",
    )
    return p.parse_args()


def main() -> None:
    args = parse_args()
    init_warehouse()

    run_id = start_ingestion_run(
        source="SEC", job_name="parse_filings", parser_version="phase-b"
    )

    total_parsed = 0
    total_rows = 0
    total_errors = 0

    try:
        if args.forms in ("13f", "all"):
            stats = parse_13f_filings(
                limit=args.limit,
                batch_size=args.batch_size,
                fetch_missing_xml=not args.no_fetch_xml,
            )
            total_parsed += stats["parsed"]
            total_rows += stats["rows_inserted"]
            total_errors += stats["errors"]

        if args.forms in ("form4", "all"):
            stats = parse_form4_filings(
                limit=args.limit,
                batch_size=args.batch_size,
            )
            total_parsed += stats["parsed"]
            total_rows += stats["rows_inserted"]
            total_errors += stats["errors"]

        status = "COMPLETED" if total_errors == 0 else "PARTIAL"
        finish_ingestion_run(
            run_id=run_id,
            status=status,
            rows_ingested=total_rows,
            rows_failed=total_errors,
            notes=f"filings_parsed={total_parsed}",
        )
        logger.info(
            "Parse job {} | parsed={} rows={} errors={}",
            status, total_parsed, total_rows, total_errors,
        )
    except Exception as exc:
        finish_ingestion_run(
            run_id=run_id,
            status="FAILED",
            rows_ingested=total_rows,
            rows_failed=total_errors,
            notes=str(exc),
        )
        raise


if __name__ == "__main__":
    main()
