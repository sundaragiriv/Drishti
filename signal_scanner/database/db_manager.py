"""SQLite CRUD operations for Signal Command Center V2.

Uses WAL journal mode so the scanner thread can write while the
Dash dashboard reads concurrently without locking.
"""

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional, Tuple

from loguru import logger

from signal_scanner.config import DB_PATH
from signal_scanner.database.models import (
    CREATE_INDEXES,
    CREATE_EOD_ANALYSIS_TABLE,
    CREATE_OPTION_SETUP_OUTCOMES_TABLE,
    CREATE_OPTION_SETUPS_TABLE,
    CREATE_PAPER_TRADES_TABLE,
    CREATE_SCAN_HISTORY_TABLE,
    CREATE_SIGNALS_TABLE,
)


class DatabaseManager:
    """Manages all SQLite read/write operations."""

    def __init__(self, db_path: Optional[str] = None) -> None:
        self._db_path = db_path or str(DB_PATH)

    # ------------------------------------------------------------------
    # Connection helpers
    # ------------------------------------------------------------------

    @contextmanager
    def _get_connection(self) -> Generator[sqlite3.Connection, None, None]:
        """Yield a connection with WAL mode and dict-like row access."""
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self._db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Schema management
    # ------------------------------------------------------------------

    def init_db(self) -> None:
        """Create tables and indexes if they don't exist."""
        with self._get_connection() as conn:
            conn.execute(CREATE_SIGNALS_TABLE)
            conn.execute(CREATE_SCAN_HISTORY_TABLE)
            conn.execute(CREATE_PAPER_TRADES_TABLE)
            conn.execute(CREATE_OPTION_SETUPS_TABLE)
            conn.execute(CREATE_OPTION_SETUP_OUTCOMES_TABLE)
            conn.execute(CREATE_EOD_ANALYSIS_TABLE)
            self._migrate_paper_trades(conn)
            self._migrate_option_setups(conn)
            self._migrate_eod_analysis(conn)
            self._migrate_scan_history(conn)
            for idx_sql in CREATE_INDEXES:
                conn.execute(idx_sql)
        # Initialize idea ledger (uses same DB)
        from signal_scanner.paper.idea_ledger import IdeaLedger
        self._idea_ledger = IdeaLedger(self._db_path)
        logger.info(f"Database initialized at {self._db_path}")

    def _migrate_paper_trades(self, conn: sqlite3.Connection) -> None:
        """Add newer paper_trades columns for older databases."""
        rows = conn.execute("PRAGMA table_info(paper_trades)").fetchall()
        existing = {row["name"] for row in rows}
        additions = {
            "instrument_type": "TEXT DEFAULT 'STOCK'",
            "option_type": "TEXT",
            "option_expiry": "TEXT",
            "option_strike": "REAL",
            "entry_signal": "TEXT",
            "entry_score": "REAL",
            "entry_rr_ratio": "REAL",
            "entry_market_regime": "TEXT",
            "entry_gex_status": "TEXT",
            "entry_session_time": "TEXT",
            "entry_trade_conditions": "TEXT",
            # IBKR order tracking (added Feb 27 2026)
            "ibkr_parent_order_id": "INTEGER",
            "ibkr_tp_order_id": "INTEGER",
            "ibkr_sl_order_id": "INTEGER",
            "ibkr_order_status": "TEXT",
            "ibkr_fill_price": "REAL",
            "ibkr_fill_time": "TEXT",
            "ibkr_perm_id": "INTEGER",
            # Classification
            "strategy_type": "TEXT DEFAULT 'UNKNOWN'",
            "execution_mode": "TEXT DEFAULT 'SIM'",
            # Trailing stop tracking (added Mar 7 2026)
            "trail_high": "REAL",
            "trail_activated": "INTEGER DEFAULT 0",
            # Idea linkage (added Mar 17 2026)
            "idea_id": "INTEGER",
        }
        for column, definition in additions.items():
            if column not in existing:
                conn.execute(f"ALTER TABLE paper_trades ADD COLUMN {column} {definition}")
        # One-time backfill: derive strategy_type from recommendation_source
        if "strategy_type" not in existing:
            self._backfill_strategy_types(conn)

    def _backfill_strategy_types(self, conn: sqlite3.Connection) -> None:
        """Derive strategy_type from recommendation_source for existing rows."""
        mappings = [
            ("SCANNER_MTF%", "SCANNER_MTF"),
            ("VWAP_MR_ML%", "VWAP_MR"),
            ("FPB_ML%", "FPB"),
            ("MANUAL%", "MANUAL"),
            ("OPTION_IDEA%", "OPTION_IDEA"),
        ]
        for pattern, stype in mappings:
            conn.execute(
                "UPDATE paper_trades SET strategy_type = ? "
                "WHERE recommendation_source LIKE ? AND strategy_type = 'UNKNOWN'",
                (stype, pattern),
            )
        # Tag SWING trades
        conn.execute(
            "UPDATE paper_trades SET strategy_type = 'SWING' "
            "WHERE recommendation_source LIKE '%SWING%' AND strategy_type = 'UNKNOWN'"
        )

    def _migrate_eod_analysis(self, conn: sqlite3.Connection) -> None:
        """Add review workflow columns for older eod_analysis schemas."""
        rows = conn.execute("PRAGMA table_info(eod_analysis)").fetchall()
        existing = {row["name"] for row in rows}
        additions = {
            "action_status": "TEXT NOT NULL DEFAULT 'PENDING'",
            "action_notes": "TEXT",
            "action_updated_ts": "TEXT",
        }
        for column, definition in additions.items():
            if column not in existing:
                conn.execute(f"ALTER TABLE eod_analysis ADD COLUMN {column} {definition}")

    def _migrate_scan_history(self, conn: sqlite3.Connection) -> None:
        """Add latency tracking columns for scan_history in older schemas."""
        rows = conn.execute("PRAGMA table_info(scan_history)").fetchall()
        existing = {row["name"] for row in rows}
        additions = {
            "duration_seconds": "REAL",
            "scan_type": "TEXT",
        }
        for column, definition in additions.items():
            if column not in existing:
                conn.execute(f"ALTER TABLE scan_history ADD COLUMN {column} {definition}")

    def _migrate_option_setups(self, conn: sqlite3.Connection) -> None:
        """Add lifecycle columns for option setups in older schemas."""
        rows = conn.execute("PRAGMA table_info(option_setups)").fetchall()
        existing = {row["name"] for row in rows}
        additions = {
            "idea_state": "TEXT NOT NULL DEFAULT 'ACTIVE'",
            "confirm_count": "INTEGER NOT NULL DEFAULT 1",
            "invalid_reason": "TEXT",
            "is_taken": "INTEGER NOT NULL DEFAULT 0",
            "taken_at": "TEXT",
            "last_validated_ts": "TEXT",
            "option_bid": "REAL",
            "option_ask": "REAL",
            "option_last": "REAL",
            "option_mid": "REAL",
            "option_spread_pct": "REAL",
            "option_volume": "REAL",
            "option_open_interest": "REAL",
            "quote_ts": "TEXT",
            "liquidity_score": "REAL",
            "liquidity_state": "TEXT DEFAULT 'UNKNOWN'",
        }
        for column, definition in additions.items():
            if column not in existing:
                conn.execute(f"ALTER TABLE option_setups ADD COLUMN {column} {definition}")

    # ------------------------------------------------------------------
    # Signal CRUD
    # ------------------------------------------------------------------

    def upsert_signal(self, data: Dict[str, Any]) -> None:
        """Insert or replace a signal row (upsert on symbol+timeframe+timestamp)."""
        sql = """
            INSERT OR REPLACE INTO signals
                (symbol, timestamp, timeframe, score, signal,
                 price, sma_200, sma_50, price_vs_sma, price_vs_sma_pct,
                 zero_gamma_level, gamma_wall_up, gamma_wall_down, gex_status,
                 rsi, rsi_slope, adx, adx_slope, atr,
                 volume_ratio, vwap, vwap_status,
                 trend_direction, recommendation,
                 stop_loss, target_1, target_2, rr_ratio,
                 trade_conditions,
                 distance_to_resistance_pct, distance_to_support_pct,
                 prior_day_high, prior_day_low, prior_day_close,
                 relative_strength, market_regime, signal_age, session_time,
                 sector, last_updated)
            VALUES
                (:symbol, :timestamp, :timeframe, :score, :signal,
                 :price, :sma_200, :sma_50, :price_vs_sma, :price_vs_sma_pct,
                 :zero_gamma_level, :gamma_wall_up, :gamma_wall_down, :gex_status,
                 :rsi, :rsi_slope, :adx, :adx_slope, :atr,
                 :volume_ratio, :vwap, :vwap_status,
                 :trend_direction, :recommendation,
                 :stop_loss, :target_1, :target_2, :rr_ratio,
                 :trade_conditions,
                 :distance_to_resistance_pct, :distance_to_support_pct,
                 :prior_day_high, :prior_day_low, :prior_day_close,
                 :relative_strength, :market_regime, :signal_age, :session_time,
                 :sector, :last_updated)
        """
        with self._get_connection() as conn:
            conn.execute(sql, data)

    def get_latest_signals(
        self,
        symbols: Optional[List[str]] = None,
        signal_filter: Optional[str] = None,
        min_score: int = 0,
        sectors: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """Fetch the most recent signal per symbol+timeframe, with optional filters."""
        sql = """
            SELECT * FROM signals
            WHERE id IN (
                SELECT MAX(id) FROM signals GROUP BY symbol, timeframe
            )
            AND score >= ?
        """
        params: List[Any] = [min_score]

        if signal_filter and signal_filter != "ALL":
            sql += " AND signal = ?"
            params.append(signal_filter)

        if symbols:
            placeholders = ",".join("?" for _ in symbols)
            sql += f" AND symbol IN ({placeholders})"
            params.extend(symbols)

        if sectors:
            placeholders = ",".join("?" for _ in sectors)
            sql += f" AND sector IN ({placeholders})"
            params.extend(sectors)

        sql += " ORDER BY score DESC"

        with self._get_connection() as conn:
            rows = conn.execute(sql, params).fetchall()
            return [dict(row) for row in rows]

    def get_signal_history(
        self,
        symbol: str,
        timeframe: Optional[str] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """Fetch recent signal history for a specific symbol."""
        sql = "SELECT * FROM signals WHERE symbol = ?"
        params: List[Any] = [symbol]

        if timeframe:
            sql += " AND timeframe = ?"
            params.append(timeframe)

        sql += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)

        with self._get_connection() as conn:
            rows = conn.execute(sql, params).fetchall()
            return [dict(row) for row in rows]

    def get_recommendation_history(
        self,
        limit: int = 300,
        include_hold: bool = True,
    ) -> List[Dict[str, Any]]:
        """Fetch recent recommendation rows across all symbols/timeframes."""
        sql = "SELECT * FROM signals"
        params: List[Any] = []
        if not include_hold:
            sql += " WHERE recommendation IN ('BUY', 'SELL')"
        sql += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)
        with self._get_connection() as conn:
            rows = conn.execute(sql, params).fetchall()
            return [dict(row) for row in rows]

    def get_latest_prices_for_symbols(self, symbols: List[str]) -> Dict[str, float]:
        """Return most recent non-null signal price for each symbol."""
        if not symbols:
            return {}
        uniq = sorted({str(s).upper() for s in symbols if s})
        if not uniq:
            return {}
        placeholders = ",".join("?" for _ in uniq)
        sql = f"""
            SELECT s.symbol, s.price
            FROM signals s
            JOIN (
                SELECT symbol, MAX(id) AS max_id
                FROM signals
                WHERE symbol IN ({placeholders}) AND price IS NOT NULL
                GROUP BY symbol
            ) latest
              ON s.symbol = latest.symbol AND s.id = latest.max_id
        """
        with self._get_connection() as conn:
            rows = conn.execute(sql, uniq).fetchall()
        out: Dict[str, float] = {}
        for r in rows:
            symbol = str(r["symbol"]).upper()
            try:
                out[symbol] = float(r["price"])
            except (TypeError, ValueError):
                continue
        return out

    def get_unique_sectors(self) -> List[str]:
        """Return sorted list of distinct sectors in the database."""
        sql = """
            SELECT DISTINCT sector FROM signals
            WHERE sector IS NOT NULL AND sector != ''
            ORDER BY sector
        """
        with self._get_connection() as conn:
            rows = conn.execute(sql).fetchall()
            return [row["sector"] for row in rows]

    # ------------------------------------------------------------------
    # Scan history
    # ------------------------------------------------------------------

    def record_scan(
        self,
        start: str,
        end: str,
        count: int,
        found: int,
        errors: int,
        source: str,
        duration_seconds: float = None,
        scan_type: str = None,
    ) -> None:
        """Record metadata about a completed scan cycle."""
        sql = """
            INSERT INTO scan_history
                (scan_start, scan_end, symbols_scanned, signals_found, errors,
                 data_source, duration_seconds, scan_type)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """
        with self._get_connection() as conn:
            conn.execute(sql, (start, end, count, found, errors, source,
                               duration_seconds, scan_type))

    def get_last_scan(self) -> Optional[Dict[str, Any]]:
        """Return metadata of the most recent scan."""
        sql = "SELECT * FROM scan_history ORDER BY id DESC LIMIT 1"
        with self._get_connection() as conn:
            row = conn.execute(sql).fetchone()
            return dict(row) if row else None

    # ------------------------------------------------------------------
    # Paper trading
    # ------------------------------------------------------------------

    def create_paper_trade(self, data: Dict[str, Any]) -> int:
        """Create a new OPEN paper trade and return its ID."""
        sql = """
            INSERT INTO paper_trades
                (opened_at, symbol, side, entry_price, quantity, notional,
                 stop_loss, target_1, target_2, status, recommendation_source,
                 instrument_type, option_type, option_expiry, option_strike,
                 entry_signal, entry_score, entry_rr_ratio, entry_market_regime,
                 entry_gex_status, entry_session_time, entry_trade_conditions,
                 fees, created_ts)
            VALUES
                (:opened_at, :symbol, :side, :entry_price, :quantity, :notional,
                 :stop_loss, :target_1, :target_2, :status, :recommendation_source,
                 :instrument_type, :option_type, :option_expiry, :option_strike,
                 :entry_signal, :entry_score, :entry_rr_ratio, :entry_market_regime,
                 :entry_gex_status, :entry_session_time, :entry_trade_conditions,
                 :fees, :created_ts)
        """
        with self._get_connection() as conn:
            cursor = conn.execute(sql, data)
            return int(cursor.lastrowid)

    def close_paper_trade(
        self,
        trade_id: int,
        closed_at: str,
        exit_price: float,
        exit_reason: str,
        realized_pnl: float,
        realized_pnl_pct: float,
        fees: float,
    ) -> None:
        """Close an OPEN paper trade and persist realized P&L."""
        sql = """
            UPDATE paper_trades
            SET
                closed_at = ?,
                exit_price = ?,
                status = 'CLOSED',
                exit_reason = ?,
                realized_pnl = ?,
                realized_pnl_pct = ?,
                fees = ?
            WHERE id = ? AND status = 'OPEN'
        """
        with self._get_connection() as conn:
            conn.execute(
                sql,
                (
                    closed_at,
                    exit_price,
                    exit_reason,
                    realized_pnl,
                    realized_pnl_pct,
                    fees,
                    trade_id,
                ),
            )

    def update_paper_trade_source(self, trade_id: int, recommendation_source: str) -> None:
        """Update the recommendation_source tag on an open paper trade."""
        sql = "UPDATE paper_trades SET recommendation_source = ? WHERE id = ? AND status = 'OPEN'"
        with self._get_connection() as conn:
            conn.execute(sql, (recommendation_source, trade_id))

    def update_paper_trade_stop(self, trade_id: int, new_stop: float) -> None:
        """Update the stop_loss on an open paper trade (e.g. trailing stop)."""
        sql = "UPDATE paper_trades SET stop_loss = ? WHERE id = ? AND status = 'OPEN'"
        with self._get_connection() as conn:
            conn.execute(sql, (new_stop, trade_id))

    def update_paper_trade_trail(self, trade_id: int, new_stop: float, trail_high: float) -> None:
        """Update stop_loss and trail_high for an active trailing stop."""
        sql = """UPDATE paper_trades
                 SET stop_loss = ?, trail_high = ?, trail_activated = 1
                 WHERE id = ? AND status = 'OPEN'"""
        with self._get_connection() as conn:
            conn.execute(sql, (new_stop, trail_high, trade_id))

    # ------------------------------------------------------------------
    # IBKR order tracking
    # ------------------------------------------------------------------

    def update_paper_trade_ibkr_orders(
        self,
        trade_id: int,
        parent_order_id: int,
        tp_order_id: int,
        sl_order_id: int,
        perm_id: int,
    ) -> None:
        """Store IBKR bracket order IDs after placement."""
        sql = """
            UPDATE paper_trades
            SET ibkr_parent_order_id = ?,
                ibkr_tp_order_id = ?,
                ibkr_sl_order_id = ?,
                ibkr_perm_id = ?,
                ibkr_order_status = 'SUBMITTED',
                execution_mode = 'LIVE'
            WHERE id = ?
        """
        with self._get_connection() as conn:
            conn.execute(sql, (parent_order_id, tp_order_id, sl_order_id, perm_id, trade_id))

    def update_paper_trade_ibkr_status(
        self,
        trade_id: int,
        status: str,
        fill_price: float = None,
        fill_time: str = None,
    ) -> None:
        """Update IBKR order status and optional fill data."""
        if fill_price is not None:
            sql = """
                UPDATE paper_trades
                SET ibkr_order_status = ?,
                    ibkr_fill_price = ?,
                    ibkr_fill_time = ?
                WHERE id = ?
            """
            with self._get_connection() as conn:
                conn.execute(sql, (status, fill_price, fill_time, trade_id))
        else:
            sql = "UPDATE paper_trades SET ibkr_order_status = ? WHERE id = ?"
            with self._get_connection() as conn:
                conn.execute(sql, (status, trade_id))

    def get_live_open_trades(self) -> list:
        """Return all OPEN trades with execution_mode='LIVE'."""
        sql = "SELECT * FROM paper_trades WHERE status = 'OPEN' AND execution_mode = 'LIVE' ORDER BY opened_at ASC"
        with self._get_connection() as conn:
            rows = conn.execute(sql).fetchall()
            return [dict(row) for row in rows]

    def get_trade_by_ibkr_perm_id(self, perm_id: int) -> dict:
        """Lookup trade by IBKR permanent ID (for restart reconciliation)."""
        sql = "SELECT * FROM paper_trades WHERE ibkr_perm_id = ? ORDER BY id DESC LIMIT 1"
        with self._get_connection() as conn:
            row = conn.execute(sql, (perm_id,)).fetchone()
            return dict(row) if row else {}

    def get_trade_by_ibkr_order_id(self, order_id: int) -> dict:
        """Lookup trade by any IBKR order ID (parent, TP, or SL)."""
        sql = """
            SELECT * FROM paper_trades
            WHERE ibkr_parent_order_id = ?
               OR ibkr_tp_order_id = ?
               OR ibkr_sl_order_id = ?
            ORDER BY id DESC LIMIT 1
        """
        with self._get_connection() as conn:
            row = conn.execute(sql, (order_id, order_id, order_id)).fetchone()
            return dict(row) if row else {}

    def get_trade_by_id(self, trade_id: int) -> dict:
        """Get a single paper trade by ID."""
        sql = "SELECT * FROM paper_trades WHERE id = ?"
        with self._get_connection() as conn:
            row = conn.execute(sql, (trade_id,)).fetchone()
            return dict(row) if row else {}

    def get_open_paper_trades(self) -> List[Dict[str, Any]]:
        """Return all OPEN paper trades."""
        sql = "SELECT * FROM paper_trades WHERE status = 'OPEN' ORDER BY opened_at ASC"
        with self._get_connection() as conn:
            rows = conn.execute(sql).fetchall()
            return [dict(row) for row in rows]

    def get_recent_paper_trades(self, limit: int = 100) -> List[Dict[str, Any]]:
        """Return most recent paper trades (open and closed)."""
        sql = "SELECT * FROM paper_trades ORDER BY id DESC LIMIT ?"
        with self._get_connection() as conn:
            rows = conn.execute(sql, (limit,)).fetchall()
            return [dict(row) for row in rows]

    def get_symbol_recent_loss_count(self, symbol: str, limit: int = 5) -> int:
        """Return count of losing trades in last N CLOSED trades for a symbol."""
        sql = """
            SELECT realized_pnl
            FROM paper_trades
            WHERE symbol = ? AND status = 'CLOSED'
            ORDER BY id DESC
            LIMIT ?
        """
        with self._get_connection() as conn:
            rows = conn.execute(sql, (symbol, limit)).fetchall()
        return sum(1 for r in rows if float(r["realized_pnl"] or 0.0) < 0.0)

    def get_paper_performance(self) -> Dict[str, Any]:
        """Aggregate paper trading performance metrics."""
        with self._get_connection() as conn:
            open_count = conn.execute(
                "SELECT COUNT(*) AS c FROM paper_trades WHERE status = 'OPEN'"
            ).fetchone()["c"]
            closed_count = conn.execute(
                "SELECT COUNT(*) AS c FROM paper_trades WHERE status = 'CLOSED'"
            ).fetchone()["c"]
            realized = conn.execute(
                "SELECT COALESCE(SUM(realized_pnl), 0) AS v FROM paper_trades WHERE status = 'CLOSED'"
            ).fetchone()["v"]
            wins = conn.execute(
                "SELECT COUNT(*) AS c FROM paper_trades WHERE status = 'CLOSED' AND realized_pnl > 0"
            ).fetchone()["c"]
            losses = conn.execute(
                "SELECT COUNT(*) AS c FROM paper_trades WHERE status = 'CLOSED' AND realized_pnl < 0"
            ).fetchone()["c"]
            fees = conn.execute(
                "SELECT COALESCE(SUM(fees), 0) AS v FROM paper_trades"
            ).fetchone()["v"]

        win_rate = round((wins / closed_count) * 100, 1) if closed_count else 0.0
        return {
            "open_positions": int(open_count),
            "closed_trades": int(closed_count),
            "wins": int(wins),
            "losses": int(losses),
            "win_rate": win_rate,
            "realized_pnl": round(float(realized), 2),
            "fees_paid": round(float(fees), 2),
        }

    # ------------------------------------------------------------------
    # Manual trades (My Trades tab — reuses paper_trades table)
    # ------------------------------------------------------------------

    def create_manual_trade(self, data: Dict[str, Any]) -> int:
        """Create a manual trade entry in paper_trades and return its ID."""
        defaults = {
            "status": "OPEN",
            "recommendation_source": "MANUAL",
            "instrument_type": "STOCK",
            "option_type": None,
            "option_expiry": None,
            "option_strike": None,
            "entry_signal": None,
            "entry_score": None,
            "entry_rr_ratio": None,
            "entry_market_regime": None,
            "entry_gex_status": None,
            "entry_session_time": None,
            "entry_trade_conditions": None,
            "fees": 0,
        }
        defaults.update(data)
        defaults.setdefault("notional", round(
            float(defaults.get("entry_price") or 0) * int(defaults.get("quantity") or 0), 2
        ))
        sql = """
            INSERT INTO paper_trades
                (opened_at, symbol, side, entry_price, quantity, notional,
                 stop_loss, target_1, target_2, status, recommendation_source,
                 instrument_type, option_type, option_expiry, option_strike,
                 entry_signal, entry_score, entry_rr_ratio, entry_market_regime,
                 entry_gex_status, entry_session_time, entry_trade_conditions,
                 fees, created_ts)
            VALUES
                (:opened_at, :symbol, :side, :entry_price, :quantity, :notional,
                 :stop_loss, :target_1, :target_2, :status, :recommendation_source,
                 :instrument_type, :option_type, :option_expiry, :option_strike,
                 :entry_signal, :entry_score, :entry_rr_ratio, :entry_market_regime,
                 :entry_gex_status, :entry_session_time, :entry_trade_conditions,
                 :fees, :created_ts)
        """
        with self._get_connection() as conn:
            cursor = conn.execute(sql, defaults)
            return int(cursor.lastrowid)

    def close_manual_trade(
        self,
        trade_id: int,
        closed_at: str,
        exit_price: float,
        exit_qty: Optional[int] = None,
        fees: float = 0,
        notes: str = "",
    ) -> Optional[Dict[str, Any]]:
        """Close a manual trade, compute realized P&L, return updated row."""
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM paper_trades WHERE id = ? AND status = 'OPEN'",
                (trade_id,),
            ).fetchone()
            if not row:
                return None
            row = dict(row)
            entry = float(row["entry_price"])
            qty = int(exit_qty or row["quantity"])
            side = row["side"]
            if side == "LONG":
                pnl = round((exit_price - entry) * qty, 2)
            else:
                pnl = round((entry - exit_price) * qty, 2)
            pnl_pct = round((pnl / (entry * qty)) * 100, 2) if entry * qty else 0.0
            reason = f"MANUAL_EXIT"
            if notes:
                reason = f"MANUAL_EXIT: {notes}"
            conn.execute(
                """UPDATE paper_trades
                   SET closed_at = ?, exit_price = ?, status = 'CLOSED',
                       exit_reason = ?, realized_pnl = ?, realized_pnl_pct = ?, fees = ?
                   WHERE id = ? AND status = 'OPEN'""",
                (closed_at, exit_price, reason, pnl, pnl_pct, fees, trade_id),
            )
            row.update({
                "closed_at": closed_at, "exit_price": exit_price,
                "status": "CLOSED", "exit_reason": reason,
                "realized_pnl": pnl, "realized_pnl_pct": pnl_pct,
            })
            return row

    def get_manual_trades(self, limit: int = 200) -> List[Dict[str, Any]]:
        """Return manual trades (recommendation_source starts with MANUAL)."""
        sql = """
            SELECT * FROM paper_trades
            WHERE recommendation_source LIKE 'MANUAL%'
            ORDER BY id DESC LIMIT ?
        """
        with self._get_connection() as conn:
            rows = conn.execute(sql, (limit,)).fetchall()
            return [dict(r) for r in rows]

    def get_manual_performance(self) -> Dict[str, Any]:
        """Aggregate manual trade performance metrics."""
        prefix = "MANUAL%"
        with self._get_connection() as conn:
            open_count = conn.execute(
                "SELECT COUNT(*) AS c FROM paper_trades WHERE status = 'OPEN' AND recommendation_source LIKE ?",
                (prefix,),
            ).fetchone()["c"]
            closed_count = conn.execute(
                "SELECT COUNT(*) AS c FROM paper_trades WHERE status = 'CLOSED' AND recommendation_source LIKE ?",
                (prefix,),
            ).fetchone()["c"]
            realized = conn.execute(
                "SELECT COALESCE(SUM(realized_pnl), 0) AS v FROM paper_trades WHERE status = 'CLOSED' AND recommendation_source LIKE ?",
                (prefix,),
            ).fetchone()["v"]
            wins = conn.execute(
                "SELECT COUNT(*) AS c FROM paper_trades WHERE status = 'CLOSED' AND realized_pnl > 0 AND recommendation_source LIKE ?",
                (prefix,),
            ).fetchone()["c"]
            losses = conn.execute(
                "SELECT COUNT(*) AS c FROM paper_trades WHERE status = 'CLOSED' AND realized_pnl < 0 AND recommendation_source LIKE ?",
                (prefix,),
            ).fetchone()["c"]
        win_rate = round((wins / closed_count) * 100, 1) if closed_count else 0.0
        return {
            "open_positions": int(open_count),
            "closed_trades": int(closed_count),
            "wins": int(wins),
            "losses": int(losses),
            "win_rate": win_rate,
            "realized_pnl": round(float(realized), 2),
        }

    # ------------------------------------------------------------------
    # Idea-linked trade operations
    # ------------------------------------------------------------------

    @property
    def idea_ledger(self):
        """Access the IdeaLedger instance."""
        if not hasattr(self, "_idea_ledger"):
            from signal_scanner.paper.idea_ledger import IdeaLedger
            self._idea_ledger = IdeaLedger(self._db_path)
        return self._idea_ledger

    def create_trade_from_idea(self, idea_id: int,
                               overrides: Dict[str, Any] = None) -> Optional[int]:
        """Create a paper trade linked to an idea. Returns trade_id or None.

        Populates trade fields from the idea snapshot, applies overrides,
        then transitions idea to ENTERED.
        """
        idea = self.idea_ledger.get_idea(idea_id)
        if not idea:
            logger.warning(f"create_trade_from_idea: idea {idea_id} not found")
            return None
        if idea["state"] not in ("NEW", "ACTIVE", "WATCHING"):
            logger.warning(f"create_trade_from_idea: idea {idea_id} state={idea['state']}, cannot enter")
            return None

        now = datetime.now(timezone.utc).isoformat()
        data = {
            "opened_at": now,
            "symbol": idea["symbol"],
            "side": idea["side"],
            "entry_price": idea["entry_price"],
            "stop_loss": idea["stop_loss"],
            "target_1": idea["target_1"],
            "target_2": idea["target_2"],
            "quantity": 0,  # must be set by caller or override
            "recommendation_source": "MANUAL",
            "strategy_type": "IDEA",
            "entry_score": idea.get("conviction"),
            "entry_rr_ratio": idea.get("rr_ratio"),
            "entry_market_regime": idea.get("market_regime"),
            "entry_trade_conditions": (
                f"IdeaID={idea_id} | Conv={idea.get('conviction')} | "
                f"Phase={idea.get('accum_phase')} | ML={idea.get('ml_score')}"
            ),
            "created_ts": now,
        }
        if overrides:
            data.update(overrides)

        # Size position if quantity not provided
        if not data.get("quantity") or data["quantity"] == 0:
            price = float(data.get("entry_price") or 0)
            if price > 0:
                from math import ceil
                data["quantity"] = ceil(10_000 / price) if price >= 10 else 1000

        trade_id = self.create_manual_trade(data)

        # Link idea → trade
        with self._get_connection() as conn:
            conn.execute(
                "UPDATE paper_trades SET idea_id = ? WHERE id = ?",
                (idea_id, trade_id),
            )

        # Transition idea to ENTERED
        self.idea_ledger.mark_entered(idea_id, trade_id)
        logger.info(f"Trade {trade_id} created from idea {idea_id} ({idea['symbol']} {idea['side']})")
        return trade_id

    def close_trade_and_update_idea(
        self, trade_id: int, exit_price: float,
        exit_reason: str = "MANUAL_EXIT", fees: float = 0,
    ) -> Optional[Dict[str, Any]]:
        """Close a trade and propagate outcome back to its linked idea."""
        result = self.close_manual_trade(
            trade_id,
            closed_at=datetime.now(timezone.utc).isoformat(),
            exit_price=exit_price,
            fees=fees,
            notes=exit_reason,
        )
        if not result:
            return None

        # If trade was linked to an idea, close the idea too
        idea_id = result.get("idea_id")
        if idea_id:
            self.idea_ledger.mark_closed(
                idea_id,
                exit_price=exit_price,
                pnl=result.get("realized_pnl"),
                pnl_pct=result.get("realized_pnl_pct"),
            )
        return result

    def get_unified_ledger(self, status: str = None,
                           limit: int = 200) -> List[Dict[str, Any]]:
        """Unified view of all trades (auto + manual) with idea linkage.

        Joins paper_trades LEFT JOIN ideas to show idea context alongside
        trade data. Returns newest first.
        """
        sql = """
            SELECT t.*,
                   i.state as idea_state,
                   i.source as idea_source,
                   i.confirm_count as idea_confirms,
                   i.first_seen_at as idea_first_seen,
                   i.ev_score as idea_ev_score
            FROM paper_trades t
            LEFT JOIN ideas i ON t.idea_id = i.id
        """
        params: list = []
        if status:
            sql += " WHERE t.status = ?"
            params.append(status)
        sql += " ORDER BY t.id DESC LIMIT ?"
        params.append(limit)

        with self._get_connection() as conn:
            rows = conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Option setups
    # ------------------------------------------------------------------

    def upsert_option_setup(self, row: Dict[str, Any]) -> None:
        """Insert/update a candidate option setup."""
        sql = """
            INSERT INTO option_setups
                (symbol, option_type, expiry_date, strike, underlying_price,
                 recommendation, signal, score, rr_ratio, market_regime, gex_status,
                 option_bid, option_ask, option_last, option_mid, option_spread_pct,
                 option_volume, option_open_interest, quote_ts, liquidity_score, liquidity_state,
                 rationale, idea_state, confirm_count, invalid_reason, status, created_ts, updated_ts)
            VALUES
                (:symbol, :option_type, :expiry_date, :strike, :underlying_price,
                 :recommendation, :signal, :score, :rr_ratio, :market_regime, :gex_status,
                 :option_bid, :option_ask, :option_last, :option_mid, :option_spread_pct,
                 :option_volume, :option_open_interest, :quote_ts, :liquidity_score, :liquidity_state,
                 :rationale, :idea_state, :confirm_count, :invalid_reason, :status, :created_ts, :updated_ts)
            ON CONFLICT(symbol, option_type, expiry_date, strike)
            DO UPDATE SET
                underlying_price = excluded.underlying_price,
                recommendation = excluded.recommendation,
                signal = excluded.signal,
                score = excluded.score,
                rr_ratio = excluded.rr_ratio,
                market_regime = excluded.market_regime,
                gex_status = excluded.gex_status,
                option_bid = excluded.option_bid,
                option_ask = excluded.option_ask,
                option_last = excluded.option_last,
                option_mid = excluded.option_mid,
                option_spread_pct = excluded.option_spread_pct,
                option_volume = excluded.option_volume,
                option_open_interest = excluded.option_open_interest,
                quote_ts = excluded.quote_ts,
                liquidity_score = excluded.liquidity_score,
                liquidity_state = excluded.liquidity_state,
                rationale = excluded.rationale,
                idea_state = excluded.idea_state,
                confirm_count = option_setups.confirm_count + 1,
                invalid_reason = excluded.invalid_reason,
                status = excluded.status,
                updated_ts = excluded.updated_ts
        """
        with self._get_connection() as conn:
            conn.execute(sql, row)

    def get_option_setups(self, status: str = "ACTIVE", limit: int = 200) -> List[Dict[str, Any]]:
        """Fetch persisted option setups."""
        sql = """
            SELECT *
            FROM option_setups
            WHERE status = ?
            ORDER BY updated_ts DESC, score DESC
            LIMIT ?
        """
        with self._get_connection() as conn:
            rows = conn.execute(sql, (status, limit)).fetchall()
            return [dict(row) for row in rows]

    def get_recent_option_setups_for_symbol(
        self,
        symbol: str,
        option_type: str,
        status: str = "ACTIVE",
        limit: int = 10,
    ) -> List[Dict[str, Any]]:
        """Fetch recent setups for one symbol and option direction."""
        sql = """
            SELECT *
            FROM option_setups
            WHERE status = ? AND symbol = ? AND option_type = ?
            ORDER BY updated_ts DESC
            LIMIT ?
        """
        with self._get_connection() as conn:
            rows = conn.execute(
                sql,
                (
                    str(status or "ACTIVE").upper(),
                    str(symbol or "").upper(),
                    str(option_type or "").upper(),
                    int(limit),
                ),
            ).fetchall()
            return [dict(row) for row in rows]

    def expire_old_option_setups(self, keep_days: int = 5) -> int:
        """Mark stale setups as EXPIRED to keep dashboard focused."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=keep_days)).isoformat()
        sql = """
            UPDATE option_setups
            SET status = 'EXPIRED', idea_state = 'EXPIRED', updated_ts = ?
            WHERE status = 'ACTIVE' AND is_taken = 0 AND updated_ts < ?
        """
        now_iso = datetime.now(timezone.utc).isoformat()
        with self._get_connection() as conn:
            cur = conn.execute(sql, (now_iso, cutoff))
            return int(cur.rowcount or 0)

    def get_option_setups_for_validation(self, status: str = "ACTIVE") -> List[Dict[str, Any]]:
        """Return option setup rows needed for lifecycle validation."""
        sql = """
            SELECT id, symbol, option_type, expiry_date, strike, underlying_price,
                   recommendation, signal, score, rr_ratio,
                   market_regime, gex_status,
                   option_bid, option_ask, option_last, option_mid, option_spread_pct,
                   option_volume, option_open_interest, quote_ts, liquidity_score, liquidity_state,
                   idea_state, invalid_reason,
                   is_taken, status, created_ts, updated_ts, confirm_count
            FROM option_setups
            WHERE status = ?
            ORDER BY updated_ts DESC
        """
        with self._get_connection() as conn:
            rows = conn.execute(sql, (status,)).fetchall()
            return [dict(row) for row in rows]

    def update_option_setup_state(
        self,
        setup_id: int,
        idea_state: str,
        invalid_reason: str = "",
        validated_ts: Optional[str] = None,
    ) -> None:
        """Persist lifecycle validation state for one option setup."""
        state = str(idea_state or "ACTIVE").upper()
        valid = {"NEW", "STRONG", "ACTIVE", "WEAKENING", "INVALID", "EXPIRED"}
        if state not in valid:
            state = "ACTIVE"
        reason = str(invalid_reason or "").strip()
        ts = validated_ts or datetime.now(timezone.utc).isoformat()
        sql = """
            UPDATE option_setups
            SET idea_state = ?, invalid_reason = ?, last_validated_ts = ?, updated_ts = ?
            WHERE id = ?
        """
        with self._get_connection() as conn:
            conn.execute(sql, (state, reason, ts, ts, int(setup_id)))

    def set_option_setup_taken(self, setup_id: int, is_taken: bool) -> None:
        """Mark/unmark an option idea as taken by user."""
        now_iso = datetime.now(timezone.utc).isoformat()
        sql = """
            UPDATE option_setups
            SET is_taken = ?,
                taken_at = CASE WHEN ? = 1 THEN COALESCE(taken_at, ?) ELSE NULL END,
                updated_ts = ?
            WHERE id = ?
        """
        flag = 1 if bool(is_taken) else 0
        with self._get_connection() as conn:
            conn.execute(sql, (flag, flag, now_iso, now_iso, int(setup_id)))

    def has_open_option_trade(
        self,
        symbol: str,
        option_type: str,
        option_expiry: str,
        option_strike: float,
    ) -> bool:
        """Return True if an OPEN OPTION paper trade already exists for same contract."""
        sql = """
            SELECT 1
            FROM paper_trades
            WHERE status = 'OPEN'
              AND instrument_type = 'OPTION'
              AND symbol = ?
              AND option_type = ?
              AND option_expiry = ?
              AND ABS(COALESCE(option_strike, 0) - ?) < 0.0001
            LIMIT 1
        """
        with self._get_connection() as conn:
            row = conn.execute(
                sql,
                (
                    str(symbol or "").upper(),
                    str(option_type or "").upper(),
                    str(option_expiry or ""),
                    float(option_strike or 0.0),
                ),
            ).fetchone()
            return row is not None

    def evaluate_option_setup_outcomes(self, horizons_minutes: Optional[List[int]] = None) -> int:
        """Evaluate realized directional outcomes for option ideas using future underlying prices."""
        horizons = sorted({int(h) for h in (horizons_minutes or [30, 60, 1440]) if int(h) > 0})
        if not horizons:
            return 0

        inserted = 0
        now_utc = datetime.now(timezone.utc)
        with self._get_connection() as conn:
            setups = conn.execute(
                """
                SELECT id, symbol, option_type, created_ts, underlying_price
                FROM option_setups
                WHERE status IN ('ACTIVE', 'EXPIRED')
                """
            ).fetchall()
            existing = conn.execute(
                "SELECT setup_id, horizon_minutes FROM option_setup_outcomes"
            ).fetchall()
            existing_keys = {(int(r["setup_id"]), int(r["horizon_minutes"])) for r in existing}

            for s in setups:
                setup_id = int(s["id"])
                symbol = str(s["symbol"] or "").upper()
                option_type = str(s["option_type"] or "").upper()
                created_dt = self._parse_iso(s["created_ts"])
                entry_px = self._as_float(s["underlying_price"])
                if not symbol or option_type not in ("CALL", "PUT") or created_dt is None or entry_px is None or entry_px <= 0:
                    continue

                for horizon in horizons:
                    key = (setup_id, horizon)
                    if key in existing_keys:
                        continue
                    target_dt = created_dt + timedelta(minutes=horizon)
                    if target_dt > now_utc:
                        continue

                    row = conn.execute(
                        """
                        SELECT price, timestamp
                        FROM signals
                        WHERE symbol = ?
                          AND price IS NOT NULL
                          AND timestamp >= ?
                        ORDER BY timestamp ASC
                        LIMIT 1
                        """,
                        (symbol, target_dt.isoformat()),
                    ).fetchone()
                    if not row:
                        continue

                    future_px = self._as_float(row["price"])
                    if future_px is None or future_px <= 0:
                        continue

                    move_pct = ((future_px - entry_px) / entry_px) * 100.0
                    signed = move_pct if option_type == "CALL" else -move_pct
                    if signed > 0:
                        outcome = "WIN"
                        hit = 1
                    elif signed < 0:
                        outcome = "LOSS"
                        hit = 0
                    else:
                        outcome = "FLAT"
                        hit = 0

                    conn.execute(
                        """
                        INSERT OR IGNORE INTO option_setup_outcomes
                            (setup_id, symbol, option_type, horizon_minutes,
                             entry_underlying, future_underlying, move_pct, signed_move_pct,
                             outcome, hit, evaluated_at, source_signal_ts)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            setup_id,
                            symbol,
                            option_type,
                            horizon,
                            round(entry_px, 6),
                            round(future_px, 6),
                            round(move_pct, 6),
                            round(signed, 6),
                            outcome,
                            hit,
                            datetime.now(timezone.utc).isoformat(),
                            row["timestamp"],
                        ),
                    )
                    inserted += 1
                    existing_keys.add(key)
        return inserted

    # ------------------------------------------------------------------
    # End-of-day analysis
    # ------------------------------------------------------------------

    def get_closed_paper_trades_for_date(self, trade_date: str) -> List[Dict[str, Any]]:
        """Fetch CLOSED trades whose close date matches YYYY-MM-DD."""
        sql = """
            SELECT *
            FROM paper_trades
            WHERE status = 'CLOSED' AND substr(closed_at, 1, 10) = ?
            ORDER BY id ASC
        """
        with self._get_connection() as conn:
            rows = conn.execute(sql, (trade_date,)).fetchall()
            return [dict(row) for row in rows]

    def upsert_eod_analysis(self, row: Dict[str, Any]) -> None:
        """Persist EOD analytics for one date."""
        sql = """
            INSERT INTO eod_analysis
                (trade_date, total_trades, wins, losses, win_rate, realized_pnl,
                 avg_loss, max_loss, top_loss_reason, insights_json, suggested_actions,
                 action_status, action_notes, action_updated_ts, created_ts)
            VALUES
                (:trade_date, :total_trades, :wins, :losses, :win_rate, :realized_pnl,
                 :avg_loss, :max_loss, :top_loss_reason, :insights_json, :suggested_actions,
                 :action_status, :action_notes, :action_updated_ts, :created_ts)
            ON CONFLICT(trade_date)
            DO UPDATE SET
                total_trades = excluded.total_trades,
                wins = excluded.wins,
                losses = excluded.losses,
                win_rate = excluded.win_rate,
                realized_pnl = excluded.realized_pnl,
                avg_loss = excluded.avg_loss,
                max_loss = excluded.max_loss,
                top_loss_reason = excluded.top_loss_reason,
                insights_json = excluded.insights_json,
                suggested_actions = excluded.suggested_actions,
                action_status = CASE
                    WHEN eod_analysis.action_status = 'PENDING' THEN excluded.action_status
                    ELSE eod_analysis.action_status
                END,
                created_ts = excluded.created_ts
        """
        payload = {
            **row,
            "action_status": row.get("action_status", "PENDING"),
            "action_notes": row.get("action_notes"),
            "action_updated_ts": row.get("action_updated_ts"),
        }
        with self._get_connection() as conn:
            conn.execute(sql, payload)

    def get_recent_eod_analysis(self, limit: int = 30) -> List[Dict[str, Any]]:
        """Return recent EOD summaries for dashboard."""
        sql = "SELECT * FROM eod_analysis ORDER BY trade_date DESC LIMIT ?"
        with self._get_connection() as conn:
            rows = conn.execute(sql, (limit,)).fetchall()
            out = []
            for r in rows:
                row = dict(r)
                if row.get("insights_json"):
                    try:
                        row["insights"] = json.loads(row["insights_json"])
                    except json.JSONDecodeError:
                        row["insights"] = {}
                else:
                    row["insights"] = {}
                out.append(row)
            return out

    def get_latest_completed_eod_analysis(self, current_trade_date: str) -> Optional[Dict[str, Any]]:
        """Return latest EOD row strictly before current_trade_date (YYYY-MM-DD)."""
        sql = """
            SELECT *
            FROM eod_analysis
            WHERE trade_date < ?
            ORDER BY trade_date DESC
            LIMIT 1
        """
        with self._get_connection() as conn:
            row = conn.execute(sql, (current_trade_date,)).fetchone()
        if not row:
            return None
        out = dict(row)
        if out.get("insights_json"):
            try:
                out["insights"] = json.loads(out["insights_json"])
            except json.JSONDecodeError:
                out["insights"] = {}
        else:
            out["insights"] = {}
        return out

    def update_eod_action_status(
        self,
        trade_date: str,
        action_status: str,
        action_notes: str = "",
    ) -> None:
        """Update review workflow decision for one EOD date."""
        valid = {"PENDING", "IMPLEMENT", "WATCH", "IGNORE"}
        status = str(action_status or "PENDING").upper()
        if status not in valid:
            status = "PENDING"
        notes = str(action_notes or "").strip()
        now_iso = datetime.now(timezone.utc).isoformat()
        sql = """
            UPDATE eod_analysis
            SET action_status = ?, action_notes = ?, action_updated_ts = ?
            WHERE trade_date = ?
        """
        with self._get_connection() as conn:
            conn.execute(sql, (status, notes, now_iso, trade_date))

    # ------------------------------------------------------------------
    # Persisted alert quality metrics
    # ------------------------------------------------------------------

    def get_signal_alert_success_metrics(
        self,
        lookback_days: int = 7,
        horizon_minutes: int = 30,
        min_score: int = 60,
    ) -> Dict[str, Any]:
        """Evaluate BUY/SELL alerts using future persisted prices after a fixed horizon."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).isoformat()
        sql = """
            SELECT symbol, timeframe, timestamp, recommendation, price, score
            FROM signals
            WHERE recommendation IN ('BUY', 'SELL')
              AND price IS NOT NULL
              AND score >= ?
              AND timestamp >= ?
            ORDER BY symbol ASC, timeframe ASC, timestamp ASC
        """
        with self._get_connection() as conn:
            rows = [dict(r) for r in conn.execute(sql, (min_score, cutoff)).fetchall()]

        grouped: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
        for r in rows:
            key = (str(r.get("symbol")), str(r.get("timeframe")))
            grouped.setdefault(key, []).append(r)

        evaluated = 0
        wins = 0
        losses = 0
        move_pcts: List[float] = []

        for _, seq in grouped.items():
            n = len(seq)
            for i in range(n):
                cur = seq[i]
                ts = self._parse_iso(cur.get("timestamp"))
                px = self._as_float(cur.get("price"))
                rec = cur.get("recommendation")
                if ts is None or px is None or px <= 0 or rec not in ("BUY", "SELL"):
                    continue

                target = ts + timedelta(minutes=horizon_minutes)
                fut = None
                for j in range(i + 1, n):
                    ts2 = self._parse_iso(seq[j].get("timestamp"))
                    px2 = self._as_float(seq[j].get("price"))
                    if ts2 is None or px2 is None or px2 <= 0:
                        continue
                    if ts2 >= target:
                        fut = px2
                        break
                if fut is None:
                    continue

                move_pct = ((fut - px) / px) * 100.0
                signed = move_pct if rec == "BUY" else -move_pct
                move_pcts.append(signed)
                evaluated += 1
                if signed > 0:
                    wins += 1
                elif signed < 0:
                    losses += 1

        win_rate = round((wins / evaluated) * 100.0, 1) if evaluated else 0.0
        avg_move = round(sum(move_pcts) / len(move_pcts), 3) if move_pcts else 0.0
        return {
            "evaluated_alerts": evaluated,
            "wins": wins,
            "losses": losses,
            "win_rate": win_rate,
            "avg_signed_move_pct": avg_move,
            "horizon_minutes": horizon_minutes,
            "lookback_days": lookback_days,
            "min_score": min_score,
        }

    @staticmethod
    def _parse_iso(value: Any) -> Optional[datetime]:
        try:
            if not value:
                return None
            return datetime.fromisoformat(str(value))
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _as_float(value: Any) -> Optional[float]:
        try:
            if value is None:
                return None
            return float(value)
        except (TypeError, ValueError):
            return None

    # ------------------------------------------------------------------
    # Maintenance
    # ------------------------------------------------------------------

    def cleanup_old_signals(self, days: int = 7) -> int:
        """Delete signals older than *days*. Returns number of rows deleted."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        sql = "DELETE FROM signals WHERE timestamp < ?"
        with self._get_connection() as conn:
            cursor = conn.execute(sql, (cutoff,))
            deleted = cursor.rowcount
        if deleted:
            logger.info(f"Cleaned up {deleted} signals older than {days} days")
        return deleted
