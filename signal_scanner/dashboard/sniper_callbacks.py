"""Sniper Board + Performance + Global Search callbacks.

Wires up:
  - Sniper Board: loads EV-ranked trade ideas from intelligence_scores + IdeaBridge
  - Performance: open/closed trades, analytics, regime-stratified stats
  - Global Search: ticker input → ISR navigation
  - Regime Banner: live HMM state display
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from dash import Input, Output, State, callback_context, html, no_update
from dash.exceptions import PreventUpdate

from signal_scanner.config import DashboardConfig
from signal_scanner.institutional_intel.config import safe_duckdb_connect

cfg = DashboardConfig()
logger = logging.getLogger(__name__)

# Drishti v1 feature flag: AI convergence signals (PULLBACK_SNIPER /
# SMART_MONEY_CONVERGENCE / SWING_CONFLUENCE / ACCUMULATION_BREAKOUT) currently
# inject a +10% EV boost on Sniper Board rows. They've never been validated
# against forward returns — disabled while we focus on validated edges.
# Flip to True to bring them back unchanged.
INJECT_CONVERGENCE_SIGNALS = False

# Regime state display mapping
REGIME_DISPLAY = {
    0: ("CRASH", "#ff4488", "All entries blocked"),
    1: ("DISTRIBUTING", "#ff8c00", "SHORT only"),
    2: ("ACCUMULATING", "#ffd43b", "LONG allowed (tight stops)"),
    3: ("MEAN-REV", "#4da3ff", "LONG allowed"),
    4: ("TRENDING", "#00ff88", "LONG entries primary"),
}


def register_sniper_callbacks(app, db_manager, scanner=None) -> None:
    """Register Sniper Board, Performance, and Global Search callbacks."""

    # ------------------------------------------------------------------
    # 0. IBKR STATUS BADGE — live connection indicator
    # ------------------------------------------------------------------
    @app.callback(
        Output("ibkr-status-badge", "children"),
        Output("ibkr-status-badge", "style"),
        Input("sniper-refresh-interval", "n_intervals"),
        prevent_initial_call=False,
    )
    def update_ibkr_status(_n):
        connected = False
        account = ""
        port = ""
        try:
            if scanner and hasattr(scanner, "_connector"):
                conn = scanner._connector
                if hasattr(conn, "_ib") and conn._ib and conn._ib.isConnected():
                    connected = True
                    accts = conn._ib.managedAccounts()
                    account = accts[0] if accts else ""
                    port = str(getattr(conn, "_port", ""))
        except Exception:
            pass

        if connected:
            is_paper = account.startswith("DU") or account.startswith("DF")
            acct_type = "PAPER" if is_paper else "LIVE"
            icon_cls = "fas fa-plug-circle-check"
            label = f"IBKR: {acct_type} ({account})"
            style = {
                "display": "inline-flex", "alignItems": "center", "gap": "4px",
                "padding": "2px 10px", "borderRadius": "12px", "fontSize": "0.72rem",
                "fontWeight": "600", "marginRight": "10px",
                "backgroundColor": "#001a00" if is_paper else "#1a0000",
                "border": f"1px solid {'#00ff88' if is_paper else '#ff4488'}",
                "color": "#00ff88" if is_paper else "#ff4488",
            }
        else:
            icon_cls = "fas fa-plug-circle-xmark"
            label = "IBKR: DISCONNECTED"
            style = {
                "display": "inline-flex", "alignItems": "center", "gap": "4px",
                "padding": "2px 10px", "borderRadius": "12px", "fontSize": "0.72rem",
                "fontWeight": "600", "marginRight": "10px",
                "backgroundColor": "#1a0000", "border": "1px solid #ff4488",
                "color": "#ff4488",
            }

        children = [
            html.I(className=icon_cls, style={"fontSize": "0.65rem"}),
            html.Span(label),
        ]
        return children, style

    # ------------------------------------------------------------------
    # 0b. DATA FRESHNESS BADGE — shows when sniper board data was last computed
    # ------------------------------------------------------------------
    @app.callback(
        Output("sniper-freshness-badge", "children"),
        Output("sniper-degraded-banner", "children"),
        Output("sniper-degraded-banner", "style"),
        Input("sniper-refresh-interval", "n_intervals"),
        prevent_initial_call=False,
    )
    def update_freshness_badge(_n):
        from signal_scanner.core.readiness import compute_price_freshness
        from signal_scanner.core.telemetry import record_skip, SkipReason, Subsystem

        badge_text = ""
        # Inline DEGRADED banner is suppressed — the top status bar's
        # READINESS pill is the single source of truth for degraded state.
        # We still run the freshness check below so scanner.readiness and
        # scanner.data_degraded stay accurate.
        degraded_text = ""
        degraded_style = {"display": "none"}
        try:
            price_ok, lag, latest_str = compute_price_freshness()
            if latest_str:
                badge_text = f"Prices as of: {latest_str}"

                if not price_ok:
                    record_skip(Subsystem.EXECUTION_LOOP, SkipReason.DATA_STALE,
                                 f"dashboard: {lag}d stale", persist=False)
                    if scanner:
                        scanner.data_degraded = True
                        scanner.data_freshness = {
                            "ok": False, "prices_age_days": lag,
                            "latest_price_date": latest_str,
                        }
                        if scanner.readiness:
                            scanner.readiness.prices_age_days = lag
                            scanner.readiness.latest_price_date = latest_str
                else:
                    if scanner:
                        scanner.data_degraded = False
                        scanner.data_freshness = {
                            "ok": True, "prices_age_days": lag,
                            "latest_price_date": latest_str,
                        }
                        if scanner.readiness:
                            scanner.readiness.prices_age_days = lag
                            scanner.readiness.latest_price_date = latest_str
        except Exception:
            pass

        return badge_text, degraded_text, degraded_style

    # ------------------------------------------------------------------
    # 1a. KPI chip clicks → side/source filter
    #     Clicking a KPI chip (SETUPS, LONG, SHORT, TRIPLE LOCK) drives the
    #     filter inputs that update_sniper_board() already watches.
    # ------------------------------------------------------------------
    @app.callback(
        Output("sniper-side-filter", "value", allow_duplicate=True),
        Output("sniper-source-filter", "value", allow_duplicate=True),
        Input("sniper-chip-all", "n_clicks"),
        Input("sniper-chip-long", "n_clicks"),
        Input("sniper-chip-short", "n_clicks"),
        Input("sniper-chip-triple", "n_clicks"),
        State("sniper-side-filter", "value"),
        State("sniper-source-filter", "value"),
        prevent_initial_call=True,
    )
    def kpi_chip_to_filter(_n_all, _n_long, _n_short, _n_triple,
                            cur_side, cur_source):
        ctx = callback_context
        if not ctx.triggered:
            raise PreventUpdate
        trigger_id = ctx.triggered[0]["prop_id"].split(".")[0]
        if trigger_id == "sniper-chip-all":
            return "ALL", "ALL"
        if trigger_id == "sniper-chip-long":
            return "LONG", cur_source or "ALL"
        if trigger_id == "sniper-chip-short":
            return "SHORT", cur_source or "ALL"
        if trigger_id == "sniper-chip-triple":
            return cur_side or "ALL", "TRIPLE_LOCK"
        raise PreventUpdate

    # ------------------------------------------------------------------
    # 1b. Reflect active filter on chips (highlight which chip is selected)
    # ------------------------------------------------------------------
    @app.callback(
        Output("sniper-chip-all", "className"),
        Output("sniper-chip-long", "className"),
        Output("sniper-chip-short", "className"),
        Output("sniper-chip-triple", "className"),
        Input("sniper-side-filter", "value"),
        Input("sniper-source-filter", "value"),
        prevent_initial_call=False,
    )
    def highlight_active_chip(side, source):
        base = "kb-kpi-chip clickable"
        active = "kb-kpi-chip clickable active"
        side = (side or "ALL").upper()
        source = (source or "ALL").upper()
        return (
            active if side == "ALL" and source == "ALL" else base,
            active if side == "LONG" else base,
            active if side == "SHORT" else base,
            active if source == "TRIPLE_LOCK" else base,
        )

    # ------------------------------------------------------------------
    # 1. GLOBAL SEARCH — ticker input → selected-ticker (ISR navigation)
    # ------------------------------------------------------------------
    @app.callback(
        Output("selected-ticker", "data", allow_duplicate=True),
        Output("global-search-input", "value"),
        Input("global-search-btn", "n_clicks"),
        Input("global-search-input", "n_submit"),
        State("global-search-input", "value"),
        prevent_initial_call=True,
    )
    def global_search(n_clicks, n_submit, ticker):
        if not ticker or not ticker.strip():
            raise PreventUpdate
        symbol = ticker.strip().upper()
        return symbol, ""

    # ------------------------------------------------------------------
    # 3. SNIPER BOARD — load and display EV-ranked trade ideas
    # ------------------------------------------------------------------
    @app.callback(
        Output("sniper-board-table", "data"),
        Output("sniper-total", "children"),
        Output("sniper-long", "children"),
        Output("sniper-short", "children"),
        Output("sniper-triple", "children"),
        Output("sniper-avg-rr", "children"),
        Output("sniper-regime", "children"),
        Output("sniper-regime", "style"),
        Output("sniper-top10-table", "data"),
        Output("sniper-top10-summary", "children"),
        Input("sniper-refresh-interval", "n_intervals"),
        Input("sniper-regime-toggle", "value"),
        Input("sniper-side-filter", "value"),
        Input("sniper-source-filter", "value"),
        prevent_initial_call=False,
    )
    def update_sniper_board(_n, regime_aligned, side_filter, source_filter):
        conn = safe_duckdb_connect(read_only=True)
        if conn is None:
            return [], "0", "0", "0", "0", "0.0", "---", {"color": "#888"}, [], "Sit out — no qualifying setups"

        try:
            from signal_scanner.core.live_bar_store import LiveBarStore
            store = LiveBarStore()
            ideas = _load_sniper_ideas(conn, store)
            # Drishti v2: tag rows with their slow-edge source ("Why is this here?")
            try:
                from signal_scanner.dashboard.director_clusters import (
                    get_cluster_tickers_in_window,
                )
                director_set = get_cluster_tickers_in_window()
            except Exception:
                director_set = set()
            for idea in ideas:
                idea["why_tags"] = _build_why_tags(idea, director_set)
            # Daily revalidation — compute fast status on top of slow thesis
            from signal_scanner.paper.idea_revalidator import revalidate_all_ideas
            ideas = revalidate_all_ideas(conn, ideas)
            # Thesis freshness — is the institutional thesis still alive?
            from signal_scanner.institutional_intel.intelligence.thesis_freshness import enrich_ideas_with_freshness
            ideas = enrich_ideas_with_freshness(conn, ideas)
        except Exception as e:
            logger.warning("Sniper revalidation error: %s", e)
        finally:
            conn.close()

        if not ideas:
            return [], "0", "0", "0", "0", "0.0", "---", {"color": "#888"}, [], "Sit out — no qualifying setups"

        # Get current regime for badge
        regime_state = None
        try:
            from signal_scanner.institutional_intel.intelligence.regime_hmm import DailyRegimeHMM
            hmm = DailyRegimeHMM()
            hmm.load()
            if hmm._model is not None:
                regime_state, _probs, regime_name = hmm.current_regime()
        except Exception:
            pass

        # Apply regime badge + rule-match highlight to each idea
        from signal_scanner.dashboard.trade_rules import rule_match_mark
        for idea in ideas:
            if regime_state is not None:
                label, color, _ = REGIME_DISPLAY.get(regime_state, ("?", "#888", ""))
                idea["regime_badge"] = label
            else:
                idea["regime_badge"] = "N/A"
            idea["rule_match"] = rule_match_mark("swing", idea, regime_state)

        # Apply filters
        filtered = ideas
        if regime_aligned and regime_state is not None:
            # Only show ideas aligned with regime
            if regime_state == 0:
                filtered = []  # CRASH — no ideas
            elif regime_state == 1:
                filtered = [i for i in filtered if i.get("side") == "SHORT"]
            # States 2, 3, 4 allow LONG

        if side_filter != "ALL":
            filtered = [i for i in filtered if i.get("side") == side_filter]

        if source_filter != "ALL":
            filtered = [i for i in filtered if i.get("source_badge") == source_filter]

        # Compute KPI values
        n_total = len(filtered)
        n_long = sum(1 for i in filtered if i.get("side") == "LONG")
        n_short = sum(1 for i in filtered if i.get("side") == "SHORT")
        n_triple = sum(1 for i in filtered if i.get("source_badge") == "TRIPLE_LOCK")
        rr_values = [i.get("rr_ratio", 0) for i in filtered if i.get("rr_ratio")]
        avg_rr = f"{sum(rr_values) / len(rr_values):.1f}" if rr_values else "0.0"

        # Regime KPI
        if regime_state is not None:
            r_label, r_color, _ = REGIME_DISPLAY.get(regime_state, ("?", "#888", ""))
            regime_text = r_label
            regime_style = {"color": r_color}
        else:
            regime_text = "---"
            regime_style = {"color": "#888"}

        # Hide MISSED/INVALIDATED by default (unless explicitly filtered)
        if source_filter == "ALL" and side_filter == "ALL":
            filtered = [i for i in filtered
                        if i.get("daily_status") not in ("MISSED", "INVALIDATED")]

        # Smart sort: tier first → status → actionability → conviction
        from signal_scanner.paper.idea_revalidator import STATUS_PRIORITY, TIER_PRIORITY
        filtered.sort(key=lambda x: (
            TIER_PRIORITY.get(x.get("tier", "Bronze"), 3),       # Platinum first
            STATUS_PRIORITY.get(x.get("daily_status", "ACTIVE"), 3),
            -x.get("current_rr", x.get("rr_ratio", 0)),         # better R:R first
            abs(x.get("distance_pct", 0)),                        # closer to entry first
            -x.get("_conviction", x.get("ev_score", 0)),          # conviction as tiebreak
        ))
        for rank, idea in enumerate(filtered, 1):
            idea["rank"] = rank

        # Top 10 = first 10 of `filtered` (already smart-sorted above).
        top10 = filtered[:10]
        if top10:
            n_long_top10 = sum(1 for i in top10 if i.get("side") == "LONG")
            n_short_top10 = sum(1 for i in top10 if i.get("side") == "SHORT")
            top10_summary = (
                f"{len(top10)} setup(s) • {n_long_top10} long / {n_short_top10} short"
                f" • avg R:R {avg_rr}"
            )
        else:
            top10_summary = "Sit out — no qualifying setups today"

        return (filtered, str(n_total), str(n_long), str(n_short),
                str(n_triple), avg_rr, regime_text, regime_style,
                top10, top10_summary)

    # ------------------------------------------------------------------
    # 4. SNIPER BOARD — row click → detail panel
    # ------------------------------------------------------------------
    @app.callback(
        Output("sniper-detail-panel", "hidden"),
        Output("sniper-detail-symbol", "children"),
        Output("sniper-detail-conviction", "children"),
        Output("sniper-detail-ml", "children"),
        Output("sniper-detail-phase", "children"),
        Output("sniper-detail-pressure", "children"),
        Output("sniper-detail-squeeze", "children"),
        Output("sniper-detail-insider", "children"),
        Output("sniper-detail-idea-id", "data"),
        Output("sniper-detail-idea-state", "children"),
        Input("sniper-board-table", "active_cell"),
        Input("sniper-top10-table", "active_cell"),
        State("sniper-board-table", "derived_virtual_data"),
        State("sniper-board-table", "data"),
        State("sniper-top10-table", "derived_virtual_data"),
        State("sniper-top10-table", "data"),
        prevent_initial_call=True,
    )
    def sniper_row_detail(main_cell, top_cell, main_vdata, main_data,
                          top_vdata, top_data):
        from dash import ctx
        # Either table can drive the detail panel.
        if ctx.triggered_id == "sniper-top10-table":
            active_cell = top_cell
            view_data = top_vdata if top_vdata else top_data
        else:
            active_cell = main_cell
            view_data = main_vdata if main_vdata else main_data

        if not active_cell or not view_data:
            raise PreventUpdate

        row_idx = active_cell.get("row", 0)
        if row_idx >= len(view_data):
            raise PreventUpdate

        row = view_data[row_idx]
        symbol = row.get("symbol", "?")

        # Fetch detail data from intelligence_scores
        conn = safe_duckdb_connect(read_only=True)
        if conn is None:
            return (False, symbol, "?", "?", "?", "?", "?", "?", None, "")

        try:
            detail = conn.execute("""
                SELECT conviction_score, ml_score_v2, accum_phase,
                       institutional_pressure, squeeze_score, insider_effect_score
                FROM intelligence_scores
                WHERE ticker = ? AND report_quarter = (
                    SELECT MAX(report_quarter) FROM intelligence_scores
                    WHERE data_quality_score >= 75
                )
            """, [symbol]).fetchone()
        finally:
            conn.close()

        # Get idea lifecycle state
        idea_id = None
        idea_state_text = ""
        try:
            from signal_scanner.paper.idea_ledger import IdeaLedger
            from signal_scanner.config import ScannerConfig
            cfg = ScannerConfig()
            ledger = IdeaLedger(cfg.db_path)
            side = row.get("side", "LONG")
            idea = ledger.get_idea_for_symbol(symbol, side)
            if idea:
                idea_id = idea["id"]
                state = idea["state"]
                confirms = idea.get("confirm_count", 0)
                idea_state_text = f"{state} ({confirms}x)"
        except Exception:
            pass

        if detail:
            conv, ml, phase, pressure, squeeze, insider = detail
            return (
                False, symbol,
                f"{conv:.0f}" if conv else "N/A",
                f"{ml:.0f}" if ml else "N/A",
                str(phase) if phase else "N/A",
                f"{pressure:.0f}" if pressure else "0",
                f"{squeeze:.0f}" if squeeze else "0",
                f"{insider:.1f}" if insider else "N/A",
                idea_id, idea_state_text,
            )
        return (False, symbol, "N/A", "N/A", "N/A", "0", "0", "N/A",
                idea_id, idea_state_text)

    # ------------------------------------------------------------------
    # 5. SNIPER → ISR navigation (click "View Full ISR")
    # ------------------------------------------------------------------
    @app.callback(
        Output("selected-ticker", "data", allow_duplicate=True),
        Input("sniper-goto-isr", "n_clicks"),
        State("sniper-detail-symbol", "children"),
        prevent_initial_call=True,
    )
    def sniper_goto_isr(n_clicks, symbol):
        if not n_clicks or not symbol:
            raise PreventUpdate
        return symbol

    # ------------------------------------------------------------------
    # 5b. SNIPER — Enter Trade from idea
    # ------------------------------------------------------------------
    # 5b. SNIPER — Open trade entry modal (pre-filled with idea defaults)
    # ------------------------------------------------------------------
    @app.callback(
        Output("sniper-trade-modal", "is_open"),
        Output("sniper-trade-modal-title", "children"),
        Output("sniper-trade-price", "value"),
        Output("sniper-trade-qty", "value"),
        Output("sniper-trade-stop", "value"),
        Output("sniper-trade-target", "value"),
        Input("sniper-enter-trade", "n_clicks"),
        Input("sniper-trade-cancel", "n_clicks"),
        State("sniper-detail-symbol", "children"),
        State("sniper-board-table", "active_cell"),
        State("sniper-board-table", "data"),
        State("sniper-board-table", "derived_virtual_data"),
        State("sniper-trade-modal", "is_open"),
        prevent_initial_call=True,
    )
    def sniper_open_trade_modal(enter_clicks, cancel_clicks, symbol,
                                active_cell, data, virtual_data, is_open):
        from dash import ctx
        trigger = ctx.triggered_id
        if trigger == "sniper-trade-cancel":
            return False, "", None, None, None, None
        if trigger != "sniper-enter-trade" or not enter_clicks or not symbol:
            raise PreventUpdate

        # Resolve the clicked row from the SORTED/FILTERED view — active_cell.row
        # indexes the displayed order, not the raw data array.
        view = virtual_data if virtual_data else (data or [])
        row = {}
        if active_cell is not None and view:
            idx = active_cell.get("row", 0)
            if 0 <= idx < len(view):
                row = view[idx]
        # Fall back to matching by the detail-panel symbol if indexing missed.
        if (not row or row.get("symbol") != symbol) and view:
            row = next((r for r in view if r.get("symbol") == symbol), row)

        side = row.get("side", "LONG")
        entry_price = row.get("entry_price")
        stop = row.get("stop_price")
        target = row.get("target_1")

        # Realtime: re-anchor to the LIVE price at click-time so the operator
        # enters at levels relative to where the stock trades NOW.
        try:
            from signal_scanner.core.live_bar_store import LiveBarStore
            live_px = LiveBarStore().get_latest_price(symbol)
            if live_px and live_px > 0:
                lconn = safe_duckdb_connect(read_only=True)
                if lconn:
                    try:
                        e2, s2, t1b, _t2, _rr = _estimate_levels(
                            lconn, symbol, side, entry_override=live_px)
                    finally:
                        lconn.close()
                    if e2:
                        entry_price, stop, target = e2, s2, t1b
        except Exception:
            pass

        # Default quantity
        qty = None
        if entry_price and entry_price > 0:
            from math import ceil
            qty = ceil(10_000 / entry_price) if entry_price >= 10 else 1000

        title = f"Enter {side} Trade: {symbol}"
        return True, title, entry_price, qty, stop, target

    # ------------------------------------------------------------------
    # 5b2. SNIPER — Confirm trade entry from modal
    # ------------------------------------------------------------------
    @app.callback(
        Output("sniper-action-toast", "children"),
        Output("sniper-action-toast", "is_open"),
        Output("sniper-action-toast", "header"),
        Output("sniper-trade-modal", "is_open", allow_duplicate=True),
        Input("sniper-trade-confirm", "n_clicks"),
        State("sniper-detail-symbol", "children"),
        State("sniper-detail-idea-id", "data"),
        State("sniper-trade-price", "value"),
        State("sniper-trade-qty", "value"),
        State("sniper-trade-stop", "value"),
        State("sniper-trade-target", "value"),
        State("sniper-trade-time", "value"),
        State("sniper-trade-notes", "value"),
        State("sniper-board-table", "active_cell"),
        State("sniper-board-table", "data"),
        prevent_initial_call=True,
    )
    def sniper_confirm_trade(n_clicks, symbol, idea_id,
                             price, qty, stop, target, entry_time, notes,
                             active_cell, data):
        if not n_clicks or not symbol:
            raise PreventUpdate

        # Validate required fields
        if not price or not qty:
            return "Entry price and quantity are required", True, "Validation Error", True

        try:
            from signal_scanner.config import ScannerConfig
            from signal_scanner.database.db_manager import DatabaseManager as DBManager
            from datetime import datetime, timezone

            cfg = ScannerConfig()
            db = DBManager(cfg.db_path)
            db.init_db()

            row = {}
            if active_cell and data:
                idx = active_cell.get("row", 0)
                if idx < len(data):
                    row = data[idx]

            # Parse entry time or use now
            opened_at = entry_time.strip() if entry_time and entry_time.strip() else None
            if not opened_at:
                opened_at = datetime.now(timezone.utc).isoformat()

            overrides = {
                "entry_price": float(price),
                "quantity": int(qty),
                "stop_loss": float(stop) if stop else None,
                "target_1": float(target) if target else None,
                "opened_at": opened_at,
                "execution_mode": "SIM",
            }
            if notes:
                overrides["entry_trade_conditions"] = (
                    overrides.get("entry_trade_conditions", "") +
                    f" | Notes={notes}"
                )

            if idea_id:
                trade_id = db.create_trade_from_idea(idea_id, overrides=overrides)
                if notes:
                    db.idea_ledger.add_note(idea_id, notes)
            else:
                # Create idea first
                side = row.get("side", "LONG")
                idea_data = {
                    "symbol": symbol,
                    "side": side,
                    "source": f"MANUAL_{row.get('source_badge', 'SNIPER')}",
                    "entry_price": float(price),
                    "stop_loss": float(stop) if stop else None,
                    "target_1": float(target) if target else None,
                    "rr_ratio": row.get("rr_ratio"),
                    "conviction": row.get("_conviction"),
                    "ev_score": row.get("ev_score"),
                }
                new_idea_id = db.idea_ledger.upsert_idea(idea_data)
                trade_id = db.create_trade_from_idea(new_idea_id, overrides=overrides)
                if notes:
                    db.idea_ledger.add_note(new_idea_id, notes)
                idea_id = new_idea_id

            if trade_id:
                return (f"Trade #{trade_id} opened: {symbol} @ ${price} x{qty}",
                        True, "Trade Entered", False)
            return ("Failed to enter trade — check position limits",
                    True, "Entry Blocked", True)
        except Exception as e:
            return (f"Error: {e}", True, "Error", True)

    # ------------------------------------------------------------------
    # 5c. SNIPER — Watch idea
    # ------------------------------------------------------------------
    @app.callback(
        Output("sniper-action-toast", "children", allow_duplicate=True),
        Output("sniper-action-toast", "is_open", allow_duplicate=True),
        Output("sniper-action-toast", "header", allow_duplicate=True),
        Input("sniper-watch-idea", "n_clicks"),
        State("sniper-detail-symbol", "children"),
        State("sniper-detail-idea-id", "data"),
        State("sniper-board-table", "active_cell"),
        State("sniper-board-table", "data"),
        prevent_initial_call=True,
    )
    def sniper_watch_idea(n_clicks, symbol, idea_id, active_cell, data):
        if not n_clicks or not symbol:
            raise PreventUpdate

        try:
            from signal_scanner.config import ScannerConfig
            from signal_scanner.database.db_manager import DatabaseManager as DBManager

            cfg = ScannerConfig()
            db = DBManager(cfg.db_path)
            db.init_db()

            if idea_id:
                db.idea_ledger.set_watching(idea_id)
                return (f"{symbol} idea #{idea_id} marked as WATCHING", True, "Watching")
            else:
                # Create a new idea in WATCHING state
                row = {}
                if active_cell and data:
                    idx = active_cell.get("row", 0)
                    if idx < len(data):
                        row = data[idx]
                side = row.get("side", "LONG")
                idea_data = {
                    "symbol": symbol,
                    "side": side,
                    "source": f"MANUAL_{row.get('source_badge', 'SNIPER')}",
                    "entry_price": row.get("entry_price"),
                    "stop_loss": row.get("stop_price"),
                    "target_1": row.get("target_1"),
                    "rr_ratio": row.get("rr_ratio"),
                    "conviction": row.get("_conviction"),
                    "ev_score": row.get("ev_score"),
                }
                new_id = db.idea_ledger.upsert_idea(idea_data)
                db.idea_ledger.set_watching(new_id)
                return (f"{symbol} added to watchlist as idea #{new_id}", True, "Watching")
        except Exception as e:
            return (f"Error: {e}", True, "Error")

    # ------------------------------------------------------------------
    # 6. PERFORMANCE — sub-tab switching
    # ------------------------------------------------------------------
    @app.callback(
        Output("perf-open-content", "hidden"),
        Output("perf-closed-content", "hidden"),
        Output("perf-analytics-content", "hidden"),
        Output("perf-manual-content", "hidden"),
        Output("perf-releases-content", "hidden"),
        Input("performance-subtabs", "active_tab"),
        prevent_initial_call=False,
    )
    def switch_performance_subtab(active_tab):
        # (open, closed, analytics, manual, releases)
        tab_map = {
            "perf-open-tab":      (False, True,  True,  True,  True),
            "perf-closed-tab":    (True,  False, True,  True,  True),
            "perf-analytics-tab": (True,  True,  False, True,  True),
            "perf-manual-tab":    (True,  True,  True,  False, True),
            "perf-releases-tab":  (True,  True,  True,  True,  False),
        }
        return tab_map.get(active_tab, (False, True, True, True, True))

    # ------------------------------------------------------------------
    # 7. PERFORMANCE — load open + closed trades, KPIs
    # ------------------------------------------------------------------
    @app.callback(
        Output("perf-open-table", "data"),
        Output("perf-closed-table", "data"),
        Output("perf-open", "children"),
        Output("perf-closed", "children"),
        Output("perf-win-rate", "children"),
        Output("perf-unrealized", "children"),
        Output("perf-unrealized", "style"),
        Output("perf-pnl", "children"),
        Output("perf-expectancy", "children"),
        Input("sniper-refresh-interval", "n_intervals"),
        Input("perf-regime-filter", "value"),
        Input("perf-strategy-filter", "value"),
        prevent_initial_call=False,
    )
    def update_performance(_n, regime_filter, strategy_filter):
        try:
            all_trades = db_manager.get_recent_paper_trades(limit=500)
        except Exception as e:
            logger.warning("Performance load error: %s", e)
            return [], [], "0", "0", "0%", "$0", {}, "$0", "$0"

        open_trades = [t for t in all_trades if t.get("status") == "OPEN"]
        closed_trades = [t for t in all_trades if t.get("status") == "CLOSED"]

        # Enrich open trades with current price + gain from DuckDB latest close
        if open_trades:
            try:
                from signal_scanner.institutional_intel.config import safe_duckdb_connect
                conn = safe_duckdb_connect(read_only=True)
                if conn:
                    symbols = list({t.get("symbol", "") for t in open_trades if t.get("symbol")})
                    placeholders = ",".join(["?"] * len(symbols))
                    price_rows = conn.execute(f"""
                        SELECT p.ticker, p.close
                        FROM fact_daily_prices p
                        INNER JOIN (
                            SELECT ticker, MAX(trade_date) AS max_date
                            FROM fact_daily_prices
                            WHERE ticker IN ({placeholders})
                            GROUP BY ticker
                        ) latest ON p.ticker = latest.ticker AND p.trade_date = latest.max_date
                    """, symbols).fetchall()
                    conn.close()
                    price_map = {r[0]: float(r[1]) for r in price_rows if r[1]}
                    for t in open_trades:
                        sym = t.get("symbol", "")
                        entry = float(t.get("entry_price") or 0)
                        curr = price_map.get(sym)
                        side = str(t.get("side", "LONG")).upper()
                        if curr and entry:
                            raw_pct = ((curr - entry) / entry * 100) if side == "LONG" else ((entry - curr) / entry * 100)
                            qty = float(t.get("quantity") or 1)
                            gain_dollar = (curr - entry) * qty if side == "LONG" else (entry - curr) * qty
                            t["current_price"] = round(curr, 2)
                            t["gain_pct"] = f"{raw_pct:+.2f}%"
                            t["gain_dollar"] = f"${gain_dollar:+.2f}"
                            t["gain_pct_raw"] = round(raw_pct, 4)
                        else:
                            t["current_price"] = None
                            t["gain_pct"] = "—"
                            t["gain_dollar"] = "—"
                            t["gain_pct_raw"] = 0
            except Exception as e:
                logger.debug(f"Live price fetch for open trades failed: {e}")
                for t in open_trades:
                    t.setdefault("current_price", None)
                    t.setdefault("gain_pct", "—")
                    t.setdefault("gain_dollar", "—")
                    t.setdefault("gain_pct_raw", 0)

        # Apply filters to closed trades
        if regime_filter and regime_filter != "ALL":
            closed_trades = [t for t in closed_trades
                             if t.get("entry_market_regime") == regime_filter]
        if strategy_filter and strategy_filter != "ALL":
            closed_trades = [t for t in closed_trades
                             if t.get("strategy_type") == strategy_filter]

        # Compute KPIs
        n_open = len(open_trades)
        n_closed = len(closed_trades)
        wins = [t for t in closed_trades
                if (t.get("realized_pnl") or 0) > 0]
        win_rate = f"{len(wins) / n_closed * 100:.0f}%" if n_closed else "0%"

        # Unrealized P&L (from enriched open trades)
        unrealized_total = 0.0
        for t in open_trades:
            try:
                raw = t.get("gain_pct_raw", 0) or 0
                entry = float(t.get("entry_price") or 0)
                qty = float(t.get("quantity") or 0)
                side = str(t.get("side", "LONG")).upper()
                curr = t.get("current_price")
                if curr and entry and qty:
                    if side == "LONG":
                        unrealized_total += (curr - entry) * qty
                    else:
                        unrealized_total += (entry - curr) * qty
            except Exception:
                pass

        unrealized_str = f"${unrealized_total:+,.0f}"
        unrealized_style = {
            "color": "#00ff88" if unrealized_total >= 0 else "#ff4488",
            "fontSize": "1.3rem", "fontWeight": "800",
            "fontFamily": "'JetBrains Mono', monospace",
        }

        total_pnl = sum(t.get("realized_pnl", 0) or 0 for t in closed_trades)
        pnl_str = f"${total_pnl:,.2f}"

        expectancy = f"${total_pnl / n_closed:,.2f}" if n_closed else "$0"

        return (open_trades, closed_trades,
                str(n_open), str(n_closed), win_rate,
                unrealized_str, unrealized_style,
                pnl_str, expectancy)

    # ------------------------------------------------------------------
    # 7b. PERFORMANCE — open position close panel (show on row select)
    # ------------------------------------------------------------------
    @app.callback(
        Output("perf-close-panel", "hidden"),
        Output("perf-close-info", "children"),
        Output("perf-close-trade-id", "data"),
        Output("perf-close-price", "value"),
        Input("perf-open-table", "selected_rows"),
        Input("perf-close-cancel", "n_clicks"),
        State("perf-open-table", "data"),
        prevent_initial_call=True,
    )
    def perf_show_close_panel(selected_rows, cancel_clicks, table_data):
        from dash import ctx
        if ctx.triggered_id == "perf-close-cancel":
            return True, "", None, None

        if not selected_rows or not table_data:
            return True, "", None, None

        row = table_data[selected_rows[0]]
        if row.get("status") != "OPEN":
            return True, "", None, None

        trade_id = row.get("id")
        symbol = row.get("symbol", "?")
        side = row.get("side", "?")
        entry = float(row.get("entry_price") or 0)
        qty = row.get("quantity", 0)
        current = row.get("current_price")
        info = (f"{symbol} | {side} | Entry: ${entry:.2f} | Qty: {qty} | "
                f"Current: ${current:.2f}" if current else
                f"{symbol} | {side} | Entry: ${entry:.2f} | Qty: {qty}")
        return False, info, trade_id, current

    # ------------------------------------------------------------------
    # 7c. PERFORMANCE — execute close trade
    # ------------------------------------------------------------------
    @app.callback(
        Output("perf-close-status", "children"),
        Output("perf-close-panel", "hidden", allow_duplicate=True),
        Output("perf-open-table", "selected_rows"),
        Input("perf-close-btn", "n_clicks"),
        State("perf-close-trade-id", "data"),
        State("perf-close-price", "value"),
        State("perf-close-reason", "value"),
        prevent_initial_call=True,
    )
    def perf_close_trade(n_clicks, trade_id, exit_price, reason):
        if not n_clicks or not trade_id:
            raise PreventUpdate
        if not exit_price or float(exit_price) <= 0:
            return "Exit price is required.", False, no_update

        try:
            result = db_manager.close_trade_and_update_idea(
                trade_id=int(trade_id),
                exit_price=float(exit_price),
                exit_reason=reason or "MANUAL_EXIT",
            )
            if not result:
                return "Trade not found or already closed.", False, no_update
            pnl = result.get("realized_pnl", 0)
            symbol = result.get("symbol", "?")
            return (
                f"Closed {symbol} — P&L: ${pnl:+,.2f} ({result.get('realized_pnl_pct', 0):+.1f}%)",
                True, [],
            )
        except Exception as e:
            return f"Error: {e}", False, no_update

    # ------------------------------------------------------------------
    # 8. PERFORMANCE — analytics regime breakdown
    # ------------------------------------------------------------------
    @app.callback(
        Output("perf-regime-breakdown", "data"),
        Output("perf-overall-wr", "children"),
        Output("perf-regime-wr", "children"),
        Output("perf-profit-factor", "children"),
        Input("performance-subtabs", "active_tab"),
        Input("sniper-refresh-interval", "n_intervals"),
        prevent_initial_call=False,
    )
    def update_analytics(active_tab, _n):
        if active_tab != "perf-analytics-tab":
            raise PreventUpdate

        try:
            all_trades = db_manager.get_recent_paper_trades(limit=1000)
        except Exception as e:
            logger.warning("Analytics error: %s", e)
            return [], "0%", "0%", "0.0"

        closed = [t for t in all_trades if t.get("status") == "CLOSED"]
        if not closed:
            return [], "0%", "0%", "0.0"

        # Overall WR
        wins = [t for t in closed if (t.get("realized_pnl") or 0) > 0]
        overall_wr = f"{len(wins) / len(closed) * 100:.1f}%"

        # Profit factor
        gross_profit = sum(t.get("realized_pnl", 0) for t in closed
                          if (t.get("realized_pnl") or 0) > 0)
        gross_loss = abs(sum(t.get("realized_pnl", 0) for t in closed
                            if (t.get("realized_pnl") or 0) < 0))
        pf = f"{gross_profit / gross_loss:.2f}" if gross_loss else "INF"

        # Regime-stratified breakdown
        regime_buckets = {}
        for t in closed:
            regime = t.get("entry_market_regime", "UNKNOWN") or "UNKNOWN"
            if regime not in regime_buckets:
                regime_buckets[regime] = []
            regime_buckets[regime].append(t)

        breakdown = []
        with_regime_wins = 0
        with_regime_total = 0
        for regime, trades in sorted(regime_buckets.items()):
            n = len(trades)
            w = sum(1 for t in trades if (t.get("realized_pnl") or 0) > 0)
            wr = f"{w / n * 100:.1f}%" if n else "0%"
            avg_pnl = sum(t.get("realized_pnl", 0) or 0 for t in trades) / n if n else 0
            gp = sum(t.get("realized_pnl", 0) for t in trades if (t.get("realized_pnl") or 0) > 0)
            gl = abs(sum(t.get("realized_pnl", 0) for t in trades if (t.get("realized_pnl") or 0) < 0))
            rpf = f"{gp / gl:.2f}" if gl else "INF"
            pnls = [t.get("realized_pnl", 0) or 0 for t in trades]
            best = f"${max(pnls):,.2f}" if pnls else "$0"
            worst = f"${min(pnls):,.2f}" if pnls else "$0"

            # Track "with regime" = traded in allowed states (2,3,4)
            if regime in ("ACCUMULATION", "MEAN_REVERSION", "BULL_TREND"):
                with_regime_wins += w
                with_regime_total += n

            breakdown.append({
                "regime": regime,
                "n_trades": n,
                "win_rate": wr,
                "avg_pnl": round(avg_pnl, 2),
                "profit_factor": rpf,
                "best_trade": best,
                "worst_trade": worst,
            })

        regime_wr = (f"{with_regime_wins / with_regime_total * 100:.1f}%"
                     if with_regime_total else "N/A")

        return breakdown, overall_wr, regime_wr, pf


def _build_why_tags(idea: dict, director_set: set) -> str:
    """Compose a compact 'why is this on the board?' tag string per row.

    DIR    : ticker is inside a recent Director-led insider cluster (validated edge)
    TRIPLE : Triple Lock (highest-conviction setup)
    SQ     : squeeze candidate (high squeeze_score)
    ACCUM  : institutional accumulation phase (EARLY/ACTIVE/LATE_ACCUM)
    13F    : institutional thesis row (all sniper-board rows come from 13F intel)
    """
    tags = []
    tkr = idea.get("symbol", "")
    if tkr and tkr in director_set:
        tags.append("DIR")
    src = str(idea.get("source_badge", "")).upper()
    if src == "TRIPLE_LOCK":
        tags.append("TRIPLE")
    if src == "SQUEEZE":
        tags.append("SQ")
    phase = str(idea.get("_phase") or "").upper()
    if "ACCUM" in phase:
        tags.append("ACCUM")
    # Every sniper-board row is built off 13F institutional intelligence
    tags.append("13F")
    return " · ".join(tags)


def _load_sniper_ideas(conn, store=None) -> list[dict]:
    """Load EV-ranked trade ideas from intelligence_scores.

    LONG ideas: swing_signal=BUY + conviction >= 65 (institutional accumulation)
    SHORT ideas: short_swing_signal=SHORT + short_conviction_score >= 45 (institutional distribution)
    Both pools merged and EV-ranked together.

    `store` is a LiveBarStore; when a symbol is being streamed its live price
    anchors the entry/stop/target so the levels are tradeable in real time.
    """
    try:
        from signal_scanner.institutional_intel.config import get_active_quarter
        quarter = get_active_quarter(conn)
    except Exception:
        quarter = "2025-Q4"

    # --- LONG ideas (accumulation-based) ---
    try:
        long_rows = conn.execute("""
            SELECT
                ticker, swing_signal, conviction_score, ml_score_v2,
                accum_phase, triple_lock, squeeze_score, short_squeeze_score,
                institutional_pressure, insider_effect_score, trend_score,
                price_momentum_90d, price_above_200sma, data_quality_score,
                NULL as short_conviction_score,
                COALESCE(dark_pool_pct_avg, 0) as dark_pool_pct_avg,
                COALESCE(swing_flow_score, 0) as swing_flow_score
            FROM intelligence_scores
            WHERE report_quarter = ?
              AND swing_signal = 'BUY'
              AND conviction_score >= 65
              AND data_quality_score >= 50
            ORDER BY conviction_score DESC
            LIMIT 30
        """, [quarter]).fetchall()
    except Exception as e:
        logger.warning("Sniper LONG ideas query error: %s", e)
        long_rows = []

    # --- SHORT ideas (distribution-based) ---
    try:
        short_rows = conn.execute("""
            SELECT
                ticker, 'SHORT' as swing_signal,
                COALESCE(conviction_score, 0) as conviction_score,
                COALESCE(ml_score_v2, 0) as ml_score_v2,
                accum_phase, triple_lock, squeeze_score, short_squeeze_score,
                institutional_pressure, insider_effect_score, trend_score,
                price_momentum_90d, price_above_200sma, data_quality_score,
                short_conviction_score,
                COALESCE(dark_pool_pct_avg, 0) as dark_pool_pct_avg,
                COALESCE(swing_flow_score, 0) as swing_flow_score
            FROM intelligence_scores
            WHERE report_quarter = ?
              AND short_swing_signal = 'SHORT'
              AND data_quality_score >= 50
            ORDER BY short_conviction_score DESC
            LIMIT 25
        """, [quarter]).fetchall()
    except Exception as e:
        logger.warning("Sniper SHORT ideas query error: %s", e)
        short_rows = []

    all_rows = long_rows + short_rows
    if not all_rows:
        return []

    ideas = []
    for row in all_rows:
        (ticker, signal, conv, ml, phase, triple, squeeze, short_sq,
         pressure, insider, trend, momentum, above_200, quality,
         short_conv, dp_pct, flow_score) = row

        side = "LONG" if signal == "BUY" else "SHORT"

        # Use the right conviction score for ranking
        effective_conv = float(conv or 0) if side == "LONG" else float(short_conv or conv or 0)

        # Compute source badge
        if triple:
            source = "TRIPLE_LOCK"
        elif (squeeze or 0) >= 70 or (short_sq or 0) >= 70:
            source = "SQUEEZE"
        elif side == "SHORT":
            source = "DISTRIBUTION"
        else:
            source = "SWING"

        # Signal strength: independent signals agreeing (different criteria per side)
        strength_count = 0
        if side == "LONG":
            if (conv or 0) >= 70:       strength_count += 1
            if (ml or 0) >= 60:         strength_count += 1
            if (pressure or 0) >= 40:   strength_count += 1
            if (insider or 0) >= 3:     strength_count += 1
            if above_200:               strength_count += 1
        else:
            if (short_conv or 0) >= 55: strength_count += 1
            if not above_200:           strength_count += 1  # below 200 SMA = confirms short
            if (momentum or 0) < -5:    strength_count += 1  # negative momentum
            if (ml or 0) >= 60:         strength_count += 1  # ML confirms
            if (short_sq or 0) <= 30:   strength_count += 1  # low squeeze risk

        filled = min(strength_count, 5)
        empty = 5 - filled
        strength_visual = ("*" * filled) + ("." * empty)

        # Realtime: anchor levels to the live price when the symbol is streamed
        live_px = None
        if store is not None:
            try:
                live_px = store.get_latest_price(ticker)
            except Exception:
                live_px = None
        entry, stop, t1, t2, rr = _estimate_levels(conn, ticker, side, entry_override=live_px)
        price_source = "LIVE" if (live_px and live_px > 0) else "EOD"

        # EV score uses effective conviction for both sides
        ev = round(effective_conv * (rr or 1) * filled / 100, 1)

        ideas.append({
            "rank": 0,
            "symbol": ticker,
            "current_price": entry,
            "price_source": price_source,
            "side": side,
            "signal_strength": strength_visual,
            "regime_badge": "",
            "entry_price": entry,
            "stop_price": stop,
            "target_1": t1,
            "target_2": t2,
            "rr_ratio": rr,
            "source_badge": source,
            "ev_score": ev,
            # Hidden detail fields
            "_conviction": round(effective_conv, 1),
            "_ml": ml,
            "_phase": phase,
            "_pressure": pressure,
            "_squeeze": squeeze,
            "_insider": insider,
            "_short_conv": short_conv,
            "_dark_pool_pct": round(float(dp_pct or 0), 1),
            "_swing_flow": round(float(flow_score or 0), 1),
            "flow": round(float(flow_score or 0), 0) if flow_score else "",
        })

    # --- Inject convergence signals from AI signals engine ---
    # These are confirmation signals (not predictive). They show up as source
    # badges on the unified Sniper Board alongside SWING/TRIPLE_LOCK/SQUEEZE.
    _CONVERGENCE_TYPES = [
        "PULLBACK_SNIPER",
        "SMART_MONEY_CONVERGENCE",
        "SWING_CONFLUENCE",
        "ACCUMULATION_BREAKOUT",
    ]
    _CONVERGENCE_BADGE_MAP = {
        "PULLBACK_SNIPER": "PULLBACK",
        "SMART_MONEY_CONVERGENCE": "CONVERGENCE",
        "SWING_CONFLUENCE": "CONFLUENCE",
        "ACCUMULATION_BREAKOUT": "BREAKOUT",
    }
    try:
        if not INJECT_CONVERGENCE_SIGNALS:
            raise RuntimeError("INJECT_CONVERGENCE_SIGNALS=False (Drishti v1)")
        from signal_scanner.institutional_intel.reports.ai_signals import AISignalEngine
        engine = AISignalEngine()
        conv_signals = engine.detect_signals(
            quarter=quarter,
            signal_types=_CONVERGENCE_TYPES,
        )
        existing_symbols = {i["symbol"] for i in ideas}
        for sig in conv_signals:
            m = sig.get("metrics", {})
            ticker = sig.get("ticker", "")
            sig_type = sig.get("signal_type", "")

            # Convergence only enriches existing swing ideas — no standalone injection.
            # Swing Snipers is a thesis-driven board, not a convergence surface.
            if ticker not in existing_symbols:
                continue

            for idea in ideas:
                if idea["symbol"] == ticker:
                    badge = _CONVERGENCE_BADGE_MAP.get(sig_type, "CONVERGENCE")
                    existing_conv = idea.get("_convergence_tags", [])
                    if badge not in existing_conv:
                        existing_conv.append(badge)
                    idea["_convergence_tags"] = existing_conv
                    idea["ev_score"] = round(idea["ev_score"] * 1.1, 1)
                    break
    except Exception as e:
        logger.debug("Convergence signal inject failed: %s", e)

    # Enrich with idea lifecycle state from idea_ledger
    try:
        from signal_scanner.paper.idea_ledger import IdeaLedger
        from signal_scanner.config import ScannerConfig
        cfg = ScannerConfig()
        ledger = IdeaLedger(cfg.db_path)
        alive_ideas = ledger.get_alive_ideas()
        # Build lookup: (symbol, side) → idea state info
        idea_lookup = {}
        for ai in alive_ideas:
            key = (ai["symbol"], ai["side"])
            if key not in idea_lookup:
                idea_lookup[key] = ai

        for idea in ideas:
            key = (idea["symbol"], idea["side"])
            if key in idea_lookup:
                ai = idea_lookup[key]
                idea["_idea_id"] = ai["id"]
                idea["_idea_state"] = ai["state"]
                idea["_idea_confirms"] = ai["confirm_count"]
                idea["_idea_first_seen"] = ai["first_seen_at"]
            else:
                idea["_idea_id"] = None
                idea["_idea_state"] = None
                idea["_idea_confirms"] = 0
    except Exception as e:
        logger.debug("Sniper idea lifecycle enrichment failed: %s", e)

    # Flatten convergence tags into display column
    for idea in ideas:
        tags = idea.get("_convergence_tags", [])
        idea["convergence"] = " + ".join(tags) if tags else ""

    # Sort by EV descending
    ideas.sort(key=lambda x: x.get("ev_score", 0), reverse=True)
    return ideas


def _estimate_levels(conn, ticker: str, side: str, entry_override: float | None = None):
    """Estimate entry, stop, T1, T2 from daily ATR.

    `entry_override` anchors the levels to a LIVE price (so stops/targets are
    relative to where the stock trades NOW, not yesterday's close). ATR is
    still measured from daily bars — a stable volatility unit.
    """
    try:
        row = conn.execute("""
            SELECT close, high, low
            FROM fact_daily_prices
            WHERE ticker = ?
            ORDER BY trade_date DESC
            LIMIT 20
        """, [ticker]).fetchall()
    except Exception:
        return None, None, None, None, None

    if not row or len(row) < 5:
        return None, None, None, None, None

    latest_close = row[0][0]
    # Anchor to the live price when provided, else fall back to last close.
    base = entry_override if (entry_override and entry_override > 0) else latest_close
    # ATR approximation from last 20 daily bars
    ranges = [r[1] - r[2] for r in row if r[1] and r[2]]
    atr = sum(ranges) / len(ranges) if ranges else base * 0.02

    if side == "LONG":
        entry = round(base, 2)
        stop = round(base - 1.5 * atr, 2)
        risk = entry - stop
        t1 = round(entry + 2.5 * risk, 2)
        t2 = round(entry + 4.0 * risk, 2)
    else:
        entry = round(base, 2)
        stop = round(base + 1.5 * atr, 2)
        risk = stop - entry
        t1 = round(entry - 2.5 * risk, 2)
        t2 = round(entry - 4.0 * risk, 2)

    rr = round(abs(t1 - entry) / risk, 1) if risk > 0 else 0
    return entry, stop, t1, t2, rr
