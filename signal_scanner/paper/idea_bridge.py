"""Idea-to-Trade Bridge — auto-enters paper trades from intelligence idea sources.

Runs after each scan cycle. Pulls top ideas from:
  1. Swing Ideas (high-conviction BUY/SHORT from intelligence_scores)
  2. AI Triple Lock (conviction + ML + insider convergence)

Each source is clearly tagged on the paper trade for traceability.
Uses ATR-based stops and targets derived from actual price data.

Usage:
    Called from multi_symbol_scanner.py after scan_watchlist() completes.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Dict, List, Optional

from loguru import logger

from signal_scanner.core.telemetry import (
    record_skip, record_funnel, SkipReason, Subsystem,
    FUNNEL_CANDIDATES, FUNNEL_SETUPS, FUNNEL_ATTEMPTED,
    FUNNEL_ENTERED, FUNNEL_SKIPPED,
)
from signal_scanner.institutional_intel.config import safe_duckdb_connect

try:
    from signal_scanner.institutional_intel.intelligence.regime_hmm import (
        DailyRegimeHMM, REGIME_NAMES,
    )
    _HMM_AVAILABLE = True
except ImportError:
    _HMM_AVAILABLE = False

try:
    from zoneinfo import ZoneInfo
    NY_TZ = ZoneInfo("America/New_York")
except ImportError:
    NY_TZ = timezone.utc


class IdeaBridge:
    """Bridges intelligence ideas to paper trades via PaperTrader.enter_idea_trade()."""

    MAX_IDEAS_PER_CYCLE = 50  # Paper mode — enter all qualifying ideas
    MIN_CONVICTION = 65       # Lower bar to generate more trade data
    STOP_ATR_MULT = 2.0      # Stop = entry - 2x ATR

    def __init__(self, paper_trader, db_manager) -> None:
        self._pt = paper_trader
        self._db = db_manager
        # Pull target multiples from ScannerConfig — set once at construction.
        # 1R primary / 1.5R stretch picked from rr_analysis_live_config.py
        # backtest (2026-04-25). Override via cfg if you want to A/B.
        cfg = paper_trader._cfg
        self.TARGET_RR = float(getattr(cfg, "paper_idea_target_r_multiple", 1.0))
        self.STRETCH_RR = float(getattr(cfg, "paper_idea_stretch_target_r_multiple", 1.5))
        self._entered_today: set = set()
        self._last_date: str = ""
        self._hmm: Optional[DailyRegimeHMM] = None
        self._hmm_state: Optional[int] = None
        self._hmm_name: str = ""
        self._load_hmm()
        # Idea ledger for persistent lifecycle
        self._idea_ledger = db_manager.idea_ledger

    def _load_hmm(self) -> None:
        """Load saved HMM model for regime gating."""
        if not _HMM_AVAILABLE:
            logger.debug("IdeaBridge: hmmlearn not available, regime gate disabled")
            return
        try:
            hmm = DailyRegimeHMM()
            hmm.load()
            self._hmm = hmm
            logger.info("IdeaBridge: HMM regime model loaded (fitted {})", hmm._fit_date)
        except FileNotFoundError:
            logger.info("IdeaBridge: no saved HMM model, regime gate disabled")
        except Exception as e:
            logger.warning("IdeaBridge: HMM load failed: {}", e)

    def _update_regime(self) -> None:
        """Refresh current regime state from HMM."""
        if self._hmm is None:
            return
        try:
            state, probs, name = self._hmm.current_regime()
            self._hmm_state = state
            self._hmm_name = name
            logger.info("IdeaBridge regime: {} (state {})", name, state)
        except Exception as e:
            logger.warning("IdeaBridge: regime update failed: {}", e)
            self._hmm_state = None

    def _regime_allows_side(self, side: str) -> bool:
        """Check if the current HMM regime allows this trade side."""
        if self._hmm is None or self._hmm_state is None:
            return True  # no model = no gate

        if side == "LONG":
            allowed = self._hmm.is_long_allowed(self._hmm_state)
        elif side == "SHORT":
            allowed = self._hmm.is_short_allowed(self._hmm_state)
        else:
            allowed = True

        if not allowed:
            record_skip(Subsystem.IDEA_BRIDGE, SkipReason.REGIME_BLOCKED,
                         f"{side} blocked by {self._hmm_name} state {self._hmm_state}")
            logger.debug(
                "IdeaBridge: {} blocked by regime {} (state {})",
                side, self._hmm_name, self._hmm_state,
            )
        return allowed

    def process_ideas(self) -> int:
        """Pull ideas from all sources and enter qualifying ones as paper trades.

        Returns number of trades entered.
        """
        now = datetime.now(timezone.utc)
        today = now.astimezone(NY_TZ).strftime("%Y-%m-%d") if NY_TZ else now.strftime("%Y-%m-%d")

        # Reset daily tracking
        if today != self._last_date:
            self._entered_today = set()
            self._last_date = today
            self._update_regime()  # refresh regime once per day

        # Skip outside market hours (10:00 AM - 3:30 PM ET)
        et = now.astimezone(NY_TZ) if NY_TZ else now
        if et.hour < 10 or (et.hour >= 15 and et.minute >= 30) or et.hour >= 16:
            return 0

        # HMM regime gate: State 0 (CRASH) = no trades at all
        if self._hmm_state == 0:
            record_skip(Subsystem.IDEA_BRIDGE, SkipReason.REGIME_BLOCKED, "CRASH state 0")
            logger.info("IdeaBridge: REGIME BLOCKED — state 0 (CRASH), no entries")
            return 0

        entered = 0
        open_symbols = {p["symbol"] for p in self._db.get_open_paper_trades()}

        # Fetch price + ATR data for all candidates at once
        price_data = self._load_price_atr_data()
        if not price_data:
            logger.debug("IdeaBridge: no price data available")
            return 0

        # 1. Swing Ideas — highest conviction BUY/SHORT from intelligence
        try:
            swing_ideas = self._get_swing_ideas(open_symbols, price_data)
            _swing_sub = "idea_swing_buy"
            record_funnel(_swing_sub, FUNNEL_CANDIDATES, len([i for i in swing_ideas if i.get("side") == "LONG"]))
            record_funnel("idea_swing_short", FUNNEL_CANDIDATES, len([i for i in swing_ideas if i.get("side") == "SHORT"]))
            for idea in swing_ideas:
                _src = "idea_swing_buy" if idea.get("side") == "LONG" else "idea_swing_short"
                if entered >= self.MAX_IDEAS_PER_CYCLE:
                    break
                if idea["symbol"] in self._entered_today:
                    record_funnel(_src, FUNNEL_SKIPPED)
                    continue
                if not self._regime_allows_side(idea["side"]):
                    record_funnel(_src, FUNNEL_SKIPPED)
                    # Still persist idea even if regime blocks entry
                    self._idea_ledger.upsert_idea(idea)
                    continue
                record_funnel(_src, FUNNEL_SETUPS)
                record_funnel(_src, FUNNEL_ATTEMPTED)
                trade_id = self._persist_and_enter(idea, open_symbols)
                if trade_id:
                    record_funnel(_src, FUNNEL_ENTERED)
                    entered += 1
                else:
                    # Idea persisted but trade not entered (gates blocked)
                    self._idea_ledger.upsert_idea(idea)
                    record_funnel(_src, FUNNEL_SKIPPED)
        except Exception as e:
            logger.error("IdeaBridge swing ideas error: {}", e)

        # 2. SHORT Distribution Ideas — institutional exit pressure (works in DISTRIBUTION regime)
        try:
            short_ideas = self._get_short_distribution_ideas(open_symbols, price_data)
            _dist_sub = "idea_short_dist"
            record_funnel(_dist_sub, FUNNEL_CANDIDATES, len(short_ideas))
            for idea in short_ideas:
                if entered >= self.MAX_IDEAS_PER_CYCLE:
                    break
                if idea["symbol"] in self._entered_today:
                    record_funnel(_dist_sub, FUNNEL_SKIPPED)
                    continue
                if not self._regime_allows_side(idea["side"]):
                    record_funnel(_dist_sub, FUNNEL_SKIPPED)
                    self._idea_ledger.upsert_idea(idea)
                    continue
                record_funnel(_dist_sub, FUNNEL_SETUPS)
                record_funnel(_dist_sub, FUNNEL_ATTEMPTED)
                trade_id = self._persist_and_enter(idea, open_symbols)
                if trade_id:
                    record_funnel(_dist_sub, FUNNEL_ENTERED)
                    entered += 1
                else:
                    self._idea_ledger.upsert_idea(idea)
                    record_funnel(_dist_sub, FUNNEL_SKIPPED)
        except Exception as e:
            logger.error("IdeaBridge SHORT distribution ideas error: {}", e)

        # 3. AI Triple Lock — convergence of conviction + ML + insiders
        try:
            ai_ideas = self._get_triple_lock_ideas(open_symbols, price_data)
            _tl_sub = "idea_triple_lock"
            record_funnel(_tl_sub, FUNNEL_CANDIDATES, len(ai_ideas))
            for idea in ai_ideas:
                if entered >= self.MAX_IDEAS_PER_CYCLE:
                    break
                if idea["symbol"] in self._entered_today:
                    record_funnel(_tl_sub, FUNNEL_SKIPPED)
                    continue
                if not self._regime_allows_side(idea["side"]):
                    record_funnel(_tl_sub, FUNNEL_SKIPPED)
                    self._idea_ledger.upsert_idea(idea)
                    continue
                record_funnel(_tl_sub, FUNNEL_SETUPS)
                record_funnel(_tl_sub, FUNNEL_ATTEMPTED)
                trade_id = self._persist_and_enter(idea, open_symbols)
                if trade_id:
                    record_funnel(_tl_sub, FUNNEL_ENTERED)
                    entered += 1
                else:
                    self._idea_ledger.upsert_idea(idea)
                    record_funnel(_tl_sub, FUNNEL_SKIPPED)
        except Exception as e:
            logger.error("IdeaBridge AI Triple Lock error: {}", e)

        if entered > 0:
            logger.info("IdeaBridge: entered {} idea trades this cycle", entered)
        return entered

    def _persist_and_enter(self, idea: Dict, open_symbols: set) -> Optional[int]:
        """Persist idea to ledger and enter trade if possible.

        Returns trade_id if entered, None otherwise.
        """
        # Persist/update idea in ledger
        idea_id = self._idea_ledger.upsert_idea(idea)

        # Enter trade
        idea["market_regime"] = self._hmm_name or "UNKNOWN"
        trade_id = self._pt.enter_idea_trade(idea)
        if trade_id:
            # Link trade → idea
            self._idea_ledger.mark_entered(idea_id, trade_id)
            try:
                with self._db._get_connection() as conn:
                    conn.execute(
                        "UPDATE paper_trades SET idea_id = ? WHERE id = ?",
                        (idea_id, trade_id),
                    )
            except Exception:
                pass  # non-fatal: linkage is best-effort
            self._entered_today.add(idea["symbol"])
            open_symbols.add(idea["symbol"])
            return trade_id
        return None

    def _load_price_atr_data(self) -> Dict[str, Dict]:
        """Load latest close + ATR-20d for all tickers from fact_daily_prices."""
        conn = safe_duckdb_connect(read_only=True)
        if not conn:
            return {}
        try:
            rows = conn.execute("""
                WITH latest AS (
                    SELECT ticker, close, high, low,
                           LAG(close) OVER (PARTITION BY ticker ORDER BY trade_date) as prev_close
                    FROM fact_daily_prices
                    WHERE trade_date >= (SELECT MAX(trade_date) - INTERVAL '30' DAY FROM fact_daily_prices)
                ),
                atr AS (
                    SELECT ticker,
                           AVG(GREATEST(
                               high - low,
                               ABS(high - COALESCE(prev_close, close)),
                               ABS(low - COALESCE(prev_close, close))
                           )) as atr_20
                    FROM latest
                    WHERE prev_close IS NOT NULL
                    GROUP BY ticker
                    HAVING COUNT(*) >= 10
                ),
                last_price AS (
                    SELECT ticker, close
                    FROM fact_daily_prices
                    WHERE trade_date = (SELECT MAX(trade_date) FROM fact_daily_prices)
                )
                SELECT p.ticker, p.close, a.atr_20
                FROM last_price p
                JOIN atr a ON p.ticker = a.ticker
                WHERE p.close > 5
            """).fetchall()
            return {r[0]: {"close": r[1], "atr": r[2]} for r in rows}
        finally:
            conn.close()

    def _get_swing_ideas(self, open_symbols: set, price_data: Dict) -> List[Dict]:
        """Top swing ideas from intelligence_scores with BUY/SHORT signal."""
        conn = safe_duckdb_connect(read_only=True)
        if not conn:
            return []
        try:
            rows = conn.execute("""
                SELECT ticker, swing_signal, conviction_score, accum_phase,
                       ml_score_v2, triple_lock, price_above_200sma
                FROM intelligence_scores
                WHERE report_quarter = (
                    SELECT MAX(report_quarter) FROM intelligence_scores
                    WHERE data_quality_score >= 75
                )
                AND swing_signal IN ('BUY', 'SHORT')
                AND conviction_score >= ?
                AND accum_phase IN ('EARLY_ACCUM', 'ACTIVE_ACCUM', 'LATE_ACCUM')
                ORDER BY conviction_score DESC
                LIMIT 30
            """, [self.MIN_CONVICTION]).fetchall()

            ideas = []
            for r in rows:
                ticker = r[0]
                if ticker in open_symbols or ticker in self._entered_today:
                    continue
                if ticker not in price_data:
                    continue

                signal = r[1]
                side = "LONG" if signal == "BUY" else "SHORT"
                conviction = float(r[2])
                triple_lock = bool(r[5])
                above_200 = r[6]

                # For LONG: price_above_200sma should be 1 (unless Triple Lock)
                if side == "LONG" and above_200 == 0 and not triple_lock:
                    continue

                pd = price_data[ticker]
                entry = pd["close"]
                atr = pd["atr"]
                risk = atr * self.STOP_ATR_MULT

                if side == "LONG":
                    stop = round(entry - risk, 2)
                    target = round(entry + risk * self.TARGET_RR, 2)
                else:
                    stop = round(entry + risk, 2)
                    target = round(entry - risk * self.TARGET_RR, 2)

                if stop <= 0 or entry <= 0:
                    continue

                ideas.append({
                    "symbol": ticker,
                    "side": side,
                    "entry_price": round(entry, 2),
                    "stop_loss": stop,
                    "target_1": target,
                    "target_2": round(entry + risk * self.STRETCH_RR, 2) if side == "LONG" else round(entry - risk * self.STRETCH_RR, 2),
                    "source": f"SWING_IDEA_{signal}",
                    "conviction": conviction,
                    "accum_phase": r[3],
                    "ml_score": float(r[4]) if r[4] else None,
                    "score": conviction,
                    "rr_ratio": self.TARGET_RR,
                    "instrument_type": "STOCK",
                })
                if len(ideas) >= 10:
                    break
            return ideas
        finally:
            conn.close()

    def _get_short_distribution_ideas(self, open_symbols: set, price_data: Dict) -> List[Dict]:
        """SHORT ideas from distribution-phase tickers scored by short_conviction_engine."""
        conn = safe_duckdb_connect(read_only=True)
        if not conn:
            return []
        try:
            rows = conn.execute("""
                SELECT ticker, short_conviction_score, accum_phase,
                       ml_score_v2, price_above_200sma, price_momentum_90d
                FROM intelligence_scores
                WHERE report_quarter = (
                    SELECT MAX(report_quarter) FROM intelligence_scores
                    WHERE data_quality_score >= 75
                )
                AND short_swing_signal = 'SHORT'
                AND accum_phase IN ('DISTRIBUTION', 'DECLINE')
                ORDER BY short_conviction_score DESC
                LIMIT 15
            """).fetchall()

            ideas = []
            for r in rows:
                ticker = r[0]
                if ticker in open_symbols or ticker in self._entered_today:
                    continue
                if ticker not in price_data:
                    continue

                short_conv = float(r[1] or 0)
                pd = price_data[ticker]
                entry = pd["close"]
                atr = pd["atr"]
                risk = atr * self.STOP_ATR_MULT

                stop = round(entry + risk, 2)
                target = round(entry - risk * self.TARGET_RR, 2)

                if stop <= 0 or entry <= 0 or target <= 0:
                    continue

                ideas.append({
                    "symbol": ticker,
                    "side": "SHORT",
                    "entry_price": round(entry, 2),
                    "stop_loss": stop,
                    "target_1": target,
                    "target_2": round(entry - risk * self.STRETCH_RR, 2),
                    "source": "SWING_IDEA_SHORT_DIST",
                    "conviction": short_conv,
                    "accum_phase": r[2],
                    "ml_score": float(r[3]) if r[3] else None,
                    "score": short_conv,
                    "rr_ratio": self.TARGET_RR,
                    "instrument_type": "STOCK",
                })
                if len(ideas) >= 5:
                    break
            return ideas
        finally:
            conn.close()

    def _get_triple_lock_ideas(self, open_symbols: set, price_data: Dict) -> List[Dict]:
        """Triple Lock ideas — conviction + ML + insider convergence."""
        conn = safe_duckdb_connect(read_only=True)
        if not conn:
            return []
        try:
            rows = conn.execute("""
                SELECT ticker, conviction_score, accum_phase, ml_score_v2,
                       price_above_200sma
                FROM intelligence_scores
                WHERE report_quarter = (
                    SELECT MAX(report_quarter) FROM intelligence_scores
                    WHERE data_quality_score >= 75
                )
                AND conviction_score >= 70
                AND accum_phase IN ('EARLY_ACCUM', 'ACTIVE_ACCUM', 'LATE_ACCUM')
                AND ml_score_v2 >= 70
                AND triple_lock = TRUE
                AND swing_signal = 'BUY'
                AND price_above_200sma = 1
                ORDER BY conviction_score DESC
                LIMIT 10
            """).fetchall()

            ideas = []
            for r in rows:
                ticker = r[0]
                if ticker in open_symbols or ticker in self._entered_today:
                    continue
                if ticker not in price_data:
                    continue

                pd = price_data[ticker]
                entry = pd["close"]
                atr = pd["atr"]
                risk = atr * self.STOP_ATR_MULT

                stop = round(entry - risk, 2)
                target = round(entry + risk * self.TARGET_RR, 2)

                if stop <= 0:
                    continue

                ideas.append({
                    "symbol": ticker,
                    "side": "LONG",
                    "entry_price": round(entry, 2),
                    "stop_loss": stop,
                    "target_1": target,
                    "target_2": round(entry + risk * self.STRETCH_RR, 2),
                    "source": "AI_TRIPLE_LOCK",
                    "conviction": float(r[1]),
                    "accum_phase": r[2],
                    "ml_score": float(r[3]) if r[3] else None,
                    "score": float(r[1]),
                    "rr_ratio": self.TARGET_RR,
                    "instrument_type": "STOCK",
                })
            return ideas
        finally:
            conn.close()
