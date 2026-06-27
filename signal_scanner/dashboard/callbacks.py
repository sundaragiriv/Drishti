"""Dash callbacks — all dashboard interactivity.

V2: MTF aggregated data from scanner, column picker, regime banner,
timezone-fixed timestamps, new stat cards (BUY/SELL counts).
"""

import re
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional

import duckdb
from dash import Input, Output, State, callback_context, html, no_update
from dash.exceptions import PreventUpdate

from signal_scanner.config import DashboardConfig, ScannerConfig
from signal_scanner.core.watchlist_manager import WatchlistManager
from signal_scanner.dashboard.layouts.main_view import EXTRA_COLUMN_IDS
from signal_scanner.institutional_intel.config import WAREHOUSE_PATH
from signal_scanner.scanner.signal_ranker import SignalRanker

cfg = DashboardConfig()
scan_cfg = ScannerConfig()
try:
    from zoneinfo import ZoneInfo
    NY_TZ = ZoneInfo("America/New_York")
except ImportError:
    NY_TZ = timezone.utc

_SYMBOL_WATCHLIST_RE = re.compile(r"^symbol:([A-Z][A-Z0-9.]{0,9})$")


def _to_new_york_hms(value) -> str:
    """Convert ISO timestamp-like input to YYYY-MM-DD HH:MM:SS ET in America/New_York."""
    if not value:
        return ""
    try:
        dt = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return str(value)[:19]
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(NY_TZ).strftime("%Y-%m-%d %H:%M:%S ET")


