"""Live VWAP Mean Reversion scanner with ML filtering.

Runs on a 5-minute schedule during market hours:
  - 9:50 AM - 11:00 AM ET: scan for new entries + check exits
  - 11:00 AM - 3:30 PM ET: check exits only (trailing stop, target, time stop)

Fetches 1-min bars from IBKR, computes VWAP_MR features matching the
training pipeline, scores with the trained LightGBM model, and creates
paper trades for high-probability setups.

Strategy rules (matching backtester):
  - Filter: accum_phase IN (ACTIVE_ACCUM, LATE_ACCUM, EARLY_ACCUM), conviction >= 65
  - Setup: Price dips > 0.3% below running VWAP
  - Entry: First bar closing above VWAP after dip, volume > 1.2x avg bar vol
  - Stop: Day-low at entry time OR entry - ATR (whichever tighter)
  - ML gate: Only enter if raw probability >= configured threshold
  - Targets: 1R / 2R (R = entry - stop)

Usage:
    # Runs automatically via scheduler in main.py
    # Or test standalone:
    python -m signal_scanner.paper.vwap_mr_live --test
"""

from __future__ import annotations

import pickle
from datetime import datetime, date, timezone
from math import ceil, floor
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import numpy as np
import pandas as pd
from loguru import logger

from signal_scanner.core.telemetry import (
    record_skip, record_funnel, SkipReason, Subsystem,
    FUNNEL_CANDIDATES, FUNNEL_SETUPS, FUNNEL_ATTEMPTED,
    FUNNEL_ENTERED, FUNNEL_SKIPPED,
)

try:
    from zoneinfo import ZoneInfo

    NY_TZ = ZoneInfo("America/New_York")
except ImportError:
    NY_TZ = timezone.utc

# ---------------------------------------------------------------------------
# Strategy parameters
# ---------------------------------------------------------------------------

# Entry window (ET) — data shows 10:00-11:00 has best recovery rate
ENTRY_START_HOUR, ENTRY_START_MIN = 10, 0
ENTRY_END_HOUR, ENTRY_END_MIN = 11, 30

# Exit check window (ET) — wider than entry, to manage open positions
EXIT_START_HOUR, EXIT_START_MIN = 9, 50
EXIT_END_HOUR, EXIT_END_MIN = 15, 55

# VWAP setup detection
VWAP_DIP_PCT = -0.3  # Price must dip > 0.3% below VWAP (was -0.8, too strict)
ENTRY_VOL_MULT = 1.2  # Entry bar volume > 1.2x average bar volume

# Intelligence filters
CONVICTION_MIN = 65
ACCUM_PHASES = {"ACTIVE_ACCUM", "LATE_ACCUM", "EARLY_ACCUM", "EXPANSION"}

# ML gate — SELECTIVE MODE (relaxed from P97 sniper that produced 0 trades)
ML_PROB_MIN = 0.50  # Raw prob floor (model still scores)
ML_PERCENTILE_MIN = 80  # Top 20% signals (was 97 = 0 trades ever)

# STRUCTURAL FILTERS (relaxed from sniper mode that never fired)
SNIPER_VWAP_CROSS_MIN = 2   # Minimum VWAP crosses by entry time (was 3)
SNIPER_VWAP_CROSS_MAX = 7   # Maximum VWAP crosses (>7 = choppy) (was 5)
SNIPER_PRICE_VS_VWAP_MIN = -1.0  # Price within -1.0% of VWAP (was -0.5%)

# Position management — SNIPER (1-2 trades/day max)
MAX_VWAP_MR_POSITIONS = 15  # Paper mode — maximize trade data
MAX_ENTRIES_PER_DAY = 20    # Paper mode — enter all qualifying setups
TIME_STOP_HOUR, TIME_STOP_MIN = 15, 30  # Force-close at 3:30 PM ET

# Trailing stop config — data-proven: trail 0.5R from peak = +3.06R avg
TRAIL_DISTANCE_R = 0.5  # Trail stop 0.5R behind highest price after 1R hit
TRAIL_ACTIVATE_AT_1R = True  # Activate trailing after 1R reached

# Paper trading sizing (matches main paper trader defaults)
RISK_PER_TRADE_PCT = 1.0
STARTING_CAPITAL = 50_000.0
NOTIONAL_CAP = 10_000.0
MIN_NOTIONAL = 5_000.0

# ---------------------------------------------------------------------------
# Feature definitions (must match intraday_ml.py)
# ---------------------------------------------------------------------------

FEATURES_PREOPEN = ["prev_close", "gap_pct", "atr_20d"]
FEATURES_OR = [
    "open_930", "or_high", "or_low", "or_range",
    "or_volume", "avg_or_volume_20d", "volume_ratio", "or_range_vs_atr",
]
FEATURES_BY_1000 = [
    "vwap_at_1000", "price_vs_vwap_1000", "rel_volume_1000",
    "first_30min_range_pct", "ret_5min_0945", "ret_15min_1000",
    "ret_30min_1000", "ret_vs_spy_1000", "consolidation_bars",
]
FEATURES_BREAKOUT = ["or_breakout", "or_breakdown"]
FEATURES_INTEL_NUMERIC = [
    "conviction_score", "expected_value", "squeeze_score",
    "short_squeeze_score", "tier1_count",
]
FEATURES_INTEL_BINARY = ["insider_cluster"]
FEATURES_CATEGORICAL = ["accum_phase", "swing_signal", "sector"]
ALL_FEATURES = (
    FEATURES_PREOPEN + FEATURES_OR + FEATURES_BY_1000
    + FEATURES_BREAKOUT + FEATURES_INTEL_NUMERIC
    + FEATURES_INTEL_BINARY + FEATURES_CATEGORICAL
)
DERIVED_FEATURES = [
    "gap_vs_atr", "or_range_pct", "or_volume_log",
    "vwap_distance_abs", "rsi_proxy", "price_vs_or_mid",
]

# ---------------------------------------------------------------------------
# Recommendation source prefix for paper trades
# ---------------------------------------------------------------------------
REC_SOURCE_PREFIX = "VWAP_MR_ML"


