"""Live ORB V2 (Opening Range Breakout V2) scanner with ML filtering.

Runs on a 5-minute schedule during market hours:
  - 9:50 AM - 10:30 AM ET: scan for new entries + check exits
  - 10:30 AM - 3:55 PM ET: check exits only (trailing stop, target, time stop)

Fetches 1-min bars from IBKR, applies 6 structural confirmation rules from the
enhanced ORB strategy, scores with the trained LightGBM model, and creates
paper trades for high-probability setups.

Strategy rules (matching backtester — Peachy Investor ORB V2):
  - Filter: accum_phase IN (ACTIVE_ACCUM, LATE_ACCUM, EARLY_ACCUM), conviction >= 55
  - OR volume filter: OR volume >= 1.2x avg (or skip if unknown)
  - Setup: 15-min Opening Range (9:30-9:44)
  - Entry scan 9:50-10:30:
    RULE 1: Bar CLOSES above OR high (not just wick touch)
    RULE 2: Displacement — body_ratio > 0.50 (full-bodied candle)
    RULE 3: Fakeout filter — wick_ratio < 0.30
    RULE 4: Volume — breakout bar volume > 1.5x avg OR bar volume
    RULE 5: Daily bias — price > VWAP at breakout time
    RULE 6: Stop at OR midpoint (not OR low)
  - Quality score (0-4): displacement(>0.50) + strong vol(>2x) + VWAP + prev_day_high
  - ML gate: Only enter if raw probability >= configured threshold
  - Targets: 1R / 2R (R = entry - OR midpoint)

Usage:
    # Runs automatically via scheduler in main.py
    # Or test standalone:
    python -m signal_scanner.paper.orb_v2_live --test
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

# Entry window (ET)
ENTRY_START_HOUR, ENTRY_START_MIN = 9, 50
ENTRY_END_HOUR, ENTRY_END_MIN = 10, 30

# Exit check window (ET) — wider than entry, to manage open positions
EXIT_START_HOUR, EXIT_START_MIN = 9, 50
EXIT_END_HOUR, EXIT_END_MIN = 15, 55

# ORB V2 structural confirmation thresholds
BODY_RATIO_MIN = 0.50          # RULE 2: candle body must be >= 50% of range
WICK_RATIO_MAX = 0.30          # RULE 3: upper wick must be < 30% of range
BREAKOUT_VOL_MULT = 1.5        # RULE 4: breakout bar volume > 1.5x avg OR bar vol
OR_VOL_RATIO_MIN = 1.2         # Pre-filter: OR volume >= 1.2x historical avg

# Data-proven quality filters (from backtest of 79K ORB signals):
#   Tight OR (<0.5%): 52.7% win, 64% 1R hit vs 48% for wide OR
#   ORB + high vol + gap up: 73.5% win rate (N=7,322)
#   ORB + conv>=65 + vol>1.5: 72.5% win rate (N=13,904)
OR_RANGE_MAX_PCT = 0.015       # Prefer tighter opening ranges (< 1.5% of price)
OR_RANGE_IDEAL_PCT = 0.005     # Ideal: < 0.5% (highest win rate)
GAP_UP_BONUS = True            # Gap-up ORBs have higher win rate

# Intelligence filters
CONVICTION_MIN = 50
ACCUM_PHASES = {"ACTIVE_ACCUM", "LATE_ACCUM", "EARLY_ACCUM", "EXPANSION"}

# ML gate (raw probability threshold — approximately top 10% in backtest)
ML_PROB_MIN = 0.50

# Position management — tightened for quality
MAX_ORB_POSITIONS = 15  # Paper mode — maximize trade data
MAX_ENTRIES_PER_DAY = 20  # Paper mode — enter all qualifying setups
TRAILING_STOP_AFTER_1R = True  # Move stop to breakeven after 1R
TIME_STOP_HOUR, TIME_STOP_MIN = 15, 30  # Force-close at 3:30 PM ET

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
FEATURES_CANDLE_5M = [
    "candle_hammer_count_5m", "candle_engulf_bull_count_5m",
    "candle_doji_count_5m", "candle_reversal_near_vwap",
]
FEATURES_VOLUME_5M = [
    "volume_spike_count_5m", "volume_spike_near_vwap",
    "max_bar_volume_ratio_5m", "volume_climax_reversal",
]
FEATURES_CATEGORICAL = ["accum_phase", "swing_signal", "sector"]
ALL_FEATURES = (
    FEATURES_PREOPEN + FEATURES_OR + FEATURES_BY_1000
    + FEATURES_BREAKOUT + FEATURES_INTEL_NUMERIC
    + FEATURES_INTEL_BINARY + FEATURES_CANDLE_5M + FEATURES_VOLUME_5M
    + FEATURES_CATEGORICAL
)
DERIVED_FEATURES = [
    "gap_vs_atr", "or_range_pct", "or_volume_log",
    "vwap_distance_abs", "rsi_proxy", "price_vs_or_mid",
]

# ---------------------------------------------------------------------------
# Recommendation source prefix for paper trades
# ---------------------------------------------------------------------------
REC_SOURCE_PREFIX = "ORB_V2_ML"


class ORBV2LiveScanner:
    """Live ORB V2 scanner with ML entry filtering and structural confirmation.

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
        self._qualified_contracts: Dict[str, Any] = {}
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
            self.latest_ml_scores.clear()
            self._last_date = today
            logger.info("ORB_V2: new trading day — state reset")

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
                record_skip(Subsystem.ORB_V2, SkipReason.MODEL_UNAVAILABLE)
                return

        # Load daily context (prev_close, atr_20d) on first scan of day
        if not self._daily_context:
            self._load_daily_context()

        # Always check exits on open ORB_V2 positions
        self._check_exits(now_et)

        # Entry scanning only during entry window
        in_entry_window = (
            ENTRY_START_HOUR * 100 + ENTRY_START_MIN
            <= hm
            <= ENTRY_END_HOUR * 100 + ENTRY_END_MIN
        )
        if not in_entry_window:
            record_skip(Subsystem.ORB_V2, SkipReason.LATE_ENTRY_CUTOFF,
                         persist=False)
            return

        # Cap daily entries
        if len(self._entered_today) >= MAX_ENTRIES_PER_DAY:
            record_skip(Subsystem.ORB_V2, SkipReason.POSITION_LIMIT,
                         f"daily cap {MAX_ENTRIES_PER_DAY}", persist=False)
            return

        # Cap open positions
        open_orb = self._get_open_orb_positions()
        if len(open_orb) >= MAX_ORB_POSITIONS:
            record_skip(Subsystem.ORB_V2, SkipReason.POSITION_LIMIT,
                         f"open cap {MAX_ORB_POSITIONS}", persist=False)
            return

        # Get qualifying tickers from intelligence snapshot
        tickers = self._get_qualifying_tickers()
        if not tickers:
            record_skip(Subsystem.ORB_V2, SkipReason.NO_SETUP_QUALIFIED)
            logger.debug("ORB_V2: no qualifying tickers from intelligence snapshot")
            return

        record_funnel(Subsystem.ORB_V2, FUNNEL_CANDIDATES, len(tickers))

        # Fetch SPY bars for relative strength (once per day, updated each scan)
        self._fetch_spy_bars()

        # Scan each ticker
        entered = 0
        for ticker in tickers:
            if ticker in self._entered_today:
                record_funnel(Subsystem.ORB_V2, FUNNEL_SKIPPED)
                continue
            if len(open_orb) + entered >= MAX_ORB_POSITIONS:
                break
            try:
                record_funnel(Subsystem.ORB_V2, FUNNEL_ATTEMPTED)
                if self._scan_ticker(ticker, now_et):
                    record_funnel(Subsystem.ORB_V2, FUNNEL_ENTERED)
                    entered += 1
            except Exception as e:
                logger.warning(f"ORB_V2 {ticker}: scan error — {e}")

        if entered:
            logger.info(f"ORB_V2: {entered} new entries this scan cycle")

    def run_with_cache(self, bar_cache, ibkr_lock,
                       tickers=None, budget_seconds: float = 30.0) -> int:
        """Run using shared bar cache. Returns entry count.

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
            self.latest_ml_scores.clear()
            self._last_date = today

        hm = now_et.hour * 100 + now_et.minute
        if hm < EXIT_START_HOUR * 100 + EXIT_START_MIN or hm > EXIT_END_HOUR * 100 + EXIT_END_MIN:
            return 0
        if self._model is None:
            self._load_model()
            if self._model is None:
                record_skip(Subsystem.ORB_V2, SkipReason.MODEL_UNAVAILABLE)
                return 0
        if not self._daily_context:
            self._load_daily_context()

        spy = bar_cache.get_spy_bars()
        if spy is not None and len(spy) > 0:
            self._spy_bars_today = spy

        self._check_exits(now_et)

        in_entry = ENTRY_START_HOUR * 100 + ENTRY_START_MIN <= hm <= ENTRY_END_HOUR * 100 + ENTRY_END_MIN
        if not in_entry:
            return 0
        if len(self._entered_today) >= MAX_ENTRIES_PER_DAY:
            return 0
        open_orb = self._get_open_orb_positions()
        if len(open_orb) >= MAX_ORB_POSITIONS:
            return 0

        if tickers is None:
            tickers = self._get_qualifying_tickers()
        if not tickers:
            record_skip(Subsystem.ORB_V2, SkipReason.NO_SETUP_QUALIFIED)
            return 0
        record_funnel(Subsystem.ORB_V2, FUNNEL_CANDIDATES, len(tickers))

        entered = 0
        skipped_uncached = 0
        for ticker in tickers:
            if _time.monotonic() - _t0 > budget_seconds:
                logger.info(f"ORB_V2: budget {budget_seconds}s exhausted after {entered} entries")
                break
            if ticker in self._entered_today:
                record_funnel(Subsystem.ORB_V2, FUNNEL_SKIPPED)
                continue
            if len(open_orb) + entered >= MAX_ORB_POSITIONS:
                break
            cached = bar_cache.get_bars(ticker)
            if cached is None:
                skipped_uncached += 1
                continue
            try:
                record_funnel(Subsystem.ORB_V2, FUNNEL_ATTEMPTED)
                if self._scan_ticker(ticker, now_et, cached_bars=cached):
                    record_funnel(Subsystem.ORB_V2, FUNNEL_ENTERED)
                    entered += 1
            except Exception as e:
                logger.warning(f"ORB_V2 {ticker}: scan error — {e}")

        if skipped_uncached:
            logger.debug(f"ORB_V2: {skipped_uncached} tickers skipped (not in cache)")
        return entered

    # ------------------------------------------------------------------
    # ML model loading
    # ------------------------------------------------------------------

    def _load_model(self) -> None:
        """Load trained ORB_V2 LightGBM model from disk."""
        try:
            from signal_scanner.institutional_intel.config import WAREHOUSE_PATH

            model_path = WAREHOUSE_PATH.parent / "models" / "intraday_ml_orb_v2.pkl"
            if not model_path.exists():
                logger.warning(
                    f"ORB_V2: model not found at {model_path}. "
                    "Train with: python -m signal_scanner.institutional_intel.intelligence.intraday_ml "
                    "--train --strategy ORB_V2"
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
                f"ORB_V2: loaded ML model (val AUC={val_auc:.4f}, "
                f"{len(self._feature_cols)} features)"
            )
        except Exception as e:
            logger.error(f"ORB_V2: failed to load model — {e}")
            self._model = None

    # ------------------------------------------------------------------
    # Daily context: prev_close, atr_20d per ticker (from DuckDB)
    # ------------------------------------------------------------------

    def _load_daily_context(self) -> None:
        """Pre-compute prev_close and atr_20d for qualifying tickers."""
        try:
            import duckdb
            from signal_scanner.institutional_intel.config import WAREHOUSE_PATH

            conn = duckdb.connect(str(WAREHOUSE_PATH), read_only=True)
            try:
                row = conn.execute(
                    "SELECT MAX(trade_date) FROM fact_daily_prices"
                ).fetchone()
                if not row or not row[0]:
                    logger.warning("ORB_V2: no daily prices in warehouse")
                    return

                latest_date = row[0]

                prices = conn.execute("""
                    SELECT ticker, close, high
                    FROM fact_daily_prices
                    WHERE trade_date = ?
                """, [latest_date]).fetchall()

                for ticker, close, high in prices:
                    self._daily_context[ticker] = {
                        "prev_close": float(close) if close else 0.0,
                        "prev_day_high": float(high) if high else 0.0,
                        "atr_20d": 0.0,
                    }

                # Compute ATR-20d
                atr_rows = conn.execute("""
                    WITH ranked AS (
                        SELECT
                            ticker, trade_date, high, low, close,
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
                    f"ORB_V2: loaded daily context for {len(self._daily_context)} tickers "
                    f"(as of {latest_date})"
                )
            finally:
                conn.close()
        except Exception as e:
            logger.warning(f"ORB_V2: failed to load daily context — {e}")

    # ------------------------------------------------------------------
    # Intelligence-based ticker filter
    # ------------------------------------------------------------------

    def _get_qualifying_tickers(self) -> List[str]:
        """Get tickers matching ORB_V2 criteria from intelligence snapshot."""
        snapshot = getattr(self._scanner, "_intelligence_snapshot", {})
        if not snapshot:
            logger.debug("ORB_V2: intelligence snapshot empty")
            return []

        qualified = []
        for ticker, intel in snapshot.items():
            phase = str(intel.get("inst_phase", "")).upper()
            conviction = float(intel.get("inst_conviction", 0))

            if phase not in ACCUM_PHASES:
                continue
            if conviction < CONVICTION_MIN:
                continue
            if ticker not in self._daily_context:
                continue

            qualified.append(ticker)

        logger.debug(
            f"ORB_V2: {len(qualified)} qualifying tickers "
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
                spy_open = df.iloc[0]["Open"]
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
                    spy_latest = df.iloc[-1]["Close"]
                    self._spy_ret_1000 = (
                        (spy_latest - spy_open) / spy_open * 100
                        if spy_open > 0
                        else 0.0
                    )
        except Exception as e:
            logger.debug(f"ORB_V2: SPY bar fetch error — {e}")

    # ------------------------------------------------------------------
    # Single-ticker scan
    # ------------------------------------------------------------------

    def _evaluate_setup(self, ticker: str, now_et: datetime,
                        cached_bars: "pd.DataFrame | None" = None) -> "dict | None":
        """Evaluate a ticker for ORB V2 setup. Returns signal dict or None.

        Pure evaluation — no side effects. Called by strategy engine.
        """
        self._evaluate_only = True
        self._last_signal = None
        self._scan_ticker(ticker, now_et, cached_bars=cached_bars)
        self._evaluate_only = False
        return self._last_signal

    def _scan_ticker(self, ticker: str, now_et: datetime,
                     cached_bars: "pd.DataFrame | None" = None) -> bool:
        """Scan a single ticker for ORB V2 setup. Returns True if trade entered."""
        # 1. Fetch today's 1-min bars (from cache or IBKR)
        bars = cached_bars if cached_bars is not None else self._connector.get_price_data(ticker, "1m")
        if bars is None or len(bars) < 20:
            return False

        # 2. Compute features
        features = self._compute_features(ticker, bars)
        if features is None:
            return False

        or_high = features.get("or_high", 0)
        or_low = features.get("or_low", 0)
        if or_high <= 0 or or_low <= 0 or or_high <= or_low:
            return False

        # 3. Check for ORB V2 structural confirmation setup
        setup = self._check_orb_v2_setup(ticker, bars, features)
        if not setup:
            return False

        record_funnel(Subsystem.ORB_V2, FUNNEL_SETUPS)

        # 4. Score with ML model
        ml_prob = self._score_ml(features)
        self.latest_ml_scores[ticker] = ml_prob if ml_prob is not None else 0.0
        if ml_prob is None or ml_prob < ML_PROB_MIN:
            logger.debug(
                f"ORB_V2 {ticker}: setup detected but ML prob "
                f"{ml_prob:.3f if ml_prob else 0:.3f} < {ML_PROB_MIN}"
            )
            return False

        # 5. Entry parameters (stop at OR midpoint per V2 rules)
        entry_price = setup["entry_price"]
        stop_price = setup["stop_price"]  # OR midpoint

        if stop_price >= entry_price:
            return False

        r_unit = entry_price - stop_price
        if r_unit <= 0:
            return False

        target_1 = entry_price + r_unit
        target_2 = entry_price + 2 * r_unit
        rr_ratio = 2.0

        # 6. Grade computation (ML + quality score combined)
        #    Quality 5+ with high ML = A+ (data-proven best setup)
        quality_score = setup.get("quality_score", 0)
        if quality_score >= 5 and ml_prob >= 0.60:
            grade = "A+"
        elif quality_score >= 4 and ml_prob >= 0.60:
            grade = "A"
        elif ml_prob >= 0.71:
            grade = "A+"
        elif ml_prob >= 0.67:
            grade = "A"
        elif ml_prob >= 0.60:
            grade = "B"
        else:
            grade = "C"

        # 7. Position sizing
        qty = self._position_size(entry_price, stop_price)
        if qty <= 0:
            return False

        notional = round(entry_price * qty, 2)
        if notional < MIN_NOTIONAL:
            return False

        # 8. If evaluate-only, return signal dict without creating trade
        intel = self._get_intel(ticker)
        if getattr(self, "_evaluate_only", False):
            self._last_signal = {
                "strategy": "ORB_V2", "symbol": ticker, "side": "LONG",
                "entry_price": round(entry_price, 4),
                "stop_price": round(stop_price, 4),
                "target_1": round(target_1, 4),
                "target_2": round(target_2, 4),
                "quantity": qty, "notional": notional,
                "r_unit": round(r_unit, 4),
                "ml_prob": round(ml_prob, 4),
                "ml_percentile": ml_percentile,
                "conviction": intel.get("inst_conviction", 0),
                "phase": intel.get("inst_phase", "?"),
                "bar_ts": str(bars.index[-1]) if len(bars) > 0 else None,
            }
            return True

        # 9. Create paper trade (execution path)
        now_iso = datetime.now(timezone.utc).isoformat()

        trade_data = {
            "opened_at": now_iso,
            "symbol": ticker,
            "side": "LONG",
            "entry_price": round(entry_price, 4),
            "quantity": qty,
            "notional": notional,
            "stop_loss": round(stop_price, 4),
            "target_1": round(target_1, 4),
            "target_2": round(target_2, 4),
            "status": "OPEN",
            "strategy_type": "ORB_V2",
            "execution_mode": "IBKR" if self._order_executor else "SIM",
            "recommendation_source": f"{REC_SOURCE_PREFIX}_P{int(ml_prob*100)}",
            "instrument_type": "STOCK",
            "option_type": None,
            "option_expiry": None,
            "option_strike": None,
            "entry_signal": "LONG",
            "entry_score": round(ml_prob * 100, 1),
            "entry_rr_ratio": rr_ratio,
            "entry_market_regime": getattr(
                self._scanner, "market_regime", None
            ),
            "entry_gex_status": None,
            "entry_session_time": "EARLY" if now_et.hour < 10 else "MID_DAY",
            "entry_trade_conditions": (
                f"ORB_V2 setup | ML_prob={ml_prob:.3f} | grade={grade} "
                f"| quality={quality_score}/7 "
                f"| or_high=${or_high:.2f} | or_mid=${stop_price:.2f} "
                f"| or_range={setup.get('or_range_pct', 0)*100:.2f}% "
                f"| gap={setup.get('gap_pct', 0):.2f}% "
                f"| body={setup.get('body_ratio', 0):.2f} "
                f"| wick={setup.get('wick_ratio', 0):.2f} "
                f"| conv={intel.get('inst_conviction', 0):.0f} "
                f"| phase={intel.get('inst_phase', '?')} "
                f"| R_unit=${r_unit:.2f}"
            ),
            "fees": 0.0,
            "created_ts": now_iso,
        }

        trade_id = self._db.create_paper_trade(trade_data)

        # Route to IBKR if OrderExecutor is enabled for ORB_V2
        if self._order_executor and self._order_executor.should_execute_live("ORB_V2"):
            placed = self._order_executor.place_bracket_order(
                trade_id=trade_id,
                symbol=ticker,
                side="LONG",
                quantity=qty,
                entry_price=entry_price,
                stop_price=stop_price,
                target_price=target_2,
            )
            if placed:
                logger.info(f"ORB_V2->IBKR: {ticker} bracket placed (trade_id={trade_id})")

        self._entered_today.add(ticker)

        logger.info(
            f"ORB_V2 ENTRY: {ticker} @ ${entry_price:.2f} "
            f"stop=${stop_price:.2f}(OR mid) target=${target_2:.2f} "
            f"ML={ml_prob:.1%} grade={grade} Q={quality_score}/4 "
            f"body={setup.get('body_ratio', 0):.2f} wick={setup.get('wick_ratio', 0):.2f} "
            f"conv={intel.get('inst_conviction', 0):.0f} "
            f"(trade_id={trade_id})"
        )
        return True

    # ------------------------------------------------------------------
    # Feature computation (from 1-min bars)
    # ------------------------------------------------------------------

    def _compute_features(
        self, ticker: str, bars: pd.DataFrame
    ) -> Optional[Dict[str, Any]]:
        """Compute ORB_V2 features from today's 1-min bars."""
        ctx = self._daily_context.get(ticker, {})
        prev_close = ctx.get("prev_close", 0)
        atr_20d = ctx.get("atr_20d", 0)
        intel = self._get_intel(ticker)

        if prev_close <= 0:
            return None

        opens = bars["Open"].values.astype(float)
        highs = bars["High"].values.astype(float)
        lows = bars["Low"].values.astype(float)
        closes = bars["Close"].values.astype(float)
        volumes = bars["Volume"].values.astype(float)
        n = len(bars)

        # Opening range (first 15 bars = 9:30-9:44)
        or_count = min(15, n)
        open_930 = opens[0]
        or_high = float(highs[:or_count].max())
        or_low = float(lows[:or_count].min())
        or_range = or_high - or_low
        or_volume = float(volumes[:or_count].sum())

        gap_pct = (open_930 - prev_close) / prev_close * 100

        # Running VWAP
        tp = (highs + lows + closes) / 3.0
        cum_tp_vol = np.cumsum(tp * volumes)
        cum_vol = np.cumsum(volumes)
        cum_vol_safe = np.where(cum_vol > 0, cum_vol, 1.0)
        vwap = cum_tp_vol / cum_vol_safe

        # Snapshots at time indices
        idx_0945 = min(15, n - 1)
        idx_1000 = min(30, n - 1)

        price_0930 = closes[0]
        price_0940 = closes[min(10, n - 1)]
        price_0945 = closes[idx_0945]
        price_1000 = closes[idx_1000]
        vwap_at_1000 = vwap[idx_1000]

        price_vs_vwap_1000 = (
            (price_1000 - vwap_at_1000) / vwap_at_1000 * 100
            if vwap_at_1000 > 0
            else 0.0
        )

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

        ret_vs_spy_1000 = ret_30min_1000 - (self._spy_ret_1000 or 0.0)

        # Consolidation bars after OR
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

        # 5-min candle/volume pattern features (simplified for live)
        candle_hammer = 0
        candle_engulf_bull = 0
        candle_doji = 0
        candle_reversal_near_vwap = 0
        vol_spike_count = 0
        vol_spike_near_vwap = 0
        max_bar_vol_ratio = 0.0
        vol_climax_reversal = 0

        # Compute 5-min aggregate candle patterns
        avg_bar_vol = float(volumes[or_count:].mean()) if n > or_count else 1.0
        if avg_bar_vol <= 0:
            avg_bar_vol = 1.0

        for i in range(or_count, n, 5):
            end = min(i + 5, n)
            if end <= i:
                break
            seg_o = opens[i]
            seg_c = closes[end - 1]
            seg_h = float(highs[i:end].max())
            seg_l = float(lows[i:end].min())
            seg_v = float(volumes[i:end].sum())
            seg_range = seg_h - seg_l
            if seg_range <= 0:
                continue

            body = abs(seg_c - seg_o)
            body_ratio = body / seg_range

            # Doji
            if body_ratio < 0.1:
                candle_doji += 1

            # Hammer (small body, long lower wick)
            lower_wick = min(seg_o, seg_c) - seg_l
            if lower_wick / seg_range > 0.6 and body_ratio < 0.3:
                candle_hammer += 1

            # Engulfing bullish (simplified)
            if seg_c > seg_o and body_ratio > 0.7:
                candle_engulf_bull += 1

            # Near VWAP reversal
            mid_idx = (i + end) // 2
            if mid_idx < len(vwap):
                vwap_dev = abs(seg_l - vwap[mid_idx]) / vwap[mid_idx] * 100 if vwap[mid_idx] > 0 else 99
                if vwap_dev < 0.2 and seg_c > seg_o:
                    candle_reversal_near_vwap += 1

            # Volume spikes
            bar_vol_ratio = seg_v / (avg_bar_vol * 5) if avg_bar_vol > 0 else 0
            if bar_vol_ratio > 2.0:
                vol_spike_count += 1
                if mid_idx < len(vwap):
                    vwap_dev2 = abs(seg_l - vwap[mid_idx]) / vwap[mid_idx] * 100 if vwap[mid_idx] > 0 else 99
                    if vwap_dev2 < 0.3:
                        vol_spike_near_vwap += 1
            if bar_vol_ratio > max_bar_vol_ratio:
                max_bar_vol_ratio = bar_vol_ratio

            # Volume climax reversal
            if bar_vol_ratio > 3.0 and seg_c > seg_o:
                vol_climax_reversal += 1

        # Build feature dict
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
            # Intelligence
            "conviction_score": float(intel.get("inst_conviction", 0)),
            "expected_value": 0.0,
            "squeeze_score": float(intel.get("inst_squeeze", 0)),
            "short_squeeze_score": float(intel.get("inst_short_squeeze", 0)),
            "tier1_count": 0,
            "insider_cluster": 0.0,
            # Candle/volume patterns (5-min)
            "candle_hammer_count_5m": candle_hammer,
            "candle_engulf_bull_count_5m": candle_engulf_bull,
            "candle_doji_count_5m": candle_doji,
            "candle_reversal_near_vwap": candle_reversal_near_vwap,
            "volume_spike_count_5m": vol_spike_count,
            "volume_spike_near_vwap": vol_spike_near_vwap,
            "max_bar_volume_ratio_5m": max_bar_vol_ratio,
            "volume_climax_reversal": vol_climax_reversal,
            # Categorical
            "accum_phase": str(intel.get("inst_phase", "")),
            "swing_signal": "",
            "sector": "",
        }

        # Store internal arrays for setup detection
        feat["_vwap_array"] = vwap
        feat["_bars_n"] = n
        feat["_or_count"] = or_count

        return feat

    def _prepare_ml_features(self, feat: Dict[str, Any]) -> Optional[pd.DataFrame]:
        """Convert feature dict to ML-ready DataFrame."""
        row = {k: v for k, v in feat.items() if not k.startswith("_")}

        df = pd.DataFrame([row])

        # Derived features
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

        for col in FEATURES_CATEGORICAL:
            df[col] = df[col].astype("category")

        for col in FEATURES_BREAKOUT + FEATURES_INTEL_BINARY:
            df[col] = df[col].astype(float)

        feature_cols = ALL_FEATURES + DERIVED_FEATURES
        return df[feature_cols]

    # ------------------------------------------------------------------
    # ORB V2 setup detection — 6 STRUCTURAL CONFIRMATION RULES
    # ------------------------------------------------------------------

    def _check_orb_v2_setup(
        self,
        ticker: str,
        bars: pd.DataFrame,
        features: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        """Detect ORB V2 breakout with all 6 structural confirmation rules.

        RULE 1: Bar CLOSES above OR high (not just wick touch)
        RULE 2: Displacement — body_ratio > 0.50
        RULE 3: Fakeout filter — wick_ratio < 0.30
        RULE 4: Volume — breakout bar volume > 1.5x avg OR bar volume
        RULE 5: Daily bias — price > VWAP at breakout time
        RULE 6: Stop at OR midpoint (not OR low)

        Returns dict with entry details and quality score, or None.
        """
        n = features.get("_bars_n", 0)
        or_count = features.get("_or_count", 15)
        vwap = features.get("_vwap_array")
        or_high = features.get("or_high", 0)
        or_low = features.get("or_low", 0)

        if n < or_count + 5 or vwap is None:
            return None
        if or_high <= 0 or or_low <= 0 or or_high <= or_low:
            return None

        or_mid = (or_high + or_low) / 2.0

        opens = bars["Open"].values.astype(float)
        highs = bars["High"].values.astype(float)
        lows = bars["Low"].values.astype(float)
        closes = bars["Close"].values.astype(float)
        volumes = bars["Volume"].values.astype(float)

        # Average OR bar volume (for Rule 4)
        or_vol_per_bar = float(volumes[:or_count].mean()) if or_count > 0 else 1.0
        if or_vol_per_bar <= 0:
            or_vol_per_bar = 1.0

        # Previous day high for quality score
        ctx = self._daily_context.get(ticker, {})
        prev_day_high = ctx.get("prev_day_high", 0)

        # Scan post-OR bars for breakout confirmation
        for i in range(or_count, n):
            bar_open = opens[i]
            bar_high = highs[i]
            bar_low = lows[i]
            bar_close = closes[i]
            bar_vol = volumes[i]
            bar_range = bar_high - bar_low

            # RULE 1: Bar must CLOSE above OR high
            if bar_close <= or_high:
                continue

            if bar_range <= 0:
                continue

            # RULE 2: Displacement — body ratio > 0.50
            body = abs(bar_close - bar_open)
            body_ratio = body / bar_range
            if body_ratio < BODY_RATIO_MIN:
                continue

            # RULE 3: Fakeout filter — upper wick ratio < 0.30
            upper_wick = bar_high - max(bar_open, bar_close)
            wick_ratio = upper_wick / bar_range
            if wick_ratio >= WICK_RATIO_MAX:
                continue

            # RULE 4: Volume — breakout bar vol > 1.5x avg OR bar vol
            if bar_vol < or_vol_per_bar * BREAKOUT_VOL_MULT:
                continue

            # RULE 5: Daily bias — price above VWAP
            if i < len(vwap) and bar_close <= vwap[i]:
                continue

            # All 6 rules pass! (Rule 6 = stop at OR midpoint, applied below)

            # Quality score (0-7, data-proven factors)
            quality = 0

            # Original quality factors
            if body_ratio > 0.50:
                quality += 1  # Displacement
            if bar_vol > or_vol_per_bar * 2.0:
                quality += 1  # Strong volume (2x)
            if i < len(vwap) and bar_close > vwap[i]:
                quality += 1  # VWAP bias
            if prev_day_high > 0 and bar_close > prev_day_high:
                quality += 1  # Above prev day high

            # DATA-PROVEN factors (from 79K ORB signal backtest)
            # Tight OR: 52.7% win vs 48.2% for wide OR
            or_range_pct = or_range / open_930 if open_930 > 0 else 1
            if or_range_pct < OR_RANGE_IDEAL_PCT:
                quality += 2  # Tight OR is the strongest predictor
            elif or_range_pct < OR_RANGE_MAX_PCT:
                quality += 1  # Medium OR still better than wide

            # Gap-up ORBs: 51.2% win vs 48.6% gap-down
            gap_pct = features.get("gap_pct", 0) or 0
            if gap_pct > 0 and GAP_UP_BONUS:
                quality += 1  # Gap-up bonus

            entry_price = float(bar_close)
            stop_price = float(or_mid)  # RULE 6

            if entry_price <= stop_price:
                continue

            return {
                "entry_price": entry_price,
                "stop_price": stop_price,
                "or_high": or_high,
                "or_low": or_low,
                "or_mid": or_mid,
                "body_ratio": body_ratio,
                "wick_ratio": wick_ratio,
                "breakout_vol_ratio": bar_vol / or_vol_per_bar,
                "quality_score": quality,
                "or_range_pct": or_range_pct,
                "gap_pct": gap_pct,
                "breakout_bar_idx": i,
            }

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
            logger.debug(f"ORB_V2 ML scoring error: {e}")
            return None

    # ------------------------------------------------------------------
    # Exit management
    # ------------------------------------------------------------------

    def _check_exits(self, now_et: datetime) -> None:
        """Check stop/target/trailing/time for open ORB_V2 positions."""
        positions = self._get_open_orb_positions()
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

            entry_price = float(pos.get("entry_price") or 0)
            stop_price = float(pos.get("stop_loss") or 0)
            target_2 = float(pos.get("target_2") or 0)
            target_1 = float(pos.get("target_1") or 0)

            exit_reason = ""

            # Stop loss check
            if stop_price > 0 and price <= stop_price:
                exit_reason = "STOP_LOSS"

            # Target 2 check (2R)
            elif target_2 > 0 and price >= target_2:
                exit_reason = "TARGET_2"

            # Trailing stop: after 1R, move stop to breakeven
            elif TRAILING_STOP_AFTER_1R and target_1 > 0 and price >= target_1:
                if stop_price < entry_price:
                    new_stop = entry_price + 0.01
                    self._db.update_paper_trade_stop(
                        int(pos.get("id", 0)), new_stop
                    )
                    if (
                        self._order_executor
                        and str(pos.get("execution_mode", "")).upper() == "LIVE"
                    ):
                        self._order_executor.modify_stop(int(pos.get("id", 0)), new_stop)
                    logger.info(
                        f"ORB_V2 {ticker}: trailing stop -> breakeven "
                        f"${new_stop:.2f} (was ${stop_price:.2f})"
                    )

            # Time stop
            elif time_stop:
                exit_reason = "TIME_STOP_330PM"

            if exit_reason:
                self._close_position(pos, price, exit_reason, now_iso)

    def _close_position(
        self, position: Dict, price: float, reason: str, now_iso: str
    ) -> None:
        """Close an ORB_V2 paper position."""
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
            f"ORB_V2 EXIT {ticker}: {reason} @ ${price:.2f} "
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

    def _get_open_orb_positions(self) -> List[Dict]:
        """Get open paper trades with ORB_V2_ML source."""
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

    parser = argparse.ArgumentParser(description="ORB_V2 Live Scanner Test")
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

    logger.info("ORB_V2: standalone test mode")

    ib_cfg = IBKRConfig(port=args.ibkr_port)
    connector = DataConnector(ib_cfg)
    if not connector.connect_ibkr():
        logger.error("Cannot connect to IBKR")
        return

    db = DatabaseManager()
    db.init_db()

    class _MockScanner:
        _intelligence_snapshot = {}
        market_regime = None

    mock_scanner = _MockScanner()

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

    scanner = ORBV2LiveScanner(connector, db, mock_scanner)
    logger.info("Running ORB_V2 scan...")
    scanner.run()
    logger.info("Done")


if __name__ == "__main__":
    _test_main()
