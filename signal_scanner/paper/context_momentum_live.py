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

# Stop/target sizing — the intraday stop is a fraction of the stock's DAILY
# ATR(14), not the 1-min bar range (which was noise-tight: pennies on a $200
# stock). Targets are R-multiples of that risk unit.
STOP_DAILY_ATR_FRAC = 0.5   # stop distance = 0.5 x daily ATR(14)
MIN_STOP_PCT = 0.006        # floor: stop is always >= 0.6% of price
TARGET_1_R = 2.0
TARGET_2_R = 3.0


class ContextMomentumScanner:
    """Context-driven intraday entry — fires on convergence, not patterns."""

    def __init__(self, bar_store, db_manager, intelligence_snapshot: dict = None):
        self._store = bar_store
        self._db = db_manager
        self._snapshot = intelligence_snapshot or {}
        self._entered_today: set = set()
        self._last_date: str = ""
        self._atr_cache: dict = {}   # {ticker: daily_ATR(14)} — rebuilt once/day
        self._atr_date: str = ""

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

        # Size the stop to the stock's DAILY ATR(14) — real daily movement,
        # not 1-min noise. Fall back to the intraday bar range if the daily
        # ATR is unavailable. A 0.6%-of-price floor is always enforced so a
        # stop can never land inside normal intraday wiggle.
        daily_atr = self._atr_cache.get(ticker)
        if daily_atr and daily_atr > 0:
            stop_dist = STOP_DAILY_ATR_FRAC * daily_atr
        else:
            ranges = bars["High"] - bars["Low"]
            bar_atr = float(ranges.tail(20).mean()) if len(ranges) >= 20 else float(ranges.mean())
            stop_dist = 1.5 * bar_atr
        stop_dist = max(stop_dist, MIN_STOP_PCT * entry_price)

        stop = round(entry_price - stop_dist, 4)
        r_unit = entry_price - stop
        if r_unit <= 0:
            return None

        target_1 = round(entry_price + TARGET_1_R * r_unit, 4)
        target_2 = round(entry_price + TARGET_2_R * r_unit, 4)

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
        self._ensure_daily_atr_cache(today)

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

    def _ensure_daily_atr_cache(self, today: str) -> None:
        """Build a {ticker: daily ATR(14)} map once per day from the warehouse.

        Sized stops need the stock's real daily movement; the live bar store
        only holds today's 1-min bars, so we read daily OHLC from DuckDB.
        Read-only + cached once/day to avoid per-cycle DB load.
        """
        if self._atr_date == today and self._atr_cache:
            return
        self._atr_date = today
        try:
            from signal_scanner.institutional_intel.config import safe_duckdb_connect
            conn = safe_duckdb_connect(read_only=True)
            if conn is None:
                return
            try:
                rows = conn.execute(
                    """
                    WITH recent AS (
                        SELECT ticker, trade_date, high, low, close,
                               LAG(close) OVER (PARTITION BY ticker ORDER BY trade_date) AS prev_close
                        FROM fact_daily_prices
                        WHERE trade_date >= current_date - INTERVAL '40' DAY
                    ),
                    tr AS (
                        SELECT ticker,
                               GREATEST(high - low, ABS(high - prev_close), ABS(low - prev_close)) AS tr,
                               ROW_NUMBER() OVER (PARTITION BY ticker ORDER BY trade_date DESC) AS rn
                        FROM recent
                        WHERE prev_close IS NOT NULL
                    )
                    SELECT ticker, AVG(tr) AS atr14
                    FROM tr WHERE rn <= 14
                    GROUP BY ticker
                    """
                ).fetchall()
                self._atr_cache = {r[0]: float(r[1]) for r in rows if r[1] and r[1] > 0}
                logger.info("CONTEXT_MOMENTUM: daily ATR cache built for {} tickers",
                            len(self._atr_cache))
            finally:
                conn.close()
        except Exception as e:
            logger.warning("CONTEXT_MOMENTUM: daily ATR cache failed ({}); using fallback stops", e)

    def _compute_qty(self, price: float) -> int:
        from math import ceil
        if price >= 10:
            return ceil(10_000 / price)
        return 1000
