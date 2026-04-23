"""Idea Ledger — persistent idea lifecycle management.

Ideas flow through a state machine:

    NEW → ACTIVE → WATCHING → ENTERED → CLOSED
                 ↘ INVALIDATED
                 ↘ EXPIRED
                 ↘ ARCHIVED

State definitions:
    NEW         — just generated, not yet confirmed by a second scan cycle
    ACTIVE      — confirmed (seen in 2+ consecutive cycles), eligible for entry
    WATCHING    — operator marked for monitoring but not entering
    ENTERED     — a trade has been opened (auto or manual), linked via trade_id
    INVALIDATED — setup conditions broke (phase change, stop blown pre-entry, etc.)
    EXPIRED     — exceeded max_age_days without being entered
    CLOSED      — trade linked to this idea has been closed
    ARCHIVED    — operator dismissed or old idea cleaned up

Persistence:
    SQLite table `ideas` in the same signals.db used by paper_trades.

Carryover policy:
    - Ideas persist across days until explicitly expired/invalidated/archived.
    - ACTIVE ideas auto-expire after MAX_IDEA_AGE_DAYS (default 5).
    - At start of day, _daily_housekeeping() runs expiration + revalidation.
    - Ideas that generated a trade (ENTERED/CLOSED) are never auto-expired.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

from loguru import logger


# ---------------------------------------------------------------------------
# Idea lifecycle states
# ---------------------------------------------------------------------------

class IdeaState(str, Enum):
    NEW = "NEW"
    ACTIVE = "ACTIVE"
    WATCHING = "WATCHING"
    ENTERED = "ENTERED"
    INVALIDATED = "INVALIDATED"
    EXPIRED = "EXPIRED"
    CLOSED = "CLOSED"
    ARCHIVED = "ARCHIVED"


# Valid state transitions
TRANSITIONS: Dict[IdeaState, set] = {
    IdeaState.NEW: {IdeaState.ACTIVE, IdeaState.INVALIDATED, IdeaState.EXPIRED, IdeaState.ARCHIVED},
    IdeaState.ACTIVE: {IdeaState.WATCHING, IdeaState.ENTERED, IdeaState.INVALIDATED, IdeaState.EXPIRED, IdeaState.ARCHIVED},
    IdeaState.WATCHING: {IdeaState.ACTIVE, IdeaState.ENTERED, IdeaState.INVALIDATED, IdeaState.EXPIRED, IdeaState.ARCHIVED},
    IdeaState.ENTERED: {IdeaState.CLOSED},
    IdeaState.INVALIDATED: {IdeaState.ARCHIVED},
    IdeaState.EXPIRED: {IdeaState.ARCHIVED},
    IdeaState.CLOSED: {IdeaState.ARCHIVED},
    IdeaState.ARCHIVED: set(),  # terminal
}

# Which states are "alive" (visible on sniper board, eligible for entry)
ALIVE_STATES = {IdeaState.NEW, IdeaState.ACTIVE, IdeaState.WATCHING}

# Which states are terminal
TERMINAL_STATES = {IdeaState.INVALIDATED, IdeaState.EXPIRED, IdeaState.CLOSED, IdeaState.ARCHIVED}


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

MAX_IDEA_AGE_DAYS = 5
CONFIRM_CYCLES_TO_ACTIVATE = 2  # seen in N cycles → NEW→ACTIVE


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

CREATE_IDEAS_TABLE = """
CREATE TABLE IF NOT EXISTS ideas (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol              TEXT    NOT NULL,
    side                TEXT    NOT NULL,          -- LONG or SHORT
    source              TEXT    NOT NULL,          -- SWING_IDEA_BUY, AI_TRIPLE_LOCK, etc.
    state               TEXT    NOT NULL DEFAULT 'NEW',

    -- Snapshot at idea creation
    entry_price         REAL,
    stop_loss           REAL,
    target_1            REAL,
    target_2            REAL,
    rr_ratio            REAL,
    conviction          REAL,
    ml_score            REAL,
    accum_phase         TEXT,
    market_regime       TEXT,
    squeeze_score       REAL,
    ev_score            REAL,

    -- Lifecycle tracking
    confirm_count       INTEGER NOT NULL DEFAULT 1,
    first_seen_at       TEXT    NOT NULL,          -- ISO timestamp
    last_seen_at        TEXT    NOT NULL,          -- ISO timestamp, updated each cycle
    activated_at        TEXT,                       -- when NEW→ACTIVE
    entered_at          TEXT,                       -- when trade opened
    closed_at           TEXT,                       -- when trade closed
    invalidated_at      TEXT,
    expired_at          TEXT,
    archived_at         TEXT,

    -- Trade linkage
    trade_id            INTEGER,                   -- FK to paper_trades.id (NULL until entered)
    trade_exit_price    REAL,
    trade_pnl           REAL,
    trade_pnl_pct       REAL,

    -- Daily status (fast layer — never mutates thesis)
    daily_status        TEXT    DEFAULT 'ACTIVE',     -- ACTIVE/RECONFIRMED/STRETCHED/MISSED/INVALIDATED/STALE
    daily_status_reason TEXT,
    daily_status_at     TEXT,                          -- when last revalidated
    thesis_price        REAL,                          -- price when thesis was first observable
    thesis_date         TEXT,                          -- date thesis became observable

    -- Invalidation / notes
    invalid_reason      TEXT,
    operator_notes      TEXT,

    -- Dedup key: symbol + side + source + date(first_seen_at)
    created_ts          TEXT    NOT NULL,
    UNIQUE(symbol, side, source, first_seen_at)
);
"""

CREATE_IDEAS_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_ideas_state ON ideas(state);",
    "CREATE INDEX IF NOT EXISTS idx_ideas_symbol ON ideas(symbol);",
    "CREATE INDEX IF NOT EXISTS idx_ideas_trade_id ON ideas(trade_id);",
]


# ---------------------------------------------------------------------------
# IdeaLedger
# ---------------------------------------------------------------------------

class IdeaLedger:
    """Persistent idea lifecycle manager backed by SQLite."""

    def __init__(self, db_path: str):
        self._db_path = db_path
        self._init_schema()

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_schema(self) -> None:
        with self._get_conn() as conn:
            conn.execute(CREATE_IDEAS_TABLE)
            for idx in CREATE_IDEAS_INDEXES:
                conn.execute(idx)

    # ------------------------------------------------------------------
    # Core operations
    # ------------------------------------------------------------------

    def upsert_idea(self, idea: Dict[str, Any]) -> int:
        """Insert a new idea or bump confirm_count if already exists today.

        Returns the idea ID.
        """
        now = datetime.now(timezone.utc).isoformat()
        symbol = idea["symbol"]
        side = idea.get("side", "LONG")
        source = idea["source"]

        with self._get_conn() as conn:
            # Check for existing alive idea (same symbol+side+source)
            existing = conn.execute(
                """SELECT id, state, confirm_count FROM ideas
                   WHERE symbol = ? AND side = ? AND source = ?
                   AND state IN ('NEW', 'ACTIVE', 'WATCHING')
                   ORDER BY id DESC LIMIT 1""",
                (symbol, side, source),
            ).fetchone()

            if existing:
                idea_id = existing["id"]
                new_count = existing["confirm_count"] + 1
                new_state = existing["state"]

                # Auto-promote NEW → ACTIVE after enough confirmations
                if (new_state == IdeaState.NEW.value
                        and new_count >= CONFIRM_CYCLES_TO_ACTIVATE):
                    new_state = IdeaState.ACTIVE.value
                    conn.execute(
                        "UPDATE ideas SET state = ?, activated_at = ? WHERE id = ?",
                        (new_state, now, idea_id),
                    )

                # Update confirmation + snapshot
                conn.execute(
                    """UPDATE ideas SET
                        confirm_count = ?,
                        last_seen_at = ?,
                        entry_price = COALESCE(?, entry_price),
                        stop_loss = COALESCE(?, stop_loss),
                        target_1 = COALESCE(?, target_1),
                        target_2 = COALESCE(?, target_2),
                        rr_ratio = COALESCE(?, rr_ratio),
                        conviction = COALESCE(?, conviction),
                        ml_score = COALESCE(?, ml_score),
                        accum_phase = COALESCE(?, accum_phase),
                        market_regime = COALESCE(?, market_regime),
                        squeeze_score = COALESCE(?, squeeze_score),
                        ev_score = COALESCE(?, ev_score),
                        state = ?
                    WHERE id = ?""",
                    (
                        new_count, now,
                        idea.get("entry_price"), idea.get("stop_loss"),
                        idea.get("target_1"), idea.get("target_2"),
                        idea.get("rr_ratio"), idea.get("conviction"),
                        idea.get("ml_score"), idea.get("accum_phase"),
                        idea.get("market_regime"), idea.get("squeeze_score"),
                        idea.get("ev_score"),
                        new_state, idea_id,
                    ),
                )
                return idea_id
            else:
                # Insert new
                cursor = conn.execute(
                    """INSERT INTO ideas
                        (symbol, side, source, state,
                         entry_price, stop_loss, target_1, target_2,
                         rr_ratio, conviction, ml_score, accum_phase,
                         market_regime, squeeze_score, ev_score,
                         confirm_count, first_seen_at, last_seen_at,
                         created_ts)
                    VALUES (?, ?, ?, 'NEW',
                            ?, ?, ?, ?,
                            ?, ?, ?, ?,
                            ?, ?, ?,
                            1, ?, ?, ?)""",
                    (
                        symbol, side, source,
                        idea.get("entry_price"), idea.get("stop_loss"),
                        idea.get("target_1"), idea.get("target_2"),
                        idea.get("rr_ratio"), idea.get("conviction"),
                        idea.get("ml_score"), idea.get("accum_phase"),
                        idea.get("market_regime"), idea.get("squeeze_score"),
                        idea.get("ev_score"),
                        now, now, now,
                    ),
                )
                return cursor.lastrowid

    def transition(self, idea_id: int, new_state: IdeaState,
                   reason: str = "", trade_id: int = None) -> bool:
        """Transition an idea to a new state. Returns True if successful."""
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT state FROM ideas WHERE id = ?", (idea_id,)
            ).fetchone()
            if not row:
                logger.warning(f"IdeaLedger: idea {idea_id} not found")
                return False

            current = IdeaState(row["state"])
            if new_state not in TRANSITIONS.get(current, set()):
                logger.warning(
                    f"IdeaLedger: invalid transition {current}→{new_state} "
                    f"for idea {idea_id}"
                )
                return False

            now = datetime.now(timezone.utc).isoformat()
            updates = {"state": new_state.value}

            if new_state == IdeaState.ENTERED:
                updates["entered_at"] = now
                if trade_id:
                    updates["trade_id"] = trade_id
            elif new_state == IdeaState.CLOSED:
                updates["closed_at"] = now
            elif new_state == IdeaState.INVALIDATED:
                updates["invalidated_at"] = now
                updates["invalid_reason"] = reason
            elif new_state == IdeaState.EXPIRED:
                updates["expired_at"] = now
            elif new_state == IdeaState.ARCHIVED:
                updates["archived_at"] = now
            elif new_state == IdeaState.ACTIVE:
                updates["activated_at"] = now

            set_clause = ", ".join(f"{k} = ?" for k in updates)
            conn.execute(
                f"UPDATE ideas SET {set_clause} WHERE id = ?",
                list(updates.values()) + [idea_id],
            )
            return True

    def mark_entered(self, idea_id: int, trade_id: int) -> bool:
        """Shortcut: ACTIVE/WATCHING/NEW → ENTERED with trade linkage."""
        return self.transition(idea_id, IdeaState.ENTERED, trade_id=trade_id)

    def mark_closed(self, idea_id: int, exit_price: float = None,
                    pnl: float = None, pnl_pct: float = None) -> bool:
        """Mark idea CLOSED and record trade outcome."""
        ok = self.transition(idea_id, IdeaState.CLOSED)
        if ok and (exit_price is not None or pnl is not None):
            with self._get_conn() as conn:
                conn.execute(
                    """UPDATE ideas SET trade_exit_price = ?,
                       trade_pnl = ?, trade_pnl_pct = ? WHERE id = ?""",
                    (exit_price, pnl, pnl_pct, idea_id),
                )
        return ok

    def set_watching(self, idea_id: int) -> bool:
        """Operator marks idea for monitoring without entering."""
        return self.transition(idea_id, IdeaState.WATCHING)

    def invalidate(self, idea_id: int, reason: str) -> bool:
        """Mark idea invalid (phase changed, stop hit pre-entry, etc.)."""
        return self.transition(idea_id, IdeaState.INVALIDATED, reason=reason)

    def add_note(self, idea_id: int, note: str) -> None:
        """Append operator note to an idea."""
        with self._get_conn() as conn:
            conn.execute(
                """UPDATE ideas SET operator_notes =
                   CASE WHEN operator_notes IS NULL THEN ? ELSE operator_notes || '\n' || ? END
                   WHERE id = ?""",
                (note, note, idea_id),
            )

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def get_alive_ideas(self, side: str = None,
                        source: str = None) -> List[Dict[str, Any]]:
        """Get all ideas in NEW/ACTIVE/WATCHING state."""
        sql = "SELECT * FROM ideas WHERE state IN ('NEW', 'ACTIVE', 'WATCHING')"
        params: list = []
        if side:
            sql += " AND side = ?"
            params.append(side)
        if source:
            sql += " AND source = ?"
            params.append(source)
        sql += " ORDER BY ev_score DESC NULLS LAST, conviction DESC"
        with self._get_conn() as conn:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]

    def get_entered_ideas(self) -> List[Dict[str, Any]]:
        """Get ideas currently linked to open trades."""
        with self._get_conn() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM ideas WHERE state = 'ENTERED' ORDER BY entered_at DESC"
            ).fetchall()]

    def get_idea(self, idea_id: int) -> Optional[Dict[str, Any]]:
        """Get a single idea by ID."""
        with self._get_conn() as conn:
            row = conn.execute("SELECT * FROM ideas WHERE id = ?", (idea_id,)).fetchone()
            return dict(row) if row else None

    def get_idea_for_symbol(self, symbol: str, side: str = "LONG",
                            source: str = None) -> Optional[Dict[str, Any]]:
        """Get the most recent alive idea for a symbol+side."""
        sql = """SELECT * FROM ideas
                 WHERE symbol = ? AND side = ?
                 AND state IN ('NEW', 'ACTIVE', 'WATCHING')"""
        params: list = [symbol, side]
        if source:
            sql += " AND source = ?"
            params.append(source)
        sql += " ORDER BY id DESC LIMIT 1"
        with self._get_conn() as conn:
            row = conn.execute(sql, params).fetchone()
            return dict(row) if row else None

    def get_ideas_history(self, days: int = 30,
                          limit: int = 500) -> List[Dict[str, Any]]:
        """Get idea history (all states) for the last N days."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        with self._get_conn() as conn:
            return [dict(r) for r in conn.execute(
                """SELECT * FROM ideas WHERE created_ts >= ?
                   ORDER BY id DESC LIMIT ?""",
                (cutoff, limit),
            ).fetchall()]

    def get_idea_stats(self) -> Dict[str, Any]:
        """Summary statistics for operator dashboard."""
        with self._get_conn() as conn:
            counts = {}
            for row in conn.execute(
                "SELECT state, COUNT(*) as cnt FROM ideas GROUP BY state"
            ).fetchall():
                counts[row["state"]] = row["cnt"]

            # Win rate for ideas that became trades
            closed = conn.execute(
                """SELECT COUNT(*) as total,
                          SUM(CASE WHEN trade_pnl > 0 THEN 1 ELSE 0 END) as wins,
                          AVG(trade_pnl) as avg_pnl,
                          AVG(trade_pnl_pct) as avg_pnl_pct
                   FROM ideas WHERE state = 'CLOSED' AND trade_pnl IS NOT NULL"""
            ).fetchone()
            closed = dict(closed) if closed else {}

            return {
                "state_counts": counts,
                "total_alive": sum(counts.get(s.value, 0) for s in ALIVE_STATES),
                "total_entered": counts.get("ENTERED", 0) + counts.get("CLOSED", 0),
                "closed_total": closed.get("total", 0),
                "closed_wins": closed.get("wins", 0),
                "closed_avg_pnl": round(closed.get("avg_pnl") or 0, 2),
                "closed_avg_pnl_pct": round(closed.get("avg_pnl_pct") or 0, 2),
            }

    # ------------------------------------------------------------------
    # Carryover & housekeeping
    # ------------------------------------------------------------------

    def daily_housekeeping(self) -> Dict[str, int]:
        """Run at start of day. Expires old ideas, returns counts."""
        now = datetime.now(timezone.utc)
        cutoff = (now - timedelta(days=MAX_IDEA_AGE_DAYS)).isoformat()
        now_iso = now.isoformat()

        with self._get_conn() as conn:
            # Expire old NEW/ACTIVE/WATCHING ideas
            cursor = conn.execute(
                """UPDATE ideas SET state = 'EXPIRED', expired_at = ?
                   WHERE state IN ('NEW', 'ACTIVE', 'WATCHING')
                   AND first_seen_at < ?""",
                (now_iso, cutoff),
            )
            expired = cursor.rowcount

            return {"expired": expired}

    def revalidate_ideas(self, valid_symbols: set,
                         valid_phases: set = None) -> Dict[str, int]:
        """Invalidate ideas whose setup conditions have broken.

        Called after intelligence refresh to check if phase changed
        or symbol dropped from universe.
        """
        invalidated = 0
        now_iso = datetime.now(timezone.utc).isoformat()

        with self._get_conn() as conn:
            alive = conn.execute(
                "SELECT id, symbol, accum_phase FROM ideas WHERE state IN ('NEW', 'ACTIVE', 'WATCHING')"
            ).fetchall()

            for row in alive:
                symbol = row["symbol"]
                # Symbol no longer in universe
                if symbol not in valid_symbols:
                    conn.execute(
                        """UPDATE ideas SET state = 'INVALIDATED',
                           invalidated_at = ?, invalid_reason = 'dropped_from_universe'
                           WHERE id = ?""",
                        (now_iso, row["id"]),
                    )
                    invalidated += 1

        return {"invalidated": invalidated}
