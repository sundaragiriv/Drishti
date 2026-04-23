"""Canonical readiness state object for Quant-Bridge.

Single source of truth consumed by:
  - run_premarket.py  (builds it, emits final verdict)
  - main.py           (refuses to start if BLOCKED)
  - dashboard          (renders banner/badges)
  - MCP server         (exposes via tools)

The object is JSON-serialisable and can be persisted to disk
so the scanner can read the result of a prior premarket run.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import List, Optional

READINESS_FILE = Path("data/warehouse/readiness.json")


def latest_complete_trading_day(reference_date: Optional[date] = None) -> date:
    """Return the latest trading day with a complete daily bar.

    Premarket and weekend checks should expect the prior completed market
    session, not the current calendar day.
    """
    ref = reference_date or date.today()
    target = ref - timedelta(days=1)
    while target.weekday() >= 5:
        target -= timedelta(days=1)
    return target


def business_day_lag(latest_date: date, reference_date: Optional[date] = None) -> int:
    """Return trading-day lag versus the latest expected complete trading day."""
    target = latest_complete_trading_day(reference_date)
    if latest_date >= target:
        return 0

    lag = 0
    current = latest_date
    while current < target:
        current += timedelta(days=1)
        if current.weekday() < 5:
            lag += 1
    return lag


class ReadinessStatus(str, Enum):
    READY = "READY"
    DEGRADED = "DEGRADED"
    BLOCKED = "BLOCKED"


@dataclass
class ReadinessState:
    """Canonical readiness summary — minimum fields required by Codex review."""

    readiness_status: str = ReadinessStatus.READY.value
    blocked_reasons: List[str] = field(default_factory=list)
    degraded_reasons: List[str] = field(default_factory=list)

    # Data freshness
    prices_age_days: Optional[int] = None
    latest_price_date: Optional[str] = None

    # IBKR
    ibkr_connected: bool = False

    # Orphan gate
    orphan_gate_active: bool = False
    orphan_symbols: List[str] = field(default_factory=list)

    # Watchlist / universe
    configured_watchlist: str = ""
    active_scan_source: str = ""
    live_universe_size: int = 0

    # Scanner availability
    enabled_scanners: List[str] = field(default_factory=list)

    # Timestamp
    computed_at: str = ""

    # ------------------------------------------------------------------ #
    #  Builders                                                           #
    # ------------------------------------------------------------------ #

    def add_blocked(self, reason: str) -> None:
        if reason not in self.blocked_reasons:
            self.blocked_reasons.append(reason)
        self.readiness_status = ReadinessStatus.BLOCKED.value

    def add_degraded(self, reason: str) -> None:
        if reason not in self.degraded_reasons:
            self.degraded_reasons.append(reason)
        if self.readiness_status != ReadinessStatus.BLOCKED.value:
            self.readiness_status = ReadinessStatus.DEGRADED.value

    def resolve_status(self) -> str:
        """Re-derive status from current reasons."""
        if self.blocked_reasons:
            self.readiness_status = ReadinessStatus.BLOCKED.value
        elif self.degraded_reasons:
            self.readiness_status = ReadinessStatus.DEGRADED.value
        else:
            self.readiness_status = ReadinessStatus.READY.value
        return self.readiness_status

    @property
    def is_blocked(self) -> bool:
        return self.readiness_status == ReadinessStatus.BLOCKED.value

    @property
    def is_degraded(self) -> bool:
        return self.readiness_status == ReadinessStatus.DEGRADED.value

    # ------------------------------------------------------------------ #
    #  Persistence                                                        #
    # ------------------------------------------------------------------ #

    def to_dict(self) -> dict:
        return asdict(self)

    def save(self, path: Path = READINESS_FILE) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.computed_at = datetime.now(timezone.utc).isoformat()
        path.write_text(json.dumps(self.to_dict(), indent=2))

    @classmethod
    def load(cls, path: Path = READINESS_FILE) -> "ReadinessState":
        """Load readiness state from disk. Fails closed: missing/corrupt = BLOCKED."""
        if not path.exists():
            state = cls()
            state.add_blocked("READINESS_MISSING: no readiness.json — run 'python run_premarket.py' first")
            return state
        try:
            raw = path.read_text().strip()
            if not raw:
                raise ValueError("empty file")
            data = json.loads(raw)
            if not isinstance(data, dict) or "readiness_status" not in data:
                raise ValueError("invalid schema")
            return cls(**{k: v for k, v in data.items()
                         if k in cls.__dataclass_fields__})
        except Exception as exc:
            state = cls()
            state.add_blocked(f"READINESS_CORRUPT: cannot parse readiness.json ({exc})")
            return state

    def __str__(self) -> str:
        lines = [f"Readiness: {self.readiness_status}"]
        if self.blocked_reasons:
            lines.append(f"  BLOCKED:  {', '.join(self.blocked_reasons)}")
        if self.degraded_reasons:
            lines.append(f"  DEGRADED: {', '.join(self.degraded_reasons)}")
        lines.append(f"  Prices: {self.latest_price_date} ({self.prices_age_days}d lag)")
        lines.append(f"  IBKR: {'connected' if self.ibkr_connected else 'disconnected'}")
        lines.append(f"  Orphan gate: {'ACTIVE' if self.orphan_gate_active else 'clear'}")
        if self.orphan_symbols:
            lines.append(f"  Orphan symbols: {', '.join(self.orphan_symbols)}")
        lines.append(f"  Watchlist: {self.configured_watchlist}")
        lines.append(f"  Scanners: {', '.join(self.enabled_scanners) or 'none'}")
        return "\n".join(lines)


def compute_price_freshness() -> tuple[bool, int, str]:
    """Query DuckDB for price freshness. Returns (ok, age_days, latest_date_str).

    Shared helper so premarket, main, and dashboard don't duplicate this logic.
    """
    try:
        from signal_scanner.institutional_intel.config import safe_duckdb_connect
        conn = safe_duckdb_connect(read_only=True)
        if not conn:
            return False, -1, ""
        try:
            row = conn.execute(
                "SELECT MAX(trade_date) FROM fact_daily_prices"
            ).fetchone()
            if not row or not row[0]:
                return False, -1, ""
            latest = row[0] if isinstance(row[0], date) else date.fromisoformat(str(row[0]))
            lag = business_day_lag(latest)
            return lag == 0, lag, str(latest)
        finally:
            conn.close()
    except Exception:
        return False, -1, ""
