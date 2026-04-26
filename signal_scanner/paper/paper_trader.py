"""Paper trading execution engine.

Consumes MTF recommendations and tracks simulated entries/exits.
"""

from datetime import datetime, timezone
from math import ceil, floor
from typing import Dict, List, Optional

from loguru import logger

from signal_scanner.config import ScannerConfig
from signal_scanner.core.telemetry import (
    record_skip, record_funnel, SkipReason, Subsystem,
    FUNNEL_CANDIDATES, FUNNEL_SETUPS, FUNNEL_ATTEMPTED,
    FUNNEL_ENTERED, FUNNEL_SKIPPED,
)
from signal_scanner.database.db_manager import DatabaseManager

try:
    from zoneinfo import ZoneInfo
    NY_TZ = ZoneInfo("America/New_York")
except ImportError:
    NY_TZ = timezone.utc


class PaperTrader:
    """Simple rule-based paper trader tied to scanner recommendations."""

    def __init__(self, db: DatabaseManager, config: Optional[ScannerConfig] = None) -> None:
        self._db = db
        self._cfg = config or ScannerConfig()
        self._flip_pending_counts: Dict[int, int] = {}
        self._last_policy: Dict = self._default_policy()
        self._order_executor = None  # Set by main.py when IBKR live execution enabled
        self._kill_switch_logged_date: Optional[str] = None  # de-dup log once per NY day

    def _kill_switch_blocked(self) -> Optional[str]:
        """Return blocking reason string if daily DD or global R cap breached, else None.

        NY-trading-day window. Called before every new entry.
        """
        cap = float(self._cfg.paper_starting_capital or 0.0)
        if cap <= 0:
            return None

        # Midnight NY today -> UTC ISO for closed_at comparison
        now_ny = datetime.now(NY_TZ)
        midnight_ny = now_ny.replace(hour=0, minute=0, second=0, microsecond=0)
        since_utc_iso = midnight_ny.astimezone(timezone.utc).isoformat()

        dd_pct = float(self._cfg.paper_daily_max_drawdown_pct or 0.0)
        if dd_pct > 0:
            realized_today = self._db.get_realized_pnl_since(since_utc_iso)
            if realized_today <= -abs(dd_pct) * cap / 100.0:
                return (f"DAILY_DD realized=${realized_today:,.2f} "
                        f"(limit -{dd_pct:.1f}% = -${abs(dd_pct)*cap/100:,.0f})")

        r_cap_pct = float(self._cfg.paper_global_r_cap_pct or 0.0)
        if r_cap_pct > 0:
            open_risk = self._db.get_open_risk_at_stop()
            if open_risk >= abs(r_cap_pct) * cap / 100.0:
                return (f"GLOBAL_R open_risk=${open_risk:,.0f} "
                        f"(cap {r_cap_pct:.1f}% = ${abs(r_cap_pct)*cap/100:,.0f})")

        return None

    def _log_kill_switch(self, reason: str, context: str = "") -> None:
        """Log kill-switch trip once per NY day to avoid log spam."""
        today_ny = datetime.now(NY_TZ).date().isoformat()
        if self._kill_switch_logged_date != today_ny:
            logger.warning("PAPER KILL-SWITCH TRIPPED: {} {}", reason, context)
            self._kill_switch_logged_date = today_ny
        else:
            logger.debug("PAPER KILL-SWITCH: {} {}", reason, context)

    def process_scan_rows(self, rows: List[Dict]) -> None:
        """Apply entry/exit rules against the latest MTF rows."""
        if not self._cfg.paper_trading_enabled or not rows:
            return

        now_dt = datetime.now(timezone.utc)
        policy = self._resolve_runtime_policy(now_dt)
        self._last_policy = policy

        # Smart EOD evaluation: close weak positions, promote strong ones to SWING.
        self._check_eod_evaluation(rows, now_dt)

        open_positions = self._db.get_open_paper_trades()
        open_by_symbol = {p["symbol"]: p for p in open_positions}
        now_iso = now_dt.isoformat()
        open_ids = {int(p.get("id") or 0) for p in open_positions}
        self._flip_pending_counts = {
            trade_id: count
            for trade_id, count in self._flip_pending_counts.items()
            if trade_id in open_ids
        }

        # Exit checks first.
        for row in rows:
            symbol = row.get("symbol")
            position = open_by_symbol.get(symbol)
            if not position:
                continue

            price = _as_float(row.get("price"))
            if price is None or price <= 0:
                continue

            exit_reason = self._should_exit(position, row, price, policy)
            if exit_reason:
                self._close_position(position, price, exit_reason, now_iso)
                open_by_symbol.pop(symbol, None)

        # Late-entry cutoff — no new entries after configured time.
        if self._past_late_entry_cutoff(now_dt):
            record_skip(Subsystem.PAPER_TRADER, SkipReason.LATE_ENTRY_CUTOFF)
            logger.info("PAPER: past late-entry cutoff — skipping new entries")
            return

        # Daily risk kill-switch — DD or global R cap breached.
        kill_reason = self._kill_switch_blocked()
        if kill_reason:
            self._log_kill_switch(kill_reason, context="(scan entries blocked)")
            return

        # Entry checks second.
        open_count = len(open_by_symbol)
        if open_count >= self._cfg.paper_max_open_positions:
            record_skip(Subsystem.PAPER_TRADER, SkipReason.POSITION_LIMIT,
                         f"max open {self._cfg.paper_max_open_positions}", persist=False)
            return

        _mtf_sub = "scanner_mtf"
        record_funnel(_mtf_sub, FUNNEL_CANDIDATES, len(rows))

        for row in rows:
            if len(open_by_symbol) >= self._cfg.paper_max_open_positions:
                break
            symbol = row.get("symbol")
            if not symbol or symbol in open_by_symbol:
                if symbol and symbol in open_by_symbol:
                    record_funnel(_mtf_sub, FUNNEL_SKIPPED)
                continue
            recent_losses = self._db.get_symbol_recent_loss_count(symbol, limit=5)
            if recent_losses >= 3:
                record_funnel(_mtf_sub, FUNNEL_SKIPPED)
                logger.info(
                    f"PAPER SKIP {symbol}: recent loss cluster detected ({recent_losses}/5 losses)"
                )
                continue
            rec = row.get("recommendation")
            side = "LONG" if rec == "BUY" else ("SHORT" if rec == "SELL" else "")
            if not side:
                continue
            stock_state = str(row.get("stock_state") or "N/A").upper()
            confirms = int(row.get("recommendation_confirms") or 1)
            required_confirms = int(max(1, self._cfg.paper_entry_confirmations_required))
            if stock_state not in ("NEW", "CONFIRMED", "VERY_STRONG") or confirms < required_confirms:
                record_funnel(_mtf_sub, FUNNEL_SKIPPED)
                logger.info(
                    f"PAPER SKIP {symbol}: state={stock_state} confirms={confirms} "
                    f"(entry requires >={required_confirms} confirmations)"
                )
                continue
            record_funnel(_mtf_sub, FUNNEL_SETUPS)
            policy_violation = self._entry_policy_violation(row, policy)
            if policy_violation:
                record_funnel(_mtf_sub, FUNNEL_SKIPPED)
                logger.info(f"PAPER SKIP {symbol}: {policy_violation}")
                continue
            record_funnel(_mtf_sub, FUNNEL_ATTEMPTED)

            price = _as_float(row.get("price"))
            stop = _as_float(row.get("stop_loss"))
            if price is None or stop is None or price <= 0 or stop <= 0:
                continue

            qty = self._position_size(price, stop)
            if qty <= 0:
                continue

            notional = round(price * qty, 2)
            min_notional = float(self._cfg.paper_min_notional_per_trade)
            if notional < min_notional:
                logger.info(
                    f"PAPER SKIP {symbol}: position notional ${notional:.2f} < "
                    f"minimum ${min_notional:.2f}"
                )
                continue
            _is_tl = bool(row.get("inst_triple_lock") or False)
            if _is_tl:
                mode_tag = "TRIPLE_LOCK"
            elif policy.get("defensive_mode"):
                mode_tag = "DEFENSIVE"
            else:
                mode_tag = "NORMAL"
            trade_data = {
                "opened_at": now_iso,
                "symbol": symbol,
                "side": side,
                "entry_price": round(price, 4),
                "quantity": qty,
                "notional": notional,
                "stop_loss": _as_float(row.get("stop_loss")),
                "target_1": _as_float(row.get("target_1")),
                "target_2": _as_float(row.get("target_2")),
                "status": "OPEN",
                "recommendation_source": f"SCANNER_MTF_{stock_state}_{mode_tag}",
                "instrument_type": "STOCK",
                "option_type": None,
                "option_expiry": None,
                "option_strike": None,
                "entry_signal": row.get("signal"),
                "entry_score": _as_float(row.get("score")),
                "entry_rr_ratio": _as_float(row.get("rr_ratio")),
                "entry_market_regime": row.get("market_regime"),
                "entry_gex_status": row.get("gex_status"),
                "entry_session_time": row.get("session_time"),
                "entry_trade_conditions": (
                    f"{row.get('trade_conditions', '')} | StockState={stock_state} | "
                    f"Confirms={confirms} | Mode={mode_tag}"
                    + (
                        f" | ML_v2={row.get('inst_ml_score_v2', 0):.0f}"
                        f" | F4={row.get('inst_f4_distinct_60d', 0):.0f}insiders"
                        f" | Mom90d={row.get('inst_price_momentum_90d', 0):+.1f}%"
                        if _is_tl else ""
                    )
                ),
                "fees": float(self._cfg.paper_fee_per_trade),
                "created_ts": now_iso,
            }
            trade_id = self._db.create_paper_trade(trade_data)
            record_funnel(_mtf_sub, FUNNEL_ENTERED)

            # Route to IBKR if OrderExecutor is enabled for SCANNER_MTF
            if self._order_executor and self._order_executor.should_execute_live("SCANNER_MTF"):
                placed = self._order_executor.place_bracket_order(
                    trade_id=trade_id,
                    symbol=symbol,
                    side=side,
                    quantity=qty,
                    entry_price=price,
                    stop_price=stop,
                    target_price=_as_float(row.get("target_2")) or (price + 2 * abs(price - stop)),
                )
                if placed:
                    logger.info(f"PAPER->IBKR: {symbol} bracket placed (trade_id={trade_id})")

            open_by_symbol[symbol] = {"id": trade_id, **trade_data}
            if _is_tl:
                logger.info(
                    f"PAPER ENTRY {side} [TRIPLE_LOCK]: {symbol} qty={qty} @ ${price:.2f} "
                    f"stop=${stop:.2f} | ml_v2={row.get('inst_ml_score_v2', 0):.0f} "
                    f"f4_insiders={row.get('inst_f4_distinct_60d', 0):.0f} "
                    f"momentum={row.get('inst_price_momentum_90d', 0):+.1f}% "
                    f"(trade_id={trade_id})"
                )
            else:
                logger.info(
                    f"PAPER ENTRY {side}: {symbol} qty={qty} @ ${price:.2f} "
                    f"stop=${stop:.2f} (trade_id={trade_id})"
                )

    def enter_idea_trade(self, idea: Dict) -> Optional[int]:
        """Enter a paper trade from an idea source (Stock Ideas, AI Signals, Options).

        Required fields in `idea`:
            symbol, side (LONG/SHORT), entry_price, stop_loss, source (str)
        Optional:
            target_1, target_2, conviction, accum_phase, ml_score,
            instrument_type (STOCK/OPTION), option_type, option_expiry, option_strike
        Returns trade_id or None if blocked.
        """
        if not self._cfg.paper_trading_enabled:
            return None

        # Daily risk kill-switch — DD or global R cap breached.
        kill_reason = self._kill_switch_blocked()
        if kill_reason:
            self._log_kill_switch(kill_reason, context="(idea entry blocked)")
            return None

        symbol = str(idea.get("symbol") or "").upper()
        side = str(idea.get("side") or "").upper()
        price = _as_float(idea.get("entry_price"))
        stop = _as_float(idea.get("stop_loss"))
        source = str(idea.get("source") or "IDEA")

        if not symbol or side not in ("LONG", "SHORT") or not price or price <= 0:
            logger.debug("IDEA SKIP {}: invalid symbol/side/price", symbol)
            return None
        if not stop or stop <= 0:
            logger.debug("IDEA SKIP {}: no stop_loss", symbol)
            return None

        # Check not already in position
        open_positions = self._db.get_open_paper_trades()
        open_symbols = {p["symbol"] for p in open_positions}
        if symbol in open_symbols:
            record_skip(Subsystem.PAPER_TRADER, SkipReason.DUPLICATE_SYMBOL, symbol)
            logger.debug("IDEA SKIP {}: already has open position", symbol)
            return None

        # Max positions check
        if len(open_positions) >= self._cfg.paper_max_open_positions:
            record_skip(Subsystem.PAPER_TRADER, SkipReason.POSITION_LIMIT, symbol)
            logger.debug("IDEA SKIP {}: max open positions reached", symbol)
            return None

        # Recent loss cluster
        recent_losses = self._db.get_symbol_recent_loss_count(symbol, limit=5)
        if recent_losses >= 3:
            logger.info("IDEA SKIP {}: recent loss cluster ({}/5)", symbol, recent_losses)
            return None

        qty = self._position_size(price, stop)
        if qty <= 0:
            return None
        notional = round(price * qty, 2)

        now_iso = datetime.now(timezone.utc).isoformat()
        conviction = idea.get("conviction", "")
        phase = idea.get("accum_phase", "")
        conditions = f"Source={source} | Conv={conviction} | Phase={phase}"
        if idea.get("ml_score"):
            conditions += f" | ML={idea['ml_score']}"

        trade_data = {
            "opened_at": now_iso,
            "symbol": symbol,
            "side": side,
            "entry_price": round(price, 4),
            "quantity": qty,
            "notional": notional,
            "stop_loss": stop,
            "target_1": _as_float(idea.get("target_1")),
            "target_2": _as_float(idea.get("target_2")),
            "status": "OPEN",
            "recommendation_source": source,
            "strategy_type": idea.get("strategy_type") or (
                "SWING" if "SWING" in source.upper() else
                "AI_TRIPLE_LOCK" if "TRIPLE" in source.upper() else
                "IDEA"
            ),
            "execution_mode": "IBKR" if (self._order_executor and self._order_executor.should_execute_live("IDEA_BRIDGE")) else "SIM",
            "instrument_type": idea.get("instrument_type", "STOCK"),
            "option_type": idea.get("option_type"),
            "option_expiry": idea.get("option_expiry"),
            "option_strike": _as_float(idea.get("option_strike")),
            "entry_signal": side,
            "entry_score": _as_float(idea.get("score")),
            "entry_rr_ratio": _as_float(idea.get("rr_ratio")),
            "entry_market_regime": idea.get("market_regime"),
            "entry_gex_status": idea.get("gex_status"),
            "entry_session_time": idea.get("session_time"),
            "entry_trade_conditions": conditions,
            "fees": float(self._cfg.paper_fee_per_trade),
            "created_ts": now_iso,
            # Catalyst-check experiment metadata (Tier 1, 2026-04-26)
            "cohort": idea.get("cohort"),
            "catalyst_check_at": idea.get("catalyst_check_at"),
            "catalyst_flag": (
                1 if idea.get("catalyst_flag") else 0
                if "catalyst_flag" in idea else None
            ),
            "catalyst_reasons": idea.get("catalyst_reasons"),
        }
        trade_id = self._db.create_paper_trade(trade_data)
        logger.info(
            "IDEA ENTRY {}: {} {} qty={} @ ${:.2f} stop=${:.2f} | source={}",
            symbol, side, idea.get("instrument_type", "STOCK"),
            qty, price, stop, source,
        )

        # Route to IBKR if OrderExecutor is enabled for IDEA source
        if self._order_executor and self._order_executor.should_execute_live("IDEA_BRIDGE"):
            target_price = _as_float(idea.get("target_1")) or (
                price + 2 * abs(price - stop) if side == "LONG"
                else price - 2 * abs(price - stop)
            )
            placed = self._order_executor.place_bracket_order(
                trade_id=trade_id,
                symbol=symbol,
                side=side,
                quantity=qty,
                entry_price=price,
                stop_price=stop,
                target_price=target_price,
            )
            if placed:
                logger.info("IDEA->IBKR: {} bracket placed (trade_id={})", symbol, trade_id)

        return trade_id

    def run_eod_evaluation(self, rows: List[Dict]) -> None:
        """Public hook for the scheduled EOD cron job."""
        if not self._cfg.paper_trading_enabled:
            return
        now_dt = datetime.now(timezone.utc)
        self._check_eod_evaluation(rows, now_dt, force=True)

    def get_policy_status(self) -> Dict:
        """Return latest runtime paper-trading policy state."""
        return dict(self._last_policy)

    # ------------------------------------------------------------------
    # Time-guard helpers
    # ------------------------------------------------------------------

    def _et_now(self, now_utc: datetime) -> datetime:
        """Convert UTC datetime to ET."""
        return now_utc.astimezone(NY_TZ) if NY_TZ else now_utc

    def _past_late_entry_cutoff(self, now_utc: datetime) -> bool:
        """Return True if current ET time is past the late-entry cutoff."""
        et = self._et_now(now_utc)
        cutoff_hour = int(self._cfg.late_entry_cutoff_hour)
        cutoff_minute = int(self._cfg.late_entry_cutoff_minute)
        return (et.hour, et.minute) >= (cutoff_hour, cutoff_minute)

    def _past_eod_evaluation_time(self, now_utc: datetime) -> bool:
        """Return True if current ET time is past the EOD evaluation time."""
        et = self._et_now(now_utc)
        eod_hour = int(self._cfg.eod_evaluation_hour)
        eod_minute = int(self._cfg.eod_evaluation_minute)
        return (et.hour, et.minute) >= (eod_hour, eod_minute)

    def _check_eod_evaluation(self, rows: List[Dict], now_utc: datetime, force: bool = False) -> None:
        """Smart EOD: close weak positions, promote strong ones to SWING.

        Positions with promising momentum/score are promoted rather than
        force-closed, allowing them to carry as swing trades.
        """
        if not force and not self._past_eod_evaluation_time(now_utc):
            return

        open_positions = self._db.get_open_paper_trades()
        if not open_positions:
            return

        now_iso = now_utc.isoformat()
        context_by_symbol = {str(r.get("symbol") or "").upper(): r for r in rows if r.get("symbol")}

        for position in open_positions:
            symbol = str(position.get("symbol") or "").upper()
            trade_id = int(position.get("id") or 0)
            source = str(position.get("recommendation_source") or "")

            # Already promoted to swing — check max hold days instead.
            if "SWING" in source:
                opened_at = position.get("opened_at")
                if opened_at and self._cfg.swing_max_hold_days > 0:
                    try:
                        opened_dt = datetime.fromisoformat(str(opened_at))
                        if opened_dt.tzinfo is None:
                            opened_dt = opened_dt.replace(tzinfo=timezone.utc)
                        days_held = (now_utc - opened_dt).days
                        if days_held >= self._cfg.swing_max_hold_days:
                            price = self._latest_price(symbol, context_by_symbol)
                            if price:
                                self._close_position(position, price, "SWING_MAX_HOLD_EXCEEDED", now_iso)
                                logger.info(f"PAPER EOD: {symbol} swing max hold {days_held}d exceeded — closed")
                    except (TypeError, ValueError):
                        pass
                continue

            row = context_by_symbol.get(symbol)
            price = self._latest_price(symbol, context_by_symbol)
            if not price:
                # No current data — keep position open rather than force-closing
                logger.info(f"PAPER EOD: {symbol} no price data — keeping open")
                continue

            # Evaluate for swing promotion.
            if self._cfg.swing_promotion_enabled and self._qualifies_for_swing(position, row, price):
                # Promote to swing — update source tag so we don't re-evaluate tomorrow.
                new_source = source.replace("NORMAL", "SWING").replace("DEFENSIVE", "SWING")
                if "SWING" not in new_source:
                    new_source = f"{source}_SWING"
                self._db.update_paper_trade_source(trade_id, new_source)
                logger.info(
                    f"PAPER EOD: {symbol} promoted to SWING (score={row.get('score') if row else '?'}, "
                    f"trend aligned, momentum promising)"
                )
            else:
                # Weak position — close at EOD.
                self._close_position(position, price, "EOD_WEAK_CLOSE", now_iso)
                logger.info(f"PAPER EOD: {symbol} weak at EOD — closed @ ${price:.2f}")

    def _qualifies_for_swing(self, position: Dict, row: Optional[Dict], price: float) -> bool:
        """Check if a position qualifies for overnight swing promotion."""
        if not row:
            return False

        # Score gate.
        score = _as_float(row.get("score"))
        if score is None or score < self._cfg.swing_min_score:
            return False

        # R:R gate.
        rr = _as_float(row.get("rr_ratio"))
        if rr is None or rr < self._cfg.swing_min_rr:
            return False

        # Signal must still agree with side.
        side = position.get("side")
        signal = str(row.get("signal") or "").upper()
        if side == "LONG" and signal != "LONG":
            return False
        if side == "SHORT" and signal != "SHORT":
            return False

        # Trend alignment.
        if self._cfg.swing_require_trend_alignment:
            trend = str(row.get("trend_direction") or "").upper()
            if side == "LONG" and trend not in ("UPTREND",):
                return False
            if side == "SHORT" and trend not in ("DOWNTREND",):
                return False

        # Optional: must be in profit.
        if self._cfg.swing_require_positive_pnl:
            entry_price = _as_float(position.get("entry_price"))
            if entry_price:
                pnl = (price - entry_price) if side == "LONG" else (entry_price - price)
                if pnl <= 0:
                    return False

        # Signal momentum should not be weakening.
        momentum = str(row.get("signal_momentum") or "").upper()
        if momentum == "WEAKENING":
            return False

        return True

    def _latest_price(self, symbol: str, context: Dict[str, Dict]) -> Optional[float]:
        """Get latest price for symbol from scan context, with DuckDB fallback."""
        row = context.get(symbol)
        if row:
            p = _as_float(row.get("price"))
            if p and p > 0:
                return p
        # Fallback: latest close from DuckDB daily prices
        try:
            from signal_scanner.institutional_intel.config import safe_duckdb_connect
            conn = safe_duckdb_connect(read_only=True)
            if conn:
                try:
                    r = conn.execute(
                        "SELECT close FROM fact_daily_prices "
                        "WHERE ticker = ? ORDER BY trade_date DESC LIMIT 1",
                        [symbol],
                    ).fetchone()
                    if r and r[0]:
                        return float(r[0])
                finally:
                    conn.close()
        except Exception:
            pass
        return None

    def _position_size(self, entry: float, stop: float) -> int:
        """Position sizing: target ~$10K notional per stock trade.

        Rules:
        - Stock < $10: 1000 shares
        - Stock >= $10: ceil($10K / entry) to ensure >= $10K
        - Never exceed notional cap ($15K)
        """
        if entry <= 0:
            return 0
        target_notional = float(self._cfg.paper_min_notional_per_trade)  # $10K
        notional_cap = float(self._cfg.paper_leverage_per_trade)  # $15K

        if entry < 10.0:
            qty = 1000
        else:
            qty = ceil(target_notional / entry)

        # Enforce notional cap
        if qty * entry > notional_cap:
            qty = ceil(notional_cap / entry)

        return int(max(qty, 1))

    def _should_exit(self, position: Dict, row: Dict, price: float, policy: Dict) -> str:
        """Return exit reason if position should close now."""
        side = position.get("side")
        stop = _as_float(position.get("stop_loss"))
        target_2 = _as_float(position.get("target_2"))
        rec = row.get("recommendation")
        signal = row.get("signal")
        trade_id = int(position.get("id") or 0)

        if side == "LONG":
            if stop is not None and price <= stop:
                self._flip_pending_counts.pop(trade_id, None)
                return "STOP_LOSS"
            if target_2 is not None and price >= target_2:
                self._flip_pending_counts.pop(trade_id, None)
                return "TARGET_2"
            # Only flip on actual OPPOSITE direction — HOLD/NEUTRAL should
            # NOT trigger a flip exit (that's what stop loss is for)
            flip = signal == "SHORT"
        elif side == "SHORT":
            if stop is not None and price >= stop:
                self._flip_pending_counts.pop(trade_id, None)
                return "STOP_LOSS"
            if target_2 is not None and price <= target_2:
                self._flip_pending_counts.pop(trade_id, None)
                return "TARGET_2"
            flip = signal == "LONG"
        else:
            flip = False

        if not flip:
            self._flip_pending_counts.pop(trade_id, None)
            return ""

        if not bool(policy.get("require_flip_confirmation")):
            self._flip_pending_counts.pop(trade_id, None)
            return "RECOMMENDATION_FLIP"

        confirm_cycles = max(0, int(policy.get("flip_confirm_cycles") or 0))
        pending = int(self._flip_pending_counts.get(trade_id, 0)) + 1
        self._flip_pending_counts[trade_id] = pending
        if pending <= confirm_cycles:
            logger.info(
                f"PAPER HOLD {position.get('symbol')}: flip pending confirmation "
                f"({pending}/{confirm_cycles})"
            )
            return ""
        self._flip_pending_counts.pop(trade_id, None)
        return "RECOMMENDATION_FLIP"

    def _entry_policy_violation(self, row: Dict, policy: Dict) -> str:
        """Return reason when current row fails the active entry policy."""
        # --- Institutional Intelligence gates (non-blocking — degrade if absent) ---
        inst_phase          = str(row.get("inst_phase")          or "UNKNOWN").upper()
        inst_conviction     = float(row.get("inst_conviction")   or 0)
        inst_triple_lock    = bool(row.get("inst_triple_lock")   or False)
        inst_ml_score_v2    = float(row.get("inst_ml_score_v2")  or 0)
        inst_price_abv_200  = int(row.get("inst_price_above_200sma") if row.get("inst_price_above_200sma") is not None else -1)
        rec                 = str(row.get("recommendation")      or "").upper()
        side                = "LONG" if rec == "BUY" else ("SHORT" if rec == "SELL" else "")

        if side == "LONG" and inst_phase == "DISTRIBUTION" and inst_conviction > 50:
            return (
                f"intel gate: DISTRIBUTION phase (conviction={inst_conviction:.0f}) "
                "— institutional exit confirmed, no LONG entry"
            )

        # Momentum pre-filter: block LONG entries where price is below 200 SMA
        # (inst_price_above_200sma = 0 means below, -1 = unknown/unavailable).
        # Triple Lock tickers bypass this filter — convergence of all signals overrides.
        if side == "LONG" and inst_price_abv_200 == 0 and not inst_triple_lock:
            return (
                "momentum gate: price below 200-day SMA (broken trend) — "
                "no LONG entry without Triple Lock confirmation"
            )

        # Low-conviction filter: block entries where intel data exists but conviction is very low
        # Triple Lock entries bypass this — they have multiple confirming signals.
        if not inst_triple_lock and inst_conviction > 0 and inst_conviction < 30:
            score = float(row.get("score") or 0)
            min_score = float(
                self._cfg.paper_entry_min_score
                if not policy.get("defensive_mode")
                else policy.get("min_score") or self._cfg.paper_defensive_score_min
            )
            if score < min_score + 10:
                return (
                    f"intel gate: very low institutional conviction ({inst_conviction:.0f}/100) "
                    f"and scan score {score:.0f} below boosted threshold"
                )

        # ML v2 score gate: if ml_score_v2 is available and very low, skip
        # Triple Lock entries bypass this — all three signals already confirm quality.
        inst_ml_score = float(row.get("inst_ml_score") or 0)
        if not inst_triple_lock and inst_ml_score_v2 > 0 and inst_ml_score_v2 < 30 and inst_conviction < 60:
            score = float(row.get("score") or 0)
            min_score = float(
                self._cfg.paper_entry_min_score
                if not policy.get("defensive_mode")
                else policy.get("min_score") or self._cfg.paper_defensive_score_min
            )
            if score < min_score + 10:
                return (
                    f"ml v2 gate: ml_score_v2={inst_ml_score_v2:.0f} + conviction={inst_conviction:.0f} "
                    "too low — model predicts underperformance vs SPY"
                )
        # ML v1 fallback gate (when v2 not yet scored)
        if not inst_triple_lock and inst_ml_score_v2 == 0 and inst_ml_score > 0 and inst_ml_score < 30 and inst_conviction < 60:
            score = float(row.get("score") or 0)
            min_score = float(
                self._cfg.paper_entry_min_score
                if not policy.get("defensive_mode")
                else policy.get("min_score") or self._cfg.paper_defensive_score_min
            )
            if score < min_score + 10:
                return (
                    f"ml gate: ml_score={inst_ml_score:.0f} + conviction={inst_conviction:.0f} "
                    "too low — model predicts underperformance vs SPY"
                )

        # Insider Effect gate: historical pattern validation
        # When insiders buy this stock, does it historically beat SPY?
        inst_insider_effect = float(row.get("inst_insider_effect") or 0)
        inst_insider_wr90 = row.get("inst_insider_wr90")
        if (
            side == "LONG"
            and not inst_triple_lock
            and inst_insider_effect > 0
            and inst_insider_wr90 is not None
            and inst_insider_wr90 < 45
            and inst_conviction < 50
        ):
            return (
                f"insider effect gate: historical insider buy win rate {inst_insider_wr90:.0f}% "
                f"(below 45%) + low conviction ({inst_conviction:.0f}) — pattern unfavorable"
            )

        # Squeeze-aware gate: if institutional squeeze score is high and
        # we're going SHORT, that's dangerous — institutions + shorts = squeeze risk
        inst_short_squeeze = float(row.get("inst_short_squeeze") or 0)
        if side == "SHORT" and inst_short_squeeze >= 60 and inst_conviction >= 50:
            return (
                f"squeeze gate: short_squeeze_score {inst_short_squeeze:.0f} "
                f"+ conviction {inst_conviction:.0f} — squeeze risk too high for SHORT"
            )

        # Signal expiry — reject stale signals.
        signal_age = int(row.get("signal_age") or 0)
        max_age = int(self._cfg.signal_expiry_scans)
        if max_age > 0 and signal_age > max_age:
            return f"entry gate: signal_age {signal_age} > expiry {max_age}"

        if bool(self._cfg.paper_entry_require_setup_trigger):
            signal = str(row.get("signal") or "").upper()
            sweep_sig = str(row.get("sweep_reclaim_signal") or "NONE").upper()
            vwap_sig = str(row.get("vwap_reversion_signal") or "NONE").upper()
            long_ok = sweep_sig == "BULLISH_SWEEP_RECLAIM" or vwap_sig == "LONG_REVERSION"
            short_ok = sweep_sig == "BEARISH_SWEEP_RECLAIM" or vwap_sig == "SHORT_REVERSION"
            if signal == "LONG" and not long_ok:
                return "entry gate: missing LONG setup trigger (sweep reclaim or VWAP reversion)"
            if signal == "SHORT" and not short_ok:
                return "entry gate: missing SHORT setup trigger (sweep reclaim or VWAP reversion)"

        if not bool(policy.get("defensive_mode")):
            # Baseline entry quality gates (always on).
            min_confirms = int(max(1, self._cfg.paper_entry_confirmations_required))
            confirms = int(row.get("recommendation_confirms") or 1)
            if confirms < min_confirms:
                return f"entry gate: confirmations {confirms} < {min_confirms}"

            score = _as_float(row.get("score"))
            min_score = float(self._cfg.paper_entry_min_score)
            # Triple Lock bonus: all three signals converge — strongest possible setup
            if inst_triple_lock:
                min_score = max(min_score - 8, 52.0)
            elif inst_phase in ("ACTIVE_ACCUM", "LATE_ACCUM") and inst_conviction > 70:
                # Standard institutional bonus: relax by 5 pts
                min_score = max(min_score - 5, 55.0)
            if score is None or score < min_score:
                return f"entry gate: score {score or 0:.1f} < {min_score:.1f}"

            rr = _as_float(row.get("rr_ratio"))
            min_rr = float(self._cfg.paper_entry_min_rr)
            if rr is None or rr < min_rr:
                return f"entry gate: RR {rr or 0:.2f} < {min_rr:.2f}"

            mtf = _as_float(row.get("mtf_score"))
            min_mtf = float(self._cfg.paper_entry_min_mtf_score)
            if mtf is None or mtf < min_mtf:
                return f"entry gate: MTF {mtf or 0:.2f} < {min_mtf:.2f}"

            sessions = {
                str(s).upper()
                for s in (self._cfg.paper_entry_allowed_sessions or [])
                if str(s).strip()
            }
            session = str(row.get("session_time") or "").upper()
            if sessions and session not in sessions:
                return f"entry gate: session {session or 'UNKNOWN'} not allowed"

            if bool(self._cfg.paper_entry_require_trend_alignment):
                signal = str(row.get("signal") or "").upper()
                price = _as_float(row.get("price"))
                sma_200 = _as_float(row.get("sma_200"))
                sma_50 = _as_float(row.get("sma_50"))
                if price is None or sma_200 is None or sma_50 is None:
                    return "entry gate: missing SMA alignment inputs"
                if signal == "LONG" and (price <= sma_200 or sma_50 < sma_200):
                    return "entry gate: LONG requires price>SMA200 and SMA50>=SMA200"
                if signal == "SHORT" and (price >= sma_200 or sma_50 > sma_200):
                    return "entry gate: SHORT requires price<SMA200 and SMA50<=SMA200"

            # GEX alignment gate.
            if bool(self._cfg.paper_entry_require_gex_alignment):
                signal = str(row.get("signal") or "").upper()
                gex = str(row.get("gex_status") or "").upper()
                if gex and gex != "UNKNOWN":
                    if signal == "LONG" and gex != "ABOVE_ZERO_GAMMA":
                        return f"entry gate: LONG requires ABOVE_ZERO_GAMMA, got {gex}"
                    if signal == "SHORT" and gex != "BELOW_ZERO_GAMMA":
                        return f"entry gate: SHORT requires BELOW_ZERO_GAMMA, got {gex}"

            # Market regime gate.
            if bool(self._cfg.paper_entry_require_regime_gate):
                signal = str(row.get("signal") or "").upper()
                regime = str(row.get("market_regime") or "").upper()
                if regime:
                    if signal == "LONG" and regime == "RISK_OFF":
                        return "entry gate: LONG blocked in RISK_OFF regime"
                    if signal == "SHORT" and regime == "RISK_ON":
                        return "entry gate: SHORT blocked in RISK_ON regime"

            # Distance-to-level gate.
            min_dist = float(self._cfg.paper_entry_min_distance_to_level_pct)
            if min_dist > 0:
                signal = str(row.get("signal") or "").upper()
                dist_res = _as_float(row.get("distance_to_resistance_pct"))
                dist_sup = _as_float(row.get("distance_to_support_pct"))
                if signal == "LONG" and dist_res is not None and dist_res < min_dist:
                    return f"entry gate: resistance {dist_res:.1f}% < min {min_dist:.1f}%"
                if signal == "SHORT" and dist_sup is not None and dist_sup < min_dist:
                    return f"entry gate: support {dist_sup:.1f}% < min {min_dist:.1f}%"

            return ""

        min_score = float(policy.get("min_score") or 0.0)
        min_rr = float(policy.get("min_rr_ratio") or 0.0)
        min_age = int(policy.get("min_signal_age") or 1)
        allowed_sessions = {
            str(s).upper()
            for s in (policy.get("allowed_sessions") or [])
            if str(s).strip()
        }

        score = _as_float(row.get("score"))
        if score is None or score < min_score:
            return f"defensive gate: score {score or 0:.1f} < {min_score:.1f}"

        rr = _as_float(row.get("rr_ratio"))
        if rr is None or rr < min_rr:
            return f"defensive gate: RR {rr or 0:.2f} < {min_rr:.2f}"

        signal_age = int(row.get("signal_age") or 0)
        if signal_age < min_age:
            return f"defensive gate: signal_age {signal_age} < {min_age}"

        session = str(row.get("session_time") or "").upper()
        if allowed_sessions and session not in allowed_sessions:
            return f"defensive gate: session {session or 'UNKNOWN'} not allowed"

        return ""

    def _close_position(self, position: Dict, exit_price: float, reason: str, closed_at: str) -> None:
        """Close position and persist realized P&L."""
        entry = _as_float(position.get("entry_price"))
        qty = int(position.get("quantity", 0))
        side = position.get("side")
        trade_id = int(position["id"])
        self._flip_pending_counts.pop(trade_id, None)
        fees = float(position.get("fees") or 0.0) + float(self._cfg.paper_fee_per_trade)

        if entry is None or qty <= 0:
            return

        gross = (exit_price - entry) * qty if side == "LONG" else (entry - exit_price) * qty
        realized = gross - fees
        notional = float(position.get("notional") or (entry * qty))
        realized_pct = (realized / notional) * 100 if notional > 0 else 0.0

        # Cancel IBKR bracket legs if this is a LIVE trade
        if (
            self._order_executor
            and str(position.get("execution_mode", "")).upper() == "LIVE"
        ):
            self._order_executor.cancel_bracket(trade_id)

        self._db.close_paper_trade(
            trade_id=trade_id,
            closed_at=closed_at,
            exit_price=round(exit_price, 4),
            exit_reason=reason,
            realized_pnl=round(realized, 2),
            realized_pnl_pct=round(realized_pct, 2),
            fees=round(fees, 2),
        )
        logger.info(
            f"PAPER EXIT {side}: {position.get('symbol')} reason={reason} "
            f"@ ${exit_price:.2f} pnl=${realized:.2f} (trade_id={trade_id})"
        )

        # Propagate close to linked idea
        idea_id = position.get("idea_id")
        if idea_id:
            try:
                self._db.idea_ledger.mark_closed(
                    idea_id,
                    exit_price=round(exit_price, 4),
                    pnl=round(realized, 2),
                    pnl_pct=round(realized_pct, 2),
                )
            except Exception as e:
                logger.debug(f"Idea close propagation failed for idea {idea_id}: {e}")

    def _default_policy(self) -> Dict:
        """Return baseline policy when no EOD context is applied."""
        return {
            "mode": "NORMAL",
            "defensive_mode": False,
            "min_score": int(self._cfg.paper_defensive_score_min),
            "min_rr_ratio": float(self._cfg.paper_defensive_rr_min),
            "min_signal_age": int(self._cfg.paper_defensive_signal_age_min),
            "allowed_sessions": [str(s).upper() for s in self._cfg.paper_defensive_allowed_sessions],
            "flip_confirm_cycles": int(max(0, self._cfg.paper_flip_confirm_cycles)),
            "require_flip_confirmation": bool(self._cfg.paper_require_flip_confirmation_always),
            "source_trade_date": "",
            "source_action_status": "N/A",
            "source_reason": "EOD policy disabled or unavailable",
        }

    def _resolve_runtime_policy(self, now_utc: datetime) -> Dict:
        """Build runtime policy from latest completed EOD record."""
        policy = self._default_policy()
        if not self._cfg.paper_defensive_auto_from_eod:
            policy["source_reason"] = "paper_defensive_auto_from_eod=False"
            return policy

        today_et = now_utc.astimezone(NY_TZ).date().isoformat() if NY_TZ else now_utc.date().isoformat()
        eod = self._db.get_latest_completed_eod_analysis(today_et)
        if not eod:
            policy["source_reason"] = "No completed EOD row available yet"
            return policy

        insights = eod.get("insights") or {}
        total_trades = int(eod.get("total_trades") or 0)
        realized_pnl = _as_float(eod.get("realized_pnl")) or 0.0
        stop_loss_rate = _as_float(insights.get("stop_loss_rate_pct")) or 0.0
        flip_rate = _as_float(insights.get("flip_rate_pct")) or 0.0
        expectancy = _as_float(insights.get("expectancy_per_trade")) or 0.0
        profit_factor = _as_float(insights.get("profit_factor")) or 0.0

        # Only trigger defensive metrics when we have real trade data.
        # Empty/zero EOD records should NOT lock out trading.
        if total_trades < 5 and not insights:
            metric_trigger = False
            logger.debug(
                "Defensive metrics skipped: insufficient trade data "
                "(total_trades={}, insights empty)", total_trades,
            )
        else:
            metric_trigger = (
                realized_pnl < 0
                or expectancy < 0
                or (profit_factor < 1.0 and total_trades >= 5)
                or stop_loss_rate >= float(self._cfg.paper_stop_loss_rate_trigger_pct)
                or flip_rate >= float(self._cfg.paper_flip_rate_trigger_pct)
            )
        action_status = str(eod.get("action_status") or "PENDING").upper()

        # Auto-exit: if latest EOD is >3 days old with IMPLEMENT/WATCH and
        # zero trades since, downgrade to normal — prevents permanent lockout.
        eod_date_str = str(eod.get("trade_date") or "")
        if eod_date_str and action_status in ("IMPLEMENT", "WATCH"):
            from datetime import date as _date
            try:
                eod_date = _date.fromisoformat(eod_date_str)
                today = now_utc.date() if hasattr(now_utc, "date") else _date.today()
                days_stale = (today - eod_date).days
                if days_stale >= 3:
                    logger.info(
                        "Defensive auto-exit: EOD {} is {}d old with status={} — "
                        "reverting to NORMAL to prevent permanent lockout",
                        eod_date_str, days_stale, action_status,
                    )
                    action_status = "WATCH"
                    metric_trigger = False
            except (ValueError, TypeError):
                pass

        if action_status == "IGNORE":
            defensive = False
            reason = "EOD status=IGNORE"
        elif action_status == "IMPLEMENT":
            defensive = True
            reason = "EOD status=IMPLEMENT"
        elif action_status == "WATCH":
            defensive = metric_trigger
            reason = "EOD status=WATCH + metric trigger" if defensive else "EOD status=WATCH"
        else:
            defensive = metric_trigger
            reason = "EOD status=PENDING + metric trigger" if defensive else "EOD status=PENDING"

        policy.update(
            {
                "mode": "DEFENSIVE" if defensive else "NORMAL",
                "defensive_mode": defensive,
                "require_flip_confirmation": bool(
                    self._cfg.paper_require_flip_confirmation_always
                    or (defensive and self._cfg.paper_flip_confirm_cycles > 0)
                ),
                "source_trade_date": str(eod.get("trade_date") or ""),
                "source_action_status": action_status,
                "source_reason": reason,
            }
        )

        # IMPLEMENT = explicit tightening beyond general defensive defaults.
        if action_status == "IMPLEMENT" and defensive:
            policy["min_score"] = max(float(policy.get("min_score") or 0), 70.0)
            policy["min_rr_ratio"] = max(float(policy.get("min_rr_ratio") or 0), 1.8)
            policy["min_signal_age"] = max(int(policy.get("min_signal_age") or 1), 2)
            logger.info(
                "EOD IMPLEMENT action auto-applied: defensive gates tightened "
                "(score≥70, RR≥1.8, age≥2)"
            )

        return policy


def _as_float(value) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None
