"""Live Intraday Bar Store — SQLite-backed shared data plane.

Architecture:
  - One bar printer writes bars (IBKR → SQLite)
  - Many strategy readers consume bars (SQLite → evaluate)
  - No strategy touches IBKR for market data
  - Execution is a separate consumer

SQLite WAL mode for concurrent read/write.

Tables:
  session_universe   — tracked symbols for the day
  live_intraday_bars — 1-min OHLCV bars (append-only during session)
  live_symbol_status — per-symbol freshness and health
  live_runtime_health — component-level heartbeats
"""

from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd
from loguru import logger


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS session_universe (
    session_date    TEXT    NOT NULL,
    symbol          TEXT    NOT NULL,
    tier            INTEGER NOT NULL DEFAULT 1,
    source_eligibility TEXT,          -- VWAP_MR,FPB,ORB_V2 comma-separated
    conviction      REAL,
    accum_phase     TEXT,
    open_position   INTEGER NOT NULL DEFAULT 0,
    added_reason    TEXT,
    added_at        TEXT    NOT NULL,
    updated_at      TEXT    NOT NULL,
    PRIMARY KEY (session_date, symbol)
);

CREATE TABLE IF NOT EXISTS live_intraday_bars (
    symbol          TEXT    NOT NULL,
    bar_ts          TEXT    NOT NULL,      -- ISO timestamp of bar start
    open            REAL    NOT NULL,
    high            REAL    NOT NULL,
    low             REAL    NOT NULL,
    close           REAL    NOT NULL,
    volume          INTEGER NOT NULL DEFAULT 0,
    fetch_ts        TEXT    NOT NULL,      -- when this bar was fetched
    source          TEXT    NOT NULL DEFAULT 'IBKR',
    PRIMARY KEY (symbol, bar_ts)
);

CREATE TABLE IF NOT EXISTS live_symbol_status (
    symbol          TEXT    PRIMARY KEY,
    last_bar_ts     TEXT,
    bar_count       INTEGER NOT NULL DEFAULT 0,
    bar_age_seconds REAL    NOT NULL DEFAULT 0,
    is_stale        INTEGER NOT NULL DEFAULT 0,
    last_fetch_status TEXT  NOT NULL DEFAULT 'PENDING',
    last_fetch_error TEXT,
    last_fetch_at   TEXT
);

CREATE TABLE IF NOT EXISTS live_strategy_signals (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy        TEXT    NOT NULL,
    symbol          TEXT    NOT NULL,
    signal_ts       TEXT    NOT NULL,
    bar_ts_used     TEXT,
    signal_type     TEXT,              -- ENTRY, SETUP_DETECTED, EXIT, etc.
    score           REAL,
    percentile      REAL,
    rationale       TEXT,
    freshness_state TEXT,              -- FRESH, STALE, MISSING
    status          TEXT    NOT NULL DEFAULT 'NEW',
    recommendation_source TEXT
);

CREATE TABLE IF NOT EXISTS live_runtime_health (
    component       TEXT    PRIMARY KEY,
    heartbeat_ts    TEXT    NOT NULL,
    cycles_completed INTEGER NOT NULL DEFAULT 0,
    errors          INTEGER NOT NULL DEFAULT 0,
    lag_seconds     REAL    NOT NULL DEFAULT 0,
    notes           TEXT
);

