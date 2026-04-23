"""EOD Exit Sweep — close all intraday positions before market close.

Runs at 3:50 PM ET. Closes all CONTEXT_MOMENTUM paper trades at current price.
Pattern strategy trades (VWAP_MR, FPB, ORB_V2) have their own exit logic.

Usage:
    Called from strategy engine scheduler near EOD.
"""

from __future__ import annotations

from datetime import datetime
from typing import Dict, List

from loguru import logger


# Intraday strategies that must exit before close
INTRADAY_STRATEGIES = {"CONTEXT_MOMENTUM", "VWAP_MR", "FPB", "ORB_V2"}

# EOD exit time
EOD_EXIT_HOUR = 15
EOD_EXIT_MIN = 50  # 3:50 PM ET


def should_run_eod_exit(now_et: datetime) -> bool:
    """Check if it's time for EOD exit sweep."""
    hm = now_et.hour * 100 + now_et.minute
    return hm >= EOD_EXIT_HOUR * 100 + EOD_EXIT_MIN


def run_eod_exit(db_manager, bar_store) -> Dict[str, int]:
    """Close all open intraday paper trades at current price.

    Returns: {closed: N, errors: N}
    """
    closed = 0
    errors = 0

    try:
        open_trades = db_manager.get_open_paper_trades()
        intraday_trades = [
            t for t in open_trades
            if (t.get("strategy_type") or t.get("recommendation_source", ""))
            in INTRADAY_STRATEGIES
            or "CONTEXT_MOMENTUM" in str(t.get("recommendation_source", ""))
        ]

        if not intraday_trades:
            return {"closed": 0, "errors": 0}

        now_iso = datetime.utcnow().isoformat()

        for trade in intraday_trades:
            trade_id = trade.get("id")
            symbol = trade.get("symbol", "?")
            entry_price = float(trade.get("entry_price") or 0)
            qty = int(trade.get("quantity") or 0)
            side = trade.get("side", "LONG")

            # Get current price from bar store
            current = bar_store.get_latest_price(symbol)
            if not current:
                # Fallback to entry price (flat close)
                current = entry_price

            # Compute P&L
            if side == "LONG":
                pnl = round((current - entry_price) * qty, 2)
            else:
                pnl = round((entry_price - current) * qty, 2)

            notional = entry_price * qty if entry_price and qty else 1
            pnl_pct = round((pnl / notional) * 100, 2) if notional > 0 else 0

            try:
                db_manager.close_paper_trade(
                    trade_id=trade_id,
                    closed_at=now_iso,
                    exit_price=round(current, 4),
                    exit_reason="EOD_EXIT",
                    realized_pnl=pnl,
                    realized_pnl_pct=pnl_pct,
                    fees=0,
                )
                closed += 1
                logger.info(
                    "EOD EXIT: {} {} @ ${:.2f} → ${:.2f} P&L=${:+.2f} ({:+.1f}%)",
                    symbol, side, entry_price, current, pnl, pnl_pct,
                )
            except Exception as e:
                logger.warning("EOD exit failed for {} (trade #{}): {}", symbol, trade_id, e)
                errors += 1

    except Exception as e:
        logger.warning("EOD exit sweep error: {}", e)
        errors += 1

    if closed > 0:
        logger.info("EOD EXIT SWEEP: {} trades closed, {} errors", closed, errors)

    return {"closed": closed, "errors": errors}
