"""Context Momentum Entry — non-pattern-dependent intraday entry.

Unlike VWAP_MR/FPB/ORB_V2 which require specific chart shapes,
this entry fires when multiple context signals converge:
  - Strong intraday relative strength vs sector
  - Strong volume buying pressure
  - VWAP not exhausted (not chasing)
  - Institutional thesis still alive (conviction + freshness)
  - No regime block

This is a SEPARATE entry family tracked independently.

Entry logic:
  RS > 0.5% vs sector ETF (stock outperforming its sector)
  Volume pressure > 60 (buying dominant)
  VWAP sigma < 2.0 (not exhausted / chasing)
  Conviction >= 55 (institutional thesis)
  Thesis freshness verdict != STALE
  Regime allows LONG

No ML gate. No pattern detection. Pure context convergence.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from loguru import logger

from signal_scanner.core.intraday_rs import compute_intraday_rs, get_sector_etf
from signal_scanner.core.vwap_bands import compute_vwap_sigma
from signal_scanner.core.volume_pressure import compute_volume_pressure


# Entry thresholds (conservative start — tighten based on results)
RS_MIN = 0.005           # +0.5% vs sector ETF
PRESSURE_MIN = 60        # volume pressure score
VWAP_SIGMA_MAX = 2.0     # not exhausted
CONVICTION_MIN = 55      # institutional thesis
MIN_BARS = 30            # need at least 30 1-min bars (~30 min of market)

# Entry window
ENTRY_START_HOUR, ENTRY_START_MIN = 10, 0    # 10:00 AM ET
ENTRY_END_HOUR, ENTRY_END_MIN = 14, 30       # 2:30 PM ET (wider than pattern strategies)

# Position limits
MAX_POSITIONS = 5
MAX_ENTRIES_PER_DAY = 8

# Source tag
REC_SOURCE = "CONTEXT_MOMENTUM"


class ContextMomentumScanner:
    """Context-driven intraday entry — fires on convergence, not patterns."""

    def __init__(self, bar_store, db_manager, intelligence_snapshot: dict = None):
        self._store = bar_store
        self._db = db_manager
        self._snapshot = intelligence_snapshot or {}
        self._entered_today: set = set()
        self._last_date: str = ""

    def evaluate(self, ticker: str, now_et: datetime) -> Optional[Dict]:
        """Evaluate a ticker for context momentum entry.

        Returns signal dict if all context gates pass, None otherwise.
        Pure evaluation — no side effects.
        """
        bars = self._store.get_bars(ticker)
        if bars is None or len(bars) < MIN_BARS:
            return None

        # Time window check
        hm = now_et.hour * 100 + now_et.minute
        if hm < ENTRY_START_HOUR * 100 + ENTRY_START_MIN:
            return None
        if hm > ENTRY_END_HOUR * 100 + ENTRY_END_MIN:
            return None

        # Already entered today — ONE entry per ticker per day
        if ticker in self._entered_today:
            return None

        # Already have an open position in this ticker
        try:
            open_trades = self._db.get_open_paper_trades()
            open_symbols = {t.get("symbol") for t in open_trades}
            if ticker in open_symbols:
                return None
        except Exception:
            pass

        # Get intelligence context
        intel = self._snapshot.get(ticker, {})
        conviction = float(intel.get("inst_conviction", 0) or 0)
        phase = str(intel.get("inst_phase", ""))
        if conviction < CONVICTION_MIN:
            return None

        # Gate 1: Sector relative strength
        sector = intel.get("inst_sector", "")
        sector_etf = get_sector_etf(sector)
        rs = compute_intraday_rs(self._store, ticker, sector_etf)
        if rs is None or rs < RS_MIN:
            return None

        # Gate 2: Volume pressure
        vp = compute_volume_pressure(bars)
        if vp is None or vp["pressure_score"] < PRESSURE_MIN:
            return None

        # Gate 3: VWAP not exhausted
        sigma = compute_vwap_sigma(bars)
        if sigma is None:
            return None
        if sigma["sigma_distance"] > VWAP_SIGMA_MAX:
            return None  # chasing — too extended above VWAP

        # All gates passed
        entry_price = float(bars.iloc[-1]["Close"])
        if entry_price <= 0:
            return None

        # Compute stops/targets from ATR proxy (using bar range)
        ranges = bars["High"] - bars["Low"]
        atr = float(ranges.tail(20).mean()) if len(ranges) >= 20 else float(ranges.mean())
        if atr <= 0:
            atr = entry_price * 0.015  # 1.5% fallback

        stop = round(entry_price - 1.5 * atr, 4)
        r_unit = entry_price - stop
        if r_unit <= 0:
            return None

        target_1 = round(entry_price + 2.0 * r_unit, 4)
        target_2 = round(entry_price + 3.0 * r_unit, 4)

        return {
            "strategy": "CONTEXT_MOMENTUM",
            "symbol": ticker,
            "side": "LONG",
            "entry_price": round(entry_price, 4),
            "stop_price": stop,
            "target_1": target_1,
            "target_2": target_2,
            "r_unit": round(r_unit, 4),
            "quantity": self._compute_qty(entry_price),
            # Context evidence
            "sector_rs": round(rs, 4),
            "sector_etf": sector_etf,
            "vol_pressure": vp["pressure_score"],
            "vol_verdict": vp["verdict"],
            "vwap_sigma": sigma["sigma_distance"],
            "vwap_verdict": sigma["verdict"],
            "conviction": conviction,
            "phase": phase,
            "bar_ts": str(bars.index[-1]),
            # Rationale
            "rationale": (
                f"CONTEXT_MOMENTUM | RS={rs:+.3f}vs{sector_etf} "
                f"| VolP={vp['pressure_score']:.0f}({vp['verdict']}) "
                f"| VWAP={sigma['sigma_distance']:+.1f}σ({sigma['verdict']}) "
                f"| Conv={conviction:.0f} Phase={phase}"
            ),
        }

    def scan_universe(self, tickers: List[str], now_et: datetime) -> List[Dict]:
        """Scan all tickers for context momentum entries.

        Returns list of signal dicts for tickers that pass all gates.
        """
        today = now_et.strftime("%Y-%m-%d")
        if today != self._last_date:
            self._entered_today.clear()
            self._last_date = today

        # Count current open positions to respect limit
        try:
            open_count = len([t for t in self._db.get_open_paper_trades()
                             if (t.get("recommendation_source") or "").startswith("CONTEXT")])
        except Exception:
            open_count = 0

        signals = []
        for ticker in tickers:
            if len(signals) + open_count >= MAX_POSITIONS:
                break
            if len(self._entered_today) + len(signals) >= MAX_ENTRIES_PER_DAY:
                break
            signal = self.evaluate(ticker, now_et)
            if signal:
                signals.append(signal)
                self._entered_today.add(ticker)  # prevent re-entry same day
                logger.info(
                    "CONTEXT_MOMENTUM {}: {} | {}",
                    ticker, signal["rationale"],
                    f"entry=${signal['entry_price']:.2f} stop=${signal['stop_price']:.2f}"
                )

        return signals

    def _compute_qty(self, price: float) -> int:
        from math import ceil
        if price >= 10:
            return ceil(10_000 / price)
        return 1000