CREATE INDEX IF NOT EXISTS idx_bars_symbol ON live_intraday_bars(symbol);
CREATE INDEX IF NOT EXISTS idx_bars_ts ON live_intraday_bars(bar_ts);
CREATE INDEX IF NOT EXISTS idx_signals_strategy ON live_strategy_signals(strategy, symbol);
"""

# Freshness thresholds
STALE_SECONDS = 180  # bar older than 3 min during market hours = stale (allows cold cycle headroom)

# Default DB path
DEFAULT_LIVE_DB = Path(__file__).resolve().parent.parent / "data" / "live_session.db"


class LiveBarStore:
    """SQLite-backed live intraday bar store.

    One writer (bar printer), many readers (strategies).
    """

    def __init__(self, db_path: str = None):
        self._db_path = str(db_path or DEFAULT_LIVE_DB)
        self._init_db()

    def _get_conn(self, readonly: bool = False) -> sqlite3.Connection:
        conn = sqlite3.connect(
            f"file:{self._db_path}{'?mode=ro' if readonly else ''}",
            uri=True, timeout=10,
        )
        conn.row_factory = sqlite3.Row
        if not readonly:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _init_db(self) -> None:
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._get_conn() as conn:
            conn.executescript(_SCHEMA)

    # ------------------------------------------------------------------
    # Session universe
    # ------------------------------------------------------------------

    def set_universe(self, session_date: str, symbols: List[Dict]) -> int:
        """Set the tracked universe for the session.

        Args:
            session_date: YYYY-MM-DD
            symbols: list of {symbol, tier, source_eligibility, conviction,
                              accum_phase, open_position, added_reason}

        Returns: number of symbols set.
        """
        now = datetime.now(timezone.utc).isoformat()
        with self._get_conn() as conn:
            for s in symbols:
                conn.execute("""
                    INSERT INTO session_universe
                        (session_date, symbol, tier, source_eligibility,
                         conviction, accum_phase, open_position,
                         added_reason, added_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(session_date, symbol) DO UPDATE SET
                        tier = excluded.tier,
                        source_eligibility = excluded.source_eligibility,
                        conviction = excluded.conviction,
                        open_position = excluded.open_position,
                        updated_at = excluded.updated_at
                """, (
                    session_date, s["symbol"], s.get("tier", 1),
                    s.get("source_eligibility", ""),
                    s.get("conviction"), s.get("accum_phase"),
                    1 if s.get("open_position") else 0,
                    s.get("added_reason", "premarket"),
                    now, now,
                ))
        return len(symbols)

    def get_universe(self, session_date: str,
                     tier: int = None) -> List[Dict[str, Any]]:
        """Get tracked symbols for the session."""
        sql = "SELECT * FROM session_universe WHERE session_date = ?"
        params: list = [session_date]
        if tier is not None:
            sql += " AND tier = ?"
            params.append(tier)
        sql += " ORDER BY tier, conviction DESC"
        with self._get_conn(readonly=True) as conn:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]

    def get_tracked_symbols(self, session_date: str,
                            tier: int = None) -> List[str]:
        """Get just the symbol list for the session."""
        rows = self.get_universe(session_date, tier)
        return [r["symbol"] for r in rows]

    # ------------------------------------------------------------------
    # Bar writing (bar printer only)
    # ------------------------------------------------------------------

    def write_bars(self, symbol: str, bars_df: pd.DataFrame,
                   source: str = "IBKR") -> int:
        """Write 1-min bars for a symbol. Incremental — only inserts new bars.

        Args:
            symbol: ticker
            bars_df: DataFrame with index=Date, columns=[Open,High,Low,Close,Volume]
            source: data source tag

        Returns: number of new bars inserted.
        """
        if bars_df is None or len(bars_df) == 0:
            return 0

        now = datetime.now(timezone.utc).isoformat()
        inserted = 0

        with self._get_conn() as conn:
            # Get latest existing bar for this symbol
            existing = conn.execute(
                "SELECT MAX(bar_ts) FROM live_intraday_bars WHERE symbol = ?",
                (symbol,),
            ).fetchone()[0]

            for idx in bars_df.index:
                bar_ts = str(idx)
                if existing and bar_ts <= existing:
                    continue  # skip already-stored bars

                conn.execute("""
                    INSERT OR IGNORE INTO live_intraday_bars
                        (symbol, bar_ts, open, high, low, close, volume, fetch_ts, source)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    symbol, bar_ts,
                    float(bars_df.loc[idx, "Open"]),
                    float(bars_df.loc[idx, "High"]),
                    float(bars_df.loc[idx, "Low"]),
                    float(bars_df.loc[idx, "Close"]),
                    int(bars_df.loc[idx, "Volume"]),
                    now, source,
                ))
                inserted += 1

            # Update symbol status
            last_bar = str(bars_df.index[-1])
            conn.execute("""
                INSERT INTO live_symbol_status
                    (symbol, last_bar_ts, bar_count, bar_age_seconds,
                     is_stale, last_fetch_status, last_fetch_at)
                VALUES (?, ?, ?, 0, 0, 'OK', ?)
                ON CONFLICT(symbol) DO UPDATE SET
                    last_bar_ts = excluded.last_bar_ts,
                    bar_count = (SELECT COUNT(*) FROM live_intraday_bars WHERE symbol = ?),
                    bar_age_seconds = 0,
                    is_stale = 0,
                    last_fetch_status = 'OK',
                    last_fetch_error = NULL,
                    last_fetch_at = excluded.last_fetch_at
            """, (symbol, last_bar, 0, now, symbol))

        return inserted

    def mark_fetch_error(self, symbol: str, error: str) -> None:
        """Record a fetch failure for a symbol."""
        now = datetime.now(timezone.utc).isoformat()
        with self._get_conn() as conn:
            conn.execute("""
                INSERT INTO live_symbol_status
                    (symbol, last_fetch_status, last_fetch_error, last_fetch_at,
                     bar_age_seconds, is_stale)
                VALUES (?, 'ERROR', ?, ?, 999, 1)
                ON CONFLICT(symbol) DO UPDATE SET
                    last_fetch_status = 'ERROR',
                    last_fetch_error = ?,
                    last_fetch_at = ?
            """, (symbol, error, now, error, now))

    # ------------------------------------------------------------------
    # Bar reading (strategies)
    # ------------------------------------------------------------------

    def get_bars(self, symbol: str) -> Optional[pd.DataFrame]:
        """Get all today's 1-min bars for a symbol as DataFrame.

        Returns DataFrame with columns [Open, High, Low, Close, Volume]
        indexed by bar_ts. Returns None if no bars.
        """
        with self._get_conn(readonly=True) as conn:
            rows = conn.execute("""
                SELECT bar_ts, open, high, low, close, volume
                FROM live_intraday_bars
                WHERE symbol = ?
                ORDER BY bar_ts
            """, (symbol,)).fetchall()

        if not rows:
            return None

        data = [{
            "Date": r["bar_ts"],
            "Open": r["open"], "High": r["high"],
            "Low": r["low"], "Close": r["close"],
            "Volume": r["volume"],
        } for r in rows]
        df = pd.DataFrame(data)
        df.set_index("Date", inplace=True)
        return df

    def get_latest_price(self, symbol: str) -> Optional[float]:
        """Get the latest close price for a symbol."""
        with self._get_conn(readonly=True) as conn:
            row = conn.execute("""
                SELECT close FROM live_intraday_bars
                WHERE symbol = ? ORDER BY bar_ts DESC LIMIT 1
            """, (symbol,)).fetchone()
        return float(row["close"]) if row else None

    def get_symbol_status(self, symbol: str) -> Optional[Dict]:
        """Get freshness status for a symbol."""
        with self._get_conn(readonly=True) as conn:
            row = conn.execute(
                "SELECT * FROM live_symbol_status WHERE symbol = ?",
                (symbol,),
            ).fetchone()
        return dict(row) if row else None

    def get_all_status(self) -> List[Dict]:
        """Get freshness status for all tracked symbols."""
        with self._get_conn(readonly=True) as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM live_symbol_status ORDER BY is_stale DESC, symbol"
            ).fetchall()]

    def get_fresh_symbols(self) -> List[str]:
        """Get symbols that have non-stale bars."""
        with self._get_conn(readonly=True) as conn:
            rows = conn.execute(
                "SELECT symbol FROM live_symbol_status WHERE is_stale = 0"
            ).fetchall()
        return [r["symbol"] for r in rows]

    # ------------------------------------------------------------------
    # Strategy signal recording
    # ------------------------------------------------------------------

    def record_signal(self, signal: Dict[str, Any]) -> int:
        """Record a strategy signal/idea."""
        now = datetime.now(timezone.utc).isoformat()
        with self._get_conn() as conn:
            cursor = conn.execute("""
                INSERT INTO live_strategy_signals
                    (strategy, symbol, signal_ts, bar_ts_used,
                     signal_type, score, percentile, rationale,
                     freshness_state, status, recommendation_source)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                signal.get("strategy", ""),
                signal.get("symbol", ""),
                now,
                signal.get("bar_ts_used"),
                signal.get("signal_type", "SETUP_DETECTED"),
                signal.get("score"),
                signal.get("percentile"),
                signal.get("rationale"),
                signal.get("freshness_state", "FRESH"),
                signal.get("status", "NEW"),
                signal.get("recommendation_source"),
            ))
            return cursor.lastrowid

    def get_pending_signals(self, strategy: str = None) -> List[Dict]:
        """Get signals pending execution."""
        sql = "SELECT * FROM live_strategy_signals WHERE status IN ('NEW', 'PENDING_EXECUTION')"
        params: list = []
        if strategy:
            sql += " AND strategy = ?"
            params.append(strategy)
        sql += " ORDER BY signal_ts"
        with self._get_conn(readonly=True) as conn:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]

    def mark_signal_processed(self, signal_id: int, status: str = "PROCESSED") -> None:
        """Mark a signal as processed by execution engine."""
        with self._get_conn() as conn:
            conn.execute(
                "UPDATE live_strategy_signals SET status = ? WHERE id = ?",
                (status, signal_id),
            )

    # ------------------------------------------------------------------
    # Health tracking
    # ------------------------------------------------------------------

    def update_health(self, component: str, cycles: int = 0,
                      errors: int = 0, lag: float = 0,
                      notes: str = "") -> None:
        """Update health heartbeat for a component."""
        now = datetime.now(timezone.utc).isoformat()
        with self._get_conn() as conn:
            conn.execute("""
                INSERT INTO live_runtime_health
                    (component, heartbeat_ts, cycles_completed, errors,
                     lag_seconds, notes)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(component) DO UPDATE SET
                    heartbeat_ts = excluded.heartbeat_ts,
                    cycles_completed = cycles_completed + ?,
                    errors = errors + ?,
                    lag_seconds = excluded.lag_seconds,
                    notes = excluded.notes
            """, (component, now, cycles, errors, lag, notes,
                  cycles, errors))

    def update_staleness(self) -> int:
        """Mark symbols as stale if bar_age exceeds threshold.

        Called periodically by bar printer. Returns count of stale symbols.
        """
        now_epoch = time.time()
        stale_count = 0
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT symbol, last_bar_ts, last_fetch_at FROM live_symbol_status"
            ).fetchall()
            for r in rows:
                last_fetch = r["last_fetch_at"]
                if last_fetch:
                    try:
                        fetch_dt = datetime.fromisoformat(last_fetch.replace("Z", "+00:00"))
                        age = now_epoch - fetch_dt.timestamp()
                    except Exception:
                        age = 9999
                else:
                    age = 9999

                is_stale = 1 if age > STALE_SECONDS else 0
                if is_stale:
                    stale_count += 1
                conn.execute("""
                    UPDATE live_symbol_status
                    SET bar_age_seconds = ?, is_stale = ?
                    WHERE symbol = ?
                """, (round(age, 1), is_stale, r["symbol"]))
        return stale_count

    # ------------------------------------------------------------------
    # Session management
    # ------------------------------------------------------------------

    def clear_session(self) -> None:
        """Clear all live data for a fresh session start."""
        with self._get_conn() as conn:
            conn.execute("DELETE FROM live_intraday_bars")
            conn.execute("DELETE FROM live_symbol_status")
            conn.execute("DELETE FROM live_strategy_signals")
            conn.execute("DELETE FROM live_runtime_health")
        logger.info("LiveBarStore: session cleared")

    def get_session_summary(self) -> Dict[str, Any]:
        """Get session health summary."""
        with self._get_conn(readonly=True) as conn:
            bar_count = conn.execute(
                "SELECT COUNT(*) FROM live_intraday_bars"
            ).fetchone()[0]
            symbol_count = conn.execute(
                "SELECT COUNT(DISTINCT symbol) FROM live_intraday_bars"
            ).fetchone()[0]
            stale_count = conn.execute(
                "SELECT COUNT(*) FROM live_symbol_status WHERE is_stale = 1"
            ).fetchone()[0]
            signal_count = conn.execute(
                "SELECT COUNT(*) FROM live_strategy_signals"
            ).fetchone()[0]
            pending = conn.execute(
                "SELECT COUNT(*) FROM live_strategy_signals WHERE status = 'NEW'"
            ).fetchone()[0]

        return {
            "total_bars": bar_count,
            "symbols_with_bars": symbol_count,
            "stale_symbols": stale_count,
            "total_signals": signal_count,
            "pending_signals": pending,
        }
