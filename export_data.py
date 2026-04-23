"""
Quant-Bridge Data Export Utility
Exports all DuckDB + SQLite data to Parquet/CSV for use in external tools.

Usage:
    python export_data.py --format parquet     # Parquet (recommended)
    python export_data.py --format csv         # CSV (universal)
    python export_data.py --format both        # Both formats
    python export_data.py --tables fact_daily_prices,intelligence_scores  # Specific tables only
"""

import argparse, os, sys, time
import duckdb

DUCKDB_PATH = "data/warehouse/sec_intel.duckdb"
SQLITE_PATH = "signal_scanner/data/signals.db"
EXPORT_DIR = "data/exports"


def export_duckdb(fmt="parquet", tables=None):
    """Export DuckDB tables to parquet or CSV."""
    conn = duckdb.connect(DUCKDB_PATH, read_only=True)
    all_tables = [
        r[0]
        for r in conn.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema='main' ORDER BY table_name"
        ).fetchall()
    ]

    if tables:
        all_tables = [t for t in all_tables if t in tables]

    os.makedirs(f"{EXPORT_DIR}/duckdb", exist_ok=True)

    for tbl in all_tables:
        count = conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
        if count == 0:
            print(f"  SKIP {tbl} (empty)")
            continue

        t0 = time.time()
        if fmt in ("parquet", "both"):
            out = f"{EXPORT_DIR}/duckdb/{tbl}.parquet"
            conn.execute(
                f"COPY {tbl} TO '{out}' (FORMAT PARQUET, COMPRESSION ZSTD)"
            )
            sz = os.path.getsize(out) / 1024 / 1024
            print(f"  {tbl} -> parquet ({count:,} rows, {sz:.1f} MB, {time.time()-t0:.1f}s)")

        if fmt in ("csv", "both"):
            t0 = time.time()
            out = f"{EXPORT_DIR}/duckdb/{tbl}.csv"
            conn.execute(f"COPY {tbl} TO '{out}' (FORMAT CSV, HEADER)")
            sz = os.path.getsize(out) / 1024 / 1024
            print(f"  {tbl} -> csv ({count:,} rows, {sz:.1f} MB, {time.time()-t0:.1f}s)")

    conn.close()


def export_sqlite(fmt="parquet"):
    """Export SQLite tables via DuckDB's sqlite scanner."""
    conn = duckdb.connect(":memory:")
    conn.execute("INSTALL sqlite; LOAD sqlite;")
    conn.execute(f"ATTACH '{SQLITE_PATH}' AS sdb (TYPE sqlite, READ_ONLY)")

    tables = [
        r[0]
        for r in conn.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema='sdb' AND table_name != 'sqlite_sequence'"
        ).fetchall()
    ]

    os.makedirs(f"{EXPORT_DIR}/sqlite", exist_ok=True)

    for tbl in tables:
        count = conn.execute(f"SELECT COUNT(*) FROM sdb.{tbl}").fetchone()[0]
        if count == 0:
            print(f"  SKIP {tbl} (empty)")
            continue

        t0 = time.time()
        if fmt in ("parquet", "both"):
            out = f"{EXPORT_DIR}/sqlite/{tbl}.parquet"
            conn.execute(
                f"COPY (SELECT * FROM sdb.{tbl}) TO '{out}' (FORMAT PARQUET, COMPRESSION ZSTD)"
            )
            sz = os.path.getsize(out) / 1024 / 1024
            print(f"  {tbl} -> parquet ({count:,} rows, {sz:.1f} MB, {time.time()-t0:.1f}s)")

        if fmt in ("csv", "both"):
            t0 = time.time()
            out = f"{EXPORT_DIR}/sqlite/{tbl}.csv"
            conn.execute(
                f"COPY (SELECT * FROM sdb.{tbl}) TO '{out}' (FORMAT CSV, HEADER)"
            )
            sz = os.path.getsize(out) / 1024 / 1024
            print(f"  {tbl} -> csv ({count:,} rows, {sz:.1f} MB, {time.time()-t0:.1f}s)")

    conn.close()


def main():
    parser = argparse.ArgumentParser(description="Export Quant-Bridge data")
    parser.add_argument(
        "--format", choices=["parquet", "csv", "both"], default="parquet"
    )
    parser.add_argument(
        "--tables",
        type=str,
        default=None,
        help="Comma-separated table names (DuckDB only)",
    )
    parser.add_argument(
        "--skip-sqlite", action="store_true", help="Skip SQLite export"
    )
    args = parser.parse_args()

    table_filter = set(args.tables.split(",")) if args.tables else None

    print(f"\n=== Exporting DuckDB ({DUCKDB_PATH}) -> {args.format} ===")
    export_duckdb(args.format, table_filter)

    if not args.skip_sqlite:
        print(f"\n=== Exporting SQLite ({SQLITE_PATH}) -> {args.format} ===")
        export_sqlite(args.format)

    print(f"\nDone! Files in: {EXPORT_DIR}/")
    print("\nTo import into another DuckDB:")
    print("  import duckdb")
    print("  conn = duckdb.connect('new.duckdb')")
    print(f"  conn.execute(\"CREATE TABLE t AS SELECT * FROM read_parquet('{EXPORT_DIR}/duckdb/table.parquet')\")")
    print("\nTo import into PostgreSQL (via DuckDB):")
    print("  INSTALL postgres; LOAD postgres;")
    print("  ATTACH 'host=localhost dbname=quant' AS pg (TYPE postgres);")
    print("  CREATE TABLE pg.fact_daily_prices AS SELECT * FROM read_parquet('...');")


if __name__ == "__main__":
    main()
