"""Session Archiver — moves live intraday data to DuckDB after market close.

Copies from SQLite live_session.db → DuckDB fact_intraday_bars + archives
signals and health for replay/analysis.

Runs once at EOD. No live dependency on DuckDB during market hours.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Dict

from loguru import logger

from signal_scanner.core.live_bar_store import LiveBarStore


def archive_session(
    store: LiveBarStore,
    session_date: str = None,
) -> Dict[str, int]:
    """Archive live session data to DuckDB warehouse.

    Args:
        store: LiveBarStore with today's data
        session_date: YYYY-MM-DD (default: today)

    Returns: dict with counts of archived items.
    """
    if session_date is None:
        session_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    from signal_scanner.institutional_intel.config import safe_duckdb_connect

    results = {
        "bars_archived": 0,
        "signals_archived": 0,
        "symbols": 0,
    }

    # Read all bars from live store
    all_status = store.get_all_status()
    symbols_with_bars = [s["symbol"] for s in all_status if s.get("bar_count", 0) > 0]
    results["symbols"] = len(symbols_with_bars)

    if not symbols_with_bars:
        logger.info("SessionArchiver: no bars to archive for {}", session_date)
        return results

    # Connect to DuckDB for writing
    conn = safe_duckdb_connect(read_only=False)
    if not conn:
        logger.warning("SessionArchiver: cannot connect to DuckDB for archiving")
        return results

    try:
        # Ensure table exists
        conn.execute("""
            CREATE TABLE IF NOT EXISTS fact_intraday_bars (
                ticker      VARCHAR NOT NULL,
                trade_date  DATE NOT NULL,
                bar_time    TIMESTAMP NOT NULL,
                open        DOUBLE,
                high        DOUBLE,
                low         DOUBLE,
                close       DOUBLE,
                volume      BIGINT,
                source      VARCHAR DEFAULT 'IBKR',
                ingested_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (ticker, bar_time)
            )
        """)

        # Archive bars symbol by symbol
        bars_total = 0
        for symbol in symbols_with_bars:
            bars_df = store.get_bars(symbol)
            if bars_df is None or len(bars_df) == 0:
                continue

            for idx in bars_df.index:
                try:
                    conn.execute("""
                        INSERT OR IGNORE INTO fact_intraday_bars
                            (ticker, trade_date, bar_time, open, high, low, close, volume, source)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'IBKR')
                    """, [
                        symbol,
                        session_date,
                        str(idx),
                        float(bars_df.loc[idx, "Open"]),
                        float(bars_df.loc[idx, "High"]),
                        float(bars_df.loc[idx, "Low"]),
                        float(bars_df.loc[idx, "Close"]),
                        int(bars_df.loc[idx, "Volume"]),
                    ])
                    bars_total += 1
                except Exception:
                    pass  # skip duplicates

        results["bars_archived"] = bars_total

        # Archive signals to DuckDB
        conn.execute("""
            CREATE TABLE IF NOT EXISTS archived_strategy_signals (
                session_date    DATE,
                strategy        VARCHAR,
                symbol          VARCHAR,
                signal_ts       VARCHAR,
                bar_ts_used     VARCHAR,
                signal_type     VARCHAR,
                score           DOUBLE,
                percentile      DOUBLE,
                rationale       VARCHAR,
                freshness_state VARCHAR,
                status          VARCHAR,
                recommendation_source VARCHAR
            )
        """)

        import sqlite3
        live_conn = sqlite3.connect(store._db_path)
        live_conn.row_factory = sqlite3.Row
        signals = live_conn.execute(
            "SELECT * FROM live_strategy_signals"
        ).fetchall()

        signals_written = 0
        for sig in signals:
            try:
                conn.execute("""
                    INSERT INTO archived_strategy_signals
                        (session_date, strategy, symbol, signal_ts, bar_ts_used,
                         signal_type, score, percentile, rationale,
                         freshness_state, status, recommendation_source)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, [
                    session_date, sig["strategy"], sig["symbol"],
                    sig["signal_ts"], sig["bar_ts_used"],
                    sig["signal_type"], sig["score"], sig["percentile"],
                    sig["rationale"], sig["freshness_state"],
                    sig["status"], sig["recommendation_source"],
                ])
                signals_written += 1
            except Exception:
                pass
        results["signals_archived"] = signals_written

        # Archive runtime health to DuckDB
        conn.execute("""
            CREATE TABLE IF NOT EXISTS archived_runtime_health (
                session_date    DATE,
                component       VARCHAR,
                heartbeat_ts    VARCHAR,
                cycles_completed INTEGER,
                errors          INTEGER,
                lag_seconds     DOUBLE,
                notes           VARCHAR
            )
        """)

        health_rows = live_conn.execute(
            "SELECT * FROM live_runtime_health"
        ).fetchall()
        live_conn.close()

        health_written = 0
        for h in health_rows:
            try:
                conn.execute("""
                    INSERT INTO archived_runtime_health
                        (session_date, component, heartbeat_ts, cycles_completed,
                         errors, lag_seconds, notes)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, [
                    session_date, h["component"], h["heartbeat_ts"],
                    h["cycles_completed"], h["errors"],
                    h["lag_seconds"], h["notes"],
                ])
                health_written += 1
            except Exception:
                pass
        results["health_archived"] = health_written

        logger.info(
            "SessionArchiver: {} | {} symbols | {} bars | {} signals | {} health archived to DuckDB",
            session_date, results["symbols"], bars_total, signals_written, health_written,
        )

    except Exception as e:
        logger.warning("SessionArchiver error: {}", e)
    finally:
        conn.close()

    return results