def _to_new_york_datetime(value) -> Optional[datetime]:
    """Convert timestamp-like value to timezone-aware NY datetime."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(NY_TZ)


def _watchlist_mode(value: Optional[str]) -> tuple[str, str]:
    """Return (mode, resolved_value) where mode is watchlist|symbol|none."""
    if not value:
        return "none", ""
    match = _SYMBOL_WATCHLIST_RE.match(str(value))
    if match:
        return "symbol", match.group(1)
    return "watchlist", str(value)


def _load_intelligence_by_ticker() -> Dict[str, dict]:
    """Load latest-quarter conviction/phase/signals from DuckDB for all tickers.

    Returns dict keyed by uppercase ticker, values have:
        accum_phase, conviction_score, swing_signal
    """
    try:
        from signal_scanner.institutional_intel.config import safe_duckdb_connect, get_active_quarter
        conn = safe_duckdb_connect(read_only=True)
        if conn is None:
            return {}
        q = get_active_quarter(conn)
        if not q:
            conn.close()
            return {}
        rows = conn.execute(
            """
            SELECT ticker,
                   accum_phase,
                   conviction_score,
                   swing_signal
            FROM intelligence_scores
            WHERE report_quarter = ?
            """,
            [q],
        ).fetchall()
        conn.close()
        return {
            str(r[0]).upper(): {
                "accum_phase": r[1],
                "conviction_score": r[2],
                "swing_signal": r[3],
            }
            for r in rows
            if r[0]
        }
    except Exception:
        return {}


def register_callbacks(app, db_manager, scanner) -> None:
    """Register all Dash callbacks."""
    wl_mgr = WatchlistManager()
    watchlist_options_cache = {"options": None, "lists": None}

    # ------------------------------------------------------------------
    # 1. Populate watchlist dropdown on load
    # ------------------------------------------------------------------
    @app.callback(
        Output("watchlist-dropdown", "options"),
        Output("watchlist-dropdown", "value"),
        Input("refresh-interval", "n_intervals"),
        State("watchlist-dropdown", "value"),
        prevent_initial_call=False,
    )
    def populate_watchlists(n, current_value):
        # Avoid rebuilding large symbol option lists every refresh tick.
        if n and n > 0 and current_value and watchlist_options_cache.get("options"):
            cached_valid = {opt["value"] for opt in watchlist_options_cache["options"]}
            cur = str(current_value).strip()
            if cur in cached_valid or cur.lower() in cached_valid:
                return no_update, no_update

        lists = wl_mgr.get_available_watchlists()
        if watchlist_options_cache.get("lists") == lists and watchlist_options_cache.get("options"):
            options = watchlist_options_cache["options"]
        else:
            watchlist_options = [{"label": name.upper(), "value": name} for name in lists]
            # Friendly aliases so common lookup patterns/typos are selectable.
            if "russell2000" in lists:
                watchlist_options.append({"label": "RUSSEL2000 (alias)", "value": "russell2000"})

            symbol_options = [
                {"label": f"SYMBOL: {sym}", "value": f"symbol:{sym}"}
                for sym in wl_mgr.get_all_symbols(max_symbols=500)
            ]
            options = watchlist_options + symbol_options
            watchlist_options_cache["options"] = options
            watchlist_options_cache["lists"] = lists

        valid_values = {opt["value"] for opt in options}
        current_norm = str(current_value or "").strip()
        if current_norm and current_norm.lower() in valid_values:
            current_norm = current_norm.lower()
        # Preserve user selection if it's still valid
        if current_norm and current_norm in valid_values:
            return options, current_norm
        # Initial load or stale value: pick default
        default = "universe_master" if "universe_master" in lists else ("sp500" if "sp500" in lists else (lists[0] if lists else None))
        return options, default

    # ------------------------------------------------------------------
    # 2a. Tile click → update tile-filter-store
    # ------------------------------------------------------------------
    _TILE_FILTER_MAP = {
        "stats-total-tile":  {"signal": "ALL",     "rec": None},
        "stats-long-tile":   {"signal": "LONG",    "rec": None},
        "stats-short-tile":  {"signal": "SHORT",   "rec": None},
        "stats-buy-tile":    {"signal": "ALL",     "rec": "BUY"},
        "stats-sell-tile":   {"signal": "ALL",     "rec": "SELL"},
    }

    _TILE_IDS = list(_TILE_FILTER_MAP.keys())
    _BASE_CLASS = "kb-stat-card kb-animate-in kb-stagger-{n}"
    _STAGGER = {
        "stats-total-tile": 1, "stats-long-tile": 2, "stats-short-tile": 3,
        "stats-buy-tile": 5, "stats-sell-tile": 6,
    }

    @app.callback(
        Output("tile-filter-store", "data"),
        Output("signal-filter", "value"),
        Output("stats-total-tile", "className"),
        Output("stats-long-tile", "className"),
        Output("stats-short-tile", "className"),
        Output("stats-buy-tile", "className"),
        Output("stats-sell-tile", "className"),
        Input("stats-total-tile", "n_clicks"),
        Input("stats-long-tile", "n_clicks"),
        Input("stats-short-tile", "n_clicks"),
        Input("stats-buy-tile", "n_clicks"),
        Input("stats-sell-tile", "n_clicks"),
        State("tile-filter-store", "data"),
        prevent_initial_call=True,
    )
    def handle_tile_click(n_total, n_long, n_short, n_buy, n_sell, current_store):
        """Route tile clicks to signal-filter dropdown + rec filter store + active CSS."""
        triggered = callback_context.triggered_id
        if not triggered or triggered not in _TILE_FILTER_MAP:
            raise PreventUpdate
        new_filter = _TILE_FILTER_MAP[triggered]
        sig_val = new_filter["signal"]
        # Build className for each tile: active tile gets kb-stat-tile-active appended
        classes = []
        for tile_id in _TILE_IDS:
            stagger = _STAGGER.get(tile_id, 1)
            base = f"kb-stat-card kb-animate-in kb-stagger-{stagger}"
            classes.append(base + (" kb-stat-tile-active" if tile_id == triggered else ""))
        return (new_filter, sig_val, *classes)

    # ------------------------------------------------------------------
    # 2b. Update signal table + stats cards (MTF aggregated)
    # ------------------------------------------------------------------
    @app.callback(
        Output("signal-table", "data"),
        Output("qualified-recs-table", "data"),
        Output("recommendation-history-table", "data"),
        Output("stats-total", "children"),
        Output("stats-long", "children"),
        Output("stats-short", "children"),
        Output("stats-avg", "children"),
        Output("stats-buy", "children"),
        Output("stats-sell", "children"),
        Output("active-filters-hint", "children"),
        Input("refresh-interval", "n_intervals"),
        Input("signal-filter", "value"),
        Input("score-slider", "value"),
        Input("sector-filter", "value"),
        Input("price-cap-filter", "value"),
        Input("watchlist-dropdown", "value"),
        Input("tile-filter-store", "data"),
    )
    def update_signal_table(n, signal_filter, min_score, sectors, price_cap_values, watchlist, tile_store):
        mode, selected = _watchlist_mode(watchlist)

        if mode == "watchlist" and selected:
            # Allow user selection to steer the active scanner watchlist.
            scanner.current_watchlist = selected

        # Resolve tile-driven recommendation filter (BUY / SELL / None = all)
        tile_rec_filter = (tile_store or {}).get("rec") if tile_store else None

        # Use MTF aggregated results from scanner (one row per symbol)
        # Copy rows so UI formatting never mutates scanner state in-place.
        if scanner.last_mtf_results:
            signals = [dict(s) for s in scanner.last_mtf_results]
        else:
            # Fallback for startup/race windows: reconstruct latest MTF view from DB.
            db_rows = db_manager.get_latest_signals(min_score=0)
            signals = [dict(s) for s in SignalRanker.aggregate_mtf(db_rows)] if db_rows else []

        # Apply watchlist filter
        if mode == "watchlist" and selected:
            wl_symbols = wl_mgr.load_watchlist(selected)
            if wl_symbols:
                wl_set = set(wl_symbols)
                signals = [s for s in signals if s.get("symbol") in wl_set]
        elif mode == "symbol" and selected:
            signals = [s for s in signals if s.get("symbol") == selected]

        # Apply signal filter (from dropdown or tile click)
        if signal_filter and signal_filter != "ALL":
            signals = [s for s in signals if s.get("signal") == signal_filter]

        # Apply recommendation filter (from BUY / SELL tile click)
        if tile_rec_filter in ("BUY", "SELL"):
            signals = [s for s in signals if s.get("recommendation") == tile_rec_filter]

        # Apply score filter
        min_s = min_score or 0
        signals = [s for s in signals if s.get("score", 0) >= min_s]

        # Apply sector filter
        if sectors:
            sector_set = set(sectors)
            signals = [s for s in signals if s.get("sector") in sector_set]

        # Optional price cap filter
        price_le_10 = bool(price_cap_values and "LE10" in price_cap_values)
        if price_le_10:
            def _price_ok(row):
                try:
                    p = float(row.get("price") or 0.0)
                    return p > 0 and p <= 10.0
                except (TypeError, ValueError):
                    return False
            signals = [s for s in signals if _price_ok(s)]

        # Load institutional intelligence once for all signal tickers
        intel_by_ticker = _load_intelligence_by_ticker()

        # Format for display
        for s in signals:
            # Round numeric fields
            for key in ("rsi", "adx", "volume_ratio"):
                val = s.get(key)
                if val is not None:
                    s[key] = round(float(val), 1)

            # Round stop/target/rr
            for key in ("stop_loss", "target_1", "target_2"):
                val = s.get(key)
                if val is not None:
                    s[key] = round(float(val), 2)

            rr = s.get("rr_ratio")
            if rr is not None:
                s["rr_ratio"] = round(float(rr), 1)

            # Round relative strength
            rs = s.get("relative_strength")
            if rs is not None:
                s["relative_strength"] = round(float(rs), 1)

            # Clean up GEX display
            gex = s.get("gex_status", "")
            if gex:
                s["gex_status"] = gex.replace("_", " ").replace("ZERO GAMMA", "ZG")

            # Shorten trend direction
            trend = s.get("trend_direction", "")
            s["trend_direction"] = {"UPTREND": "UP", "DOWNTREND": "DOWN", "SIDEWAYS": "SIDE"}.get(trend, trend)

            # Clean VWAP status display
            vwap = s.get("vwap_status", "")
            if vwap:
                s["vwap_status"] = vwap.replace("_VWAP", "").replace("_", " ")

            # Regime display
            regime = s.get("market_regime", "")
            if regime:
                s["market_regime"] = regime.replace("_", " ")

            # Enforce dashboard display timezone consistency.
            if s.get("last_updated"):
                s["last_updated"] = _to_new_york_hms(s.get("last_updated"))

            # Remove internal fields not needed for display
            s.pop("timeframes", None)
            s.pop("mtf_score", None)

            # Inject institutional intelligence
            sym = str(s.get("symbol") or "").upper()
            intel = intel_by_ticker.get(sym, {})
            phase_raw = str(intel.get("accum_phase") or "")
            s["inst_phase"] = phase_raw.replace("_", " ") if phase_raw else "—"
            conv = intel.get("conviction_score")
            s["inst_conviction"] = int(conv) if conv is not None else None
            s["inst_swing"] = str(intel.get("swing_signal") or "—")

        # Stats
        total = len(signals)
        longs = sum(1 for s in signals if s.get("signal") == "LONG")
        shorts = sum(1 for s in signals if s.get("signal") == "SHORT")
        avg_score = round(sum(s.get("score", 0) for s in signals) / total, 1) if total else 0
        buys = sum(1 for s in signals if s.get("recommendation") == "BUY")
        sells = sum(1 for s in signals if s.get("recommendation") == "SELL")

        qualified = [
            {
                "symbol": s.get("symbol"),
                "recommendation": s.get("recommendation"),
                "signal": s.get("signal"),
                "score": s.get("score"),
                "price": s.get("price"),
                "rr_ratio": s.get("rr_ratio"),
                "gex_status": s.get("gex_status"),
                "market_regime": s.get("market_regime"),
                "last_updated": s.get("last_updated"),
            }
            for s in signals
            if s.get("recommendation") in ("BUY", "SELL")
        ]

        history_rows = db_manager.get_recommendation_history(limit=300, include_hold=True)
        history = []
        for h in history_rows:
            row = {
                "timestamp": str(h.get("timestamp", ""))[:19],
                "symbol": h.get("symbol"),
                "timeframe": h.get("timeframe"),
                "recommendation": h.get("recommendation"),
                "signal": h.get("signal"),
                "score": h.get("score"),
                "price": h.get("price"),
                "rr_ratio": h.get("rr_ratio"),
                "gex_status": (h.get("gex_status") or "").replace("_", " ").replace("ZERO GAMMA", "ZG"),
                "market_regime": (h.get("market_regime") or "").replace("_", " "),
            }
            history.append(row)

        return (
            signals,
            qualified,
            history,
            str(total),
            str(longs),
            str(shorts),
            str(avg_score),
            str(buys),
            str(sells),
            (
                f"Active filters: watchlist={str(watchlist or 'N/A').upper()} | "
                f"signal={signal_filter or 'ALL'} | "
                + (f"rec={tile_rec_filter} | " if tile_rec_filter else "")
                + f"min_score={min_s} | "
                f"sectors={', '.join(sectors) if sectors else 'ALL'} | "
                f"price_cap={'<=10' if price_le_10 else 'ALL'}"
            ),
        )

    # ------------------------------------------------------------------
    # 3. Main navbar navigation + ISR show/hide
    # ------------------------------------------------------------------
    # 6-tab navigation: Swing Snipers | Intraday ML | Snipers | Options | Intelligence | P&L Ledger
    # Section order: intelligence(0), sniper-board(1), live-signals(2), performance(3)
    # Intraday ML / Snipers / Options all map to live-signals section with auto sub-tab switch
    # Legacy sections (stock-ideas, options-ideas, research, paper, my-trades,
    #   recommendations, options, eod, ask-kubera) always hidden.
    @app.callback(
        Output("intelligence-section", "hidden"),
        Output("sniper-board-section", "hidden"),
        Output("live-signals-section", "hidden"),
        Output("forecast-section", "hidden"),
        Output("performance-section", "hidden"),
        # Auto sub-tab switching for Intraday ML / Snipers / Options nav
        Output("ls-tabs", "value"),
        # Legacy sections — always hidden
        Output("stock-ideas-section", "hidden"),
        Output("options-ideas-section", "hidden"),
        Output("research-section", "hidden"),
        Output("paper-section", "hidden"),
        Output("my-trades-section", "hidden"),
        Output("recommendations-section", "hidden"),
        Output("options-section", "hidden"),
        Output("eod-section", "hidden"),
        Output("ask-kubera-section", "hidden"),
        # ISR
        Output("stock-report-section", "hidden"),
        Output("active-nav", "data"),
        # Active states for 7 tabs (predictive is enabled now)
        Output("nav-intelligence", "active"),
        Output("nav-sniper-board", "active"),
        Output("nav-intraday-ml", "active"),
        Output("nav-intraday-sniper", "active"),
        Output("nav-options", "active"),
        Output("nav-predictive", "active"),
        Output("nav-performance", "active"),
        # Legacy nav active states (always False)
        Output("nav-stock-ideas", "active"),
        Output("nav-options-ideas", "active"),
        Output("nav-research", "active"),
        Output("nav-paper", "active"),
        Output("nav-my-trades", "active"),
        Output("isr-previous-section", "data"),
        # Inputs: 7 main tabs + selected ticker
        Input("nav-intelligence", "n_clicks"),
        Input("nav-sniper-board", "n_clicks"),
        Input("nav-intraday-ml", "n_clicks"),
        Input("nav-intraday-sniper", "n_clicks"),
        Input("nav-options", "n_clicks"),
        Input("nav-predictive", "n_clicks"),
        Input("nav-performance", "n_clicks"),
        Input("selected-ticker", "data"),
        State("active-nav", "data"),
        State("isr-previous-section", "data"),
        prevent_initial_call=False,
    )
    def toggle_main_nav(n_intel, n_sniper, n_ml, n_sniper_intra, n_options,
                        n_predictive, n_perf, selected_ticker,
                        current_nav, prev_section):
        ctx = callback_context
        NUM_MAIN = 5   # 5 physical section divs (intelligence, sniper, live-signals, forecast, performance)
        NUM_NAV = 7    # 7 nav tabs
        NUM_LEGACY = 9  # 9 legacy hidden sections

        # Map nav IDs to section indices (0-4) + sub-tab auto-switch
        # Intraday ML / Snipers / Options all map to live-signals (2) but with different sub-tabs
        section_map = {
            "nav-intelligence": 0,
            "nav-sniper-board": 1,
            "nav-intraday-ml": 2,
            "nav-intraday-sniper": 2,
            "nav-options": 2,
            "nav-predictive": 3,
            "nav-performance": 4,
        }
        # Which sub-tab to auto-select for each nav
        subtab_map = {
            "nav-intraday-ml": "tab-intraday-ml",
            "nav-intraday-sniper": "tab-intraday-sniper",
            "nav-options": "tab-options-flow",
        }

        NUM_LEGACY_NAV = 5  # 5 old nav stubs (always False)

        # Output order (28 total):
        # [0-3] 4 hidden_main, [4] ls-tabs, [5-13] 9 legacy hidden,
        # [14] ISR hidden, [15] active-nav, [16-21] 6 nav active,
        # [22-26] 5 legacy nav active, [27] isr-previous
        nav_order = ["nav-intelligence", "nav-sniper-board", "nav-intraday-ml",
                      "nav-intraday-sniper", "nav-options", "nav-predictive",
                      "nav-performance"]

        def _build_return(nav, isr_hidden=True):
            idx = section_map.get(nav, 1)
            hidden_main = tuple(i != idx for i in range(NUM_MAIN))  # 4 values
            subtab = subtab_map.get(nav, no_update)                  # 1 value
            hidden_legacy = (True,) * NUM_LEGACY                     # 9 values
            nav_active = tuple(n == nav for n in nav_order)          # 6 values
            legacy_active = (False,) * NUM_LEGACY_NAV                # 5 values
            return (*hidden_main, subtab, *hidden_legacy,
                    isr_hidden, nav, *nav_active, *legacy_active, nav)

        # When a ticker is selected, show ISR and hide everything else
        if selected_ticker and ctx.triggered:
            trigger_id = ctx.triggered[0]["prop_id"].split(".")[0]
            if trigger_id == "selected-ticker":
                prev = current_nav or "nav-sniper-board"
                return (*(True,) * NUM_MAIN, no_update, *(True,) * NUM_LEGACY,
                        False, prev, *(False,) * NUM_NAV,
                        *(False,) * NUM_LEGACY_NAV, prev)

        # When ticker is cleared (back button), restore previous section
        if not selected_ticker and ctx.triggered:
            trigger_id = ctx.triggered[0]["prop_id"].split(".")[0]
            if trigger_id == "selected-ticker":
                nav = prev_section or "nav-sniper-board"
                return _build_return(nav, isr_hidden=True)

        # Normal navbar click — Swing Snipers is the default landing surface
        if not ctx.triggered or ctx.triggered[0]["prop_id"] == ".":
            nav = "nav-sniper-board"
        else:
            nav = ctx.triggered[0]["prop_id"].split(".")[0]

        return _build_return(nav)

    # ------------------------------------------------------------------
    # KILL SWITCH — Shutdown scanner + dashboard
    # ------------------------------------------------------------------
    @app.callback(
        Output("kill-confirm-dialog", "displayed"),
        Input("kill-switch-btn", "n_clicks"),
        prevent_initial_call=True,
    )
    def kill_switch_prompt(n):
        if n:
            return True
        return False

    @app.callback(
        Output("kill-switch-dummy", "children"),
        Input("kill-confirm-dialog", "submit_n_clicks"),
        prevent_initial_call=True,
    )
    def kill_switch_execute(n):
        if n:
            import os
            import signal
            logger.warning("KILL SWITCH activated — shutting down")
            os.kill(os.getpid(), signal.SIGTERM)
        return ""

    # ------------------------------------------------------------------
    # 3b. Ticker click wiring — Signals table → selected-ticker
    # ------------------------------------------------------------------
    @app.callback(
        Output("selected-ticker", "data", allow_duplicate=True),
        Input("signal-table", "active_cell"),
        State("signal-table", "data"),
        prevent_initial_call=True,
    )
    def signal_table_ticker_click(active_cell, table_data):
        if not active_cell or not table_data:
            return no_update
        col_id = active_cell.get("column_id", "")
        if col_id != "symbol":
            return no_update
        row = active_cell.get("row", 0)
        try:
            return table_data[row].get("symbol")
        except (IndexError, TypeError):
            return no_update

    # ------------------------------------------------------------------
    # 3c. Ticker click wiring — Paper Trades + Options tables
    # ------------------------------------------------------------------
    @app.callback(
        Output("selected-ticker", "data", allow_duplicate=True),
        Input("paper-trades-table", "active_cell"),
        Input("option-setups-table", "active_cell"),
        State("paper-trades-table", "data"),
        State("option-setups-table", "data"),
        prevent_initial_call=True,
    )
    def paper_options_ticker_click(paper_cell, opt_cell, paper_data, opt_data):
        ctx = callback_context
        if not ctx.triggered:
            return no_update
        trigger = ctx.triggered[0]["prop_id"].split(".")[0]
        if trigger == "paper-trades-table" and paper_cell and paper_data:
            if paper_cell.get("column_id") in ("symbol", "ticker"):
                try:
                    row = paper_data[paper_cell["row"]]
                    return row.get("symbol") or row.get("ticker")
                except (IndexError, KeyError):
                    pass
        if trigger == "option-setups-table" and opt_cell and opt_data:
            if opt_cell.get("column_id") in ("symbol", "ticker"):
                try:
                    row = opt_data[opt_cell["row"]]
                    return row.get("symbol") or row.get("ticker")
                except (IndexError, KeyError):
                    pass
        return no_update

    # ------------------------------------------------------------------
    # 2b. Intraday sub-tabs (Confluence / ML) -> hidden ls-tabs controller
    # ------------------------------------------------------------------
    @app.callback(
        Output("ls-tabs", "value", allow_duplicate=True),
        Input("intraday-subtabs", "active_tab"),
        prevent_initial_call=True,
    )
    def sync_intraday_subtabs(active):
        mapping = {
            "sub-confluence": "tab-intraday-sniper",
            "sub-ml":         "tab-intraday-ml",
        }
        return mapping.get(active, no_update)

    # ------------------------------------------------------------------
    # 3. Column picker — toggle hidden columns
    # ------------------------------------------------------------------
    @app.callback(
        Output("signal-table", "hidden_columns"),
        Input("column-picker", "value"),
    )
    def update_hidden_columns(selected_extras):
        if selected_extras is None:
            selected_extras = []
        # Hide extra columns that are NOT selected
        hidden = [c for c in EXTRA_COLUMN_IDS if c not in selected_extras]
        return hidden

    # ------------------------------------------------------------------
    # 4. Update sector filter options dynamically
    # ------------------------------------------------------------------
    @app.callback(
        Output("sector-filter", "options"),
        Input("watchlist-dropdown", "value"),
    )
    def update_sector_options(watchlist):
        mode, selected = _watchlist_mode(watchlist)
        if mode == "none":
            return []
        if mode == "symbol":
            return [{"label": wl_mgr.get_sector(selected), "value": wl_mgr.get_sector(selected)}]
        symbols = wl_mgr.load_watchlist(selected)
        sectors = wl_mgr.get_unique_sectors(symbols)
        return [{"label": s, "value": s} for s in sectors]

    # ------------------------------------------------------------------
    # 5. Scanner status panel + regime banner
    # ------------------------------------------------------------------
    @app.callback(
        Output("scanner-status-panel", "children"),
        Output("status-dot", "className"),
        Output("status-text", "children"),
        Output("data-source-badge", "children"),
        Output("regime-status", "children"),
        Output("regime-status", "style"),
        Output("regime-description", "children"),
        Output("regime-banner", "className"),
        Output("regime-pill", "className"),
        Input("refresh-interval", "n_intervals"),
    )
    def update_scanner_status(n):
        from dash import html

        status = scanner.get_status()
        is_scanning = status.get("is_scanning", False)
        source = status.get("data_source", "UNKNOWN")
        ibkr_connected = bool(status.get("ibkr_connected", False))
        wl = status.get("current_watchlist", "N/A")
        sym_count = status.get("symbols_count", 0)
        sig_count = status.get("signals_count", 0)
        errors = status.get("errors", 0)
        last_time = status.get("last_scan_time")
        ibkr_diag = status.get("ibkr_diagnostics") or {}
        paper_policy = status.get("paper_policy") or {}

        # Status dot
        dot_class = "kb-status-dot active" if is_scanning else "kb-status-dot idle"
        status_text = "Scanning..." if is_scanning else "Idle"

        # Time since last scan
        time_ago = "Never"
        if last_time:
            try:
                last_dt = datetime.fromisoformat(last_time)
                delta = datetime.now(timezone.utc) - last_dt
                secs = int(delta.total_seconds())
                if secs < 60:
                    time_ago = f"{secs}s ago"
                else:
                    time_ago = f"{secs // 60}m {secs % 60}s ago"
            except (ValueError, TypeError):
                time_ago = str(last_time)[:19]

        # Data source badge
        if source == "IBKR" and ibkr_connected:
            source_label = "IBKR LIVE"
            source_color = cfg.accent_cyan
        else:
            source_label = "IBKR DISCONNECTED"
            source_color = cfg.accent_short
        badge = html.Span(
            f"  {source_label}  ",
            style={
                "color": cfg.bg_color,
                "backgroundColor": source_color,
                "padding": "2px 8px",
                "borderRadius": "4px",
                "fontSize": "11px",
                "fontWeight": "bold",
            },
        )

        # Status panel
        panel_children = [
            html.Span(f"Watchlist: {wl.upper()} ({sym_count} symbols)"),
            html.Span(f"  |  Last scan: {time_ago}", style={"marginLeft": "16px"}),
            html.Span(f"  |  MTF Signals: {sig_count}", style={"marginLeft": "16px"}),
            html.Span(
                f"  |  Errors: {errors}",
                style={"marginLeft": "16px", "color": "#ff4488" if errors > 0 else "#888"},
            ),
        ]
        mode = str(paper_policy.get("mode") or "NORMAL").upper()
        mode_color = cfg.accent_long if mode == "NORMAL" else cfg.accent_neutral
        mode_source_date = str(paper_policy.get("source_trade_date") or "N/A")
        panel_children.append(
            html.Span(
                f"  |  Paper Mode: {mode} (EOD {mode_source_date})",
                style={"marginLeft": "16px", "color": mode_color},
            )
        )
        if source == "IBKR" or ibkr_diag.get("attempted_ports"):
            active_client_id = ibkr_diag.get("connected_client_id") or ibkr_diag.get("client_id", "N/A")
            diag_text = (
                f"  |  IBKR {ibkr_diag.get('host', '127.0.0.1')}:"
                f"{ibkr_diag.get('connected_port') or ibkr_diag.get('port', 'N/A')} "
                f"(clientId={active_client_id})"
            )
            panel_children.append(
                html.Span(diag_text, style={"marginLeft": "16px", "color": "#9aa4b2"})
            )
            last_err = ibkr_diag.get("last_error")
            if last_err and not ibkr_connected:
                panel_children.append(
                    html.Span(
                        f"  |  Last IBKR error: {last_err}",
                        style={"marginLeft": "16px", "color": "#ff7b7b"},
                    )
                )
        panel = html.Div(panel_children)

        # Market regime banner — HMM-based.
        # Tuple format: (label, color, description, banner_class, pill_class).
        _hmm_display = {
            0: ("CRASH",        "#ff4488", "All entries blocked", "kb-banner kb-banner-risk-off", "kb-status-pill bad"),
            1: ("DISTRIBUTING", "#ff8c00", "SHORT only",          "kb-banner kb-banner-risk-off", "kb-status-pill bad"),
            2: ("ACCUMULATING", "#ffd43b", "LONG (tight stops)",  "kb-banner kb-banner-neutral",  "kb-status-pill warn"),
            3: ("MEAN-REV",     "#4da3ff", "LONG allowed",        "kb-banner kb-banner-neutral",  "kb-status-pill warn"),
            4: ("TRENDING",     "#00ff88", "LONG primary",        "kb-banner kb-banner-risk-on",  "kb-status-pill ok"),
        }
        try:
            from signal_scanner.institutional_intel.intelligence.regime_hmm import DailyRegimeHMM
            from signal_scanner.institutional_intel.config import safe_duckdb_connect
            hmm = DailyRegimeHMM()
            hmm.load()
            if hmm._model is None:
                raise RuntimeError("No HMM model")
            _state, _probs, _name = hmm.current_regime()
            regime_display, regime_color, description, banner_style, pill_class = _hmm_display.get(
                _state, ("UNKNOWN", "#888", "", "kb-banner", "kb-status-pill"))
            regime_style = {"color": regime_color, "fontWeight": "700"}
        except Exception:
            # Fallback to old regime data
            regime_data = status.get("market_regime")
            if regime_data:
                regime = regime_data.get("regime", "UNKNOWN")
                description = regime_data.get("description", "")
                _old_colors = {"RISK_ON": "#00ff88", "RISK_OFF": "#ff4488", "NEUTRAL": cfg.accent_neutral}
                regime_color = _old_colors.get(regime, "#888")
                regime_display = regime.replace("_", " ")
                regime_style = {"color": regime_color, "fontWeight": "700"}
                _old_banner = {"RISK_ON": "kb-banner kb-banner-risk-on",
                               "RISK_OFF": "kb-banner kb-banner-risk-off",
                               "NEUTRAL": "kb-banner kb-banner-neutral"}
                banner_style = _old_banner.get(regime, "kb-banner")
                _old_pill = {"RISK_ON": "kb-status-pill ok",
                             "RISK_OFF": "kb-status-pill bad",
                             "NEUTRAL": "kb-status-pill warn"}
                pill_class = _old_pill.get(regime, "kb-status-pill")
            else:
                regime_display = "LOADING..."
                regime_style = {"color": "#888", "fontWeight": "700"}
                description = ""
                banner_style = "kb-banner"
                pill_class = "kb-status-pill"

        return (panel, dot_class, status_text, badge,
                regime_display, regime_style, description, banner_style, pill_class)

    # ------------------------------------------------------------------
    # 5b. Time guard status banner
    # ------------------------------------------------------------------
    @app.callback(
        Output("time-guard-icon", "children"),
        Output("trade-mode-status", "children"),
        Output("trade-mode-status", "style"),
        Output("entry-cutoff-status", "children"),
        Output("entry-cutoff-status", "style"),
        Output("eod-eval-status", "children"),
        Output("eod-eval-status", "style"),
        Output("swing-count-status", "children"),
        Output("time-guard-detail", "children"),
        Input("refresh-interval", "n_intervals"),
    )
    def update_time_guard_banner(n):
        now_et = datetime.now(timezone.utc).astimezone(NY_TZ) if NY_TZ else datetime.now(timezone.utc)
        cutoff_h = int(scan_cfg.late_entry_cutoff_hour)
        cutoff_m = int(scan_cfg.late_entry_cutoff_minute)
        eod_h = int(scan_cfg.eod_evaluation_hour)
        eod_m = int(scan_cfg.eod_evaluation_minute)

        past_cutoff = (now_et.hour, now_et.minute) >= (cutoff_h, cutoff_m)
        past_eod = (now_et.hour, now_et.minute) >= (eod_h, eod_m)

        # Determine trade mode
        if past_eod:
            mode_text = "CLOSED"
            mode_color = cfg.accent_short
            icon = "\u23f8 "  # pause
            detail = "Market closed — EOD evaluation complete"
        elif past_cutoff:
            mode_text = "NO NEW ENTRIES"
            mode_color = cfg.accent_neutral
            icon = "\u26a0 "  # warning
            detail = f"Past {cutoff_h}:{cutoff_m:02d} ET — exits only, no new positions"
        else:
            mode_text = "ACTIVE"
            mode_color = cfg.accent_long
            icon = "\u25b6 "  # play
            detail = f"Accepting entries until {cutoff_h}:{cutoff_m:02d} ET"

        mode_style = {"color": mode_color, "fontWeight": "bold", "fontSize": "13px"}

        # Cutoff status
        cutoff_text = f"{cutoff_h}:{cutoff_m:02d} PM"
        cutoff_style = {
            "color": cfg.accent_short if past_cutoff else cfg.text_color,
            "fontSize": "12px",
            "fontWeight": "bold",
            "textDecoration": "line-through" if past_cutoff else "none",
        }

        # EOD status
        eod_text = f"{eod_h}:{eod_m:02d} PM"
        eod_style = {
            "color": cfg.accent_short if past_eod else cfg.text_color,
            "fontSize": "12px",
            "fontWeight": "bold",
            "textDecoration": "line-through" if past_eod else "none",
        }

        # Swing count
        swing_count = 0
        try:
            open_trades = db_manager.get_open_paper_trades()
            for t in open_trades:
                src = str(t.get("recommendation_source") or "")
                if "SWING" in src:
                    swing_count += 1
        except Exception as exc:
            logger.debug("Failed to count swing trades: %s", exc)

        return (
            icon, mode_text, mode_style,
            cutoff_text, cutoff_style,
            eod_text, eod_style,
            str(swing_count),
            detail,
        )

    # ------------------------------------------------------------------
    # 5b. Paper tile click → paper-tile-filter-store
    # ------------------------------------------------------------------
    _PAPER_TILE_MAP = {
        "paper-open-tile":        {"status": "OPEN"},
        "paper-closed-tile":      {"status": "CLOSED"},
        "paper-swing-count-tile": {"status": "SWING"},
    }
    _PAPER_TILE_IDS = list(_PAPER_TILE_MAP.keys())
    _PAPER_STAGGER = {"paper-open-tile": 1, "paper-closed-tile": 2, "paper-swing-count-tile": 6}

    @app.callback(
        Output("paper-tile-filter-store", "data"),
        Output("paper-open-tile", "className"),
        Output("paper-closed-tile", "className"),
        Output("paper-swing-count-tile", "className"),
        Input("paper-open-tile", "n_clicks"),
        Input("paper-closed-tile", "n_clicks"),
        Input("paper-swing-count-tile", "n_clicks"),
        State("paper-tile-filter-store", "data"),
        prevent_initial_call=True,
    )
    def handle_paper_tile_click(n_open, n_closed, n_swing, current_store):
        """Route paper tile clicks to status filter store."""
        triggered = callback_context.triggered_id
        if not triggered or triggered not in _PAPER_TILE_MAP:
            raise PreventUpdate
        new_filter = _PAPER_TILE_MAP[triggered]
        # Toggle off if already active
        if (current_store or {}).get("status") == new_filter["status"]:
            new_filter = {"status": None}
            triggered = None  # no active tile
        classes = []
        for tile_id in _PAPER_TILE_IDS:
            stagger = _PAPER_STAGGER.get(tile_id, 1)
            base = f"kb-stat-card kb-animate-in kb-stagger-{stagger}"
            classes.append(base + (" kb-stat-tile-active" if tile_id == triggered else ""))
        return (new_filter, *classes)

    # ------------------------------------------------------------------
    # 6. Paper trading panel
    # ------------------------------------------------------------------
    @app.callback(
        Output("paper-open", "children"),
        Output("paper-closed", "children"),
        Output("paper-win-rate", "children"),
        Output("paper-pnl", "children"),
        Output("paper-equity", "children"),
        Output("paper-swing-count", "children"),
        Output("paper-trades-table", "data"),
        Input("refresh-interval", "n_intervals"),
        Input("paper-tile-filter-store", "data"),
    )
    def update_paper_panel(n, paper_tile_store):
        perf = db_manager.get_paper_performance()
        trades = db_manager.get_recent_paper_trades(limit=100)
        # Apply tile filter — OPEN/CLOSED filters by status; SWING filters by trade_mode
        _paper_status_filter = (paper_tile_store or {}).get("status") if paper_tile_store else None
        latest_price_by_symbol = {
            str(r.get("symbol")).upper(): r.get("price")
            for r in (scanner.last_mtf_results or [])
            if r.get("symbol") and r.get("price") is not None
        }
        open_symbols = [
            str(t.get("symbol")).upper()
            for t in trades
            if t.get("status") == "OPEN" and t.get("symbol")
        ]
        missing_open = [s for s in sorted(set(open_symbols)) if s not in latest_price_by_symbol]
        if missing_open:
            latest_price_by_symbol.update(db_manager.get_latest_prices_for_symbols(missing_open))

        table_rows = []
        unrealized_total = 0.0
        for t in trades:
            row = dict(t)
            for ts_key in ("opened_at", "closed_at"):
                ts = row.get(ts_key)
                if ts:
                    row[ts_key] = str(ts)[:19]
            current_price = None
            unrealized_pnl = 0.0
            if row.get("status") == "OPEN":
                symbol = str(row.get("symbol") or "").upper()
                px = latest_price_by_symbol.get(symbol)
                if px is not None:
                    try:
                        current_price = float(px)
                        entry = float(row.get("entry_price") or 0.0)
                        qty = float(row.get("quantity") or 0.0)
                        side = row.get("side")
                        gross = (
                            (current_price - entry) * qty
                            if side == "LONG"
                            else (entry - current_price) * qty
                        )
                        # Include paid entry fees so open P&L is net-consistent with closed trades.
                        unrealized_pnl = gross - float(row.get("fees") or 0.0)
                    except (TypeError, ValueError):
                        current_price = None
                        unrealized_pnl = 0.0
            row["current_price"] = round(float(current_price), 2) if current_price is not None else None
            row["unrealized_pnl"] = round(float(unrealized_pnl), 2)
            unrealized_total += float(unrealized_pnl)
            for key in ("entry_price", "exit_price", "realized_pnl", "realized_pnl_pct"):
                val = row.get(key)
                if val is not None:
                    row[key] = round(float(val), 2)

            # Compute trade_mode and days_held from recommendation_source + opened_at.
            src = str(row.get("recommendation_source") or "")
            row["trade_mode"] = "SWING" if "SWING" in src else "DAY"

            # Ensure strategy_type and execution_mode have display values
            row["strategy_type"] = row.get("strategy_type") or "UNKNOWN"
            row["execution_mode"] = row.get("execution_mode") or "SIM"
            opened_at = row.get("opened_at")
            if opened_at:
                try:
                    opened_dt = datetime.fromisoformat(str(t.get("opened_at") or ""))
                    if opened_dt.tzinfo is None:
                        opened_dt = opened_dt.replace(tzinfo=timezone.utc)
                    row["days_held"] = max(0, (datetime.now(timezone.utc) - opened_dt).days)
                except (TypeError, ValueError):
                    row["days_held"] = 0
            else:
                row["days_held"] = 0
            table_rows.append(row)

        realized = float(perf.get("realized_pnl", 0.0))
        equity = scan_cfg.paper_starting_capital + realized + unrealized_total
        win_rate = perf.get("win_rate", 0.0)

        # Count SWING positions
        swing_count = sum(1 for r in table_rows if r.get("trade_mode") == "SWING" and r.get("status") == "OPEN")

        # Apply tile filter to table display (does NOT affect perf stats/counts)
        display_rows = table_rows
        if _paper_status_filter == "OPEN":
            display_rows = [r for r in table_rows if r.get("status") == "OPEN"]
        elif _paper_status_filter == "CLOSED":
            display_rows = [r for r in table_rows if r.get("status") == "CLOSED"]
        elif _paper_status_filter == "SWING":
            display_rows = [r for r in table_rows if r.get("trade_mode") == "SWING"]

        return (
            str(perf.get("open_positions", 0)),
            str(perf.get("closed_trades", 0)),
            f"{win_rate:.1f}%",
            f"${realized:,.2f}",
            f"${equity:,.2f}",
            str(swing_count),
            display_rows,
        )

    # ------------------------------------------------------------------
    # 6b. Option tile click → option-tile-filter-store
    # ------------------------------------------------------------------
    _OPT_TILE_MAP = {
        "opt-active-tile":   {"state": "ACTIVE"},
        "opt-strong-tile":   {"state": "STRONG"},
        "opt-watching-tile": {"state": "WATCHING"},
        "opt-invalid-tile":  {"state": "INVALID"},
        "opt-taken-tile":    {"state": "TAKEN"},
        "opt-all-tile":      {"state": None},
    }
    _OPT_TILE_IDS = list(_OPT_TILE_MAP.keys())
    _OPT_STAGGER = {t: i + 1 for i, t in enumerate(_OPT_TILE_IDS)}

    @app.callback(
        Output("option-tile-filter-store", "data"),
        Output("opt-active-tile", "className"),
        Output("opt-strong-tile", "className"),
        Output("opt-watching-tile", "className"),
        Output("opt-invalid-tile", "className"),
        Output("opt-taken-tile", "className"),
        Output("opt-all-tile", "className"),
        Input("opt-active-tile", "n_clicks"),
        Input("opt-strong-tile", "n_clicks"),
        Input("opt-watching-tile", "n_clicks"),
        Input("opt-invalid-tile", "n_clicks"),
        Input("opt-taken-tile", "n_clicks"),
        Input("opt-all-tile", "n_clicks"),
        State("option-tile-filter-store", "data"),
        prevent_initial_call=True,
    )
    def handle_option_tile_click(
        n_active, n_strong, n_watching, n_invalid, n_taken, n_all, current_store
    ):
        """Route option tile clicks to idea_state filter store."""
        triggered = callback_context.triggered_id
        if not triggered or triggered not in _OPT_TILE_MAP:
            raise PreventUpdate
        new_filter = _OPT_TILE_MAP[triggered]
        # Toggle off if already active and not the "ALL" reset tile
        if triggered != "opt-all-tile" and (current_store or {}).get("state") == new_filter["state"]:
            new_filter = {"state": None}
            triggered = "opt-all-tile"
        classes = []
        for tile_id in _OPT_TILE_IDS:
            stagger = _OPT_STAGGER.get(tile_id, 1)
            base = f"kb-stat-card kb-animate-in kb-stagger-{stagger}"
            classes.append(base + (" kb-stat-tile-active" if tile_id == triggered else ""))
        return (new_filter, *classes)

    # ------------------------------------------------------------------
    # 7. Option setup panel
    # ------------------------------------------------------------------
    @app.callback(
        Output("option-setups-table", "data"),
        Output("option-last-refresh", "children"),
        Output("option-next-refresh", "children"),
        Output("opt-active", "children"),
        Output("opt-strong", "children"),
        Output("opt-watching", "children"),
        Output("opt-invalid", "children"),
        Output("opt-taken", "children"),
        Output("opt-all", "children"),
        Input("refresh-interval", "n_intervals"),
        Input("option-tile-filter-store", "data"),
    )
    def update_option_setups(n, opt_tile_store):
        _opt_state_filter = (opt_tile_store or {}).get("state") if opt_tile_store else None
        setups = db_manager.get_option_setups(status="ACTIVE", limit=200)
        rows = []
        today_ny = datetime.now(NY_TZ).date()
        mtf_by_symbol = {
            str(r.get("symbol") or "").upper(): r
            for r in (scanner.last_mtf_results or [])
            if r.get("symbol")
        }
        intel_by_ticker = _load_intelligence_by_ticker()
        for s in setups:
            row = dict(s)
            for key in ("score", "rr_ratio", "strike", "underlying_price"):
                v = row.get(key)
                if v is not None:
                    row[key] = round(float(v), 2)
            sym = str(row.get("symbol") or "").upper()
            mtf_row = mtf_by_symbol.get(sym)
            row["current_score"] = (
                round(float(mtf_row.get("score") or 0.0), 1)
                if mtf_row and mtf_row.get("score") is not None
                else None
            )
            if row.get("updated_ts"):
                dt_ny = _to_new_york_datetime(row["updated_ts"])
                if dt_ny:
                    days_old = (today_ny - dt_ny.date()).days
                    row["updated_flag"] = "TODAY" if days_old == 0 else ("RECENT" if days_old <= 2 else "STALE")
                    row["updated_ts"] = dt_ny.strftime("%Y-%m-%d %H:%M:%S ET")
                else:
                    row["updated_flag"] = "STALE"
            else:
                row["updated_flag"] = "STALE"
            if row.get("created_ts"):
                dt_created = _to_new_york_datetime(row["created_ts"])
                row["created_ts"] = dt_created.strftime("%Y-%m-%d %H:%M:%S ET") if dt_created else str(row["created_ts"])
            if row.get("last_validated_ts"):
                dt_val = _to_new_york_datetime(row["last_validated_ts"])
                row["last_validated_ts"] = dt_val.strftime("%Y-%m-%d %H:%M:%S ET") if dt_val else str(row["last_validated_ts"])
            else:
                row["last_validated_ts"] = ""
            if row.get("market_regime"):
                row["market_regime"] = str(row["market_regime"]).replace("_", " ")
            if row.get("gex_status"):
                row["gex_status"] = str(row["gex_status"]).replace("_", " ").replace("ZERO GAMMA", "ZG")
            row["idea_state"] = str(row.get("idea_state") or "ACTIVE").upper()
            row["invalid_reason"] = str(row.get("invalid_reason") or "")
            row["taken_flag"] = "YES" if int(row.get("is_taken") or 0) == 1 else "NO"
            # Enrich with institutional intelligence
            intel = intel_by_ticker.get(sym, {})
            phase_raw = str(intel.get("accum_phase") or "")
            row["inst_phase"] = phase_raw.replace("_", " ") if phase_raw else "—"
            conv = intel.get("conviction_score")
            row["inst_conviction"] = int(conv) if conv is not None else None
            row["inst_swing"] = str(intel.get("swing_signal") or "—")
            rows.append(row)

        status = scanner.get_status()
        is_scanning = bool(status.get("is_scanning", False))
        last_scan_raw = status.get("last_scan_time")
        if last_scan_raw:
            try:
                last_scan_dt = datetime.fromisoformat(str(last_scan_raw))
                if last_scan_dt.tzinfo is None:
                    last_scan_dt = last_scan_dt.replace(tzinfo=timezone.utc)
                last_scan_ny = last_scan_dt.astimezone(NY_TZ)
                last_refresh = last_scan_ny.strftime("%Y-%m-%d %H:%M:%S ET")
                next_scan_ny = (last_scan_dt + timedelta(seconds=scan_cfg.scan_interval_seconds)).astimezone(NY_TZ)
                if is_scanning:
                    next_refresh = "Scanning now..."
                else:
                    delta_seconds = int((next_scan_ny - datetime.now(NY_TZ)).total_seconds())
                    next_refresh = "Due now" if delta_seconds <= 0 else next_scan_ny.strftime("%Y-%m-%d %H:%M:%S ET")
            except (TypeError, ValueError):
                last_refresh = "Unknown"
                next_refresh = "Unknown"
        else:
            if is_scanning:
                last_refresh = "Initial scan in progress..."
                next_refresh = "Scanning now..."
            else:
                last_refresh = "Waiting for first scan"
                next_refresh = "Waiting for first scan"

        # Compute tile counts
        cnt_active  = sum(1 for r in rows if r.get("idea_state") not in ("INVALID",))
        cnt_strong  = sum(1 for r in rows if r.get("idea_state") == "STRONG")
        cnt_watching= sum(1 for r in rows if r.get("idea_state") == "WATCHING")
        cnt_invalid = sum(1 for r in rows if r.get("idea_state") == "INVALID")
        cnt_taken   = sum(1 for r in rows if r.get("taken_flag") == "YES")
        cnt_all     = len(rows)

        # Apply tile filter to display rows
        display_rows = rows
        if _opt_state_filter == "ACTIVE":
            display_rows = [r for r in rows if r.get("idea_state") not in ("INVALID",)]
        elif _opt_state_filter == "TAKEN":
            display_rows = [r for r in rows if r.get("taken_flag") == "YES"]
        elif _opt_state_filter in ("STRONG", "WATCHING", "INVALID"):
            display_rows = [r for r in rows if r.get("idea_state") == _opt_state_filter]

        return (
            display_rows, last_refresh, next_refresh,
            str(cnt_active), str(cnt_strong), str(cnt_watching),
            str(cnt_invalid), str(cnt_taken), str(cnt_all),
        )

    @app.callback(
        Output("option-save-status", "children"),
        Input("option-setups-table", "data_timestamp"),
        State("option-setups-table", "data"),
        State("option-setups-table", "data_previous"),
        prevent_initial_call=True,
    )
    def persist_option_taken_flags(ts, rows, prev_rows):
        if not rows or not prev_rows:
            raise PreventUpdate

        prev_by_id = {
            int(r.get("id")): r
            for r in prev_rows
            if r.get("id") not in (None, "")
        }
        changes = 0
        for row in rows:
            setup_id_raw = row.get("id")
            if setup_id_raw in (None, ""):
                continue
            try:
                setup_id = int(setup_id_raw)
            except (TypeError, ValueError):
                continue
            prev = prev_by_id.get(setup_id)
            if not prev:
                continue
            cur_taken = str(row.get("taken_flag") or "NO").upper()
            old_taken = str(prev.get("taken_flag") or "NO").upper()
            if cur_taken not in ("YES", "NO"):
                cur_taken = "NO"
            if old_taken not in ("YES", "NO"):
                old_taken = "NO"
            if cur_taken != old_taken:
                taking = (cur_taken == "YES")
                db_manager.set_option_setup_taken(setup_id=setup_id, is_taken=taking)
                if taking:
                    state = str(row.get("idea_state") or "").upper()
                    if state == "STRONG":
                        symbol = str(row.get("symbol") or "").upper()
                        option_type = str(row.get("option_type") or "").upper()
                        option_expiry = str(row.get("expiry_date") or "")
                        option_strike = float(row.get("strike") or 0.0)
                        if symbol and option_type in ("CALL", "PUT") and option_expiry and option_strike > 0:
                            if not db_manager.has_open_option_trade(
                                symbol=symbol,
                                option_type=option_type,
                                option_expiry=option_expiry,
                                option_strike=option_strike,
                            ):
                                rec = str(row.get("recommendation") or "")
                                side = "LONG" if rec == "BUY" else ("SHORT" if rec == "SELL" else "")
                                if side:
                                    now_iso = datetime.now(timezone.utc).isoformat()
                                    entry = float(row.get("underlying_price") or 0.0)
                                    db_manager.create_paper_trade(
                                        {
                                            "opened_at": now_iso,
                                            "symbol": symbol,
                                            "side": side,
                                            "entry_price": round(entry, 4) if entry > 0 else 0.0,
                                            "quantity": 1,
                                            "notional": round(entry, 2) if entry > 0 else 0.0,
                                            "stop_loss": None,
                                            "target_1": None,
                                            "target_2": None,
                                            "status": "OPEN",
                                            "recommendation_source": "OPTION_IDEA_STRONG",
                                            "instrument_type": "OPTION",
                                            "option_type": option_type,
                                            "option_expiry": option_expiry,
                                            "option_strike": option_strike,
                                            "entry_signal": row.get("signal"),
                                            "entry_score": float(row.get("score") or 0.0),
                                            "entry_rr_ratio": float(row.get("rr_ratio") or 0.0),
                                            "entry_market_regime": row.get("market_regime"),
                                            "entry_gex_status": row.get("gex_status"),
                                            "entry_session_time": None,
                                            "entry_trade_conditions": "Taken from STRONG option idea (underlying proxy)",
                                            "fees": 0.0,
                                            "created_ts": now_iso,
                                        }
                                    )
                changes += 1
        if changes == 0:
            raise PreventUpdate
        return f"Saved {changes} option idea update(s)"

    # ------------------------------------------------------------------
    # 8. End-of-day review panel
    # ------------------------------------------------------------------
    @app.callback(
        Output("eod-days", "children"),
        Output("eod-win-rate", "children"),
        Output("eod-pnl", "children"),
        Output("eod-losses", "children"),
        Output("eod-top-reason", "children"),
        Output("eod-alert-win", "children"),
        Output("eod-alert-meta", "children"),
        Output("eod-analysis-table", "data"),
        Input("refresh-interval", "n_intervals"),
    )
    def update_eod_panel(n):
        rows = db_manager.get_recent_eod_analysis(limit=30)
        alert_metrics = db_manager.get_signal_alert_success_metrics(
            lookback_days=7,
            horizon_minutes=30,
            min_score=60,
        )
        alert_meta = (
            f"Persisted alert metric: {alert_metrics['evaluated_alerts']} evaluated "
            f"(horizon={alert_metrics['horizon_minutes']}m, min_score={alert_metrics['min_score']}, "
            f"lookback={alert_metrics['lookback_days']}d, avg_move={alert_metrics['avg_signed_move_pct']}%)"
        )
        if not rows:
            return (
                "0",
                "0%",
                "$0.00",
                "0",
                "-",
                f"{alert_metrics['win_rate']:.1f}%",
                alert_meta,
                [],
            )

        total_days = len(rows)
        avg_win = sum(float(r.get("win_rate") or 0.0) for r in rows) / total_days
        total_pnl = sum(float(r.get("realized_pnl") or 0.0) for r in rows)
        total_losses = sum(int(r.get("losses") or 0) for r in rows)

        reason_count = {}
        table_rows = []
        for r in rows:
            reason = (r.get("top_loss_reason") or "NONE")
            reason_count[reason] = reason_count.get(reason, 0) + 1
            table_rows.append(
                {
                    "trade_date": r.get("trade_date"),
                    "total_trades": int(r.get("total_trades") or 0),
                    "wins": int(r.get("wins") or 0),
                    "losses": int(r.get("losses") or 0),
                    "win_rate": round(float(r.get("win_rate") or 0.0), 1),
                    "realized_pnl": round(float(r.get("realized_pnl") or 0.0), 2),
                    "avg_loss": round(float(r.get("avg_loss") or 0.0), 2),
                    "max_loss": round(float(r.get("max_loss") or 0.0), 2),
                    "top_loss_reason": reason,
                    "action_status": str(r.get("action_status") or "PENDING").upper(),
                    "action_notes": r.get("action_notes") or "",
                    "suggested_actions": r.get("suggested_actions") or "",
                }
            )

        top_reason = sorted(reason_count.items(), key=lambda x: x[1], reverse=True)[0][0]
        return (
            str(total_days),
            f"{avg_win:.1f}%",
            f"${total_pnl:,.2f}",
            str(total_losses),
            top_reason,
            f"{alert_metrics['win_rate']:.1f}%",
            alert_meta,
            table_rows,
        )

    @app.callback(
        Output("eod-save-status", "children"),
        Input("eod-analysis-table", "data_timestamp"),
        State("eod-analysis-table", "data"),
        State("eod-analysis-table", "data_previous"),
        prevent_initial_call=True,
    )
    def persist_eod_review_actions(ts, rows, prev_rows):
        if not rows or not prev_rows:
            raise PreventUpdate

        prev_by_date = {str(r.get("trade_date")): r for r in prev_rows if r.get("trade_date")}
        changes = 0
        for row in rows:
            trade_date = str(row.get("trade_date") or "")
            if not trade_date:
                continue
            prev = prev_by_date.get(trade_date)
            if not prev:
                continue

            new_status = str(row.get("action_status") or "PENDING").upper()
            old_status = str(prev.get("action_status") or "PENDING").upper()
            new_notes = str(row.get("action_notes") or "").strip()
            old_notes = str(prev.get("action_notes") or "").strip()

            if new_status != old_status or new_notes != old_notes:
                db_manager.update_eod_action_status(
                    trade_date=trade_date,
                    action_status=new_status,
                    action_notes=new_notes,
                )
                changes += 1

        if changes == 0:
            raise PreventUpdate
        stamp = datetime.now(NY_TZ).strftime("%Y-%m-%d %H:%M:%S ET")
        return f"Saved {changes} EOD review update(s) at {stamp}"

    # ------------------------------------------------------------------
    # 9. Ask Kubera — moved to kubera_callbacks.py
    # (kept as stub so old IDs don't break if referenced anywhere)
    # ------------------------------------------------------------------
    # The Ask Kubera callback is registered separately via register_kubera_callbacks(app).
    # This section intentionally left empty.

    # ------------------------------------------------------------------
    # 10. Navigate to detail view on symbol click
    # ------------------------------------------------------------------
    @app.callback(
        Output("detail-view-container", "children"),
        Output("detail-view-container", "style"),
        Output("signal-table", "style_table"),
        Input("signal-table", "active_cell"),
        Input("back-to-table-btn", "n_clicks"),
        State("signal-table", "data"),
        prevent_initial_call=True,
    )
    def handle_navigation(active_cell, back_clicks, table_data):
        from dash import html

        ctx = callback_context
        if not ctx.triggered:
            raise PreventUpdate

        trigger_id = ctx.triggered[0]["prop_id"].split(".")[0]

        # Back button
        if trigger_id == "back-to-table-btn":
            return [], {"display": "none"}, {"overflowX": "auto"}

        # Signal table clicks now go to ISR via selected-ticker store
        if trigger_id == "signal-table":
            raise PreventUpdate

        raise PreventUpdate

    # ------------------------------------------------------------------
    # 19. Terminal-dense status pills — readiness / EOD age / kill-switch
    #     Pulls from readiness.json, latest EOD log mtime, and
    #     paper_trader._kill_switch_blocked().
    # ------------------------------------------------------------------
    @app.callback(
        Output("readiness-pill", "className"),
        Output("readiness-pill-text", "children"),
        Output("eod-age-pill", "className"),
        Output("eod-age-pill-text", "children"),
        Output("kill-switch-pill", "className"),
        Output("kill-switch-pill-text", "children"),
        Input("refresh-interval", "n_intervals"),
        prevent_initial_call=False,
    )
    def update_status_pills(_n):
        import json
        from pathlib import Path

        # --- Readiness pill ---
        try:
            readiness_path = Path("data/warehouse/readiness.json")
            if readiness_path.exists():
                data = json.loads(readiness_path.read_text())
                status = (data.get("readiness_status") or "").upper()
                if status == "READY":
                    readiness_class = "kb-status-pill ok"
                    readiness_text = "READY"
                elif status == "DEGRADED":
                    readiness_class = "kb-status-pill warn"
                    readiness_text = "DEGRADED"
                elif status == "BLOCKED":
                    readiness_class = "kb-status-pill bad"
                    readiness_text = "BLOCKED"
                else:
                    readiness_class = "kb-status-pill"
                    readiness_text = status or "UNKNOWN"
            else:
                readiness_class = "kb-status-pill"
                readiness_text = "NO READINESS"
        except Exception:
            readiness_class = "kb-status-pill"
            readiness_text = "ERROR"

        # --- EOD age pill ---
        try:
            eod_logs = sorted(
                Path("logs").glob("eod_*.log"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            if eod_logs:
                age_h = (datetime.now().timestamp() - eod_logs[0].stat().st_mtime) / 3600.0
                if age_h < 4:
                    eod_class = "kb-status-pill ok"
                elif age_h < 30:
                    eod_class = "kb-status-pill warn"
                else:
                    eod_class = "kb-status-pill bad"
                if age_h < 1:
                    eod_text = f"EOD {int(age_h * 60)}M"
                elif age_h < 48:
                    eod_text = f"EOD {age_h:.0f}H"
                else:
                    eod_text = f"EOD {age_h / 24:.0f}D"
            else:
                eod_class = "kb-status-pill bad"
                eod_text = "EOD NONE"
        except Exception:
            eod_class = "kb-status-pill"
            eod_text = "EOD --"

        # --- Kill-switch pill ---
        try:
            pt = getattr(scanner, "_paper_trader", None)
            if pt is not None and hasattr(pt, "_kill_switch_blocked"):
                reason = pt._kill_switch_blocked()
                if reason:
                    kill_class = "kb-status-pill bad"
                    # show short flag — full reason is in logs
                    kill_text = "KILL ON"
                else:
                    kill_class = "kb-status-pill ok"
                    kill_text = "KILL OFF"
            else:
                kill_class = "kb-status-pill"
                kill_text = "KILL --"
        except Exception:
            kill_class = "kb-status-pill"
            kill_text = "KILL --"

        return (readiness_class, readiness_text,
                eod_class, eod_text,
                kill_class, kill_text)
