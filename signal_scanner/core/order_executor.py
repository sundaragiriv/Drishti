"""IBKR bracket order executor with event-driven fill tracking.

Places bracket orders (parent LMT + take-profit LMT + stop-loss STP) on IBKR,
tracks fills via ib_insync events, and reconciles positions on startup.

Shares the DataConnector's IB connection — no separate login needed.
Gracefully degrades: if IBKR is unavailable, trades stay in SIM mode.

Usage:
    executor = OrderExecutor(connector, db, enabled_strategies={"VWAP_MR"})
    # After creating a paper trade:
    executor.place_bracket_order(trade_id, "AAPL", "LONG", 50, 180.0, 178.0, 184.0)
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Set

from loguru import logger

from signal_scanner.core.ibkr_connector import DataConnector
from signal_scanner.core.telemetry import record_skip, SkipReason, Subsystem
from signal_scanner.database.db_manager import DatabaseManager


class OrderExecutor:
    """IBKR bracket order executor.

    Responsibilities:
      1. Place bracket orders (parent LMT + TP LMT + SL STP)
      2. Track order status via ib_insync events
      3. Map IBKR orderId/permId to paper_trade.id
      4. Handle fills, cancellations, rejections
      5. Reconcile positions on startup/reconnect
      6. Modify stops/targets on open bracket orders
      7. Cancel bracket legs when simulation logic closes a trade
    """

    def __init__(
        self,
        connector: DataConnector,
        db: DatabaseManager,
        enabled_strategies: Optional[Set[str]] = None,
    ) -> None:
        self._connector = connector
        self._db = db
        self._enabled = enabled_strategies or set()

        # In-memory order tracking
        # orderId → trade_id (for all 3 legs)
        self._order_to_trade: Dict[int, int] = {}
        # trade_id → {"parent_id", "tp_id", "sl_id", "status", "symbol"}
        self._trade_orders: Dict[int, Dict[str, Any]] = {}

        # Per-trade lock for race condition between event handler and sim exit
        self._trade_locks: Dict[int, threading.Lock] = {}

        self._events_registered = False

        # Orphan gate — blocks new entries when unresolved orphan IBKR positions exist
        self._orphan_symbols: list = []
        self._orphan_gate_active: bool = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def is_enabled(self) -> bool:
        """True if IBKR is connected and at least one strategy is enabled."""
        return bool(self._enabled) and self._connector.is_connected()

    def should_execute_live(self, strategy_type: str) -> bool:
        """Check if this strategy_type should route to IBKR."""
        return (
            strategy_type.upper() in self._enabled
            and self._connector.is_connected()
        )

    def acknowledge_orphans(self) -> None:
        """Clear the orphan gate so new entries can proceed.

        Call this after you've manually reviewed the orphan positions in TWS.
        """
        if self._orphan_gate_active:
            logger.info(
                "ORPHAN GATE CLEARED — {} orphan(s) acknowledged: {}",
                len(self._orphan_symbols), ", ".join(self._orphan_symbols),
            )
        self._orphan_gate_active = False
        self._orphan_symbols = []

    def place_bracket_order(
        self,
        trade_id: int,
        symbol: str,
        side: str,
        quantity: int,
        entry_price: float,
        stop_price: float,
        target_price: float,
    ) -> bool:
        """Place a bracket order on IBKR and persist order IDs.

        Returns True if all orders were placed successfully.
        On failure, trade stays in SIM mode (graceful degradation).
        """
        if not self._connector.is_connected():
            record_skip(Subsystem.ORDER_EXECUTOR, SkipReason.IBKR_DISCONNECTED, symbol)
            logger.debug(f"OrderExecutor: IBKR not connected — {symbol} stays SIM")
            return False

        if self._orphan_gate_active:
            record_skip(Subsystem.ORDER_EXECUTOR, SkipReason.ORPHAN_GATE, symbol)
            logger.warning(
                f"OrderExecutor: ORPHAN GATE — blocking {symbol} entry. "
                f"Orphan positions: {', '.join(self._orphan_symbols)}. "
                "Resolve in TWS or call acknowledge_orphans()."
            )
            return False

        try:
            self._ensure_events()
            ib = self._connector._ib

            # Qualify contract
            contract = self._connector._qualify_stock_contract(symbol)
            if contract is None:
                logger.warning(f"OrderExecutor: cannot qualify {symbol}")
                return False

            # Build bracket order
            action = "BUY" if side.upper() == "LONG" else "SELL"
            bracket = ib.bracketOrder(
                action=action,
                quantity=quantity,
                limitPrice=round(entry_price, 2),
                takeProfitPrice=round(target_price, 2),
                stopLossPrice=round(stop_price, 2),
            )
            parent, tp, sl = bracket

            # Place all 3 legs
            for order in bracket:
                ib.placeOrder(contract, order)

            # Extract order IDs
            parent_id = parent.orderId
            tp_id = tp.orderId
            sl_id = sl.orderId
            perm_id = parent.permId or 0

            # Store in-memory mappings
            self._order_to_trade[parent_id] = trade_id
            self._order_to_trade[tp_id] = trade_id
            self._order_to_trade[sl_id] = trade_id
            self._trade_orders[trade_id] = {
                "parent_id": parent_id,
                "tp_id": tp_id,
                "sl_id": sl_id,
                "status": "SUBMITTED",
                "symbol": symbol,
            }

            # Persist to DB
            self._db.update_paper_trade_ibkr_orders(
                trade_id=trade_id,
                parent_order_id=parent_id,
                tp_order_id=tp_id,
                sl_order_id=sl_id,
                perm_id=perm_id,
            )

            logger.info(
                f"IBKR BRACKET: {symbol} {action} x{quantity} "
                f"entry=${entry_price:.2f} stop=${stop_price:.2f} "
                f"target=${target_price:.2f} "
                f"(parent={parent_id}, tp={tp_id}, sl={sl_id}, trade_id={trade_id})"
            )
            return True

        except Exception as e:
            logger.error(f"OrderExecutor: failed to place bracket for {symbol}: {e}")
            return False

    def modify_stop(self, trade_id: int, new_stop: float) -> bool:
        """Modify the stop-loss leg of an existing bracket."""
        info = self._trade_orders.get(trade_id)
        if not info:
            return False

        try:
            ib = self._connector._ib
            if not ib or not self._connector.is_connected():
                return False

            sl_id = info["sl_id"]
            # Get open orders and find our SL
            open_orders = ib.openOrders()
            for order in open_orders:
                if order.orderId == sl_id:
                    order.auxPrice = round(new_stop, 2)
                    contract = self._connector._qualify_stock_contract(info["symbol"])
                    if contract:
                        ib.placeOrder(contract, order)
                        logger.info(
                            f"IBKR MODIFY STOP: {info['symbol']} "
                            f"sl_order={sl_id} → ${new_stop:.2f}"
                        )
                        return True
            return False
        except Exception as e:
            logger.warning(f"OrderExecutor: modify_stop failed: {e}")
            return False

    def cancel_bracket(self, trade_id: int) -> bool:
        """Cancel all open legs of a bracket (e.g., when sim logic closes trade)."""
        info = self._trade_orders.get(trade_id)
        if not info:
            return False

        try:
            ib = self._connector._ib
            if not ib or not self._connector.is_connected():
                return False

            cancelled = 0
            open_orders = ib.openOrders()
            target_ids = {info["parent_id"], info["tp_id"], info["sl_id"]}

            for order in open_orders:
                if order.orderId in target_ids:
                    ib.cancelOrder(order)
                    cancelled += 1

            if cancelled:
                logger.info(
                    f"IBKR CANCEL: {info['symbol']} cancelled {cancelled} legs "
                    f"(trade_id={trade_id})"
                )
                self._db.update_paper_trade_ibkr_status(trade_id, "CANCELLED")

            # Clean up in-memory state
            for oid in target_ids:
                self._order_to_trade.pop(oid, None)
            self._trade_orders.pop(trade_id, None)

            return cancelled > 0
        except Exception as e:
            logger.warning(f"OrderExecutor: cancel_bracket failed: {e}")
            return False

    def reconcile_on_startup(self) -> Dict[str, int]:
        """Match IBKR positions/orders to DB trades after restart.

        Returns stats: {matched, orphan_ibkr, orphan_db, closed_externally}.
        """
        stats = {"matched": 0, "orphan_ibkr": 0, "orphan_db": 0, "closed_externally": 0}

        if not self._connector.is_connected():
            logger.warning("OrderExecutor: cannot reconcile — IBKR not connected")
            return stats

        try:
            self._ensure_events()
            ib = self._connector._ib

            # Phase 1: Get IBKR state
            positions = ib.positions()
            ibkr_symbols = {}
            for pos in positions:
                sym = pos.contract.symbol
                qty = pos.position
                if qty != 0:
                    ibkr_symbols[sym] = float(qty)

            open_orders = ib.openOrders()
            ibkr_order_ids = {o.orderId for o in open_orders}

            # Phase 2: Match DB LIVE trades
            db_live = self._db.get_live_open_trades()
            for trade in db_live:
                tid = trade["id"]
                sym = trade["symbol"]
                parent_id = trade.get("ibkr_parent_order_id")
                tp_id = trade.get("ibkr_tp_order_id")
                sl_id = trade.get("ibkr_sl_order_id")
                perm_id = trade.get("ibkr_perm_id")

                # Check if IBKR still has this position
                has_position = sym in ibkr_symbols
                has_orders = (
                    (parent_id and parent_id in ibkr_order_ids)
                    or (tp_id and tp_id in ibkr_order_ids)
                    or (sl_id and sl_id in ibkr_order_ids)
                )

                if has_position or has_orders:
                    # Re-register in-memory mappings
                    if parent_id:
                        self._order_to_trade[parent_id] = tid
                    if tp_id:
                        self._order_to_trade[tp_id] = tid
                    if sl_id:
                        self._order_to_trade[sl_id] = tid
                    self._trade_orders[tid] = {
                        "parent_id": parent_id or 0,
                        "tp_id": tp_id or 0,
                        "sl_id": sl_id or 0,
                        "status": "FILLED" if has_position else "SUBMITTED",
                        "symbol": sym,
                    }
                    stats["matched"] += 1
                    logger.info(
                        f"IBKR RECONCILE: matched {sym} (trade_id={tid}, "
                        f"position={'YES' if has_position else 'NO'}, "
                        f"orders={'YES' if has_orders else 'NO'})"
                    )
                else:
                    # DB says LIVE but IBKR has no position or orders — closed externally
                    now_iso = datetime.now(timezone.utc).isoformat()
                    self._db.close_paper_trade(
                        trade_id=tid,
                        closed_at=now_iso,
                        exit_price=float(trade.get("entry_price") or 0),
                        exit_reason="IBKR_CLOSED_EXTERNALLY",
                        realized_pnl=0.0,
                        realized_pnl_pct=0.0,
                        fees=0.0,
                    )
                    stats["closed_externally"] += 1
                    logger.warning(
                        f"IBKR RECONCILE: {sym} (trade_id={tid}) closed externally — "
                        "no IBKR position or orders found"
                    )

            # Phase 3: Check for orphan IBKR positions
            db_live_symbols = {t["symbol"] for t in db_live}
            orphans = []
            for sym, qty in ibkr_symbols.items():
                if sym not in db_live_symbols:
                    stats["orphan_ibkr"] += 1
                    orphans.append(sym)
                    logger.warning(
                        f"IBKR RECONCILE: orphan IBKR position {sym} x{qty} "
                        "(no matching DB trade — handle manually)"
                    )

            # Activate orphan gate if any unresolved positions exist
            self._orphan_symbols = orphans
            if orphans:
                self._orphan_gate_active = True
                logger.error(
                    "ORPHAN GATE ACTIVE — {} orphan position(s) detected: {}. "
                    "New IBKR entries BLOCKED until orphans are resolved. "
                    "Close them in TWS or call executor.acknowledge_orphans() to override.",
                    len(orphans), ", ".join(orphans),
                )
            else:
                self._orphan_gate_active = False

            logger.info(f"IBKR reconciliation complete: {stats}")
            return stats

        except Exception as e:
            logger.error(f"OrderExecutor: reconciliation failed: {e}")
            return stats

    def check_open_orders(self) -> None:
        """Periodic check: sync order status for LIVE trades.

        Called every 60s by scheduler. Catches fills that might have been
        missed if event handler didn't fire (e.g., brief disconnect).
        """
        if not self._connector.is_connected():
            return

        try:
            ib = self._connector._ib
            # Check IBKR positions to detect fills
            positions = ib.positions()
            ibkr_pos = {}
            for pos in positions:
                if pos.position != 0:
                    ibkr_pos[pos.contract.symbol] = float(pos.position)

            # For each LIVE trade, verify alignment
            live_trades = self._db.get_live_open_trades()
            for trade in live_trades:
                sym = trade["symbol"]
                tid = trade["id"]
                status = trade.get("ibkr_order_status", "")

                # If parent submitted but we now have a position, it was filled
                if status == "SUBMITTED" and sym in ibkr_pos:
                    self._db.update_paper_trade_ibkr_status(tid, "FILLED")
                    logger.info(f"IBKR SYNC: {sym} parent filled (detected via position check)")

        except Exception as e:
            logger.debug(f"OrderExecutor: check_open_orders error: {e}")

    def shutdown(self) -> None:
        """Clean up event handlers before shutdown."""
        if self._events_registered:
            try:
                ib = self._connector._ib
                if ib:
                    ib.orderStatusEvent -= self._on_order_status
                    ib.errorEvent -= self._on_error
                self._events_registered = False
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Event handlers (called by ib_insync on order status changes)
    # ------------------------------------------------------------------

    def _ensure_events(self) -> None:
        """Register ib_insync event handlers if not already done."""
        if self._events_registered:
            return
        ib = self._connector._ib
        if ib:
            ib.orderStatusEvent += self._on_order_status
            ib.errorEvent += self._on_error
            self._events_registered = True

    def _on_order_status(self, trade) -> None:
        """Handle order status updates from ib_insync.

        Maps orderId to our trade_id and processes fills/cancellations.
        """
        try:
            order_id = trade.order.orderId
            status = trade.orderStatus.status
            filled = trade.orderStatus.filled
            remaining = trade.orderStatus.remaining
            avg_fill = trade.orderStatus.avgFillPrice

            trade_id = self._order_to_trade.get(order_id)
            if trade_id is None:
                return  # Not our order

            info = self._trade_orders.get(trade_id, {})
            symbol = info.get("symbol", "?")

            logger.debug(
                f"IBKR EVENT: {symbol} order={order_id} status={status} "
                f"filled={filled} remaining={remaining} avgFill={avg_fill}"
            )

            if status == "Filled" and remaining == 0:
                self._handle_fill(trade_id, order_id, avg_fill, info)
            elif status in ("Cancelled", "ApiCancelled"):
                logger.info(
                    f"IBKR CANCELLED: {symbol} order={order_id} (trade_id={trade_id})"
                )
            elif status == "Inactive":
                logger.warning(
                    f"IBKR INACTIVE: {symbol} order={order_id} — "
                    "possible rejection or insufficient margin"
                )
                self._db.update_paper_trade_ibkr_status(trade_id, "ERROR")

        except Exception as e:
            logger.error(f"OrderExecutor: _on_order_status error: {e}")

    def _on_error(self, reqId, errorCode, errorString, contract) -> None:
        """Handle IBKR error events."""
        # Only log trading-relevant errors
        if errorCode in (201, 202, 110, 103, 104):
            sym = contract.symbol if contract else "?"
            logger.warning(
                f"IBKR ERROR: code={errorCode} {errorString} "
                f"(reqId={reqId}, symbol={sym})"
            )
            # Try to find the trade
            trade_id = self._order_to_trade.get(reqId)
            if trade_id:
                self._db.update_paper_trade_ibkr_status(trade_id, "ERROR")

    def _handle_fill(
        self, trade_id: int, order_id: int, fill_price: float, info: Dict
    ) -> None:
        """Process a filled order — could be parent, TP, or SL."""
        lock = self._trade_locks.setdefault(trade_id, threading.Lock())
        with lock:
            # Re-read trade from DB to check it's still open
            trade = self._db.get_trade_by_id(trade_id)
            if not trade or trade.get("status") != "OPEN":
                return  # Already closed by sim or another event

            symbol = info.get("symbol", "?")
            now_iso = datetime.now(timezone.utc).isoformat()

            if order_id == info.get("parent_id"):
                # Parent filled — entry confirmed
                self._db.update_paper_trade_ibkr_status(
                    trade_id, "FILLED", fill_price, now_iso
                )
                logger.info(
                    f"IBKR FILL: {symbol} parent filled @ ${fill_price:.2f} "
                    f"(trade_id={trade_id})"
                )

            elif order_id == info.get("tp_id"):
                # Take-profit filled — close trade as winner
                entry = float(trade.get("entry_price") or fill_price)
                qty = int(trade.get("quantity") or 0)
                side = trade.get("side", "LONG")
                pnl_per = (fill_price - entry) if side == "LONG" else (entry - fill_price)
                pnl = pnl_per * qty
                pnl_pct = (pnl_per / entry * 100) if entry > 0 else 0

                self._db.close_paper_trade(
                    trade_id=trade_id,
                    closed_at=now_iso,
                    exit_price=round(fill_price, 4),
                    exit_reason="IBKR_TARGET",
                    realized_pnl=round(pnl, 2),
                    realized_pnl_pct=round(pnl_pct, 2),
                    fees=0.0,
                )
                self._cleanup_trade(trade_id)
                logger.info(
                    f"IBKR TARGET: {symbol} TP filled @ ${fill_price:.2f} "
                    f"PnL=${pnl:+.2f} (trade_id={trade_id})"
                )

            elif order_id == info.get("sl_id"):
                # Stop-loss filled — close trade as loser
                entry = float(trade.get("entry_price") or fill_price)
                qty = int(trade.get("quantity") or 0)
                side = trade.get("side", "LONG")
                pnl_per = (fill_price - entry) if side == "LONG" else (entry - fill_price)
                pnl = pnl_per * qty
                pnl_pct = (pnl_per / entry * 100) if entry > 0 else 0

                self._db.close_paper_trade(
                    trade_id=trade_id,
                    closed_at=now_iso,
                    exit_price=round(fill_price, 4),
                    exit_reason="IBKR_STOP_LOSS",
                    realized_pnl=round(pnl, 2),
                    realized_pnl_pct=round(pnl_pct, 2),
                    fees=0.0,
                )
                self._cleanup_trade(trade_id)
                logger.info(
                    f"IBKR STOP: {symbol} SL filled @ ${fill_price:.2f} "
                    f"PnL=${pnl:+.2f} (trade_id={trade_id})"
                )

    def _cleanup_trade(self, trade_id: int) -> None:
        """Remove in-memory state for a closed trade."""
        info = self._trade_orders.pop(trade_id, {})
        for key in ("parent_id", "tp_id", "sl_id"):
            oid = info.get(key)
            if oid:
                self._order_to_trade.pop(oid, None)
        self._trade_locks.pop(trade_id, None)