class VWAPMRLiveScanner:
    """Live VWAP Mean Reversion scanner with ML entry filtering.

    Designed to be instantiated once and called via scheduler every 5 minutes.
    """

    def __init__(
        self,
        connector,          # DataConnector (IBKR)
        db,                 # DatabaseManager (SQLite)
        scanner,            # MultiSymbolScanner (for intelligence snapshot)
    ) -> None:
        self._connector = connector
        self._db = db
        self._scanner = scanner
        self._model = None
        self._model_metrics: Dict[str, Any] = {}
        self._feature_cols: List[str] = []

        # Daily state — reset each morning
        self._last_date: Optional[date] = None
        self._entered_today: Set[str] = set()
        self._daily_context: Dict[str, Dict[str, float]] = {}
        self._spy_bars_today: Optional[pd.DataFrame] = None
        self._spy_ret_1000: Optional[float] = None
        self._qualified_contracts: Dict[str, Any] = {}  # cache IBKR contracts
        self._order_executor = None  # Set by main.py when IBKR live execution enabled

        # Exposed for dashboard / diagnostics
        self.latest_ml_scores: Dict[str, float] = {}

    # ------------------------------------------------------------------
    # Public API — called by scheduler
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Main entry point — called every 5 minutes by scheduler."""
        if not self._connector.is_connected():
            return

        now_et = self._et_now()
        today = now_et.date()

        # Reset daily state on new day
        if self._last_date != today:
            self._entered_today.clear()
            self._daily_context.clear()
            self._spy_bars_today = None
            self._spy_ret_1000 = None
            self._qualified_contracts.clear()
            self._last_date = today
            logger.info("VWAP_MR: new trading day — state reset")

        # Check if within operating window
        hm = now_et.hour * 100 + now_et.minute
        if hm < EXIT_START_HOUR * 100 + EXIT_START_MIN:
            return
        if hm > EXIT_END_HOUR * 100 + EXIT_END_MIN:
            return

        # Load ML model on first run
        if self._model is None:
            self._load_model()
            if self._model is None:
                record_skip(Subsystem.VWAP_MR, SkipReason.MODEL_UNAVAILABLE)
                return

        # Load daily context (prev_close, atr_20d) on first scan of day
        if not self._daily_context:
            self._load_daily_context()

        # Always check exits on open VWAP_MR positions
        self._check_exits(now_et)

        # Entry scanning only during entry window
        in_entry_window = (
            ENTRY_START_HOUR * 100 + ENTRY_START_MIN
            <= hm
            <= ENTRY_END_HOUR * 100 + ENTRY_END_MIN
        )
        if not in_entry_window:
            record_skip(Subsystem.VWAP_MR, SkipReason.LATE_ENTRY_CUTOFF,
                         persist=False)  # fires every 5 min, don't spam DB
            return

        # Cap daily entries
        if len(self._entered_today) >= MAX_ENTRIES_PER_DAY:
            record_skip(Subsystem.VWAP_MR, SkipReason.POSITION_LIMIT,
                         f"daily cap {MAX_ENTRIES_PER_DAY}", persist=False)
            return

        # Cap open positions
        open_vwap = self._get_open_vwap_mr_positions()
        if len(open_vwap) >= MAX_VWAP_MR_POSITIONS:
            record_skip(Subsystem.VWAP_MR, SkipReason.POSITION_LIMIT,
                         f"open cap {MAX_VWAP_MR_POSITIONS}", persist=False)
            return

        # Get qualifying tickers from intelligence snapshot
        tickers = self._get_qualifying_tickers()
        if not tickers:
            record_skip(Subsystem.VWAP_MR, SkipReason.NO_SETUP_QUALIFIED)
            logger.debug("VWAP_MR: no qualifying tickers from intelligence snapshot")
            return

        record_funnel(Subsystem.VWAP_MR, FUNNEL_CANDIDATES, len(tickers))

        # Fetch SPY bars for relative strength (once per day, updated each scan)
        self._fetch_spy_bars()

        # Scan each ticker
        entered = 0
        for ticker in tickers:
            if ticker in self._entered_today:
                record_funnel(Subsystem.VWAP_MR, FUNNEL_SKIPPED)
                continue
            if len(open_vwap) + entered >= MAX_VWAP_MR_POSITIONS:
                break
            try:
                record_funnel(Subsystem.VWAP_MR, FUNNEL_ATTEMPTED)
                if self._scan_ticker(ticker, now_et):
                    record_funnel(Subsystem.VWAP_MR, FUNNEL_ENTERED)
                    entered += 1
            except Exception as e:
                logger.warning(f"VWAP_MR {ticker}: scan error — {e}")

        if entered:
            logger.info(f"VWAP_MR: {entered} new entries this scan cycle")

    def run_with_cache(self, bar_cache: "IntradayBarCache",
                       ibkr_lock: "threading.Lock",
                       tickers: "list[str] | None" = None,
                       budget_seconds: float = 30.0) -> int:
        """Run using shared bar cache instead of direct IBKR calls.

        Called by IntradayScheduler for fair scheduling. Returns entry count.

        Args:
            bar_cache: Shared bar cache (read-only during evaluation).
            ibkr_lock: Not used during evaluation — kept for API compat.
            tickers: Explicit ticker list from scheduler. Only these are evaluated.
            budget_seconds: Max wall-clock seconds for this strategy's evaluation.
        """
        import time as _time
        _t0 = _time.monotonic()

        now_et = self._et_now()
        today = now_et.date()

        if self._last_date != today:
            self._entered_today.clear()
            self._daily_context.clear()
            self._spy_bars_today = None
            self._spy_ret_1000 = None
            self._qualified_contracts.clear()
            self._last_date = today

        hm = now_et.hour * 100 + now_et.minute
        if hm < EXIT_START_HOUR * 100 + EXIT_START_MIN:
            return 0
        if hm > EXIT_END_HOUR * 100 + EXIT_END_MIN:
            return 0

        if self._model is None:
            self._load_model()
            if self._model is None:
                record_skip(Subsystem.VWAP_MR, SkipReason.MODEL_UNAVAILABLE)
                return 0

        if not self._daily_context:
            self._load_daily_context()

        # Use cached SPY bars
        spy = bar_cache.get_spy_bars()
        if spy is not None and len(spy) > 0:
            self._spy_bars_today = spy
            spy_open = spy.iloc[0]["Open"]
            for idx in spy.index:
                ts = pd.Timestamp(idx)
                if hasattr(ts, "hour") and ts.hour >= 10:
                    self._spy_ret_1000 = (spy.loc[idx, "Close"] - spy_open) / spy_open * 100
                    break

        # Check exits using cached prices
        self._check_exits(now_et)

        in_entry_window = (
            ENTRY_START_HOUR * 100 + ENTRY_START_MIN <= hm
            <= ENTRY_END_HOUR * 100 + ENTRY_END_MIN
        )
        if not in_entry_window:
            return 0
        if len(self._entered_today) >= MAX_ENTRIES_PER_DAY:
            return 0

        open_vwap = self._get_open_vwap_mr_positions()
        if len(open_vwap) >= MAX_VWAP_MR_POSITIONS:
            return 0

        # Use scheduler-provided tickers (already filtered + fetched)
        if tickers is None:
            tickers = self._get_qualifying_tickers()
        if not tickers:
            record_skip(Subsystem.VWAP_MR, SkipReason.NO_SETUP_QUALIFIED)
            return 0

        record_funnel(Subsystem.VWAP_MR, FUNNEL_CANDIDATES, len(tickers))

        entered = 0
        skipped_uncached = 0
        for ticker in tickers:
            # Enforce runtime budget
            if _time.monotonic() - _t0 > budget_seconds:
                logger.info(f"VWAP_MR: budget {budget_seconds}s exhausted after {entered} entries")
                break
            if ticker in self._entered_today:
                record_funnel(Subsystem.VWAP_MR, FUNNEL_SKIPPED)
                continue
            if len(open_vwap) + entered >= MAX_VWAP_MR_POSITIONS:
                break
            # Only evaluate tickers present in cache — never fall back to live IBKR
            cached = bar_cache.get_bars(ticker)
            if cached is None:
                skipped_uncached += 1
                continue
            try:
                record_funnel(Subsystem.VWAP_MR, FUNNEL_ATTEMPTED)
                if self._scan_ticker(ticker, now_et, cached_bars=cached):
                    record_funnel(Subsystem.VWAP_MR, FUNNEL_ENTERED)
                    entered += 1
            except Exception as e:
                logger.warning(f"VWAP_MR {ticker}: scan error — {e}")

        if skipped_uncached:
            logger.debug(f"VWAP_MR: {skipped_uncached} tickers skipped (not in cache)")
        return entered

    # ------------------------------------------------------------------
    # ML model loading
    # ------------------------------------------------------------------

    def _load_model(self) -> None:
        """Load trained VWAP_MR LightGBM model from disk."""
        try:
            from signal_scanner.institutional_intel.config import WAREHOUSE_PATH

            model_path = WAREHOUSE_PATH.parent / "models" / "intraday_ml_vwap_mr.pkl"
            if not model_path.exists():
                logger.warning(
                    f"VWAP_MR: model not found at {model_path}. "
                    "Train with: python -m signal_scanner.institutional_intel.intelligence.intraday_ml "
                    "--train --strategy VWAP_MR"
                )
                return

            with open(model_path, "rb") as f:
                data = pickle.load(f)

            self._model = data["model"]
            self._model_metrics = data.get("metrics", {})
            self._feature_cols = self._model_metrics.get(
                "feature_cols", ALL_FEATURES + DERIVED_FEATURES
            )

            val_auc = self._model_metrics.get("val_auc", 0)
            logger.info(
                f"VWAP_MR: loaded ML model (val AUC={val_auc:.4f}, "
                f"{len(self._feature_cols)} features)"
            )
        except Exception as e:
            logger.error(f"VWAP_MR: failed to load model — {e}")
            self._model = None

    # ------------------------------------------------------------------
    # Daily context: prev_close, atr_20d per ticker (from DuckDB)
    # ------------------------------------------------------------------

    def _load_daily_context(self) -> None:
        """Pre-compute prev_close and atr_20d for qualifying tickers.

        Uses read-only DuckDB connection (no lock conflict with scanner).
        """
        try:
            import duckdb
            from signal_scanner.institutional_intel.config import WAREHOUSE_PATH

            conn = duckdb.connect(str(WAREHOUSE_PATH), read_only=True)
            try:
                # Get the most recent price date available
                row = conn.execute(
                    "SELECT MAX(trade_date) FROM fact_daily_prices"
                ).fetchone()
                if not row or not row[0]:
                    logger.warning("VWAP_MR: no daily prices in warehouse")
                    return

                latest_date = row[0]

                # Fetch prev_close for all tickers on latest date
                prices = conn.execute("""
                    SELECT ticker, close
                    FROM fact_daily_prices
                    WHERE trade_date = ?
                """, [latest_date]).fetchall()

                for ticker, close in prices:
                    self._daily_context[ticker] = {
                        "prev_close": float(close) if close else 0.0,
                        "atr_20d": 0.0,
                    }

                # Compute ATR-20d using the last 21 price rows per ticker
                atr_rows = conn.execute("""
                    WITH ranked AS (
                        SELECT
                            ticker,
                            trade_date,
                            high,
                            low,
                            close,
                            LAG(close) OVER (
                                PARTITION BY ticker ORDER BY trade_date
                            ) AS prev_close,
                            ROW_NUMBER() OVER (
                                PARTITION BY ticker ORDER BY trade_date DESC
                            ) AS rn
                        FROM fact_daily_prices
                    ),
                    true_ranges AS (
                        SELECT
                            ticker,
                            GREATEST(
                                high - low,
                                ABS(high - prev_close),
                                ABS(low - prev_close)
                            ) AS tr
                        FROM ranked
                        WHERE rn <= 21 AND prev_close IS NOT NULL
                    )
                    SELECT ticker, AVG(tr) AS atr_20
                    FROM true_ranges
                    GROUP BY ticker
                    HAVING COUNT(*) >= 15
                """).fetchall()

                for ticker, atr in atr_rows:
                    if ticker in self._daily_context:
                        self._daily_context[ticker]["atr_20d"] = float(atr) if atr else 0.0

                logger.info(
                    f"VWAP_MR: loaded daily context for {len(self._daily_context)} tickers "
                    f"(as of {latest_date})"
                )
            finally:
                conn.close()
        except Exception as e:
            logger.warning(f"VWAP_MR: failed to load daily context — {e}")

    # ------------------------------------------------------------------
    # Intelligence-based ticker filter
    # ------------------------------------------------------------------

    def _get_qualifying_tickers(self) -> List[str]:
        """Get tickers matching VWAP_MR criteria from intelligence snapshot."""
        snapshot = getattr(self._scanner, "_intelligence_snapshot", {})
        if not snapshot:
            logger.debug("VWAP_MR: intelligence snapshot empty")
            return []

        qualified = []
        for ticker, intel in snapshot.items():
            phase = str(intel.get("inst_phase", "")).upper()
            conviction = float(intel.get("inst_conviction", 0))

            if phase not in ACCUM_PHASES:
                continue
            if conviction < CONVICTION_MIN:
                continue
            # Skip if no daily context (no prev_close / ATR)
            if ticker not in self._daily_context:
                continue

            qualified.append(ticker)

        logger.debug(
            f"VWAP_MR: {len(qualified)} qualifying tickers "
            f"(from {len(snapshot)} in snapshot)"
        )
        return qualified

    # ------------------------------------------------------------------
    # SPY bars for relative strength
    # ------------------------------------------------------------------

    def _fetch_spy_bars(self) -> None:
        """Fetch today's SPY 1-min bars for relative strength computation."""
        try:
            df = self._connector.get_price_data("SPY", "1m")
            if df is not None and len(df) > 0:
                self._spy_bars_today = df
                # Compute SPY return from open to ~10:00
                spy_open = df.iloc[0]["Open"]
                # Find bar closest to 10:00 AM
                for idx in df.index:
                    ts = pd.Timestamp(idx)
                    if hasattr(ts, "hour") and ts.hour >= 10:
                        spy_close_1000 = df.loc[idx, "Close"]
                        self._spy_ret_1000 = (
                            (spy_close_1000 - spy_open) / spy_open * 100
                            if spy_open > 0
                            else 0.0
                        )
                        break
                else:
                    # Market not yet at 10:00 — use latest bar
                    spy_latest = df.iloc[-1]["Close"]
                    self._spy_ret_1000 = (
                        (spy_latest - spy_open) / spy_open * 100
                        if spy_open > 0
                        else 0.0
                    )
        except Exception as e:
            logger.debug(f"VWAP_MR: SPY bar fetch error — {e}")

    # ------------------------------------------------------------------
    # Single-ticker scan
    # ------------------------------------------------------------------

    def _evaluate_setup(self, ticker: str, now_et: datetime,
                        cached_bars: "pd.DataFrame | None" = None) -> "dict | None":
        """Evaluate a ticker for VWAP_MR setup. Returns signal dict or None.

        Pure evaluation — no side effects, no trade creation, no IBKR calls.
        This is the strategy engine's entry point.
        """
        bars = cached_bars if cached_bars is not None else self._connector.get_price_data(ticker, "1m")
        if bars is None or len(bars) < 15:
            return None

        features = self._compute_features(ticker, bars)
        if features is None:
            return None

        setup = self._check_vwap_setup(bars, features)
        if not setup:
            return None

        ml_prob = self._score_ml(features)
        self.latest_ml_scores[ticker] = ml_prob if ml_prob is not None else 0.0
        if ml_prob is None or ml_prob < ML_PROB_MIN:
            return None

        ml_percentile = self._compute_ml_percentile(ml_prob)
        if ml_percentile < ML_PERCENTILE_MIN:
            return None

        vwap_crosses = features.get("vwap_cross_count", 0)
        if vwap_crosses < SNIPER_VWAP_CROSS_MIN or vwap_crosses > SNIPER_VWAP_CROSS_MAX:
            return None

        price_vs_vwap = features.get("price_vs_vwap_1000", -999)
        if price_vs_vwap < SNIPER_PRICE_VS_VWAP_MIN:
            return None

        entry_price = float(bars.iloc[-1]["Close"])
        day_low = float(bars["Low"].min())
        atr = self._daily_context.get(ticker, {}).get("atr_20d", 0)

        stop_from_low = day_low - 0.01
        stop_from_atr = entry_price - atr if atr > 0 else stop_from_low
        stop_price = max(stop_from_low, stop_from_atr)

        if stop_price >= entry_price:
            return None

        r_unit = entry_price - stop_price
        if r_unit <= 0:
            return None

        target_1 = entry_price + r_unit
        target_2 = entry_price + 10 * r_unit

        qty = self._position_size(entry_price, stop_price)
        if qty <= 0:
            return None

        notional = round(entry_price * qty, 2)
        if notional < MIN_NOTIONAL:
            return None

        intel = self._get_intel(ticker)

        return {
            "strategy": "VWAP_MR",
            "symbol": ticker,
            "side": "LONG",
            "entry_price": round(entry_price, 4),
            "stop_price": round(stop_price, 4),
            "target_1": round(target_1, 4),
            "target_2": round(target_2, 4),
            "quantity": qty,
            "notional": notional,
            "r_unit": round(r_unit, 4),
            "ml_prob": round(ml_prob, 4),
            "ml_percentile": ml_percentile,
            "vwap_crosses": vwap_crosses,
            "price_vs_vwap": round(price_vs_vwap, 2),
            "conviction": intel.get("inst_conviction", 0),
            "phase": intel.get("inst_phase", "?"),
            "bar_ts": str(bars.index[-1]),
        }

    def _scan_ticker(self, ticker: str, now_et: datetime,
                     cached_bars: "pd.DataFrame | None" = None) -> bool:
        """Scan a ticker: evaluate setup + execute if signal fires.

        Calls _evaluate_setup() for pure evaluation, then handles
        trade creation and IBKR routing as side effects.
        """
        signal = self._evaluate_setup(ticker, now_et, cached_bars=cached_bars)
        if signal is None:
            return False

        record_funnel(Subsystem.VWAP_MR, FUNNEL_SETUPS)

        logger.info(
            "VWAP_MR SNIPER {}: ALL GATES PASSED | ML_pctl={} crosses={} vwap_dev={:+.2f}%",
            ticker, signal["ml_percentile"], signal["vwap_crosses"], signal["price_vs_vwap"],
        )

        # --- Execution (side effect — trade creation + IBKR routing) ---
        now_iso = datetime.now(timezone.utc).isoformat()
        trade_data = {
            "opened_at": now_iso,
            "symbol": ticker,
            "side": "LONG",
            "entry_price": signal["entry_price"],
            "quantity": signal["quantity"],
            "notional": signal["notional"],
            "stop_loss": signal["stop_price"],
            "target_1": signal["target_1"],
            "target_2": signal["target_2"],
            "status": "OPEN",
            "strategy_type": "VWAP_MR",
            "execution_mode": "IBKR" if self._order_executor else "SIM",
            "recommendation_source": f"{REC_SOURCE_PREFIX}_SNIPER_P{signal['ml_percentile']}",
            "instrument_type": "STOCK",
            "option_type": None,
            "option_expiry": None,
            "option_strike": None,
            "entry_signal": "LONG",
            "entry_score": round(signal["ml_prob"] * 100, 1),
            "entry_rr_ratio": 2.0,
            "entry_market_regime": getattr(self._scanner, "market_regime", None),
            "entry_gex_status": None,
            "entry_session_time": "EARLY" if now_et.hour < 10 else "MID_DAY",
            "entry_trade_conditions": (
                f"VWAP_MR SNIPER | ML_prob={signal['ml_prob']:.3f} pctl={signal['ml_percentile']} "
                f"| crosses={signal['vwap_crosses']} vwap_dev={signal['price_vs_vwap']:+.2f}% "
                f"| conv={signal['conviction']:.0f} | phase={signal['phase']} "
                f"| R_unit=${signal['r_unit']:.2f} | TRAIL={TRAIL_DISTANCE_R}R"
            ),
            "fees": 0.0,
            "created_ts": now_iso,
        }

        trade_id = self._db.create_paper_trade(trade_data)

        if self._order_executor and self._order_executor.should_execute_live("VWAP_MR"):
            placed = self._order_executor.place_bracket_order(
                trade_id=trade_id,
                symbol=ticker,
                side="LONG",
                quantity=signal["quantity"],
                entry_price=signal["entry_price"],
                stop_price=signal["stop_price"],
                target_price=signal["target_2"],
            )
            if placed:
                logger.info(f"VWAP_MR->IBKR: {ticker} bracket placed (trade_id={trade_id})")

        self._entered_today.add(ticker)

        logger.info(
            f"VWAP_MR ENTRY: {ticker} @ ${signal['entry_price']:.2f} "
            f"stop=${signal['stop_price']:.2f} target=${signal['target_2']:.2f} "
            f"ML={signal['ml_prob']:.1%} conv={signal['conviction']:.0f} "
            f"(trade_id={trade_id})"
        )
        return True

    # ------------------------------------------------------------------
    # Feature computation (from 1-min bars)
    # ------------------------------------------------------------------

    def _compute_features(
        self, ticker: str, bars: pd.DataFrame
    ) -> Optional[Dict[str, Any]]:
        """Compute VWAP_MR features from today's 1-min bars.

        Mirrors intraday_feature_engine._compute_day_features() but works
        on live IBKR bars (capital-case columns: Open, High, Low, Close, Volume).
        """
        ctx = self._daily_context.get(ticker, {})
        prev_close = ctx.get("prev_close", 0)
        atr_20d = ctx.get("atr_20d", 0)
        intel = self._get_intel(ticker)

        if prev_close <= 0:
            return None

        # Lowercase column references
        opens = bars["Open"].values.astype(float)
        highs = bars["High"].values.astype(float)
        lows = bars["Low"].values.astype(float)
        closes = bars["Close"].values.astype(float)
        volumes = bars["Volume"].values.astype(float)
        n = len(bars)

        # ---- Opening range (first 15 min: bars 0-14 approximately) ----
        # IBKR 1-min bars: first bar is 9:30, OR = 9:30-9:44 = first 15 bars
        or_count = min(15, n)
        open_930 = opens[0]
        or_high = float(highs[:or_count].max())
        or_low = float(lows[:or_count].min())
        or_range = or_high - or_low
        or_volume = float(volumes[:or_count].sum())

        # Gap
        gap_pct = (open_930 - prev_close) / prev_close * 100

        # ---- Running VWAP ----
        tp = (highs + lows + closes) / 3.0
        cum_tp_vol = np.cumsum(tp * volumes)
        cum_vol = np.cumsum(volumes)
        cum_vol_safe = np.where(cum_vol > 0, cum_vol, 1.0)
        vwap = cum_tp_vol / cum_vol_safe

        # ---- Snapshots at time indices ----
        # 9:30 = bar 0, 9:45 = bar 15, 10:00 = bar 30
        idx_0945 = min(15, n - 1)
        idx_1000 = min(30, n - 1)

        price_0930 = closes[0]
        price_0940 = closes[min(10, n - 1)]
        price_0945 = closes[idx_0945]
        price_1000 = closes[idx_1000]
        vwap_at_1000 = vwap[idx_1000]

        # VWAP deviation at 10:00
        price_vs_vwap_1000 = (
            (price_1000 - vwap_at_1000) / vwap_at_1000 * 100
            if vwap_at_1000 > 0
            else 0.0
        )

        # Volume at 10:00 (cumulative)
        vol_at_1000 = float(cum_vol[idx_1000])

        # First 30 min range
        idx_30min = min(30, n)
        first_30_high = float(highs[:idx_30min].max())
        first_30_low = float(lows[:idx_30min].min())
        first_30min_range_pct = (
            (first_30_high - first_30_low) / prev_close * 100
            if prev_close > 0
            else 0.0
        )

        # Returns
        ret_5min_0945 = (
            (price_0945 - price_0940) / price_0940 * 100
            if price_0940 > 0
            else 0.0
        )
        ret_15min_1000 = (
            (price_1000 - price_0945) / price_0945 * 100
            if price_0945 > 0
            else 0.0
        )
        ret_30min_1000 = (
            (price_1000 - price_0930) / price_0930 * 100
            if price_0930 > 0
            else 0.0
        )

        # Relative strength vs SPY
        ret_vs_spy_1000 = (
            ret_30min_1000 - (self._spy_ret_1000 or 0.0)
        )

        # Consolidation: consecutive bars after OR with range < 0.1% of OR high
        consol = 0
        if or_high > 0:
            threshold = or_high * 0.001
            for i in range(or_count, n):
                if (highs[i] - lows[i]) < threshold:
                    consol += 1
                else:
                    break

        # Breakout / breakdown flags
        or_breakout = False
        or_breakdown = False
        for i in range(or_count, n):
            if closes[i] > or_high and not or_breakout:
                or_breakout = True
            if closes[i] < or_low and not or_breakdown:
                or_breakdown = True

        # ---- Build feature dict ----
        feat = {
            # Pre-open
            "prev_close": prev_close,
            "gap_pct": gap_pct,
            "atr_20d": atr_20d if atr_20d > 0 else np.nan,
            # OR
            "open_930": open_930,
            "or_high": or_high,
            "or_low": or_low,
            "or_range": or_range,
            "or_volume": or_volume,
            "avg_or_volume_20d": np.nan,  # Not available live
            "volume_ratio": np.nan,  # Not available live
            "or_range_vs_atr": (
                or_range / atr_20d if atr_20d > 0 else np.nan
            ),
            # By 10:00
            "vwap_at_1000": vwap_at_1000,
            "price_vs_vwap_1000": price_vs_vwap_1000,
            "rel_volume_1000": np.nan,  # Not available live
            "first_30min_range_pct": first_30min_range_pct,
            "ret_5min_0945": ret_5min_0945,
            "ret_15min_1000": ret_15min_1000,
            "ret_30min_1000": ret_30min_1000,
            "ret_vs_spy_1000": ret_vs_spy_1000,
            "consolidation_bars": consol,
            # Breakout
            "or_breakout": float(or_breakout),
            "or_breakdown": float(or_breakdown),
            # Intelligence (from scanner snapshot)
            "conviction_score": float(intel.get("inst_conviction", 0)),
            "expected_value": 0.0,  # Not in scanner snapshot
            "squeeze_score": float(intel.get("inst_squeeze", 0)),
            "short_squeeze_score": float(intel.get("inst_short_squeeze", 0)),
            "tier1_count": 0,  # Not in scanner snapshot
            "insider_cluster": 0.0,  # Not in scanner snapshot
            # Categorical
            "accum_phase": str(intel.get("inst_phase", "")),
            "swing_signal": "",  # Not in scanner snapshot
            "sector": "",  # Not in scanner snapshot
        }

        # ---- VWAP cross count (sniper filter) ----
        vwap_cross_count = 0
        for i in range(1, n):
            if (closes[i - 1] < vwap[i - 1] and closes[i] >= vwap[i]) or \
               (closes[i - 1] >= vwap[i - 1] and closes[i] < vwap[i]):
                vwap_cross_count += 1
        feat["vwap_cross_count"] = vwap_cross_count

        # Store VWAP array for setup detection
        feat["_vwap_array"] = vwap
        feat["_bars_n"] = n
        feat["_or_count"] = or_count

        return feat

    def _prepare_ml_features(self, feat: Dict[str, Any]) -> Optional[pd.DataFrame]:
        """Convert feature dict to ML-ready DataFrame matching prepare_features()."""
        # Remove internal keys
        row = {k: v for k, v in feat.items() if not k.startswith("_")}

        df = pd.DataFrame([row])

        # Derived features (matching intraday_ml.prepare_features)
        atr_pct = df["atr_20d"] / df["prev_close"].replace(0, np.nan) * 100
        df["gap_vs_atr"] = df["gap_pct"].abs() / atr_pct.replace(0, np.nan)
        df["or_range_pct"] = df["or_range"] / df["open_930"].replace(0, np.nan) * 100
        df["or_volume_log"] = np.log1p(df["or_volume"].fillna(0).clip(lower=0))
        df["vwap_distance_abs"] = df["price_vs_vwap_1000"].abs()
        df["rsi_proxy"] = df["ret_30min_1000"].clip(-10, 10)
        or_mid = (df["or_high"] + df["or_low"]) / 2
        df["price_vs_or_mid"] = (
            (df["vwap_at_1000"] - or_mid) / or_mid.replace(0, np.nan) * 100
        )

        # Categorical encoding
        for col in FEATURES_CATEGORICAL:
            df[col] = df[col].astype("category")

        # Boolean to float (already done in feature dict, but ensure)
        for col in FEATURES_BREAKOUT + FEATURES_INTEL_BINARY:
            df[col] = df[col].astype(float)

        feature_cols = ALL_FEATURES + DERIVED_FEATURES
        return df[feature_cols]

    # ------------------------------------------------------------------
    # VWAP setup detection
    # ------------------------------------------------------------------

    def _check_vwap_setup(
        self, bars: pd.DataFrame, features: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """Detect VWAP dip + recross pattern.

        Setup: Price dips > 0.8% below running VWAP, then closes above VWAP.
        Data-proven: 1%+ dip has 38% profitable by EOD. Higher threshold
        filters shallow noise dips. The recross bar must have volume > 1.2x
        average bar volume.

        Returns setup dict with entry details, or None.
        """
        vwap = features.get("_vwap_array")
        n = features.get("_bars_n", 0)
        or_count = features.get("_or_count", 15)

        if vwap is None or n < or_count + 5:
            return None

        closes = bars["Close"].values.astype(float)
        volumes = bars["Volume"].values.astype(float)

        # Average bar volume (post-OR)
        post_or_vol = volumes[or_count:]
        if len(post_or_vol) == 0:
            return None
        avg_bar_vol = float(post_or_vol.mean()) if len(post_or_vol) > 0 else 1.0
        if avg_bar_vol <= 0:
            avg_bar_vol = 1.0

        # Scan post-OR bars for VWAP dip + recross
        dip_detected = False
        for i in range(or_count, n):
            vwap_dev_pct = (
                (closes[i] - vwap[i]) / vwap[i] * 100
                if vwap[i] > 0
                else 0.0
            )

            if vwap_dev_pct < VWAP_DIP_PCT:
                # Price is below VWAP threshold — dip detected
                dip_detected = True

            if dip_detected and closes[i] > vwap[i]:
                # Price has recrossed above VWAP
                bar_vol = volumes[i]
                if bar_vol >= avg_bar_vol * ENTRY_VOL_MULT:
                    # Volume confirmation on recross bar
                    return {
                        "dip_detected": True,
                        "recross_bar_idx": i,
                        "recross_price": float(closes[i]),
                        "recross_vwap": float(vwap[i]),
                        "recross_vol": float(bar_vol),
                        "avg_bar_vol": avg_bar_vol,
                    }
                # Recross without volume — reset dip flag (could dip again)
                dip_detected = False

        return None

    # ------------------------------------------------------------------
    # ML scoring
    # ------------------------------------------------------------------

    def _score_ml(self, features: Dict[str, Any]) -> Optional[float]:
        """Score a single ticker's features with the trained ML model."""
        if self._model is None:
            return None

        try:
            X = self._prepare_ml_features(features)
            if X is None:
                return None
            proba = self._model.predict_proba(X)[:, 1]
            return float(proba[0])
        except Exception as e:
            logger.debug(f"VWAP_MR ML scoring error: {e}")
            return None

    # ------------------------------------------------------------------
    # Exit management
    # ------------------------------------------------------------------

    def _check_exits(self, now_et: datetime) -> None:
        """Check stop/trailing/time for open VWAP_MR positions.

        Trailing stop logic (data-proven +3.06R avg with 0.5R trail):
        1. Initial stop: entry - ATR (or day low)
        2. When price hits 1R (target_1): activate trailing, move stop to breakeven
        3. Trail: stop = max_high_since_1R - TRAIL_DISTANCE_R * R_unit
        4. Stop only moves UP, never down
        """
        positions = self._get_open_vwap_mr_positions()
        if not positions:
            return

        now_iso = datetime.now(timezone.utc).isoformat()
        hm = now_et.hour * 100 + now_et.minute
        time_stop = hm >= TIME_STOP_HOUR * 100 + TIME_STOP_MIN

        for pos in positions:
            ticker = pos.get("symbol")
            if not ticker:
                continue

            price = self._get_current_price(ticker)
            if price is None:
                continue

            trade_id = int(pos.get("id", 0))
            entry_price = float(pos.get("entry_price") or 0)
            stop_price = float(pos.get("stop_loss") or 0)
            target_1 = float(pos.get("target_1") or 0)
            trail_high = float(pos.get("trail_high") or 0)
            trail_activated = int(pos.get("trail_activated") or 0)

            if entry_price <= 0:
                continue

            r_unit = target_1 - entry_price if target_1 > entry_price else 0

            exit_reason = ""

            # 1. Stop loss check (always)
            if stop_price > 0 and price <= stop_price:
                exit_reason = "STOP_LOSS" if not trail_activated else "TRAIL_STOP"

            # 2. Time stop
            elif time_stop:
                exit_reason = "TIME_STOP_330PM"

            # 3. Trailing stop management
            elif TRAIL_ACTIVATE_AT_1R and r_unit > 0:
                if not trail_activated and price >= target_1:
                    # Just hit 1R — activate trailing, move stop to breakeven
                    trail_activated = 1
                    trail_high = price
                    new_stop = entry_price + 0.01
                    self._db.update_paper_trade_trail(trade_id, new_stop, trail_high)
                    if (
                        self._order_executor
                        and str(pos.get("execution_mode", "")).upper() == "LIVE"
                    ):
                        self._order_executor.modify_stop(trade_id, new_stop)
                    logger.info(
                        f"VWAP_MR {ticker}: 1R HIT — trailing activated | "
                        f"stop→BE ${new_stop:.2f} | trail_high=${trail_high:.2f}"
                    )

                elif trail_activated:
                    # Trail is active — update trail_high and stop
                    if price > trail_high:
                        trail_high = price
                    trail_stop = trail_high - TRAIL_DISTANCE_R * r_unit
                    # Stop only moves up
                    if trail_stop > stop_price:
                        self._db.update_paper_trade_trail(trade_id, trail_stop, trail_high)
                        if (
                            self._order_executor
                            and str(pos.get("execution_mode", "")).upper() == "LIVE"
                        ):
                            self._order_executor.modify_stop(trade_id, trail_stop)
                        logger.debug(
                            f"VWAP_MR {ticker}: trail update | "
                            f"high=${trail_high:.2f} stop=${trail_stop:.2f} "
                            f"(+{(trail_high - entry_price) / r_unit:.1f}R)"
                        )

            if exit_reason:
                self._close_position(pos, price, exit_reason, now_iso)

    def _close_position(
        self, position: Dict, price: float, reason: str, now_iso: str
    ) -> None:
        """Close a VWAP_MR paper position."""
        trade_id = int(position.get("id", 0))
        ticker = position.get("symbol", "?")
        entry_price = float(position.get("entry_price") or 0)
        qty = int(position.get("quantity") or 0)
        fees = float(position.get("fees") or 0)
        side = str(position.get("side") or "LONG")

        if entry_price > 0:
            pnl_per_share = (price - entry_price) if side == "LONG" else (entry_price - price)
        else:
            pnl_per_share = 0.0
        pnl_dollars = pnl_per_share * qty - fees
        pnl_pct = (pnl_per_share / entry_price * 100) if entry_price > 0 else 0.0

        # Cancel IBKR bracket legs if this is a LIVE trade
        if (
            self._order_executor
            and str(position.get("execution_mode", "")).upper() == "LIVE"
        ):
            self._order_executor.cancel_bracket(trade_id)

        self._db.close_paper_trade(
            trade_id=trade_id,
            closed_at=now_iso,
            exit_price=round(price, 4),
            exit_reason=reason,
            realized_pnl=round(pnl_dollars, 2),
            realized_pnl_pct=round(pnl_pct, 2),
            fees=fees,
        )

        logger.info(
            f"VWAP_MR EXIT {ticker}: {reason} @ ${price:.2f} "
            f"(entry=${entry_price:.2f}, PnL=${pnl_dollars:+.2f})"
        )

    def _get_current_price(self, ticker: str) -> Optional[float]:
        """Get current price for a ticker using 1-min bars."""
        try:
            bars = self._connector.get_price_data(ticker, "1m")
            if bars is not None and len(bars) > 0:
                return float(bars.iloc[-1]["Close"])
        except Exception:
            pass
        return None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _compute_ml_percentile(self, ml_prob: float) -> int:
        """Compute ML percentile rank among today's scored tickers.

        Uses all scored tickers this session as the reference population.
        Returns integer 0-100.
        """
        scores = list(self.latest_ml_scores.values())
        if not scores or len(scores) < 3:
            # Not enough scores yet — use historical thresholds from backtest
            # P97 ~ 0.72, P99 ~ 0.78 (from intraday_ml training)
            if ml_prob >= 0.78:
                return 99
            elif ml_prob >= 0.72:
                return 97
            elif ml_prob >= 0.68:
                return 95
            elif ml_prob >= 0.60:
                return 90
            else:
                return int(ml_prob * 100)
        # Cross-sectional percentile
        below = sum(1 for s in scores if s <= ml_prob)
        return int(below / len(scores) * 100)

    def _get_open_vwap_mr_positions(self) -> List[Dict]:
        """Get open paper trades with VWAP_MR source."""
        all_open = self._db.get_open_paper_trades()
        return [
            p for p in all_open
            if str(p.get("recommendation_source", "")).startswith(REC_SOURCE_PREFIX)
        ]

    def _get_intel(self, ticker: str) -> Dict:
        """Get intelligence data for a ticker from scanner snapshot."""
        snapshot = getattr(self._scanner, "_intelligence_snapshot", {})
        return snapshot.get(ticker, {})

    def _position_size(self, entry: float, stop: float) -> int:
        """Risk-based position sizing."""
        risk_per_share = abs(entry - stop)
        if risk_per_share <= 0:
            return 0
        risk_budget = STARTING_CAPITAL * (RISK_PER_TRADE_PCT / 100.0)
        qty_risk = floor(risk_budget / risk_per_share)
        qty_notional = floor(NOTIONAL_CAP / entry) if entry > 0 else 0
        qty_cap = min(qty_risk, qty_notional)
        if qty_cap <= 0:
            return 0
        qty_min = ceil(MIN_NOTIONAL / entry) if entry > 0 else 0
        if qty_min <= 0 or qty_min > qty_cap:
            return 0
        return int(qty_min)

    @staticmethod
    def _et_now() -> datetime:
        """Current time in Eastern."""
        return datetime.now(NY_TZ)


# ---------------------------------------------------------------------------
# Standalone test
# ---------------------------------------------------------------------------

def _test_main() -> None:
    """Quick standalone test — requires IBKR connection."""
    import argparse

    parser = argparse.ArgumentParser(description="VWAP_MR Live Scanner Test")
    parser.add_argument("--test", action="store_true", help="Run a single test scan")
    parser.add_argument(
        "--ibkr-port", type=int, default=7497, help="IBKR port (default: 7497)"
    )
    args = parser.parse_args()

    if not args.test:
        parser.print_help()
        return

    from signal_scanner.config import IBKRConfig, ScannerConfig
    from signal_scanner.core.ibkr_connector import DataConnector
    from signal_scanner.database.db_manager import DatabaseManager

    logger.info("VWAP_MR: standalone test mode")

    ib_cfg = IBKRConfig(port=args.ibkr_port)
    connector = DataConnector(ib_cfg)
    if not connector.connect_ibkr():
        logger.error("Cannot connect to IBKR")
        return

    db = DatabaseManager()
    db.init_db()

    # Minimal scanner mock for intelligence snapshot
    class _MockScanner:
        _intelligence_snapshot = {}
        market_regime = None

    mock_scanner = _MockScanner()

    # Load intelligence snapshot
    try:
        import duckdb
        from signal_scanner.institutional_intel.config import WAREHOUSE_PATH

        conn = duckdb.connect(str(WAREHOUSE_PATH), read_only=True)
        try:
            row = conn.execute("""
                SELECT report_quarter FROM intelligence_scores
                WHERE data_quality_score >= 75
                GROUP BY report_quarter
                HAVING COUNT(*) >= 500
                ORDER BY report_quarter DESC LIMIT 1
            """).fetchone()
            if row:
                rows = conn.execute("""
                    SELECT ticker, accum_phase, conviction_score,
                           COALESCE(squeeze_score, 0),
                           COALESCE(short_squeeze_score, 0)
                    FROM intelligence_scores
                    WHERE report_quarter = ? AND data_quality_score >= 75
                """, [row[0]]).fetchall()
                for r in rows:
                    mock_scanner._intelligence_snapshot[r[0]] = {
                        "inst_phase": str(r[1] or ""),
                        "inst_conviction": float(r[2] or 0),
                        "inst_squeeze": float(r[3] or 0),
                        "inst_short_squeeze": float(r[4] or 0),
                    }
                logger.info(
                    f"Loaded {len(mock_scanner._intelligence_snapshot)} "
                    f"tickers from {row[0]}"
                )
        finally:
            conn.close()
    except Exception as e:
        logger.warning(f"Could not load intelligence: {e}")

    scanner = VWAPMRLiveScanner(connector, db, mock_scanner)
    logger.info("Running VWAP_MR scan...")
    scanner.run()
    logger.info("Done")


if __name__ == "__main__":
    _test_main()
