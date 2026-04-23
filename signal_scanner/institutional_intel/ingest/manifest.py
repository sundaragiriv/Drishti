"""Manifest utilities for raw SEC files."""

import hashlib
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Union

import duckdb

from signal_scanner.institutional_intel.config import WAREHOUSE_PATH

_MANIFEST_WRITE_LOCK = threading.Lock()
_CONN_LOCK = threading.Lock()
_CONN: Optional[duckdb.DuckDBPyConnection] = None


def _get_conn() -> duckdb.DuckDBPyConnection:
    """Return a cached module-level DuckDB connection (thread-safe)."""
    global _CONN
    with _CONN_LOCK:
        if _CONN is None:
            _CONN = duckdb.connect(str(WAREHOUSE_PATH))
        return _CONN


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def upsert_manifest_record(
    accession_no: str,
    form_type: str,
    local_path: Union[Path, str],
    cik: Optional[str] = None,
    filing_date: Optional[str] = None,
    source_url: Optional[str] = None,
) -> None:
    now_iso = datetime.now(timezone.utc).isoformat()
    local_path_str = str(local_path)
    sha: Optional[str] = None
    # Only hash files that actually exist on disk (skip manifest:// URIs).
    if not local_path_str.startswith("manifest://"):
        p = Path(local_path_str)
        if p.exists() and p.is_file():
            sha = file_sha256(p)
    filing_date_norm = _normalize_filing_date(filing_date)
    sql = """
        INSERT INTO raw_file_manifest
            (accession_no, form_type, cik, filing_date, source_url, local_path, sha256, received_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(accession_no) DO UPDATE SET
            form_type = excluded.form_type,
            cik = excluded.cik,
            filing_date = excluded.filing_date,
            source_url = excluded.source_url,
            local_path = excluded.local_path,
            sha256 = excluded.sha256,
            received_at = excluded.received_at
    """
    with _MANIFEST_WRITE_LOCK:
        conn = _get_conn()
        conn.execute(
            sql,
            [
                accession_no,
                form_type,
                cik,
                filing_date_norm,
                source_url,
                local_path_str,
                sha,
                now_iso,
            ],
        )


def upsert_manifest_records(records: list[dict]) -> int:
    """Bulk upsert raw_file_manifest records in a single DB transaction."""
    if not records:
        return 0
    now_iso = datetime.now(timezone.utc).isoformat()
    sql = """
        INSERT INTO raw_file_manifest
            (accession_no, form_type, cik, filing_date, source_url, local_path, sha256, received_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(accession_no) DO UPDATE SET
            form_type = excluded.form_type,
            cik = excluded.cik,
            filing_date = excluded.filing_date,
            source_url = excluded.source_url,
            local_path = excluded.local_path,
            sha256 = excluded.sha256,
            received_at = excluded.received_at
    """
    params = [
        [
            r.get("accession_no"),
            r.get("form_type"),
            r.get("cik"),
            _normalize_filing_date(r.get("filing_date")),
            r.get("source_url"),
            str(r.get("local_path") or ""),
            r.get("sha256"),
            r.get("received_at") or now_iso,
        ]
        for r in records
    ]
    with _MANIFEST_WRITE_LOCK:
        conn = _get_conn()
        conn.executemany(sql, params)
    return len(params)


def _normalize_filing_date(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    v = str(value).strip()
    if len(v) == 8 and v.isdigit():
        return f"{v[0:4]}-{v[4:6]}-{v[6:8]}"
    return v
