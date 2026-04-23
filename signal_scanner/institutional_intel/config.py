"""Configuration for institutional intelligence pipelines."""

import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import duckdb
from dotenv import load_dotenv
from loguru import logger

# Load .env from project root
load_dotenv(Path(__file__).resolve().parents[2] / ".env")

BASE_DIR = Path(__file__).resolve().parents[2]
SHARED_DATA_ROOT = os.getenv("SEC_INTEL_DATA_ROOT", "").strip()
DATA_DIR = Path(SHARED_DATA_ROOT) if SHARED_DATA_ROOT else (BASE_DIR / "data")
RAW_SEC_DIR = DATA_DIR / "raw" / "sec"
PROCESSED_DIR = DATA_DIR / "processed"
META_DIR = DATA_DIR / "meta"
WAREHOUSE_DIR = DATA_DIR / "warehouse"
WAREHOUSE_PATH = WAREHOUSE_DIR / "sec_intel.duckdb"
BULK_DIR = DATA_DIR / "bulk_sec"

# Massive.com / Polygon-compatible market data
MASSIVE_API_KEY = os.getenv("MASSIVE_API_KEY", "")
MASSIVE_BASE_URL = os.getenv("MASSIVE_BASE_URL", "https://api.polygon.io")


@dataclass
class InstitutionalIntelConfig:
    """Runtime config for local SEC ingestion and analytics."""

    user_agent: str = os.getenv(
        "SEC_USER_AGENT",
        "QuantBridge Research Contact admin@quantbridge.local",
    )
    request_timeout_seconds: int = 30
    requests_per_second: float = float(os.getenv("SEC_REQUESTS_PER_SECOND", "8.0"))
    backfill_start_date: str = "2021-01-01"


# ---------------------------------------------------------------------------
# Safe DuckDB connection helper
# ---------------------------------------------------------------------------

def _identify_lock_holder(db_path: Path) -> str:
    """Try to identify which process holds the DuckDB write lock."""
    try:
        out = subprocess.check_output(
            ["handle.exe", str(db_path)],
            stderr=subprocess.DEVNULL, timeout=5, text=True,
        )
        return out.strip()[:200]
    except Exception:
        pass
    # Fallback: parse PID from DuckDB error message if available
    return "unknown (run 'handle.exe <db_path>' or check Task Manager)"


def safe_duckdb_connect(
    read_only: bool = True,
    max_retries: int = 3,
    retry_delay: float = 0.5,
) -> Optional[duckdb.DuckDBPyConnection]:
    """Connect to the warehouse DuckDB with retry logic on lock conflicts.

    Returns None instead of raising if the database is locked after all retries,
    so callers can degrade gracefully.
    """
    for attempt in range(max_retries):
        try:
            return duckdb.connect(str(WAREHOUSE_PATH), read_only=read_only)
        except Exception as exc:
            err_msg = str(exc)
            if ("being used by another process" in err_msg or
                re.search(r"PID \d+", err_msg)) and attempt < max_retries - 1:
                time.sleep(retry_delay * (attempt + 1))
                continue
            # Final attempt failed — log and return None
            pid_match = re.search(r"PID (\d+)", err_msg)
            if "being used by another process" in err_msg or pid_match:
                holder = f"PID {pid_match.group(1)}" if pid_match else "unknown"
                logger.warning(
                    "DuckDB LOCKED by {} after {} retries — dashboard data temporarily unavailable. "
                    "If this persists >30m, the pipeline may be stuck. "
                    "Kill it with: taskkill /PID {} /F",
                    holder, max_retries, pid_match.group(1) if pid_match else "<PID>",
                )
            else:
                logger.error("DuckDB connection failed: {}", err_msg)
            return None


def get_active_quarter(conn) -> Optional[str]:
    """Canonical active quarter — ONE function used by scanner, reports, ISR, dashboard.

    Strategy: latest quarter with quality >= 75 AND >= 1000 tickers (adequate coverage).
    Early/partial quarters (e.g. Q4 2025 with 484 tickers) are excluded from the default
    but remain available in dropdowns for manual selection.
    Falls back to MAX(report_quarter) if none meets threshold.
    """
    try:
        row = conn.execute("""
            SELECT report_quarter FROM intelligence_scores
            WHERE data_quality_score >= 75
            GROUP BY report_quarter
            HAVING COUNT(*) >= 1000
            ORDER BY report_quarter DESC LIMIT 1
        """).fetchone()
        if row:
            return row[0]
        # Fallback: latest available
        row = conn.execute(
            "SELECT MAX(report_quarter) FROM intelligence_scores"
        ).fetchone()
        return row[0] if row else None
    except Exception as e:
        logger.warning("get_active_quarter failed: {}", e)
        return None
