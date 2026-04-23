"""Bulk-load SEC pre-parsed datasets into DuckDB fact tables.

Downloads quarterly ZIP files from SEC's Form 13F Data Sets and Insider
Transactions Data Sets, extracts TSV files, and loads directly into
fact_13f_positions and fact_form4_transactions using DuckDB's native
TSV/CSV reader — orders of magnitude faster than raw EDGAR download+parse.

Data sources (free, pre-parsed by SEC):
  - https://www.sec.gov/data-research/sec-markets-data/form-13f-data-sets
  - https://www.sec.gov/data-research/sec-markets-data/insider-transactions-data-sets

Usage:
    python -m signal_scanner.institutional_intel.jobs.bulk_load
    python -m signal_scanner.institutional_intel.jobs.bulk_load --from-year 2024
    python -m signal_scanner.institutional_intel.jobs.bulk_load --dataset 13f
    python -m signal_scanner.institutional_intel.jobs.bulk_load --dataset insider
"""

import argparse
import io
import re
import zipfile
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import duckdb
import requests
from loguru import logger

from signal_scanner.institutional_intel.config import WAREHOUSE_PATH
from signal_scanner.institutional_intel.warehouse.db import init_warehouse
from signal_scanner.institutional_intel.warehouse.ops import (
    finish_ingestion_run,
    start_ingestion_run,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
BULK_DIR = Path(__file__).resolve().parents[3] / "data" / "bulk_sec"

SEC_HEADERS = {
    "User-Agent": "QuantBridge Research admin@quantbridge.local",
    "Accept": "*/*",
    "Accept-Encoding": "gzip, deflate",
}

# 13F quarterly datasets (3-month windows aligned to SEC publication schedule)
# SEC publishes for: Dec-Feb, Mar-May, Jun-Aug, Sep-Nov
_13F_QUARTERS = [
    ("01dec{y0}-28feb{y1}", "Q4Q1"),  # Dec prev year → Feb
    ("01mar{y}-31may{y}", "Q1Q2"),     # Mar → May
    ("01jun{y}-31aug{y}", "Q2Q3"),     # Jun → Aug
    ("01sep{y}-30nov{y}", "Q3Q4"),     # Sep → Nov
]

# Insider datasets use calendar quarters
_INSIDER_QUARTERS = ["q1", "q2", "q3", "q4"]


def _13f_zip_urls(from_year: int, to_year: int) -> List[Tuple[str, str]]:
    """Generate 13F bulk ZIP URLs for a year range."""
    urls = []
    for year in range(from_year, to_year + 1):
        # Dec(year-1)-Feb(year)
        slug_dec = f"01dec{year - 1}-28feb{year}"
        urls.append((
            f"https://www.sec.gov/files/structureddata/data/form-13f-data-sets/{slug_dec}_form13f.zip",
            f"13f_{slug_dec}.zip",
        ))
        # Mar-May
        slug_mar = f"01mar{year}-31may{year}"
        urls.append((
            f"https://www.sec.gov/files/structureddata/data/form-13f-data-sets/{slug_mar}_form13f.zip",
            f"13f_{slug_mar}.zip",
        ))
        # Jun-Aug
        slug_jun = f"01jun{year}-31aug{year}"
        urls.append((
            f"https://www.sec.gov/files/structureddata/data/form-13f-data-sets/{slug_jun}_form13f.zip",
            f"13f_{slug_jun}.zip",
        ))
        # Sep-Nov
        slug_sep = f"01sep{year}-30nov{year}"
        urls.append((
            f"https://www.sec.gov/files/structureddata/data/form-13f-data-sets/{slug_sep}_form13f.zip",
            f"13f_{slug_sep}.zip",
        ))
    return urls


def _insider_zip_urls(from_year: int, to_year: int) -> List[Tuple[str, str]]:
    """Generate insider transactions bulk ZIP URLs for a year range."""
    urls = []
    for year in range(from_year, to_year + 1):
        for q in _INSIDER_QUARTERS:
            urls.append((
                f"https://www.sec.gov/files/structureddata/data/insider-transactions-data-sets/{year}{q}_form345.zip",
                f"insider_{year}{q}.zip",
            ))
    return urls


def _download_zip(url: str, local_path: Path) -> bool:
    """Download a ZIP file if not already cached locally."""
    if local_path.exists() and local_path.stat().st_size > 1000:
        logger.debug("Already cached: {}", local_path.name)
        return True

    logger.info("Downloading {} ...", url.split("/")[-1])
    try:
        resp = requests.get(url, headers=SEC_HEADERS, timeout=120, verify=False)
        if resp.status_code == 404:
            logger.debug("Not available yet: {}", url.split("/")[-1])
            return False
        resp.raise_for_status()
        local_path.parent.mkdir(parents=True, exist_ok=True)
        local_path.write_bytes(resp.content)
        logger.info("Downloaded {} ({:.1f} MB)", local_path.name, len(resp.content) / 1e6)
        return True
    except Exception as exc:
        logger.warning("Download failed for {}: {}", url.split("/")[-1], exc)
        return False


def _extract_tsv(zip_path: Path, tsv_name: str, out_dir: Path) -> Optional[Path]:
    """Extract a single TSV from a ZIP, return path or None."""
    try:
        with zipfile.ZipFile(zip_path) as z:
            if tsv_name not in z.namelist():
                return None
            out_path = out_dir / f"{zip_path.stem}_{tsv_name}"
            with z.open(tsv_name) as src, open(out_path, "wb") as dst:
                dst.write(src.read())
            return out_path
    except Exception as exc:
        logger.warning("Extract failed for {} from {}: {}", tsv_name, zip_path.name, exc)
        return None


def _normalize_sec_date(raw: str) -> Optional[str]:
    """Convert SEC date formats to ISO YYYY-MM-DD.

    Handles: '31-DEC-2024', '2024-12-31', '12/31/2024', '20241231'
    """
    if not raw or not raw.strip():
        return None
    raw = raw.strip()

    # Already ISO
    if re.match(r"^\d{4}-\d{2}-\d{2}$", raw):
        return raw

    # DD-MON-YYYY (SEC bulk format)
    try:
        dt = datetime.strptime(raw, "%d-%b-%Y")
        return dt.strftime("%Y-%m-%d")
    except ValueError:
        pass

    # YYYYMMDD
    if len(raw) == 8 and raw.isdigit():
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"

    return raw


# ---------------------------------------------------------------------------
# 13F Bulk Load
# ---------------------------------------------------------------------------

def load_13f_from_zip(zip_path: Path) -> Dict[str, int]:
    """Load a single 13F quarterly ZIP into fact_13f_positions + dim_manager_13f."""
    tmp_dir = zip_path.parent / "_tmp"
    tmp_dir.mkdir(exist_ok=True)

    infotable_path = _extract_tsv(zip_path, "INFOTABLE.tsv", tmp_dir)
    submission_path = _extract_tsv(zip_path, "SUBMISSION.tsv", tmp_dir)
    coverpage_path = _extract_tsv(zip_path, "COVERPAGE.tsv", tmp_dir)

    if not infotable_path or not submission_path:
        logger.warning("Missing INFOTABLE or SUBMISSION in {}", zip_path.name)
        return {"rows_inserted": 0, "errors": 1}

    conn = duckdb.connect(str(WAREHOUSE_PATH))
    now_iso = datetime.now(timezone.utc).isoformat()

    try:
        # Load TSVs into temp tables — force all columns to VARCHAR
        # to avoid date format issues (SEC uses DD-MON-YYYY)
        conn.execute(f"""
            CREATE OR REPLACE TEMP TABLE _infotable AS
            SELECT * FROM read_csv('{infotable_path.as_posix()}',
                delim='\t', header=true, all_varchar=true,
                ignore_errors=true, null_padding=true)
        """)

        conn.execute(f"""
            CREATE OR REPLACE TEMP TABLE _submission AS
            SELECT * FROM read_csv('{submission_path.as_posix()}',
                delim='\t', header=true, all_varchar=true,
                ignore_errors=true, null_padding=true)
        """)

        if coverpage_path:
            conn.execute(f"""
                CREATE OR REPLACE TEMP TABLE _coverpage AS
                SELECT * FROM read_csv('{coverpage_path.as_posix()}',
                    delim='\t', header=true, all_varchar=true,
                    ignore_errors=true, null_padding=true)
            """)

        # Count existing rows to skip already-loaded data
        existing = conn.execute("""
            SELECT COUNT(DISTINCT filing_accession_no) FROM fact_13f_positions
            WHERE filing_accession_no IN (SELECT ACCESSION_NUMBER FROM _submission)
        """).fetchone()[0]

        if existing > 0:
            logger.info("{} accessions from {} already loaded, filtering them out",
                       existing, zip_path.name)

        # JOIN and INSERT — mapping SEC columns to our schema
        coverpage_join = ""
        manager_name_col = "''"
        if coverpage_path:
            coverpage_join = "LEFT JOIN _coverpage cp ON i.ACCESSION_NUMBER = cp.ACCESSION_NUMBER"
            manager_name_col = "COALESCE(cp.FILINGMANAGER_NAME, '')"

        # SEC dates are DD-MON-YYYY (e.g. '31-DEC-2024') — convert to YYYY-MM-DD
        sql = f"""
            INSERT INTO fact_13f_positions
                (filing_accession_no, manager_cik, manager_name, report_period,
                 filed_at, issuer_name, cusip, ticker, class_title,
                 value_usd_thousands, shares, put_call, discretion,
                 source_path, ingested_at)
            SELECT
                i.ACCESSION_NUMBER,
                COALESCE(s.CIK, ''),
                {manager_name_col},
                CASE WHEN s.PERIODOFREPORT IS NOT NULL AND s.PERIODOFREPORT != ''
                     THEN strftime(strptime(s.PERIODOFREPORT, '%d-%b-%Y'), '%Y-%m-%d')
                     ELSE '' END,
                CASE WHEN s.FILING_DATE IS NOT NULL AND s.FILING_DATE != ''
                     THEN strftime(strptime(s.FILING_DATE, '%d-%b-%Y'), '%Y-%m-%d')
                     ELSE '' END,
                COALESCE(i.NAMEOFISSUER, ''),
                COALESCE(i.CUSIP, ''),
                '',
                COALESCE(i.TITLEOFCLASS, ''),
                COALESCE(TRY_CAST(i.VALUE AS DOUBLE), 0.0),
                COALESCE(TRY_CAST(i.SSHPRNAMT AS DOUBLE), 0.0),
                COALESCE(i.PUTCALL, ''),
                COALESCE(i.INVESTMENTDISCRETION, ''),
                'bulk:{zip_path.name}',
                '{now_iso}'
            FROM _infotable i
            JOIN _submission s ON i.ACCESSION_NUMBER = s.ACCESSION_NUMBER
            {coverpage_join}
            WHERE i.ACCESSION_NUMBER NOT IN (
                SELECT DISTINCT filing_accession_no FROM fact_13f_positions
            )
        """
        conn.execute(sql)

        # Count what we inserted
        inserted = conn.execute(f"""
            SELECT COUNT(*) FROM fact_13f_positions
            WHERE source_path = 'bulk:{zip_path.name}'
              AND ingested_at = '{now_iso}'
        """).fetchone()[0]

        # Upsert dim_manager_13f
        if coverpage_path:
            conn.execute("""
                INSERT INTO dim_manager_13f (manager_cik, manager_name)
                SELECT DISTINCT CAST(s.CIK AS TEXT), cp.FILINGMANAGER_NAME
                FROM _submission s
                JOIN _coverpage cp ON s.ACCESSION_NUMBER = cp.ACCESSION_NUMBER
                WHERE s.CIK IS NOT NULL AND cp.FILINGMANAGER_NAME IS NOT NULL
                ON CONFLICT(manager_cik) DO UPDATE
                SET manager_name = COALESCE(NULLIF(excluded.manager_name, ''), dim_manager_13f.manager_name)
            """)

        logger.info("Loaded {} rows from {}", inserted, zip_path.name)

        # Clean up temp files
        for p in [infotable_path, submission_path, coverpage_path]:
            if p and p.exists():
                p.unlink(missing_ok=True)

        return {"rows_inserted": inserted, "errors": 0}

    except Exception as exc:
        logger.error("Error loading {}: {}", zip_path.name, exc)
        return {"rows_inserted": 0, "errors": 1}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Insider Transactions Bulk Load
# ---------------------------------------------------------------------------

def load_insider_from_zip(zip_path: Path) -> Dict[str, int]:
    """Load a single insider transactions quarterly ZIP into fact_form4_transactions."""
    tmp_dir = zip_path.parent / "_tmp"
    tmp_dir.mkdir(exist_ok=True)

    nonderiv_path = _extract_tsv(zip_path, "NONDERIV_TRANS.tsv", tmp_dir)
    submission_path = _extract_tsv(zip_path, "SUBMISSION.tsv", tmp_dir)
    owner_path = _extract_tsv(zip_path, "REPORTINGOWNER.tsv", tmp_dir)

    if not nonderiv_path or not submission_path:
        logger.warning("Missing NONDERIV_TRANS or SUBMISSION in {}", zip_path.name)
        return {"rows_inserted": 0, "errors": 1}

    conn = duckdb.connect(str(WAREHOUSE_PATH))
    now_iso = datetime.now(timezone.utc).isoformat()

    try:
        conn.execute(f"""
            CREATE OR REPLACE TEMP TABLE _nonderiv AS
            SELECT * FROM read_csv('{nonderiv_path.as_posix()}',
                delim='\t', header=true, all_varchar=true,
                ignore_errors=true, null_padding=true)
        """)

        conn.execute(f"""
            CREATE OR REPLACE TEMP TABLE _submission AS
            SELECT * FROM read_csv('{submission_path.as_posix()}',
                delim='\t', header=true, all_varchar=true,
                ignore_errors=true, null_padding=true)
        """)

        owner_join = ""
        insider_name_col = "''"
        insider_role_col = "''"
        if owner_path:
            conn.execute(f"""
                CREATE OR REPLACE TEMP TABLE _owner AS
                SELECT * FROM read_csv('{owner_path.as_posix()}',
                    delim='\t', header=true, all_varchar=true,
                    ignore_errors=true, null_padding=true)
            """)
            # Deduplicate owners — take first per accession
            conn.execute("""
                CREATE OR REPLACE TEMP TABLE _owner_dedup AS
                SELECT ACCESSION_NUMBER,
                       FIRST(RPTOWNERNAME) AS RPTOWNERNAME,
                       FIRST(RPTOWNER_RELATIONSHIP) AS RPTOWNER_RELATIONSHIP
                FROM _owner
                GROUP BY ACCESSION_NUMBER
            """)
            owner_join = "LEFT JOIN _owner_dedup ow ON nd.ACCESSION_NUMBER = ow.ACCESSION_NUMBER"
            insider_name_col = "COALESCE(ow.RPTOWNERNAME, '')"
            insider_role_col = "COALESCE(ow.RPTOWNER_RELATIONSHIP, '')"

        # Map TRANS_ACQUIRED_DISP_CD: A=BUY, D=SELL
        # SEC dates are DD-MON-YYYY — convert to YYYY-MM-DD
        sql = f"""
            INSERT INTO fact_form4_transactions
                (filing_accession_no, issuer_cik, issuer_name, ticker,
                 insider_name, insider_role, transaction_date, transaction_code,
                 direction, shares, price, ownership_after,
                 source_path, ingested_at)
            SELECT
                nd.ACCESSION_NUMBER,
                COALESCE(s.ISSUERCIK, ''),
                COALESCE(s.ISSUERNAME, ''),
                COALESCE(s.ISSUERTRADINGSYMBOL, ''),
                {insider_name_col},
                {insider_role_col},
                CASE WHEN nd.TRANS_DATE IS NOT NULL AND nd.TRANS_DATE != ''
                     THEN strftime(strptime(nd.TRANS_DATE, '%d-%b-%Y'), '%Y-%m-%d')
                     ELSE NULL END,
                COALESCE(nd.TRANS_CODE, ''),
                CASE
                    WHEN nd.TRANS_ACQUIRED_DISP_CD = 'A' THEN 'BUY'
                    WHEN nd.TRANS_ACQUIRED_DISP_CD = 'D' THEN 'SELL'
                    ELSE 'OTHER'
                END,
                COALESCE(TRY_CAST(nd.TRANS_SHARES AS DOUBLE), 0.0),
                COALESCE(TRY_CAST(nd.TRANS_PRICEPERSHARE AS DOUBLE), 0.0),
                COALESCE(TRY_CAST(nd.SHRS_OWND_FOLWNG_TRANS AS DOUBLE), 0.0),
                'bulk:{zip_path.name}',
                '{now_iso}'
            FROM _nonderiv nd
            JOIN _submission s ON nd.ACCESSION_NUMBER = s.ACCESSION_NUMBER
            {owner_join}
            WHERE nd.ACCESSION_NUMBER NOT IN (
                SELECT DISTINCT filing_accession_no FROM fact_form4_transactions
            )
        """
        conn.execute(sql)

        inserted = conn.execute(f"""
            SELECT COUNT(*) FROM fact_form4_transactions
            WHERE source_path = 'bulk:{zip_path.name}'
              AND ingested_at = '{now_iso}'
        """).fetchone()[0]

        logger.info("Loaded {} insider rows from {}", inserted, zip_path.name)

        # Clean up
        for p in [nonderiv_path, submission_path, owner_path]:
            if p and p.exists():
                p.unlink(missing_ok=True)

        return {"rows_inserted": inserted, "errors": 0}

    except Exception as exc:
        logger.error("Error loading {}: {}", zip_path.name, exc)
        return {"rows_inserted": 0, "errors": 1}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Main orchestrator
# ---------------------------------------------------------------------------

def bulk_load_13f(from_year: int, to_year: int) -> Dict[str, int]:
    """Download and load all 13F quarterly datasets for a year range."""
    urls = _13f_zip_urls(from_year, to_year)
    total_rows = 0
    total_errors = 0
    loaded = 0

    for url, filename in urls:
        local = BULK_DIR / filename
        if not _download_zip(url, local):
            continue

        stats = load_13f_from_zip(local)
        total_rows += stats["rows_inserted"]
        total_errors += stats["errors"]
        if stats["rows_inserted"] > 0:
            loaded += 1

    logger.info(
        "13F bulk load complete | files={} | rows={} | errors={}",
        loaded, total_rows, total_errors,
    )
    return {"files_loaded": loaded, "rows_inserted": total_rows, "errors": total_errors}


def bulk_load_insider(from_year: int, to_year: int) -> Dict[str, int]:
    """Download and load all insider transaction quarterly datasets."""
    urls = _insider_zip_urls(from_year, to_year)
    total_rows = 0
    total_errors = 0
    loaded = 0

    for url, filename in urls:
        local = BULK_DIR / filename
        if not _download_zip(url, local):
            continue

        stats = load_insider_from_zip(local)
        total_rows += stats["rows_inserted"]
        total_errors += stats["errors"]
        if stats["rows_inserted"] > 0:
            loaded += 1

    logger.info(
        "Insider bulk load complete | files={} | rows={} | errors={}",
        loaded, total_rows, total_errors,
    )
    return {"files_loaded": loaded, "rows_inserted": total_rows, "errors": total_errors}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Bulk-load SEC pre-parsed datasets into Kubera warehouse"
    )
    p.add_argument(
        "--dataset", default="all", choices=["all", "13f", "insider"],
        help="Which dataset to load (default: all)",
    )
    p.add_argument(
        "--from-year", type=int, default=2024,
        help="Start year (default: 2024)",
    )
    p.add_argument(
        "--to-year", type=int, default=0,
        help="End year (default: current year)",
    )
    return p.parse_args()


def main() -> None:
    args = parse_args()
    to_year = args.to_year or date.today().year

    init_warehouse()
    BULK_DIR.mkdir(parents=True, exist_ok=True)

    run_id = start_ingestion_run(
        source="SEC_BULK", job_name="bulk_load", parser_version="phase-b"
    )

    total_rows = 0
    total_errors = 0

    try:
        if args.dataset in ("13f", "all"):
            stats = bulk_load_13f(args.from_year, to_year)
            total_rows += stats["rows_inserted"]
            total_errors += stats["errors"]

        if args.dataset in ("insider", "all"):
            stats = bulk_load_insider(args.from_year, to_year)
            total_rows += stats["rows_inserted"]
            total_errors += stats["errors"]

        status = "COMPLETED" if total_errors == 0 else "PARTIAL"
        finish_ingestion_run(
            run_id=run_id,
            status=status,
            rows_ingested=total_rows,
            rows_failed=total_errors,
            notes=f"bulk_load from_year={args.from_year} to_year={to_year}",
        )
        logger.info("Bulk load {} | rows={} errors={}", status, total_rows, total_errors)
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
