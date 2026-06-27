"""Dash callbacks for Stock Ideas + Options Ideas sections.

Replaces old Kubera Reports callbacks with timeframe-organized data loading:
  Stock Ideas:  Intraday Setups | Swing Ideas | Longterm | Short Squeeze | Custom Screen
  Options Ideas: Weekly Plays | Swing Contracts | LEAPS
  Live Signals:  AI Signals sub-tab (Scanner + EOD handled by main callbacks)
"""

import json
from datetime import datetime

import dash_bootstrap_components as dbc
from dash import Input, Output, State, html, no_update
from dash.exceptions import PreventUpdate
from loguru import logger

from signal_scanner.config import DashboardConfig

_cfg = DashboardConfig()


def _format_signal_date(iso_str: str) -> str:
    """Format ISO datetime to a compact display string like 'Feb 26, 2026 3:15 PM'."""
    if not iso_str:
        return ""
    try:
        dt = datetime.fromisoformat(iso_str)
        return dt.strftime("%b %d, %Y %I:%M %p")
    except (ValueError, TypeError):
        return str(iso_str)[:16]


def register_reports_callbacks(app, db_manager, scanner=None, live_scanners=None) -> None:
    """Register all Stock Ideas + Options Ideas + Live Scanner callbacks."""

    # Lazy-init reports engine
    _reports = {}
    _contracts = {}
    _scanner_ref = {"scanner": scanner}  # Mutable ref so it can be updated
    _live_scanners = live_scanners or {}  # {"vwap_mr": VWAPMRLiveScanner, "fpb": FPBLiveScanner}

    def _get_reports():
        if _reports.get("engine") is None:
            try:
                from signal_scanner.institutional_intel.reports.kubera_reports import (
                    KuberaReports,
                )
                _reports["engine"] = KuberaReports()
            except Exception as e:
                logger.warning(f"Kubera reports engine not available: {e}")
                _reports["engine"] = None
        return _reports["engine"]

    def _get_contracts():
        if _contracts.get("engine") is None:
            try:
                from signal_scanner.institutional_intel.reports.contract_ideas import (
                    KuberaContractIdeas,
                )
                _contracts["engine"] = KuberaContractIdeas()
            except Exception as e:
                logger.warning(f"Contract ideas engine not available: {e}")
                _contracts["engine"] = None
        return _contracts["engine"]

    # ------------------------------------------------------------------
    # 1. Populate quarter dropdown + stat cards
    # ------------------------------------------------------------------
    @app.callback(
        Output("si-quarter-dropdown", "options"),
        Output("si-quarter-dropdown", "value"),
        Output("si-stat-intraday", "children"),
        Output("si-stat-swing", "children"),
        Output("si-stat-platinum", "children"),
        Output("si-stat-ultimate", "children"),
        Output("si-stat-gold", "children"),
        Output("si-stat-squeeze", "children"),
        Input("refresh-interval", "n_intervals"),
        State("si-quarter-dropdown", "value"),
    )
    def populate_stock_ideas_metadata(n, current_quarter):
        engine = _get_reports()
        if engine is None:
            return [], None, "0", "0", "0", "0", "0", "0"

        try:
            options = engine.get_available_quarter_options()
            quarters = [o["value"] for o in options]
            quarter = current_quarter if current_quarter in quarters else None

            # On first load, default to the canonical active quarter (most recent clean).
            # Do NOT skip sparse/early quarters — they are the most current data.
            if not quarter:
                from signal_scanner.institutional_intel.config import (
                    get_active_quarter, safe_duckdb_connect,
                )
                conn = safe_duckdb_connect(read_only=True)
                if conn:
                    try:
                        quarter = get_active_quarter(conn)
                    finally:
                        conn.close()
                # If active quarter not in available list, fall back to first available
                if not quarter or quarter not in quarters:
                    quarter = quarters[0] if quarters else None

            if not quarter:
                return options, None, "0", "0", "0", "0", "0", "0"

            stats = engine.get_stock_ideas_summary(quarter)

            # Intraday count comes from scanner results (dynamic)
            intraday_count = "Live"

            return (
                options,
                quarter,
                intraday_count,
                str(stats.get("swing_count", 0)),
                str(stats.get("platinum_count", 0)),
                str(stats.get("ultimate_count", 0)),
                str(stats.get("gold_count", 0)),
                str(stats.get("squeeze_count", 0)),
            )
        except Exception as e:
            logger.error(f"Stock ideas metadata error: {e}")
            return [], None, "0", "0", "0", "0", "0", "0"

    # ------------------------------------------------------------------
    # 2. Stock Ideas — Tab switching
    # ------------------------------------------------------------------
    @app.callback(
        Output("si-intraday-container", "hidden"),
        Output("si-swing-container", "hidden"),
        Output("si-longterm-container", "hidden"),
        Output("si-squeeze-container", "hidden"),
        Output("si-custom-container", "hidden"),
        Input("si-tabs", "value"),
    )
    def toggle_stock_ideas_tabs(tab):
        mapping = {
            "tab-intraday": 0,
            "tab-swing": 1,
            "tab-longterm": 2,
            "tab-squeeze": 3,
            "tab-custom": 4,
        }
        idx = mapping.get(tab, 0)
        return tuple(i != idx for i in range(5))

    # ------------------------------------------------------------------
    # 3. Intraday Setups — from scanner MTF results
    # ------------------------------------------------------------------
    @app.callback(
        Output("si-intraday-table", "data"),
        Input("si-tabs", "value"),
        Input("refresh-interval", "n_intervals"),
    )
    def update_intraday_setups(tab, n):
        if tab != "tab-intraday":
            raise PreventUpdate
        engine = _get_reports()
        if not engine:
            return []
        try:
            # Get scanner results
            scanner_results = []
            sc = _scanner_ref.get("scanner")
            if sc:
                if hasattr(sc, "last_mtf_results") and sc.last_mtf_results:
                    scanner_results = sc.last_mtf_results
                elif hasattr(sc, "last_results") and sc.last_results:
                    scanner_results = sc.last_results

            if not scanner_results:
                return []

            setups = engine.intraday_setups(scanner_results)

            # Overlay ML scores from live scanners
            ml_scores = {}
            for name, sc in _live_scanners.items():
                if hasattr(sc, "latest_ml_scores"):
                    for sym, prob in sc.latest_ml_scores.items():
                        # Keep highest score if both scanners have a score
                        if sym not in ml_scores or prob > ml_scores[sym][0]:
                            ml_scores[sym] = (prob, name.upper())

            # Convert trigger badges to markdown for colored display
            for row in setups:
                badges = row.get("trigger_badges", "")
                if badges:
                    parts = badges.split(", ")
                    md_parts = [f"**{b}**" for b in parts]
                    row["trigger_badges"] = " ".join(md_parts)
                row["action"] = "**ENTER**"

                # Add ML score columns
                sym = row.get("symbol", "")
                if sym in ml_scores:
                    prob, strategy = ml_scores[sym]
                    pct = round(prob * 100, 1)
                    row["ml_score"] = pct
                    row["ml_strategy"] = strategy
                    if pct >= 74.6:
                        row["ml_grade"] = "A+"
                    elif pct >= 71.1:
                        row["ml_grade"] = "A"
                    elif pct >= 68.5:
                        row["ml_grade"] = "B"
                    else:
                        row["ml_grade"] = "C"
                else:
                    row["ml_score"] = None
                    row["ml_grade"] = ""
                    row["ml_strategy"] = ""

            return _round_numeric(setups)
        except Exception as e:
            logger.error(f"Intraday setups error: {e}")
            return []

    # ------------------------------------------------------------------
    # 4. Swing Ideas
    # ------------------------------------------------------------------
    @app.callback(
        Output("si-swing-table", "data"),
        Input("si-tabs", "value"),
        Input("si-quarter-dropdown", "value"),
    )
    def update_swing_ideas(tab, quarter):
        if tab != "tab-swing" or not quarter:
            raise PreventUpdate
        engine = _get_reports()
        if not engine:
            return []
        try:
            return _round_numeric(engine.swing_ideas(quarter=quarter))
        except Exception as e:
            logger.error(f"Swing ideas error: {e}")
            return []

    # ------------------------------------------------------------------
    # 5. Longterm (Tiered Report)
    # ------------------------------------------------------------------
    @app.callback(
        Output("si-longterm-table", "data"),
        Input("si-tabs", "value"),
        Input("si-quarter-dropdown", "value"),
        Input("si-longterm-tier", "value"),
    )
    def update_longterm(tab, quarter, tier):
        if tab != "tab-longterm" or not quarter:
            raise PreventUpdate
        engine = _get_reports()
        if not engine:
            return []
        try:
            if tier == "platinum":
                return _round_numeric(engine.platinum_report_v2(quarter=quarter))
            elif tier == "ultimate":
                return _round_numeric(engine.ultimate_report_v2(quarter=quarter))
            elif tier == "gold":
                return _round_numeric(engine.gold_report(quarter=quarter))
            else:
                # All tiers (6+)
                return _round_numeric(engine.tiered_report(quarter=quarter, min_confirms=6, max_confirms=10))
        except Exception as e:
            logger.error(f"Longterm report error: {e}")
            return []

    # ------------------------------------------------------------------
    # 6. Short Squeeze
    # ------------------------------------------------------------------
    @app.callback(
        Output("si-squeeze-table", "data"),
        Input("si-tabs", "value"),
        Input("si-quarter-dropdown", "value"),
    )
    def update_squeeze(tab, quarter):
        if tab != "tab-squeeze" or not quarter:
            raise PreventUpdate
        engine = _get_reports()
        if not engine:
            return []
        try:
            return _round_numeric(engine.short_squeeze_report(quarter=quarter))
        except Exception as e:
            logger.error(f"Short squeeze error: {e}")
            return []

    # ------------------------------------------------------------------
    # 7. Custom Screen
    # ------------------------------------------------------------------
    @app.callback(
        Output("si-custom-table", "data"),
        Input("screen-run-btn", "n_clicks"),
        State("si-quarter-dropdown", "value"),
        State("screen-min-price", "value"),
        State("screen-max-price", "value"),
        State("screen-min-inst", "value"),
        State("screen-min-shares-pct", "value"),
        State("screen-min-count-pct", "value"),
        State("screen-min-streak", "value"),
        prevent_initial_call=True,
    )
    def run_custom_screen(n_clicks, quarter, min_price, max_price, min_inst, min_shares_pct, min_count_pct, min_streak):
        if not n_clicks or not quarter:
            raise PreventUpdate
        engine = _get_reports()
        if not engine:
            return []
        try:
            return _round_numeric(
                engine.custom_screen(
                    quarter=quarter,
                    min_inst_count=int(min_inst) if min_inst else None,
                    min_shares_change_pct=float(min_shares_pct) if min_shares_pct else None,
                    min_count_change_pct=float(min_count_pct) if min_count_pct else None,
                    min_streak=int(min_streak) if min_streak else None,
                    min_price=float(min_price) if min_price else None,
                    max_price=float(max_price) if max_price else None,
                )
            )
        except Exception as e:
            logger.error(f"Custom screen error: {e}")
            return []

    # ==================================================================
    # OPTIONS IDEAS CALLBACKS
    # ==================================================================

    # ------------------------------------------------------------------
    # 8. Options Ideas — Tab switching
    # ------------------------------------------------------------------
    @app.callback(
        Output("oi-weekly-container", "hidden"),
        Output("oi-swing-container", "hidden"),
        Output("oi-leaps-container", "hidden"),
        Input("oi-tabs", "value"),
    )
    def toggle_options_ideas_tabs(tab):
        mapping = {
            "tab-weekly": 0,
            "tab-swing-contracts": 1,
            "tab-leaps": 2,
        }
        idx = mapping.get(tab, 0)
        return tuple(i != idx for i in range(3))

    # ------------------------------------------------------------------
    # 9. Weekly Options (0-7 DTE)
    # ------------------------------------------------------------------
    @app.callback(
        Output("oi-weekly-table", "data"),
        Input("oi-tabs", "value"),
        Input("refresh-interval", "n_intervals"),
    )
    def update_weekly_options(tab, n):
        if tab != "tab-weekly":
            raise PreventUpdate
        contract_engine = _get_contracts()
        if not contract_engine:
            return []
        try:
            scanner_results = []
            sc = _scanner_ref.get("scanner")
            if sc:
                if hasattr(sc, "last_mtf_results") and sc.last_mtf_results:
                    scanner_results = sc.last_mtf_results
                elif hasattr(sc, "last_results") and sc.last_results:
                    scanner_results = sc.last_results

            if not scanner_results:
                return []

            ideas = contract_engine.weekly_options_ideas(scanner_results)

            # Convert flags to markdown
            for row in ideas:
                flags = row.get("flags", "")
                if flags:
                    parts = flags.split(", ")
                    md_parts = [f"**{f}**" for f in parts]
                    row["flags"] = " ".join(md_parts)

            return _round_numeric(ideas)
        except Exception as e:
            logger.error(f"Weekly options error: {e}")
            return []

    # ------------------------------------------------------------------
    # 10. Swing Contracts (14-45 DTE) — existing contract ideas + pressure
    # ------------------------------------------------------------------
    @app.callback(
        Output("oi-swing-table", "data"),
        Input("oi-tabs", "value"),
        Input("si-quarter-dropdown", "value"),
    )
    def update_swing_contracts(tab, quarter):
        if tab != "tab-swing-contracts" or not quarter:
            raise PreventUpdate
        engine = _get_reports()
        if not engine:
            return []
        try:
            ideas = engine.contract_ideas(quarter=quarter)
            # Overlay ML scores from live scanners for quality ranking
            ml_scores = {}
            for name, sc in _live_scanners.items():
                if hasattr(sc, "latest_ml_scores"):
                    for sym, prob in sc.latest_ml_scores.items():
                        if sym not in ml_scores or prob > ml_scores[sym]:
                            ml_scores[sym] = prob
            for idea in ideas:
                ticker = idea.get("ticker", "")
                if ticker in ml_scores:
                    idea["ml_overlay_score"] = round(ml_scores[ticker] * 100, 1)
                else:
                    idea["ml_overlay_score"] = None
            return _round_numeric(ideas)
        except Exception as e:
            logger.error(f"Swing contracts error: {e}")
            return []

    # ------------------------------------------------------------------
    # 11. LEAPS (6-18m)
    # ------------------------------------------------------------------
    @app.callback(
        Output("oi-leaps-table", "data"),
        Input("oi-tabs", "value"),
        Input("si-quarter-dropdown", "value"),
    )
    def update_leaps(tab, quarter):
        if tab != "tab-leaps" or not quarter:
            raise PreventUpdate
        contract_engine = _get_contracts()
        if not contract_engine:
            return []
        try:
            return _round_numeric(contract_engine.leaps_ideas(quarter=quarter))
        except Exception as e:
            logger.error(f"LEAPS ideas error: {e}")
            return []

    # ==================================================================
    # LIVE SIGNALS — AI Signals sub-tab
    # ==================================================================

    # ------------------------------------------------------------------
    # 12. Live Signals — sub-tab switching
    # ------------------------------------------------------------------
    @app.callback(
        Output("ls-scanner-container", "hidden"),
        Output("ls-ai-container", "hidden"),
        Output("ls-intraday-ml-container", "hidden"),
        Output("ls-intraday-sniper-container", "hidden"),
        Output("ls-options-flow-container", "hidden"),
        Output("ls-eod-container", "hidden"),
        Input("ls-tabs", "value"),
    )
    def toggle_live_signals_tabs(tab):
        mapping = {
            "tab-scanner": 0,
            "tab-ai-signals": 1,
            "tab-intraday-ml": 2,
            "tab-intraday-sniper": 3,
            "tab-options-flow": 4,
            "tab-eod-review": 5,
        }
        idx = mapping.get(tab, 0)
        return tuple(i != idx for i in range(6))

    # ------------------------------------------------------------------
    # 12a. Intraday Ideas table
    # ------------------------------------------------------------------
    @app.callback(
        Output("intraday-ideas-table", "data"),
        Input("ls-tabs", "value"),
        Input("refresh-interval", "n_intervals"),
    )
    def update_intraday_ideas(tab, _n):
        if tab != "tab-intraday-ml":
            raise PreventUpdate
        try:
            from signal_scanner.dashboard.intraday_ideas_data import get_intraday_ideas
            from signal_scanner.core.live_bar_store import LiveBarStore
            store = LiveBarStore()
            ideas = get_intraday_ideas(db_manager, store)
            # Rule-match highlight — flag ideas that pass the page's trade rules
            try:
                from signal_scanner.dashboard.trade_rules import rule_match_mark
                from signal_scanner.institutional_intel.intelligence.regime_hmm import DailyRegimeHMM
                regime_state = None
                hmm = DailyRegimeHMM()
                hmm.load()
                if hmm._model is not None:
                    regime_state, _p, _name = hmm.current_regime()
                for idea in ideas:
                    idea["rule_match"] = rule_match_mark("intraday", idea, regime_state)
            except Exception:
                for idea in ideas:
                    idea.setdefault("rule_match", "")
            return ideas
        except Exception as e:
            logger.debug("Intraday ideas error: %s", e)
            return []

    # ------------------------------------------------------------------
    # 12b. Options Flow data
    # ------------------------------------------------------------------
    @app.callback(
        Output("options-flow-table", "data"),
        Input("ls-tabs", "value"),
        Input("refresh-interval", "n_intervals"),
    )
    def update_options_flow(tab, _n):
        if tab != "tab-options-flow":
            raise PreventUpdate
        try:
            from signal_scanner.institutional_intel.config import safe_duckdb_connect
            from signal_scanner.institutional_intel.intelligence.options_intelligence import OptionsIntelligence
            _oconn = safe_duckdb_connect(read_only=True)
            if not _oconn:
                return []
            try:
                oeng = OptionsIntelligence(_oconn)

                # Get all underlyings with data
                underlyings = [r[0] for r in _oconn.execute("""
                    SELECT DISTINCT underlying FROM fact_options_contracts
                    WHERE snapshot_date = (SELECT MAX(snapshot_date) FROM fact_options_contracts)
                    ORDER BY underlying
                """).fetchall()]

                # Build board: best expressions per underlying + summary context
                rows = []
                for ul in underlyings:
                    summary = oeng.get_underlying_summary(ul)
                    if not summary.get("has_data"):
                        continue

                    # Direction-aware: check if this underlying is a SHORT idea
                    is_short = _oconn.execute("""
                        SELECT short_swing_signal FROM intelligence_scores
                        WHERE ticker = ? AND report_quarter = (
                            SELECT MAX(report_quarter) FROM intelligence_scores WHERE data_quality_score >= 75
                        )
                    """, [ul]).fetchone()
                    direction = "SHORT" if (is_short and is_short[0] == "SHORT") else "LONG"
                    recs = oeng.recommend_expressions(ul, direction, target_delta=0.40, max_results=3)
                    for r in recs:
                        rows.append({
                            "symbol": ul,
                            "contract_type": r["contract_type"],
                            "expiry_date": r["expiry"],
                            "strike": r["strike"],
                            "signal": "LONG",
                            "open_interest": r["open_interest"],
                            "volume": r["volume"],
                            "implied_volatility": r["iv"],
                            "delta": r["delta"],
                            "bid": r["bid"],
                            "ask": r["ask"],
                            "score": r["score"],
                            "snapshot_date": summary.get("snapshot_date", ""),
                            # Summary context (flattened into row for display)
                            "_atm_iv": summary.get("atm_iv"),
                            "_skew": summary.get("call_put_skew"),
                            "_pcr": summary.get("put_call_ratio"),
                            "_call_wall": summary.get("call_wall_strike"),
                            "_put_wall": summary.get("put_wall_strike"),
                        })

                # Sort by score descending
                rows.sort(key=lambda x: -(x.get("score") or 0))
            finally:
                _oconn.close()
            return rows
        except Exception as e:
            logger.debug("Options flow load error: %s", e)
            return []

    # ------------------------------------------------------------------
    # 13. Convergence Signals (formerly AI Smart Signals)
    # ------------------------------------------------------------------
    @app.callback(
        Output("ai-signals-cards", "children"),
        Input("ls-tabs", "value"),
        Input("ai-signals-lookback", "value"),
        Input("ai-signals-type", "value"),
        Input("refresh-interval", "n_intervals"),
    )
    def update_ai_signals(tab, lookback, signal_type, _n):
        if tab != "tab-ai-signals":
            raise PreventUpdate
        # Get active quarter directly (don't depend on hidden dropdown)
        quarter = None
        try:
            from signal_scanner.institutional_intel.config import safe_duckdb_connect, get_active_quarter
            _conn = safe_duckdb_connect(read_only=True)
            if _conn:
                try:
                    quarter = get_active_quarter(_conn)
                finally:
                    _conn.close()
        except Exception:
            pass
        if not quarter:
            quarter = "2025-Q4"
        engine = _get_reports()
        if not engine:
            return [html.P("Reports engine unavailable", style={"color": _cfg.text_muted})]
        try:
            sig_types = None if signal_type == "ALL" else [signal_type]
            signals = engine.ai_signals(
                quarter=quarter,
                lookback_days=lookback or 30,
                signal_types=sig_types,
            )
            if not signals:
                return [html.P(
                    "No signals detected for this quarter/lookback combination.",
                    style={"color": _cfg.text_muted, "textAlign": "center", "padding": "40px"},
                )]

            # Deduplicate by (signal_type, ticker) — keep best, track count
            seen = {}
            deduped = []
            for s in signals:
                key = (s.get("signal_type", ""), s.get("ticker", ""))
                if key in seen:
                    seen[key]["_repeat_count"] = seen[key].get("_repeat_count", 1) + 1
                else:
                    s["_repeat_count"] = 1
                    seen[key] = s
                    deduped.append(s)

            # Filter out LOW-strength detection-only signals (noise reduction)
            actionable = [s for s in deduped if s.get("strength") != "LOW"]
            if not actionable:
                # Fall back to showing all if everything is LOW (no data to hide)
                actionable = deduped

            return [_signal_card(s) for s in actionable]
        except Exception as e:
            logger.error(f"AI signals error: {e}")
            return [html.P(f"Error: {e}", style={"color": _cfg.accent_short})]

    # -----------------------------------------------------------------------
    # Ticker click → ISR  |  Action click → My Trades prefill
    # -----------------------------------------------------------------------
    _REPORT_TABLES = [
        ("si-intraday-table",  "symbol"),
        ("si-swing-table",     "ticker"),
        ("si-longterm-table",  "ticker"),
        ("si-squeeze-table",   "ticker"),
        ("si-custom-table",    "ticker"),
        ("oi-weekly-table",    "symbol"),
        ("oi-swing-table",     "ticker"),
        ("oi-leaps-table",     "ticker"),
    ]

    from dash import Input as DashInput, Output as DashOutput, State as DashState, no_update

    for _tbl_id, _col_id in _REPORT_TABLES:
        def _make_click_cb(tbl_id, col_id):
            @app.callback(
                DashOutput("selected-ticker", "data", allow_duplicate=True),
                DashOutput("mt-prefill-store", "data", allow_duplicate=True),
                DashInput(tbl_id, "active_cell"),
                DashState(tbl_id, "data"),
                prevent_initial_call=True,
            )
            def _report_cell_click(active_cell, table_data, _c=col_id):
                if not active_cell or not table_data:
                    return no_update, no_update
                clicked_col = active_cell.get("column_id")
                try:
                    row = table_data[active_cell["row"]]
                except (IndexError, KeyError):
                    return no_update, no_update

                # Ticker click → navigate to ISR
                if clicked_col == _c:
                    return row.get(_c, no_update), no_update

                # Action click → prefill My Trades entry form
                if clicked_col == "action":
                    symbol = row.get(_c, row.get("symbol", row.get("ticker", "")))
                    # Determine side from available signal fields
                    side = "LONG"
                    for key in ("swing_signal", "signal", "recommendation"):
                        val = str(row.get(key, "")).upper()
                        if val in ("SHORT", "SELL", "PUT"):
                            side = "SHORT"
                            break
                    prefill = {
                        "symbol": str(symbol).upper(),
                        "side": side,
                        "price": row.get("price") or row.get("current_price"),
                        "stop": row.get("stop_loss") or row.get("swing_stop"),
                        "target": row.get("target_1") or row.get("swing_target"),
                    }
                    return no_update, prefill

                return no_update, no_update
        _make_click_cb(_tbl_id, _col_id)

    # -----------------------------------------------------------------------
    # AI Signal card ticker click → ISR
    # -----------------------------------------------------------------------
    from dash import ALL, MATCH, ctx

    @app.callback(
        Output("selected-ticker", "data", allow_duplicate=True),
        Input({"type": "ai-signal-ticker", "index": ALL}, "n_clicks"),
        prevent_initial_call=True,
    )
    def ai_signal_ticker_click(n_clicks_list):
        if not n_clicks_list or not any(n_clicks_list):
            raise PreventUpdate
        triggered = ctx.triggered_id
        if triggered and isinstance(triggered, dict):
            return triggered.get("index", no_update)
        raise PreventUpdate

    # ------------------------------------------------------------------
    # 14. Intraday ML tab — live model stats + recent trades + health
    # ------------------------------------------------------------------
    @app.callback(
        Output("ml-vwap-live-stats", "children"),
        Output("ml-fpb-live-stats", "children"),
        Output("ml-orb-v2-live-stats", "children"),
        Output("ml-live-pnl", "children"),
        Output("ml-live-pnl-detail", "children"),
        Output("ml-recent-trades-table", "data"),
        Output("ml-health-strip", "children"),
        Input("ls-tabs", "value"),
        Input("refresh-interval", "n_intervals"),
    )
    def update_intraday_ml_tab(tab, n):
        if tab != "tab-intraday-ml":
            raise PreventUpdate

        # Gather live scanner stats
        vwap_stats = ""
        fpb_stats = ""
        orb_v2_stats = ""
        vwap_sc = _live_scanners.get("vwap_mr")
        fpb_sc = _live_scanners.get("fpb")
        orb_v2_sc = _live_scanners.get("orb_v2")

        if vwap_sc and hasattr(vwap_sc, "latest_ml_scores"):
            n_scored = len(vwap_sc.latest_ml_scores)
            vwap_stats = f"{n_scored} tickers scored"
        else:
            vwap_stats = "Scanner not active"

        if fpb_sc and hasattr(fpb_sc, "latest_ml_scores"):
            n_scored = len(fpb_sc.latest_ml_scores)
            fpb_stats = f"{n_scored} tickers scored"
        else:
            fpb_stats = "Scanner not active"

        if orb_v2_sc and hasattr(orb_v2_sc, "latest_ml_scores"):
            n_scored = len(orb_v2_sc.latest_ml_scores)
            orb_v2_stats = f"{n_scored} tickers scored"
        else:
            orb_v2_stats = "Scanner not active"

        # Get recent ML trades from DB
        recent_trades = []
        pnl_text = "—"
        pnl_detail = "No trades today"
        try:
            trades = db_manager.get_recent_paper_trades(limit=50) or []
            ml_trades = [
                t for t in trades
                if (t.get("recommendation_source") or "").startswith(
                    ("VWAP_MR_ML", "FPB_ML", "ORB_V2_ML")
                )
                and "_SNIPER_" not in (t.get("recommendation_source") or "")
            ]

            total_pnl = 0.0
            wins = 0
            total = 0
            for t in ml_trades[:20]:
                src = t.get("recommendation_source", "")
                if src.startswith("VWAP_MR"):
                    strategy = "VWAP_MR"
                elif src.startswith("ORB_V2"):
                    strategy = "ORB_V2"
                else:
                    strategy = "FPB"
                prob_str = src.split("_P")[-1] if "_P" in src else ""
                prob = float(prob_str) / 100 if prob_str else 0

                grade = ""
                if prob >= 0.746:
                    grade = "A+"
                elif prob >= 0.711:
                    grade = "A"
                elif prob >= 0.685:
                    grade = "B"
                else:
                    grade = "C"

                entry_p = t.get("entry_price", 0) or 0
                exit_p = t.get("exit_price")
                status = t.get("status", "OPEN")
                pnl_val = ""
                if exit_p and entry_p:
                    pnl_pct = ((exit_p - entry_p) / entry_p) * 100
                    pnl_val = f"{pnl_pct:+.1f}%"
                    total_pnl += pnl_pct
                    total += 1
                    if pnl_pct > 0:
                        wins += 1
                elif status == "OPEN":
                    total += 1

                recent_trades.append({
                    "entry_time": _format_signal_date(t.get("opened_at", "")),
                    "symbol": t.get("symbol", ""),
                    "strategy": strategy,
                    "ml_prob": round(prob * 100, 1) if prob else "",
                    "grade": grade,
                    "entry_price": round(entry_p, 2) if entry_p else "",
                    "stop_price": round(t.get("stop_loss", 0) or 0, 2),
                    "status": status,
                    "pnl": pnl_val,
                })

            if total > 0:
                pnl_text = f"{total_pnl:+.1f}%"
                wr = (wins / total * 100) if total else 0
                pnl_detail = f"{total} trades | {wins}W | {wr:.0f}% WR"
        except Exception as e:
            logger.warning(f"ML trades fetch error: {e}")

        # Health strip
        health_parts = []
        if vwap_sc:
            health_parts.append(f"VWAP_MR: threshold={getattr(vwap_sc, 'ML_PROB_MIN', '?')}")
        if fpb_sc:
            health_parts.append(f"FPB: threshold={getattr(fpb_sc, 'ML_PROB_MIN', '?')}")
        if orb_v2_sc:
            health_parts.append(f"ORB_V2: threshold={getattr(orb_v2_sc, 'ML_PROB_MIN', '?')}")
        health_parts.append(f"Last refresh: interval #{n or 0}")
        health = " | ".join(health_parts)

        return vwap_stats, fpb_stats, orb_v2_stats, pnl_text, pnl_detail, recent_trades, health

    # ------------------------------------------------------------------
    # 15. Intraday Sniper tab - elite intraday buckets tracked separately
    # ------------------------------------------------------------------
    @app.callback(
        Output("sniper-vwap-stats", "children"),
        Output("sniper-vwap-detail", "children"),
        Output("sniper-fpb-stats", "children"),
        Output("sniper-fpb-detail", "children"),
        Output("sniper-live-pnl", "children"),
        Output("sniper-live-pnl-detail", "children"),
        Output("sniper-recent-trades-table", "data"),
        Output("sniper-health-strip", "children"),
        Input("ls-tabs", "value"),
        Input("refresh-interval", "n_intervals"),
    )
    def update_intraday_sniper_tab(tab, n):
        if tab != "tab-intraday-sniper":
            raise PreventUpdate

        recent_trades = []
        vwap_stats = "No trades"
        vwap_detail = "VWAP_MR sniper bucket tracked separately"
        fpb_stats = "No trades"
        fpb_detail = "FPB sniper bucket tracked separately"
        pnl_text = "—"
        pnl_detail = "No sniper trades today"

        try:
            trades = db_manager.get_recent_paper_trades(limit=100) or []
            sniper_trades = [
                t for t in trades
                if "_SNIPER_" in str(t.get("recommendation_source") or "")
            ]

            vwap_rows = []
            fpb_rows = []
            total_pnl = 0.0
            wins = 0
            closed = 0

            for t in sniper_trades[:25]:
                src = str(t.get("recommendation_source") or "")
                if src.startswith("VWAP_MR"):
                    strategy = "VWAP_MR"
                    vwap_rows.append(t)
                elif src.startswith("FPB"):
                    strategy = "FPB"
                    fpb_rows.append(t)
                else:
                    continue

                bucket = src.split("_SNIPER_")[-1] if "_SNIPER_" in src else "SNIPER"
                entry_p = t.get("entry_price", 0) or 0
                exit_p = t.get("exit_price")
                status = t.get("status", "OPEN")
                pnl_val = ""
                if exit_p and entry_p:
                    pnl_pct = ((exit_p - entry_p) / entry_p) * 100
                    pnl_val = f"{pnl_pct:+.1f}%"
                    total_pnl += pnl_pct
                    closed += 1
                    if pnl_pct > 0:
                        wins += 1

                recent_trades.append({
                    "entry_time": _format_signal_date(t.get("opened_at", "")),
                    "symbol": t.get("symbol", ""),
                    "strategy": strategy,
                    "bucket": bucket,
                    "entry_price": round(entry_p, 2) if entry_p else "",
                    "stop_price": round(t.get("stop_loss", 0) or 0, 2),
                    "status": status,
                    "pnl": pnl_val,
                })

            if vwap_rows:
                open_count = sum(1 for t in vwap_rows if t.get("status") == "OPEN")
                closed_count = sum(1 for t in vwap_rows if t.get("status") == "CLOSED")
                vwap_stats = f"{len(vwap_rows)} trades"
                vwap_detail = f"{open_count} open | {closed_count} closed"

            if fpb_rows:
                open_count = sum(1 for t in fpb_rows if t.get("status") == "OPEN")
                closed_count = sum(1 for t in fpb_rows if t.get("status") == "CLOSED")
                fpb_stats = f"{len(fpb_rows)} trades"
                fpb_detail = f"{open_count} open | {closed_count} closed"

            if closed > 0:
                pnl_text = f"{total_pnl:+.1f}%"
                wr = wins / closed * 100
                pnl_detail = f"{closed} closed | {wins}W | {wr:.0f}% WR"
            elif sniper_trades:
                open_count = sum(1 for t in sniper_trades if t.get("status") == "OPEN")
                pnl_detail = f"{open_count} open sniper trades | awaiting exits"
        except Exception as e:
            logger.warning(f"Sniper trades fetch error: {e}")

        health = (
            "Separate elite-bucket tracking for VWAP_MR/FPB sniper entries"
            f" | Last refresh: interval #{n or 0}"
        )

        return (
            vwap_stats,
            vwap_detail,
            fpb_stats,
            fpb_detail,
            pnl_text,
            pnl_detail,
            recent_trades,
            health,
        )


