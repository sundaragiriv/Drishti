"""SQLite ledger for the insider Director-cluster strategy.

Stored alongside the main scanner DB (signals.db). Tracks open and closed
positions for THIS strategy independently, so analytics don't mix with the
Kubera Sniper Board paper trades.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from signal_scanner.config import DB_PATH

_SCHEMA = """
CREATE TABLE IF NOT EXISTS insider_strategy_positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,

    -- Cluster metadata
    cluster_date DATE,
    known_date DATE,
    n_insiders INTEGER,
    n_directors INTEGER,
    n_officers INTEGER,
    total_value REAL,
    avg_buy_price REAL,

    -- Entry
    entry_date DATE,
    entry_price REAL,
    shares REAL,
    cost_basis REAL,

    -- Risk management
    atr14 REAL,
    stop_price REAL,
    target_price REAL,
    target_r_mult REAL DEFAULT 2.0,
    stop_atr_mult REAL DEFAULT 2.0,
    time_stop_days INTEGER DEFAULT 30,

    -- Lifecycle
    status TEXT NOT NULL DEFAULT 'OPEN',  -- OPEN, CLOSED
    exit_date DATE,
    exit_price REAL,
    exit_reason TEXT,           -- STOP, TARGET, TIME, ML, REGIME, MANUAL
    realized_pnl REAL,
    realized_pnl_pct REAL,

    -- IBKR linkage (null if SIM-only)
    ibkr_parent_order_id INTEGER,
    ibkr_stop_order_id INTEGER,
    ibkr_target_order_id INTEGER,
    execution_mode TEXT DEFAULT 'SIM',  -- SIM, IBKR_PAPER, IBKR_LIVE

    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_isp_ticker ON insider_strategy_positions(ticker);
CREATE INDEX IF NOT EXISTS idx_isp_status ON insider_strategy_positions(status);
CREATE INDEX IF NOT EXISTS idx_isp_entry ON insider_strategy_positions(entry_date);

CREATE TABLE IF NOT EXISTS insider_strategy_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_date DATE NOT NULL,
    new_clusters_found INTEGER,
    new_entries INTEGER,
    open_positions_before INTEGER,
    ml_exits INTEGER,
    regime_exits INTEGER,
    stop_exits INTEGER,
    target_exits INTEGER,
    time_exits INTEGER,
    open_positions_after INTEGER,
    regime_allows_long INTEGER,    -- 0/1
    notes TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_isr_date ON insider_strategy_runs(run_date);
"""


class StrategyLedger:
    """Thin wrapper around the SQLite tables for this strategy."""

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = str(db_path or DB_PATH)
        self._init_schema()

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._conn() as conn:
            conn.executescript(_SCHEMA)

    # ---- positions ----
    def open_position(self, payload: Dict[str, Any]) -> int:
        """Insert a new OPEN position. Returns the new row id."""
        cols = ", ".join(payload.keys())
        ph = ", ".join("?" for _ in payload)
        with self._conn() as conn:
            cur = conn.execute(
                f"INSERT INTO insider_strategy_positions ({cols}) VALUES ({ph})",
                list(payload.values()),
            )
            return cur.lastrowid

    def get_open_positions(self) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM insider_strategy_positions WHERE status='OPEN' "
                "ORDER BY entry_date"
            ).fetchall()
        return [dict(r) for r in rows]

    def get_position_by_ticker_open(self, ticker: str) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM insider_strategy_positions "
                "WHERE ticker=? AND status='OPEN' LIMIT 1",
                (ticker,),
            ).fetchone()
        return dict(row) if row else None

    def close_position(self, position_id: int, exit_price: float,
                       exit_reason: str, exit_date: Optional[date] = None) -> None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT entry_price, shares, cost_basis FROM insider_strategy_positions "
                "WHERE id=?", (position_id,)
            ).fetchone()
            if not row:
                return
            shares = float(row["shares"] or 0)
            cost_basis = float(row["cost_basis"] or 0)
            exit_value = exit_price * shares
            realized_pnl = exit_value - cost_basis
            realized_pnl_pct = (realized_pnl / cost_basis * 100) if cost_basis else 0

            conn.execute(
                "UPDATE insider_strategy_positions SET status='CLOSED', "
                "exit_date=?, exit_price=?, exit_reason=?, "
                "realized_pnl=?, realized_pnl_pct=?, updated_at=CURRENT_TIMESTAMP "
                "WHERE id=?",
                (str(exit_date or date.today()), exit_price, exit_reason,
                 realized_pnl, realized_pnl_pct, position_id),
            )

    def update_position(self, position_id: int, **fields) -> None:
        if not fields:
            return
        sets = ", ".join(f"{k}=?" for k in fields)
        with self._conn() as conn:
            conn.execute(
                f"UPDATE insider_strategy_positions SET {sets}, "
                f"updated_at=CURRENT_TIMESTAMP WHERE id=?",
                list(fields.values()) + [position_id],
            )

    def already_entered_recently(self, ticker: str, dedupe_days: int = 60) -> bool:
        """True if we've opened a position in this ticker within the trailing
        N days (whether currently open or closed). Prevents re-entering on
        the same cluster wave."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM insider_strategy_positions "
                "WHERE ticker=? AND entry_date >= DATE('now', ?)",
                (ticker, f"-{int(dedupe_days)} day"),
            ).fetchone()
        return row is not None

    # ---- runs ----
    def log_run(self, payload: Dict[str, Any]) -> int:
        cols = ", ".join(payload.keys())
        ph = ", ".join("?" for _ in payload)
        with self._conn() as conn:
            cur = conn.execute(
                f"INSERT INTO insider_strategy_runs ({cols}) VALUES ({ph})",
                list(payload.values()),
            )
            return cur.lastrowid
