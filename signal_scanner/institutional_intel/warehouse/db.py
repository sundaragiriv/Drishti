"""DuckDB bootstrap for institutional intelligence warehouse."""

from pathlib import Path

import duckdb
from loguru import logger

from signal_scanner.institutional_intel.config import (
    META_DIR,
    PROCESSED_DIR,
    RAW_SEC_DIR,
    WAREHOUSE_DIR,
    WAREHOUSE_PATH,
)


def ensure_data_directories() -> None:
    """Create required local storage directories."""
    for p in (RAW_SEC_DIR, PROCESSED_DIR, META_DIR, WAREHOUSE_DIR):
        Path(p).mkdir(parents=True, exist_ok=True)


def _ensure_intelligence_tables(conn: duckdb.DuckDBPyConnection) -> None:
    """Create intelligence layer tables if they were added after initial schema run."""
    schema_path = Path(__file__).with_name("schema.sql")
    sql = schema_path.read_text(encoding="utf-8")
    # Extract only CREATE TABLE statements for intelligence tables
    intelligence_tables = [
        "intelligence_scores", "dim_manager_tiers",
        "agg_sector_rotation", "backtest_results",
    ]
    for table in intelligence_tables:
        try:
            conn.execute(f"SELECT COUNT(*) FROM {table} LIMIT 0")
        except Exception:
            # Table does not exist — run full schema to create it
            conn.execute(sql)
            logger.info(f"Created new intelligence table: {table}")
            break


def _migrate_schema(conn: duckdb.DuckDBPyConnection) -> None:
    """Add new columns to existing tables if they don't exist yet."""
    migrations = [
        # Price/volume columns on agg_quarterly_holdings
        ("agg_quarterly_holdings", "avg_price", "DOUBLE"),
        ("agg_quarterly_holdings", "avg_volume", "DOUBLE"),
        ("agg_quarterly_holdings", "quarter_end_price", "DOUBLE"),
        # Price/volume columns on agg_qoq_changes
        ("agg_qoq_changes", "avg_price_current", "DOUBLE"),
        ("agg_qoq_changes", "avg_price_prior", "DOUBLE"),
        ("agg_qoq_changes", "avg_price_change_pct", "DOUBLE"),
        ("agg_qoq_changes", "avg_volume_current", "DOUBLE"),
        ("agg_qoq_changes", "avg_volume_prior", "DOUBLE"),
        ("agg_qoq_changes", "avg_volume_change_pct", "DOUBLE"),
        ("agg_qoq_changes", "current_price", "DOUBLE"),
        ("agg_qoq_changes", "price_on_report_date", "DOUBLE"),
        ("agg_qoq_changes", "price_returns_pct", "DOUBLE"),
    ]
    for table, col, dtype in migrations:
        try:
            conn.execute(f"SELECT {col} FROM {table} LIMIT 0")
        except duckdb.BinderException:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {dtype}")
            logger.debug(f"Added column {table}.{col} ({dtype})")


def init_warehouse() -> None:
    """Initialize DuckDB schema if not present."""
    ensure_data_directories()
    schema_path = Path(__file__).with_name("schema.sql")
    sql = schema_path.read_text(encoding="utf-8")

    try:
        conn = duckdb.connect(str(WAREHOUSE_PATH))
        try:
            conn.execute(sql)
            _migrate_schema(conn)
            _ensure_intelligence_tables(conn)
        finally:
            conn.close()
        logger.info(f"Institutional warehouse initialized at {WAREHOUSE_PATH}")
    except duckdb.IOException:
        # On Windows, another process may hold the write lock.
        # Tables already exist from a prior init — safe to proceed.
        logger.info(f"Institutional warehouse already in use at {WAREHOUSE_PATH} (skipping init)")