def _signal_card(signal: dict) -> dbc.Card:
    """Render a single AI signal as a styled card."""
    sig_type = signal.get("signal_type", "")
    strength = signal.get("strength", "LOW")
    metrics = signal.get("metrics", {})
    repeat_count = signal.get("_repeat_count", 1)

    alert_note = signal.get("alert_note", "")
    is_detection_only = bool(alert_note)

    if is_detection_only:
        border_color = "#555"  # Gray border for detection-only
        badge_color = "secondary"
    elif strength == "HIGH":
        border_color = _cfg.accent_long
        badge_color = "success"
    elif strength == "MEDIUM":
        border_color = _cfg.accent_primary
        badge_color = "warning"
    else:
        border_color = _cfg.text_muted
        badge_color = "secondary"

    type_icons = {
        "ACCUMULATION_BREAKOUT": "fa-chart-line",
        "INSIDER_BUYING_SURGE": "fa-user-tie",
        "SECTOR_ROTATION": "fa-exchange-alt",
        "SMART_MONEY_CONVERGENCE": "fa-brain",
        "CONTRARIAN_OPPORTUNITY": "fa-sync-alt",
        "EXIT_WARNING": "fa-exclamation-triangle",
        "HIGH_CONVICTION_PREDICTION": "fa-crosshairs",
    }
    icon = type_icons.get(sig_type, "fa-signal")
    direction = signal.get("direction", "")
    is_bearish = sig_type in ("EXIT_WARNING",) or direction == "BEARISH"

    # Override styling for prediction cards
    if sig_type == "HIGH_CONVICTION_PREDICTION":
        if direction == "BULLISH":
            border_color = _cfg.accent_long
            badge_color = "success"
        else:
            border_color = _cfg.accent_short
            badge_color = "danger"

    metric_items = []
    # Metrics to skip from the raw grid (they appear in trade intelligence instead)
    _ti_keys = {"current_price", "stop_price", "target_2r", "target_1r", "r_unit",
                "risk_reward", "hold_period", "backtest_expectancy"}
    for k, v in metrics.items():
        if k in _ti_keys:
            continue
        label = k.replace("_", " ").title()
        if isinstance(v, float):
            v = f"{v:,.2f}"
        metric_items.append(
            html.Span(
                f"{label}: {v}",
                style={
                    "fontSize": "0.72rem",
                    "padding": "2px 8px",
                    "borderRadius": "4px",
                    "backgroundColor": "rgba(255,255,255,0.05)",
                    "marginRight": "6px",
                    "marginBottom": "4px",
                    "display": "inline-block",
                },
            )
        )

    # --- Trade Intelligence block ---
    ti = signal.get("trade_intelligence", {})
    conv_cnt = signal.get("convergence_count", 0)
    conv_types = signal.get("convergence_types", [])

    _verdict_style = {
        "LONG SETUP":        (_cfg.accent_long,    "success"),
        "CONTRARIAN LONG":   (_cfg.accent_long,    "success"),
        "SHORT SETUP":       (_cfg.accent_short,   "danger"),
        "EXIT / SHORT SETUP":(_cfg.accent_short,   "danger"),
        "SECTOR WATCH":      ("#888888",           "secondary"),
    }

    ti_block = []
    if ti:
        verdict = ti.get("verdict", "")
        v_color, v_badge = _verdict_style.get(verdict, ("#888", "secondary"))
        prediction = ti.get("prediction", "")
        timeframe = ti.get("timeframe", "")
        expiry = ti.get("expiry_date", "")
        invalidation = ti.get("invalidation", "")
        qualification = ti.get("qualification", "")
        rr = ti.get("rr_ratio", "")
        entry_p = ti.get("entry")
        stop_p = ti.get("stop")
        tgt1 = ti.get("target_1")
        tgt2 = ti.get("target_2")
        risk_pct = ti.get("risk_pct")

        # Price level cells
        level_cells = []
        if entry_p is not None:
            level_cells.append(html.Div([
                html.Span("Entry", style={"fontSize": "0.62rem", "color": _cfg.text_muted, "display": "block"}),
                html.Span(f"${entry_p}", style={"fontSize": "0.8rem", "fontWeight": "700", "color": _cfg.text_color}),
            ], style={"textAlign": "center", "padding": "5px 10px", "borderRight": f"1px solid {_cfg.border_color}"}))
        if stop_p is not None:
            level_cells.append(html.Div([
                html.Span(f"Stop  ({risk_pct}%)" if risk_pct else "Stop",
                          style={"fontSize": "0.62rem", "color": _cfg.text_muted, "display": "block"}),
                html.Span(f"${stop_p}", style={"fontSize": "0.8rem", "fontWeight": "700", "color": _cfg.accent_short}),
            ], style={"textAlign": "center", "padding": "5px 10px", "borderRight": f"1px solid {_cfg.border_color}"}))
        if tgt1 is not None:
            level_cells.append(html.Div([
                html.Span("Target 1", style={"fontSize": "0.62rem", "color": _cfg.text_muted, "display": "block"}),
                html.Span(f"${tgt1}", style={"fontSize": "0.8rem", "fontWeight": "700", "color": _cfg.accent_long}),
            ], style={"textAlign": "center", "padding": "5px 10px",
                      "borderRight": f"1px solid {_cfg.border_color}" if (tgt2 and tgt2 != tgt1) else "none"}))
        if tgt2 and tgt2 != tgt1:
            level_cells.append(html.Div([
                html.Span("Target 2", style={"fontSize": "0.62rem", "color": _cfg.text_muted, "display": "block"}),
                html.Span(f"${tgt2}", style={"fontSize": "0.8rem", "fontWeight": "700", "color": _cfg.accent_long}),
            ], style={"textAlign": "center", "padding": "5px 10px"}))

        ti_block = [
            html.Hr(style={"borderColor": "rgba(255,255,255,0.08)", "margin": "8px 0 6px 0"}),
            # Row: verdict + convergence + window
            html.Div(style={"display": "flex", "alignItems": "center", "gap": "8px", "marginBottom": "6px", "flexWrap": "wrap"}, children=[
                dbc.Badge(verdict, color=v_badge, style={"fontSize": "0.7rem", "fontWeight": "700", "padding": "3px 8px"}),
                *(
                    [dbc.Badge(
                        f"CONVERGENCE x{conv_cnt}  ({' + '.join(t.replace('_', ' ') for t in conv_types[:2])})",
                        color="warning",
                        style={"fontSize": "0.65rem", "fontWeight": "700"},
                    )] if conv_cnt >= 2 else []
                ),
                html.Span(
                    f"{timeframe}  |  Expires {expiry}",
                    style={"fontSize": "0.65rem", "color": _cfg.text_muted},
                ) if timeframe else None,
                html.Span(
                    f"R:R {rr}",
                    style={"fontSize": "0.65rem", "color": _cfg.text_muted,
                           "padding": "1px 6px", "borderRadius": "3px",
                           "border": f"1px solid {_cfg.border_color}"},
                ) if rr else None,
            ]),
            # Prediction narrative
            html.Div(
                prediction,
                style={
                    "fontSize": "0.78rem",
                    "color": _cfg.text_color,
                    "backgroundColor": "rgba(255,255,255,0.04)",
                    "borderRadius": "4px",
                    "padding": "7px 10px",
                    "marginBottom": "6px",
                    "borderLeft": f"3px solid {v_color}",
                    "lineHeight": "1.5",
                },
            ) if prediction else None,
            # Price level grid
            html.Div(
                level_cells,
                style={
                    "display": "flex",
                    "backgroundColor": "rgba(255,255,255,0.03)",
                    "borderRadius": "4px",
                    "marginBottom": "6px",
                    "border": f"1px solid {_cfg.border_color}",
                    "flexWrap": "wrap",
                },
            ) if level_cells else None,
            # Invalidation
            html.Div(style={"marginBottom": "3px"}, children=[
                html.Span("Invalidated if: ", style={"fontSize": "0.68rem", "color": _cfg.accent_short, "fontWeight": "600"}),
                html.Span(invalidation, style={"fontSize": "0.68rem", "color": _cfg.text_muted}),
            ]) if invalidation else None,
            # Qualification
            html.Div(style={"marginBottom": "2px"}, children=[
                html.Span("Qualifies when: ", style={"fontSize": "0.68rem", "color": _cfg.accent_long, "fontWeight": "600"}),
                html.Span(qualification, style={"fontSize": "0.68rem", "color": _cfg.text_muted}),
            ]) if qualification else None,
        ]
        ti_block = [e for e in ti_block if e is not None]

    return dbc.Card(
        style={
            "borderLeft": f"4px solid {border_color}",
            "backgroundColor": _cfg.card_color,
            "marginBottom": "10px",
            "padding": "14px 18px",
        },
        children=[
            html.Div(
                style={"display": "flex", "justifyContent": "space-between", "alignItems": "flex-start"},
                children=[
                    html.Div([
                        html.Div(
                            style={"display": "flex", "alignItems": "center", "gap": "10px", "marginBottom": "6px"},
                            children=[
                                html.I(className=f"fas {icon}", style={"color": border_color, "fontSize": "1.1rem"}),
                                html.Span(
                                    sig_type.replace("_", " ").title(),
                                    style={"fontWeight": "600", "fontSize": "0.9rem", "color": _cfg.text_color},
                                ),
                                dbc.Badge(strength, color=badge_color, className="ms-2"),
                                *(
                                    [dbc.Badge(
                                        "DETECTION ONLY",
                                        color="dark",
                                        className="ms-1",
                                        style={"fontSize": "0.62rem", "fontWeight": "700",
                                               "border": "1px solid #666", "opacity": "0.85"},
                                    )] if is_detection_only else []
                                ),
                                *(
                                    [dbc.Badge(
                                        f"x{repeat_count} signals",
                                        color="info",
                                        className="ms-1",
                                        style={"fontSize": "0.65rem"},
                                    )] if repeat_count > 1 else []
                                ),
                                *(
                                    [dbc.Badge(
                                        f"{'UP' if direction == 'BULLISH' else 'DOWN'} NEXT WEEK",
                                        color="success" if direction == "BULLISH" else "danger",
                                        className="ms-1",
                                        style={"fontSize": "0.7rem", "fontWeight": "700"},
                                    )] if direction else []
                                ),
                                dbc.Badge(
                                    signal.get("lookback", ""),
                                    color="info",
                                    className="ms-1",
                                    style={"fontSize": "0.65rem"},
                                ),
                            ],
                        ),
                        html.Div(
                            style={"display": "flex", "alignItems": "center", "gap": "8px", "marginBottom": "4px"},
                            children=[
                                html.Span(
                                    signal.get("ticker", ""),
                                    id={"type": "ai-signal-ticker", "index": signal.get("ticker", "")},
                                    n_clicks=0,
                                    style={
                                        "fontWeight": "bold",
                                        "fontSize": "1.05rem",
                                        "color": _cfg.accent_short if is_bearish else _cfg.accent_long,
                                        "cursor": "pointer",
                                        "textDecoration": "underline",
                                        "textDecorationStyle": "dotted",
                                    },
                                ),
                                html.Span(
                                    signal.get("company", ""),
                                    style={"color": _cfg.text_muted, "fontSize": "0.82rem"},
                                ),
                                html.Span(
                                    signal.get("sector", ""),
                                    style={
                                        "color": _cfg.text_muted,
                                        "fontSize": "0.72rem",
                                        "padding": "1px 6px",
                                        "borderRadius": "3px",
                                        "border": f"1px solid {_cfg.border_color}",
                                    },
                                ),
                            ],
                        ),
                        html.P(
                            signal.get("summary", ""),
                            style={"color": _cfg.text_color, "fontSize": "0.82rem", "margin": "4px 0 8px 0"},
                        ),
                        html.Div(
                            style={"display": "flex", "alignItems": "center", "gap": "8px", "marginBottom": "6px"},
                            children=[
                                html.Div(metric_items, style={"flex": "1"}),
                                html.Span(
                                    _format_signal_date(signal.get("detected_at", "")),
                                    style={
                                        "fontSize": "0.68rem",
                                        "color": _cfg.text_muted,
                                        "whiteSpace": "nowrap",
                                    },
                                ) if signal.get("detected_at") else None,
                            ],
                        ),
                        *ti_block,
                    ]),
                ],
            ),
        ],
    )


def _round_numeric(rows: list) -> list:
    """Round float fields to 2 decimal places for display."""
    for row in rows:
        for key, val in row.items():
            if isinstance(val, float):
                row[key] = round(val, 2)
        # Inject clickable "ENTER" action column
        row.setdefault("action", "**ENTER**")
    return rows
