"""Intraday Ideas Data — provides live idea data for Intraday ML / Snipers tabs.

Reads from:
  - live_strategy_signals (SQLite) — what the strategies detected
  - ideas table (SQLite) — idea lifecycle state
  - paper_trades (SQLite) — which ideas became trades

Returns ranked idea dicts for the dashboard.
"""

from __future__ import annotations

from typing import Any, Dict, List

from loguru import logger


def get_intraday_ideas(db_manager, bar_store) -> List[Dict[str, Any]]:
    """Get current intraday ideas with lifecycle state.

    Returns list of idea dicts sorted by quality, showing:
      - NEW ideas (detected but not entered)
      - ENTERED ideas (auto-entered, linked to trade)
      - All with context evidence
    """
    ideas = []

    try:
        # Get alive ideas from idea_ledger (includes CONTEXT_MOMENTUM + pattern strategies)
        alive = db_manager.idea_ledger.get_alive_ideas()
        entered = db_manager.idea_ledger.get_entered_ideas()

        # Also get today's signals from live bar store for context
        import sqlite3
        conn = sqlite3.connect(bar_store._db_path)
        conn.row_factory = sqlite3.Row
        signals = conn.execute("""
            SELECT strategy, symbol, signal_type, score, percentile, rationale,
                   bar_ts_used, status
            FROM live_strategy_signals
            WHERE signal_type = 'ENTRY'
            ORDER BY id DESC LIMIT 50
        """).fetchall()
        conn.close()

        # Build signal lookup
        signal_map = {}
        for s in signals:
            key = (s["strategy"], s["symbol"])
            if key not in signal_map:
                signal_map[key] = dict(s)

        # Merge alive ideas with signal context
        for idea in alive:
            source = idea.get("source", "")
            symbol = idea.get("symbol", "")
            sig = signal_map.get((source, symbol), {})

            # Parse rationale for context
            rationale = sig.get("rationale", "")
            rs = ""
            vol_p = ""
            vwap = ""
            if "RS=" in rationale:
                try:
                    import json
                    ctx = json.loads(rationale)
                    rs = f"{ctx.get('sector_rs', 0):+.3f}" if ctx.get("sector_rs") else ""
                    vol_p = str(ctx.get("vol_pressure", "")) if ctx.get("vol_pressure") else ""
                    vwap = f"{ctx.get('vwap_sigma', 0):+.1f}σ" if ctx.get("vwap_sigma") is not None else ""
                except Exception:
                    pass

            ideas.append({
                "symbol": symbol,
                "side": idea.get("side", "LONG"),
                "source": source,
                "state": idea.get("state", "NEW"),
                "entry_price": idea.get("entry_price"),
                "stop_loss": idea.get("stop_loss"),
                "target_1": idea.get("target_1"),
                "conviction": idea.get("conviction"),
                "ev_score": idea.get("ev_score"),
                "confirms": idea.get("confirm_count", 1),
                "first_seen": idea.get("first_seen_at", "")[:16],
                "trade_id": idea.get("trade_id"),
                "rs": rs,
                "vol_pressure": vol_p,
                "vwap_sigma": vwap,
            })

        # Add entered ideas
        for idea in entered:
            ideas.append({
                "symbol": idea.get("symbol", ""),
                "side": idea.get("side", "LONG"),
                "source": idea.get("source", ""),
                "state": "ENTERED",
                "entry_price": idea.get("entry_price"),
                "stop_loss": idea.get("stop_loss"),
                "target_1": idea.get("target_1"),
                "conviction": idea.get("conviction"),
                "ev_score": idea.get("ev_score"),
                "confirms": idea.get("confirm_count", 1),
                "first_seen": idea.get("first_seen_at", "")[:16],
                "trade_id": idea.get("trade_id"),
                "rs": "",
                "vol_pressure": "",
                "vwap_sigma": "",
            })

        # Sort: NEW first (actionable), then ENTERED
        state_order = {"NEW": 0, "ACTIVE": 1, "WATCHING": 2, "ENTERED": 3}
        ideas.sort(key=lambda x: (state_order.get(x["state"], 5), -(x.get("conviction") or 0)))

    except Exception as e:
        logger.warning("Intraday ideas data error: {}", e)

    return ideas
