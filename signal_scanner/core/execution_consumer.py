"""Execution Consumer — reads strategy signals and creates trades.

Separated from strategy evaluation. Strategies emit signals to
live_strategy_signals with status='PENDING_EXECUTION'. This consumer
reads those signals and creates paper trades / routes IBKR orders.

If execution fails, strategy evaluation continues unaffected.

Usage:
    consumer = ExecutionConsumer(db_manager, order_executor)
    consumer.process_pending()  # called on schedule
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from loguru import logger

from signal_scanner.core.live_bar_store import LiveBarStore


class ExecutionConsumer:
    """Consumes strategy signals and creates trades."""

    def __init__(self, db_manager: Any, bar_store: LiveBarStore,
                 order_executor: Any = None):
        self._db = db_manager
        self._store = bar_store
        self._executor = order_executor
        self._processed = 0
        self._errors = 0

    def process_pending(self) -> Dict[str, int]:
        """Process all PENDING_EXECUTION signals.

        For each pending signal:
        1. Read signal details
        2. Create paper trade
        3. Optionally route to IBKR
        4. Mark signal as EXECUTED or FAILED

        Returns: {processed, executed, failed}
        """
        pending = self._store.get_pending_signals()
        # Filter to only PENDING_EXECUTION (not all NEW signals)
        pending = [s for s in pending if s.get("status") == "PENDING_EXECUTION"]

        if not pending:
            return {"processed": 0, "executed": 0, "failed": 0}

        executed = 0
        failed = 0

        for signal in pending:
            signal_id = signal.get("id")
            strategy = signal.get("strategy", "")
            symbol = signal.get("symbol", "")

            try:
                import json as _json

                # Parse full signal dict from rationale (stored as JSON by strategy engine)
                sig_data = {}
                try:
                    sig_data = _json.loads(signal.get("rationale", "{}"))
                except (ValueError, TypeError):
                    pass

                # Use strategy-computed values, fall back to defaults
                entry_price = sig_data.get("entry_price") or self._store.get_latest_price(symbol)
                if not entry_price:
                    self._store.mark_signal_processed(signal_id, "FAILED_NO_PRICE")
                    failed += 1
                    continue

                side = sig_data.get("side", "LONG")
                stop_price = sig_data.get("stop_price", round(entry_price * 0.97, 4))
                target_1 = sig_data.get("target_1", round(entry_price * 1.03, 4))
                target_2 = sig_data.get("target_2", round(entry_price * 1.06, 4))
                qty = sig_data.get("quantity") or self._compute_qty(entry_price)
                notional = sig_data.get("notional", round(entry_price * qty, 2))
                r_unit = sig_data.get("r_unit", abs(entry_price - stop_price))
                ml_pctl = sig_data.get("ml_percentile", signal.get("percentile", 0))

                now_iso = datetime.now(timezone.utc).isoformat()
                trade_data = {
                    "opened_at": now_iso,
                    "symbol": symbol,
                    "side": side,
                    "entry_price": round(entry_price, 4),
                    "quantity": qty,
                    "notional": round(notional, 2),
                    "stop_loss": round(stop_price, 4),
                    "target_1": round(target_1, 4),
                    "target_2": round(target_2, 4),
                    "status": "OPEN",
                    "strategy_type": strategy,
                    "execution_mode": "SIM",
                    "instrument_type": "STOCK",
                    "option_type": None,
                    "option_expiry": None,
                    "option_strike": None,
                    "entry_market_regime": None,
                    "entry_gex_status": None,
                    "entry_session_time": None,
                    "recommendation_source": signal.get("recommendation_source", f"{strategy}_SIGNAL"),
                    "entry_signal": side,
                    "entry_score": signal.get("score"),
                    "entry_rr_ratio": round(abs(target_1 - entry_price) / r_unit, 1) if r_unit > 0 else 2.0,
                    "entry_trade_conditions": (
                        f"Signal #{signal_id} | {strategy} | pctl={ml_pctl} "
                        f"| bar_ts={signal.get('bar_ts_used')} "
                        f"| conv={sig_data.get('conviction', '?')} phase={sig_data.get('phase', '?')}"
                    ),
                    "fees": 0.0,
                    "created_ts": now_iso,
                }

                trade_id = self._db.create_paper_trade(trade_data)

                # Route to IBKR if order executor is available and strategy is enabled
                if self._executor and self._executor.should_execute_live(strategy):
                    try:
                        placed = self._executor.place_bracket_order(
                            trade_id=trade_id,
                            symbol=symbol,
                            side=side,
                            quantity=qty,
                            entry_price=entry_price,
                            stop_price=stop_price,
                            target_price=target_2,
                        )
                        if placed:
                            # Update execution mode to IBKR
                            self._db.update_paper_trade_field(
                                trade_id, "execution_mode", "IBKR"
                            ) if hasattr(self._db, "update_paper_trade_field") else None
                            logger.info(
                                "ExecutionConsumer: {} {} → IBKR bracket placed (trade #{})",
                                strategy, symbol, trade_id,
                            )
                    except Exception as ibkr_err:
                        logger.warning(
                            "ExecutionConsumer: IBKR routing failed for {} {} (trade #{}): {}",
                            strategy, symbol, trade_id, ibkr_err,
                        )
                        # Trade still created as SIM — IBKR failure is non-fatal

                # Link trade to idea and mark idea as ENTERED
                idea_id = sig_data.get("idea_id")
                if idea_id:
                    try:
                        self._db.idea_ledger.mark_entered(idea_id, trade_id)
                        # Link idea_id on the trade
                        with self._db._get_connection() as _tc:
                            _tc.execute(
                                "UPDATE paper_trades SET idea_id = ? WHERE id = ?",
                                (idea_id, trade_id),
                            )
                    except Exception:
                        pass

                self._store.mark_signal_processed(signal_id, "EXECUTED")
                executed += 1

                routed = "IBKR" if (self._executor and self._executor.should_execute_live(strategy)) else "SIM"
                logger.info(
                    "ExecutionConsumer: {} {} → trade #{} @ ${:.2f} ({}) idea=#{}",
                    strategy, symbol, trade_id, entry_price, routed, idea_id or "none",
                )

            except Exception as e:
                logger.warning("ExecutionConsumer error for signal {}: {}", signal_id, e)
                self._store.mark_signal_processed(signal_id, f"FAILED: {str(e)[:100]}")
                failed += 1

        self._processed += len(pending)
        self._errors += failed

        if executed > 0 or failed > 0:
            logger.info(
                "ExecutionConsumer: {} pending → {} executed, {} failed",
                len(pending), executed, failed,
            )

        return {"processed": len(pending), "executed": executed, "failed": failed}

    def _compute_qty(self, price: float) -> int:
        """Default position sizing."""
        from math import ceil
        if price >= 10:
            return ceil(10_000 / price)
        return 1000
