"""Structured reason-coded telemetry for Quant-Bridge subsystems.

Provides first-class skip/block reason counters persisted to SQLite
(signal_scanner/data/signals.db) so the daily evidence report can show
exactly why each subsystem did or did not fire.

Reason codes are the canonical vocabulary; every subsystem uses these
instead of ad-hoc log messages.
"""

from __future__ import annotations

import sqlite3
import threading
from collections import defaultdict
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional


class SkipReason(str, Enum):
    """Canonical reason codes — shared across all subsystems."""

    DATA_STALE = "DATA_STALE"
    IBKR_DISCONNECTED = "IBKR_DISCONNECTED"
    ORPHAN_GATE = "ORPHAN_GATE"
    LOCK_TIMEOUT = "LOCK_TIMEOUT"
    NO_LIVE_UNIVERSE = "NO_LIVE_UNIVERSE"
    NO_SETUP_QUALIFIED = "NO_SETUP_QUALIFIED"
    MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"
    REGIME_BLOCKED = "REGIME_BLOCKED"
    POSITION_LIMIT = "POSITION_LIMIT"
    DUPLICATE_SYMBOL = "DUPLICATE_SYMBOL"
    LATE_ENTRY_CUTOFF = "LATE_ENTRY_CUTOFF"


class Subsystem(str, Enum):
    """Named subsystems that report telemetry."""

    EXECUTION_LOOP = "execution_loop"
    VWAP_MR = "VWAP_MR"
    FPB = "FPB"
    ORB_V2 = "ORB_V2"
    IDEA_BRIDGE = "IdeaBridge"
    PAPER_TRADER = "PaperTrader"
    ORDER_EXECUTOR = "OrderExecutor"


# In-memory counters (reset each session)
_counters: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
_lock = threading.Lock()

# SQLite path (co-located with signals.db)
_DB_PATH = Path("signal_scanner/data/signals.db")


