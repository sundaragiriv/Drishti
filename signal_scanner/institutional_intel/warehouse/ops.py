"""Operational helpers for ingestion run metadata."""

import threading
import time
from datetime import datetime, timezone
from typing import Optional

import duckdb

from signal_scanner.institutional_intel.config import WAREHOUSE_PATH

_CONN_LOCK = threading.Lock()
_CONN: Optional[duckdb.DuckDBPyConnection] = None


def _get_conn() -> duckdb.DuckDBPyConnection:
    """Return a cached module-level DuckDB connection (thread-safe)."""
    global _CONN
    with _CONN_LOCK:
        if _CONN is None:
            _CONN = duckdb.connect(str(WAREHOUSE_PATH))
        return _CONN


def start_ingestion_run(source: str, job_name: str, parser_version: str = "phase-a") -> int:
    """Create an ingestion run row and return run id."""
    run_id = int(time.time() * 1000)
    started_at = datetime.now(timezone.utc).isoformat()
    sql = """
        INSERT INTO ingestion_runs
            (id, source, job_name, started_at, status, rows_ingested, rows_failed, parser_version)
        VALUES (?, ?, ?, ?, 'RUNNING', 0, 0, ?)
    """
    with _CONN_LOCK:
        conn = _get_conn()
        conn.execute(sql, [run_id, source, job_name, started_at, parser_version])
    return run_id


def finish_ingestion_run(
    run_id: int,
    status: str,
    rows_ingested: int,
    rows_failed: int,
    notes: str = "",
) -> None:
    """Finalize ingestion run metrics."""
    finished_at = datetime.now(timezone.utc).isoformat()
    sql = """
        UPDATE ingestion_runs
        SET finished_at = ?,
            status = ?,
            rows_ingested = ?,
            rows_failed = ?,
            notes = ?
        WHERE id = ?
    """
    with _CONN_LOCK:
        conn = _get_conn()
        conn.execute(sql, [finished_at, status, rows_ingested, rows_failed, notes, run_id])


def get_max_manifest_filing_date() -> Optional[str]:
    """Return latest filing_date present in raw_file_manifest."""
    with _CONN_LOCK:
        conn = _get_conn()
        row = conn.execute("SELECT MAX(filing_date) AS d FROM raw_file_manifest").fetchone()
    if not row or row[0] is None:
        return None
    return str(row[0])