def _ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS skip_telemetry (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          TEXT    NOT NULL,
            trade_date  TEXT    NOT NULL,
            subsystem   TEXT    NOT NULL,
            reason      TEXT    NOT NULL,
            detail      TEXT,
            count       INTEGER NOT NULL DEFAULT 1
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_skip_tel_date
        ON skip_telemetry(trade_date, subsystem)
    """)


def record_skip(
    subsystem: str,
    reason: str,
    detail: str = "",
    *,
    persist: bool = True,
) -> None:
    """Record a structured skip event.

    Args:
        subsystem: One of Subsystem enum values (or any string).
        reason:    One of SkipReason enum values (or any string).
        detail:    Optional human-readable context.
        persist:   If True, write to SQLite in addition to in-memory counter.
    """
    # Normalize enum values to plain strings for clean dict keys
    sub_key = subsystem.value if hasattr(subsystem, "value") else str(subsystem)
    reason_key = reason.value if hasattr(reason, "value") else str(reason)

    with _lock:
        _counters[sub_key][reason_key] += 1

    if persist:
        _persist(sub_key, reason_key, detail)


def get_session_counters() -> Dict[str, Dict[str, int]]:
    """Return a snapshot of in-memory counters for the current session."""
    with _lock:
        return {sub: dict(reasons) for sub, reasons in _counters.items()}


def reset_session_counters() -> None:
    """Clear in-memory counters (typically at session start)."""
    with _lock:
        _counters.clear()


def get_daily_summary(trade_date: Optional[str] = None) -> List[dict]:
    """Return persisted skip events for a given date (default: today).

    Returns list of dicts: {subsystem, reason, detail, count, first_ts, last_ts}
    """
    if trade_date is None:
        trade_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    try:
        conn = sqlite3.connect(str(_DB_PATH))
        _ensure_table(conn)
        rows = conn.execute("""
            SELECT subsystem, reason,
                   GROUP_CONCAT(DISTINCT detail) AS details,
                   SUM(count)                    AS total,
                   MIN(ts)                       AS first_ts,
                   MAX(ts)                       AS last_ts
            FROM skip_telemetry
            WHERE trade_date = ?
            GROUP BY subsystem, reason
            ORDER BY subsystem, total DESC
        """, (trade_date,)).fetchall()
        conn.close()
        return [
            {
                "subsystem": r[0], "reason": r[1], "detail": r[2],
                "count": r[3], "first_ts": r[4], "last_ts": r[5],
            }
            for r in rows
        ]
    except Exception:
        return []


def _persist(subsystem: str, reason: str, detail: str) -> None:
    now = datetime.now(timezone.utc)
    trade_date = now.strftime("%Y-%m-%d")
    try:
        conn = sqlite3.connect(str(_DB_PATH))
        _ensure_table(conn)
        conn.execute(
            "INSERT INTO skip_telemetry (ts, trade_date, subsystem, reason, detail) "
            "VALUES (?, ?, ?, ?, ?)",
            (now.isoformat(), trade_date, subsystem, reason, detail),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass  # telemetry must never crash the host


# ===================================================================
# TRADE FUNNEL ACCOUNTING
# ===================================================================
# Tracks: candidates → setups → attempted → entered → skipped → closed
# per subsystem per day.  In-memory + persisted to SQLite.

_funnel: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
_funnel_lock = threading.Lock()

# Canonical funnel stages
FUNNEL_CANDIDATES = "candidates"
FUNNEL_SETUPS = "setups"
FUNNEL_ATTEMPTED = "attempted"
FUNNEL_ENTERED = "entered"
FUNNEL_SKIPPED = "skipped"


def _ensure_funnel_table(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS trade_funnel (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_date  TEXT    NOT NULL,
            subsystem   TEXT    NOT NULL,
            stage       TEXT    NOT NULL,
            count       INTEGER NOT NULL DEFAULT 0,
            updated_at  TEXT    NOT NULL
        )
    """)
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_funnel_key
        ON trade_funnel(trade_date, subsystem, stage)
    """)


def record_funnel(subsystem: str, stage: str, increment: int = 1) -> None:
    """Increment a funnel stage counter for a subsystem.

    Args:
        subsystem: e.g. Subsystem.VWAP_MR or "IdeaBridge_swing_buy"
        stage:     One of FUNNEL_* constants
        increment: How many to add (default 1)
    """
    sub_key = subsystem.value if hasattr(subsystem, "value") else str(subsystem)
    with _funnel_lock:
        _funnel[sub_key][stage] += increment


def get_session_funnel() -> Dict[str, Dict[str, int]]:
    """Snapshot of in-memory funnel counters."""
    with _funnel_lock:
        return {sub: dict(stages) for sub, stages in _funnel.items()}


def flush_funnel(trade_date: Optional[str] = None) -> None:
    """Persist in-memory funnel to SQLite (UPSERT). Call at EOD or periodically."""
    if trade_date is None:
        trade_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with _funnel_lock:
        snapshot = {sub: dict(stages) for sub, stages in _funnel.items()}
    if not snapshot:
        return
    try:
        conn = sqlite3.connect(str(_DB_PATH))
        _ensure_funnel_table(conn)
        now = datetime.now(timezone.utc).isoformat()
        for sub, stages in snapshot.items():
            for stage, count in stages.items():
                conn.execute("""
                    INSERT INTO trade_funnel (trade_date, subsystem, stage, count, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(trade_date, subsystem, stage)
                    DO UPDATE SET count = ?, updated_at = ?
                """, (trade_date, sub, stage, count, now, count, now))
        conn.commit()
        conn.close()
    except Exception:
        pass


def reset_funnel() -> None:
    """Clear in-memory funnel (call at session start)."""
    with _funnel_lock:
        _funnel.clear()


def get_daily_funnel(trade_date: Optional[str] = None) -> Dict[str, Dict[str, int]]:
    """Load persisted funnel for a date. Returns {subsystem: {stage: count}}."""
    if trade_date is None:
        trade_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    # Merge persisted + in-memory
    result: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
    try:
        conn = sqlite3.connect(str(_DB_PATH))
        _ensure_funnel_table(conn)
        rows = conn.execute(
            "SELECT subsystem, stage, count FROM trade_funnel WHERE trade_date = ?",
            (trade_date,),
        ).fetchall()
        conn.close()
        for sub, stage, cnt in rows:
            result[sub][stage] = cnt
    except Exception:
        pass
    # Overlay in-memory (session may have newer data)
    with _funnel_lock:
        for sub, stages in _funnel.items():
            for stage, cnt in stages.items():
                if cnt > result[sub][stage]:
                    result[sub][stage] = cnt
    return dict(result)
