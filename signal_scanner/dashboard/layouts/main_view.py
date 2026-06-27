"""Main dashboard view — Kubera Signal Command Center.

V3: Modern website-like layout with top navbar, hero section headers,
animated stat cards, and Turmeric Gold + Dark theme.
"""

import dash_bootstrap_components as dbc
from dash import dash_table, dcc, html

from signal_scanner.config import COLUMN_TOOLTIPS, DashboardConfig
from signal_scanner.dashboard.trade_rules import rules_tooltip

cfg = DashboardConfig()

# ---------------------------------------------------------------------------
# Shared table style constants (used by all DataTables)
# ---------------------------------------------------------------------------
TABLE_HEADER_STYLE = {
    "backgroundColor": cfg.card_color_elevated,
    "color": cfg.accent_primary,
    "fontWeight": "600",
    "border": "none",
    "borderBottom": f"2px solid {cfg.border_color}",
    "fontSize": "0.75rem",
    "textTransform": "uppercase",
    "letterSpacing": "0.06em",
    "padding": "12px 10px",
    "fontFamily": "'Inter', -apple-system, sans-serif",
}

TABLE_CELL_STYLE = {
    "color": cfg.text_color,
    "border": "none",
    "borderBottom": f"1px solid #1e2230",
    "padding": "8px 10px",
    "fontSize": "0.82rem",
    "fontFamily": "'JetBrains Mono', 'Consolas', monospace",
    "minWidth": "50px",
}

TABLE_FILTER_STYLE = {
    "backgroundColor": cfg.card_color,
    "color": cfg.text_color,
    "border": f"1px solid {cfg.border_color}",
}

# ---------------------------------------------------------------------------
# Column definitions: DEFAULT (always shown) vs EXTRA (configurable)
# ---------------------------------------------------------------------------
DEFAULT_COLUMNS = [
    {"name": "Symbol", "id": "symbol"},
    {"name": "Signal", "id": "signal"},
    {"name": "Rec", "id": "recommendation"},
    {"name": "Phase", "id": "inst_phase"},
    {"name": "Conv.", "id": "inst_conviction", "type": "numeric"},
    {"name": "Swing", "id": "inst_swing"},
    {"name": "State", "id": "stock_state"},
    {"name": "Confirms", "id": "recommendation_confirms", "type": "numeric"},
    {"name": "Score", "id": "score", "type": "numeric"},
    {"name": "MTF", "id": "mtf_agreement"},
    {"name": "Price", "id": "price", "type": "numeric",
     "format": dash_table.FormatTemplate.money(2)},
    {"name": "R:R", "id": "rr_ratio", "type": "numeric"},
    {"name": "Stop", "id": "stop_loss", "type": "numeric",
     "format": dash_table.FormatTemplate.money(2)},
    {"name": "T1", "id": "target_1", "type": "numeric",
     "format": dash_table.FormatTemplate.money(2)},
    {"name": "T2", "id": "target_2", "type": "numeric",
     "format": dash_table.FormatTemplate.money(2)},
    {"name": "Trend", "id": "trend_direction"},
    {"name": "Conditions", "id": "trade_conditions", "presentation": "markdown"},
]

EXTRA_COLUMNS = [
    {"name": "RSI", "id": "rsi", "type": "numeric"},
    {"name": "ADX", "id": "adx", "type": "numeric"},
    {"name": "Vol", "id": "volume_ratio", "type": "numeric"},
    {"name": "VWAP", "id": "vwap_status"},
    {"name": "GEX", "id": "gex_status"},
    {"name": "RS", "id": "relative_strength", "type": "numeric"},
    {"name": "Regime", "id": "market_regime"},
    {"name": "Age", "id": "signal_age", "type": "numeric"},
    {"name": "\u0394Score", "id": "score_delta", "type": "numeric"},
    {"name": "Momentum", "id": "signal_momentum"},
    {"name": "Session", "id": "session_time"},
    {"name": "Sector", "id": "sector"},
    {"name": "Updated", "id": "last_updated"},
]

ALL_COLUMNS = DEFAULT_COLUMNS + EXTRA_COLUMNS

# IDs of extra columns (hidden by default)
EXTRA_COLUMN_IDS = [c["id"] for c in EXTRA_COLUMNS]

# Options for column picker dropdown
COLUMN_PICKER_OPTIONS = [
    {"label": c["name"], "value": c["id"]} for c in EXTRA_COLUMNS
]


def build_main_layout() -> html.Div:
    """Construct the main dashboard layout."""
    return html.Div(
        style={"backgroundColor": cfg.bg_color, "minHeight": "100vh"},
        children=[
            # Auto-refresh interval
            dcc.Interval(
                id="refresh-interval",
                interval=cfg.refresh_interval_ms,
                n_intervals=0,
            ),

            # Hidden stores
            dcc.Store(id="active-nav", data="nav-sniper-board"),
            dcc.Store(id="selected-ticker", data=None),
            dcc.Store(id="isr-previous-section", data="nav-sniper-board"),
            dcc.Store(id="mt-prefill-store", data=None),

            # ---- TOP NAVBAR ----
            dbc.Navbar(
                className="kb-navbar",
                dark=True,
                children=[
                    dbc.Container(
                        fluid=True,
                        children=[
                            dbc.NavbarBrand(
                                html.Div(
                                    [
                                        html.Div(
                                            "DRISHTI",
                                            style={
                                                "fontWeight": "800",
                                                "fontSize": "1.1rem",
                                                "letterSpacing": "0.15em",
                                                "color": "#fff",
                                                "fontFamily": "'JetBrains Mono', monospace",
                                                "lineHeight": "1",
                                            },
                                        ),
                                        html.Div(
                                            "Road to 10 Million",
                                            style={
                                                "fontSize": "0.62rem",
                                                "color": "#ffd43b",
                                                "letterSpacing": "0.10em",
                                                "marginTop": "2px",
                                                "fontWeight": "500",
                                                "textTransform": "uppercase",
                                            },
                                        ),
                                    ],
                                    style={"display": "flex", "flexDirection": "column"},
                                ),
                                className="me-auto",
                                style={"cursor": "default"},
                            ),
                            dbc.Nav(
                                [
                                    dbc.NavLink(
                                        [html.I(className="fas fa-calendar-week me-1"), "Swing (Multi-Day)"],
                                        id="nav-sniper-board",
                                        href="#", active=True,
                                    ),
                                    dbc.NavLink(
                                        [html.I(className="fas fa-bolt me-1"), "Intraday"],
                                        id="nav-intraday-sniper",
                                        href="#",
                                    ),
                                    # Hidden alias kept so legacy callbacks targeting nav-intraday-ml don't break.
                                    dbc.NavLink(id="nav-intraday-ml", href="#", style={"display": "none"}),
                                    # Drishti v1: hidden (revisit later).
                                    # Reversible — remove the style={"display":"none"} to bring back.
                                    dbc.NavLink(
                                        [html.I(className="fas fa-layer-group me-1"), "Options"],
                                        id="nav-options",
                                        href="#",
                                        style={"display": "none"},
                                    ),
                                    dbc.NavLink(
                                        [html.I(className="fas fa-chart-area me-1"), "Forecast"],
                                        id="nav-predictive",
                                        href="#",
                                        style={"display": "none"},
                                    ),
                                    dbc.NavLink(
                                        [html.I(className="fas fa-brain me-1"), "Intelligence"],
                                        id="nav-intelligence",
                                        href="#",
                                        style={"display": "none"},
                                    ),
                                    dbc.NavLink(
                                        [html.I(className="fas fa-chart-line me-1"), "P&L Ledger"],
                                        id="nav-performance",
                                        href="#",
                                    ),
                                ],
                                navbar=True,
                                className="me-auto",
                            ),
                            # Global search bar (replaces Research tab)
                            html.Div(
                                style={"display": "flex", "alignItems": "center",
                                       "gap": "6px", "marginRight": "16px"},
                                children=[
                                    dcc.Input(
                                        id="global-search-input",
                                        type="text",
                                        placeholder="Search ticker...",
                                        debounce=True,
                                        style={
                                            "width": "140px",
                                            "backgroundColor": cfg.card_color,
                                            "color": cfg.text_color,
                                            "border": f"1px solid {cfg.border_color}",
                                            "borderRadius": "20px",
                                            "padding": "4px 14px",
                                            "fontSize": "0.78rem",
                                        },
                                    ),
                                    html.Button(
                                        html.I(className="fas fa-search"),
                                        id="global-search-btn",
                                        n_clicks=0,
                                        style={
                                            "backgroundColor": "transparent",
                                            "border": "none",
                                            "color": cfg.accent_primary,
                                            "cursor": "pointer",
                                            "fontSize": "0.85rem",
                                        },
                                    ),
                                ],
                            ),
                            # Status indicators in navbar right side
                            html.Div(
                                className="kb-navbar-status",
                                children=[
                                    # IBKR connection status
                                    html.Div(
                                        id="ibkr-status-badge",
                                        style={
                                            "display": "inline-flex",
                                            "alignItems": "center",
                                            "gap": "4px",
                                            "padding": "2px 10px",
                                            "borderRadius": "12px",
                                            "fontSize": "0.72rem",
                                            "fontWeight": "600",
                                            "marginRight": "10px",
                                            "backgroundColor": "#1a0000",
                                            "border": "1px solid #ff4488",
                                            "color": "#ff4488",
                                        },
                                        children=[
                                            html.I(className="fas fa-plug-circle-xmark",
                                                   style={"fontSize": "0.65rem"}),
                                            html.Span("IBKR: CHECKING..."),
                                        ],
                                    ),
                                    html.Span(id="status-dot",
                                             className="kb-status-dot idle",
                                             style={"display": "none"}),
                                    html.Span(id="status-text",
                                              style={"display": "none"}),
                                    html.Div(id="data-source-badge",
                                             style={"display": "none"}),
                                    # Kill switch (hidden — kept for callback compat)
                                    html.Button(id="kill-switch-btn", n_clicks=0,
                                                style={"display": "none"}),
                                    dcc.ConfirmDialog(id="kill-confirm-dialog",
                                                      message=""),
                                    html.Div(id="kill-switch-dummy",
                                             style={"display": "none"}),
                                ],
                            ),
                        ],
                    ),
                ],
            ),

            # ---- LEGACY NAV STUBS (hidden, keep IDs for old callbacks) ----
            html.Div(style={"display": "none"}, children=[
                dbc.NavLink(id="nav-stock-ideas", href="#", style={"display": "none"}),
                dbc.NavLink(id="nav-options-ideas", href="#", style={"display": "none"}),
                dbc.NavLink(id="nav-research", href="#", style={"display": "none"}),
                dbc.NavLink(id="nav-paper", href="#", style={"display": "none"}),
                dbc.NavLink(id="nav-my-trades", href="#", style={"display": "none"}),
            ]),

            # ---- PAGE CONTENT WRAPPER ----
            html.Div(
                className="kb-page-content",
                children=[
                    # ============================================================
                    # DRISHTI v2 — HERO STATUS ROW (regime + Road to 10M)
                    # The first thing the eye lands on. Bigger, clearer than the
                    # legacy pill row that lives below (kept for status detail).
                    # ============================================================
                    html.Div(
                        className="dr-fade-in",
                        style={"display": "grid",
                               "gridTemplateColumns": "minmax(0, 2.4fr) minmax(0, 1fr)",
                               "gap": "14px",
                               "marginBottom": "16px"},
                        children=[
                            # Regime card (left, dominant)
                            html.Div(
                                id="dr-regime-card",
                                className="dr-regime-card regime-accumulate",
                                children=[
                                    html.Div(
                                        html.I(id="dr-regime-icon",
                                               className="ph-duotone ph-eye",
                                               style={"color": "#fbbf24"}),
                                        className="dr-regime-icon",
                                    ),
                                    html.Div(
                                        className="dr-regime-body",
                                        children=[
                                            html.Div("MARKET REGIME",
                                                     className="dr-regime-label"),
                                            html.Div("—",
                                                     id="dr-regime-state",
                                                     className="dr-regime-state"),
                                            html.Div("Loading…",
                                                     id="dr-regime-guidance",
                                                     className="dr-regime-guidance"),
                                        ],
                                    ),
                                    html.Div(
                                        id="dr-regime-chips",
                                        className="dr-regime-side-chips",
                                    ),
                                ],
                            ),
                            # Road to 10M equity tracker (right)
                            html.Div(
                                id="dr-equity-card",
                                className="dr-equity-card",
                                style={"flexDirection": "column",
                                       "alignItems": "stretch", "gap": "6px"},
                                children=[
                                    html.Div(
                                        style={"display": "flex",
                                               "justifyContent": "space-between",
                                               "alignItems": "baseline"},
                                        children=[
                                            html.Span("ROAD TO 10M",
                                                      className="dr-equity-label"),
                                            html.Span("—",
                                                      id="dr-equity-pct-of-goal",
                                                      className="dr-equity-goal"),
                                        ],
                                    ),
                                    html.Div(
                                        style={"display": "flex",
                                               "justifyContent": "space-between",
                                               "alignItems": "baseline"},
                                        children=[
                                            html.Span("$0",
                                                      id="dr-equity-value",
                                                      className="dr-equity-value"),
                                            html.Span("—",
                                                      id="dr-equity-delta",
                                                      style={"fontFamily": "var(--dr-font-mono)",
                                                             "fontSize": "0.78rem",
                                                             "color": "var(--dr-text-muted)"}),
                                        ],
                                    ),
                                    dcc.Graph(
                                        id="dr-equity-spark",
                                        config={"displayModeBar": False,
                                                "staticPlot": True},
                                        style={"height": "44px", "marginTop": "2px"},
                                    ),
                                ],
                            ),
                        ],
                    ),

                    # ---- LEGACY TERMINAL STATUS BAR (kept for status detail) ----
                    html.Div(
                        id="regime-banner",
                        className="kb-banner",
                        children=[
                            # LEFT: 4 status pills (regime + readiness + EOD + kill)
                            html.Div(
                                className="kb-banner-pills",
                                children=[
                                    # Regime pill — keeps id="regime-status" inside
                                    # for the existing scanner_status callback.
                                    html.Span(
                                        id="regime-pill",
                                        className="kb-status-pill",
                                        children=[
                                            html.Span(className="kb-pill-dot"),
                                            html.Span("REGIME", className="kb-pill-key"),
                                            html.Span("…", id="regime-status",
                                                      className="kb-pill-val"),
                                        ],
                                    ),
                                    html.Span(
                                        id="readiness-pill",
                                        className="kb-status-pill",
                                        children=[
                                            html.Span(className="kb-pill-dot"),
                                            html.Span("READY", className="kb-pill-key"),
                                            html.Span("READY",
                                                      id="readiness-pill-text",
                                                      className="kb-pill-val"),
                                        ],
                                    ),
                                    html.Span(
                                        id="eod-age-pill",
                                        className="kb-status-pill",
                                        children=[
                                            html.Span(className="kb-pill-dot"),
                                            html.Span("EOD", className="kb-pill-key"),
                                            html.Span("--",
                                                      id="eod-age-pill-text",
                                                      className="kb-pill-val"),
                                        ],
                                    ),
                                    html.Span(
                                        id="kill-switch-pill",
                                        className="kb-status-pill",
                                        children=[
                                            html.Span(className="kb-pill-dot"),
                                            html.Span("KILL", className="kb-pill-key"),
                                            html.Span("OFF",
                                                      id="kill-switch-pill-text",
                                                      className="kb-pill-val"),
                                        ],
                                    ),
                                ],
                            ),
                            # RIGHT: muted schedule strip — Trade · Entry · EOD eval · Swing
                            html.Div(
                                id="time-guard-banner",
                                className="kb-banner-schedule",
                                children=[
                                    html.Span([
                                        html.Span("Trade ", className="kb-meta-key"),
                                        html.Span("ACTIVE", id="trade-mode-status",
                                                  className="kb-meta-val"),
                                    ], className="kb-meta-item"),
                                    html.Span([
                                        html.Span("Entry ", className="kb-meta-key"),
                                        html.Span("3:30 PM", id="entry-cutoff-status",
                                                  className="kb-meta-val"),
                                    ], className="kb-meta-item"),
                                    html.Span([
                                        html.Span("EOD eval ", className="kb-meta-key"),
                                        html.Span("3:55 PM", id="eod-eval-status",
                                                  className="kb-meta-val"),
                                    ], className="kb-meta-item"),
                                    html.Span([
                                        html.Span("Swing ", className="kb-meta-key"),
                                        html.Span("0", id="swing-count-status",
                                                  className="kb-meta-val"),
                                    ], className="kb-meta-item"),
                                ],
                            ),
                            # Hidden legacy IDs kept so existing callbacks
                            # (which output to these targets) don't crash.
                            html.Span(id="time-guard-icon", style={"display": "none"}),
                            html.Span(id="regime-description", style={"display": "none"}),
                            html.Div(id="time-guard-detail", style={"display": "none"}),
                        ],
                    ),

                    # ==============================================================
                    # SECTION: LIVE SIGNALS (legacy stub — content moved to
                    # _build_live_signals_section sub-tabs)
                    # ==============================================================
                    html.Div(id="recommendations-section", hidden=True),

                    # ==============================================================
                    # SECTION: PAPER TRADING
                    # ==============================================================
                    html.Div(
                        id="paper-section",
                        hidden=True,
                        className="kb-animate-in",
                        children=[
                            html.Div(
                                className="kb-section-header",
                                children=[
                                    html.H2([
                                        "Paper Trading",
                                        html.Span("SIMULATION", className="kb-section-badge"),
                                    ]),
                                    html.P(
                                        "Track simulated positions and performance metrics",
                                        className="kb-section-desc",
                                    ),
                                ],
                            ),
                            dbc.Row(
                                className="mb-4 g-3",
                                children=[
                                    dcc.Store(id="paper-tile-filter-store", data={"status": None}),
                                    dbc.Col(_stat_card("paper-open", "OPEN", "0", cfg.text_color, "fa-folder-open", 1, clickable=True), md=2),
                                    dbc.Col(_stat_card("paper-closed", "CLOSED", "0", cfg.accent_primary, "fa-folder-closed", 2, clickable=True), md=2),
                                    dbc.Col(_stat_card("paper-win-rate", "WIN %", "0", "#00ff88", "fa-trophy", 3), md=2),
                                    dbc.Col(_stat_card("paper-pnl", "REALIZED P&L", "$0", "#ffd43b", "fa-coins", 4), md=2),
                                    dbc.Col(_stat_card("paper-equity", "EQUITY", "$5000", cfg.text_color, "fa-wallet", 5), md=2),
                                    dbc.Col(_stat_card("paper-swing-count", "SWING", "0", "#b388ff", "fa-moon", 6, clickable=True), md=2),
                                ],
                            ),
                            html.Div(
                                className="kb-card",
                                children=[
                                    dash_table.DataTable(
                                        id="paper-trades-table",
                                        columns=[
                                            {"name": "Open Time", "id": "opened_at"},
                                            {"name": "Symbol", "id": "symbol"},
                                            {"name": "Strategy", "id": "strategy_type"},
                                            {"name": "Exec", "id": "execution_mode"},
                                            {"name": "Mode", "id": "trade_mode"},
                                            {"name": "Days", "id": "days_held", "type": "numeric"},
                                            {"name": "Instrument", "id": "instrument_type"},
                                            {"name": "Opt Type", "id": "option_type"},
                                            {"name": "Opt Expiry", "id": "option_expiry"},
                                            {"name": "Opt Strike", "id": "option_strike", "type": "numeric"},
                                            {"name": "Side", "id": "side"},
                                            {"name": "Status", "id": "status"},
                                            {"name": "Entry", "id": "entry_price", "type": "numeric", "format": dash_table.FormatTemplate.money(2)},
                                            {"name": "Current", "id": "current_price", "type": "numeric", "format": dash_table.FormatTemplate.money(2)},
                                            {"name": "Exit", "id": "exit_price", "type": "numeric", "format": dash_table.FormatTemplate.money(2)},
                                            {"name": "Qty", "id": "quantity", "type": "numeric"},
                                            {"name": "Exit Reason", "id": "exit_reason"},
                                            {"name": "Unrealized", "id": "unrealized_pnl", "type": "numeric"},
                                            {"name": "P&L", "id": "realized_pnl", "type": "numeric"},
                                            {"name": "P&L %", "id": "realized_pnl_pct", "type": "numeric"},
                                        ],
                                        data=[],
                                        page_size=10,
                                        sort_action="native",
                                        style_table={"overflowX": "auto"},
                                        style_header=TABLE_HEADER_STYLE,
                                        style_cell=TABLE_CELL_STYLE,
                                        style_data_conditional=[
                                            {"if": {"column_id": "symbol"}, "color": "#4da3ff", "cursor": "pointer", "fontWeight": "700", "textDecoration": "underline"},
                                            {"if": {"filter_query": '{side} = "LONG"', "column_id": "side"}, "color": cfg.accent_long, "fontWeight": "bold"},
                                            {"if": {"filter_query": '{side} = "SHORT"', "column_id": "side"}, "color": cfg.accent_short, "fontWeight": "bold"},
                                            {"if": {"filter_query": '{status} = "OPEN"', "column_id": "status"}, "color": cfg.accent_neutral},
                                            {"if": {"filter_query": '{status} = "CLOSED"', "column_id": "status"}, "color": "#aaa"},
                                            {"if": {"filter_query": "{unrealized_pnl} > 0", "column_id": "unrealized_pnl"}, "color": cfg.accent_long},
                                            {"if": {"filter_query": "{unrealized_pnl} < 0", "column_id": "unrealized_pnl"}, "color": cfg.accent_short},
                                            {"if": {"filter_query": "{realized_pnl} > 0", "column_id": "realized_pnl"}, "color": cfg.accent_long},
                                            {"if": {"filter_query": "{realized_pnl} < 0", "column_id": "realized_pnl"}, "color": cfg.accent_short},
                                            {"if": {"filter_query": "{realized_pnl_pct} > 0", "column_id": "realized_pnl_pct"}, "color": cfg.accent_long},
                                            {"if": {"filter_query": "{realized_pnl_pct} < 0", "column_id": "realized_pnl_pct"}, "color": cfg.accent_short},
                                            # Trade mode: SWING highlight
                                            {"if": {"filter_query": '{trade_mode} = "SWING"', "column_id": "trade_mode"}, "color": "#b388ff", "fontWeight": "bold", "backgroundColor": "#1a0d2e"},
                                            {"if": {"filter_query": '{trade_mode} = "DAY"', "column_id": "trade_mode"}, "color": cfg.accent_primary},
                                            # EOD exit reasons
                                            {"if": {"filter_query": '{exit_reason} = "EOD_WEAK_CLOSE"', "column_id": "exit_reason"}, "color": cfg.accent_neutral},
                                            {"if": {"filter_query": '{exit_reason} = "SWING_MAX_HOLD_EXCEEDED"', "column_id": "exit_reason"}, "color": "#b388ff"},
                                            {"if": {"filter_query": '{exit_reason} = "EOD_NO_DATA"', "column_id": "exit_reason"}, "color": "#ff6b6b"},
                                            # Execution mode: LIVE = green badge, SIM = muted grey
                                            {"if": {"filter_query": '{execution_mode} = "LIVE"', "column_id": "execution_mode"}, "color": "#00ff88", "fontWeight": "bold"},
                                            {"if": {"filter_query": '{execution_mode} = "SIM"', "column_id": "execution_mode"}, "color": "#888"},
                                            # Strategy type highlights
                                            {"if": {"filter_query": '{strategy_type} = "VWAP_MR"', "column_id": "strategy_type"}, "color": "#4da3ff", "fontWeight": "bold"},
                                            {"if": {"filter_query": '{strategy_type} = "SCANNER_MTF"', "column_id": "strategy_type"}, "color": cfg.accent_primary},
                                            {"if": {"filter_query": '{strategy_type} = "MANUAL"', "column_id": "strategy_type"}, "color": "#ffd43b"},
                                            # LIVE row left border highlight
                                            {"if": {"filter_query": '{execution_mode} = "LIVE"'}, "borderLeft": "3px solid #00ff88"},
                                            # SWING row left border highlight
                                            {"if": {"filter_query": '{trade_mode} = "SWING"'}, "borderLeft": "3px solid #b388ff"},
                                        ],
                                    ),
                                ],
                            ),
                        ],
                    ),

                    # ==============================================================
                    # SECTION: OPTION SETUPS
                    # ==============================================================
                    html.Div(
                        id="options-section",
                        hidden=True,
                        className="kb-animate-in",
                        children=[
                            html.Div(
                                className="kb-section-header",
                                style={"display": "flex", "alignItems": "center", "justifyContent": "space-between"},
                                children=[
                                    html.Div([
                                        html.H2([
                                            "Option Setups",
                                            html.Span("CONTRACT IDEAS", className="kb-section-badge"),
                                        ]),
                                        html.P(
                                            "Curated option ideas based on signal confluence",
                                            className="kb-section-desc",
                                        ),
                                    ]),
                                    html.Div(
                                        [
                                            html.Span(
                                                "Last Refresh: ",
                                                className="kb-label",
                                            ),
                                            html.Span(
                                                "Waiting for first scan",
                                                id="option-last-refresh",
                                                style={"color": cfg.text_color, "fontSize": "0.78rem"},
                                            ),
                                            html.Span("  |  ", style={"color": cfg.border_color, "margin": "0 8px"}),
                                            html.Span(
                                                "Next ETA: ",
                                                className="kb-label",
                                            ),
                                            html.Span(
                                                "--",
                                                id="option-next-refresh",
                                                style={"color": cfg.accent_primary, "fontSize": "0.78rem", "fontWeight": "bold"},
                                            ),
                                        ],
                                    ),
                                ],
                            ),
                            # ---- Option idea stat tiles ----
                            dcc.Store(id="option-tile-filter-store", data={"state": None}),
                            dbc.Row(
                                className="mb-3 g-3",
                                children=[
                                    dbc.Col(_stat_card("opt-active", "ACTIVE", "0", cfg.accent_long, "fa-fire", 1, clickable=True), md=2),
                                    dbc.Col(_stat_card("opt-strong", "STRONG", "0", "#00ff88", "fa-star", 2, clickable=True), md=2),
                                    dbc.Col(_stat_card("opt-watching", "WATCHING", "0", cfg.accent_primary, "fa-eye", 3, clickable=True), md=2),
                                    dbc.Col(_stat_card("opt-invalid", "INVALID", "0", "#ff4488", "fa-circle-xmark", 4, clickable=True), md=2),
                                    dbc.Col(_stat_card("opt-taken", "TAKEN", "0", "#ffd43b", "fa-hand-pointer", 5, clickable=True), md=2),
                                    dbc.Col(_stat_card("opt-all", "ALL IDEAS", "0", cfg.text_muted, "fa-list", 6, clickable=True), md=2),
                                ],
                            ),
                            html.Div(
                                className="kb-card",
                                children=[
                                    dash_table.DataTable(
                                        id="option-setups-table",
                                        columns=[
                                            {"name": "ID", "id": "id", "editable": False},
                                            {"name": "Symbol", "id": "symbol", "editable": False},
                                            {"name": "Confirms", "id": "confirm_count", "type": "numeric", "editable": False},
                                            {"name": "State", "id": "idea_state", "editable": False},
                                            {"name": "Taken", "id": "taken_flag", "presentation": "dropdown", "editable": True},
                                            {"name": "Type", "id": "option_type", "editable": False},
                                            {"name": "Expiry", "id": "expiry_date", "editable": False},
                                            {"name": "Strike", "id": "strike", "type": "numeric", "editable": False},
                                            {"name": "Underlying", "id": "underlying_price", "type": "numeric", "format": dash_table.FormatTemplate.money(2), "editable": False},
                                            {"name": "Rec", "id": "recommendation", "editable": False},
                                            {"name": "Signal", "id": "signal", "editable": False},
                                            {"name": "Idea Score", "id": "score", "type": "numeric", "editable": False},
                                            {"name": "Current Score", "id": "current_score", "type": "numeric", "editable": False},
                                            {"name": "R:R", "id": "rr_ratio", "type": "numeric", "editable": False},
                                            {"name": "Regime", "id": "market_regime", "editable": False},
                                            {"name": "GEX", "id": "gex_status", "editable": False},
                                            {"name": "Updated", "id": "updated_ts", "editable": False},
                                            {"name": "Phase", "id": "inst_phase", "editable": False},
                                            {"name": "Conviction", "id": "inst_conviction", "type": "numeric", "editable": False},
                                            {"name": "Swing Sig", "id": "inst_swing", "editable": False},
                                            {"name": "State Reason", "id": "invalid_reason", "editable": False},
                                            {"name": "Rationale", "id": "rationale", "editable": False},
                                            {"name": "Created (ET)", "id": "created_ts", "editable": False},
                                            {"name": "Last Validated (ET)", "id": "last_validated_ts", "editable": False},
                                            {"name": "Freshness", "id": "updated_flag", "editable": False},
                                        ],
                                        hidden_columns=["updated_flag", "id"],
                                        data=[],
                                        editable=True,
                                        dropdown={
                                            "taken_flag": {
                                                "options": [
                                                    {"label": "NO", "value": "NO"},
                                                    {"label": "YES", "value": "YES"},
                                                ]
                                            }
                                        },
                                        page_size=12,
                                        sort_action="native",
                                        style_table={"overflowX": "auto"},
                                        style_header=TABLE_HEADER_STYLE,
                                        style_cell=TABLE_CELL_STYLE,
                                        style_data_conditional=[
                                            {"if": {"column_id": "symbol"}, "color": "#4da3ff", "cursor": "pointer", "fontWeight": "700", "textDecoration": "underline"},
                                            {"if": {"filter_query": '{option_type} = "CALL"', "column_id": "option_type"}, "color": cfg.accent_long, "fontWeight": "bold"},
                                            {"if": {"filter_query": '{option_type} = "PUT"', "column_id": "option_type"}, "color": cfg.accent_short, "fontWeight": "bold"},
                                            {"if": {"filter_query": '{recommendation} = "BUY"', "column_id": "recommendation"}, "color": cfg.accent_long},
                                            {"if": {"filter_query": '{recommendation} = "SELL"', "column_id": "recommendation"}, "color": cfg.accent_short},
                                            {"if": {"filter_query": '{idea_state} = "NEW"', "column_id": "idea_state"}, "color": cfg.accent_primary, "fontWeight": "bold"},
                                            {"if": {"filter_query": '{idea_state} = "STRONG"', "column_id": "idea_state"}, "color": "#00ff88", "fontWeight": "bold"},
                                            {"if": {"filter_query": '{idea_state} = "ACTIVE"', "column_id": "idea_state"}, "color": cfg.accent_long, "fontWeight": "bold"},
                                            {"if": {"filter_query": '{idea_state} = "WEAKENING"', "column_id": "idea_state"}, "color": cfg.accent_neutral, "fontWeight": "bold"},
                                            {"if": {"filter_query": '{idea_state} = "INVALID"', "column_id": "idea_state"}, "color": cfg.accent_short, "fontWeight": "bold"},
                                            {"if": {"filter_query": '{taken_flag} = "YES"', "column_id": "taken_flag"}, "color": cfg.accent_primary, "fontWeight": "bold"},
                                            {"if": {"filter_query": '{updated_flag} = "TODAY"', "column_id": "updated_ts"}, "color": cfg.accent_long, "fontWeight": "bold"},
                                            {"if": {"filter_query": '{updated_flag} = "RECENT"', "column_id": "updated_ts"}, "color": cfg.text_color},
                                            {"if": {"filter_query": '{updated_flag} = "STALE"', "column_id": "updated_ts"}, "color": cfg.text_color},
                                            # Intelligence column coloring
                                            {"if": {"filter_query": '{inst_phase} = "ACTIVE ACCUM"', "column_id": "inst_phase"}, "color": "#00c896", "fontWeight": "600"},
                                            {"if": {"filter_query": '{inst_phase} = "EARLY ACCUM"', "column_id": "inst_phase"}, "color": "#4dc9ff"},
                                            {"if": {"filter_query": '{inst_phase} = "LATE ACCUM"', "column_id": "inst_phase"}, "color": "#f5a623"},
                                            {"if": {"filter_query": '{inst_phase} = "EXPANSION"', "column_id": "inst_phase"}, "color": "#a78bfa"},
                                            {"if": {"filter_query": '{inst_phase} = "DISTRIBUTION"', "column_id": "inst_phase"}, "color": "#e05252"},
                                            {"if": {"filter_query": '{inst_swing} = "BUY"', "column_id": "inst_swing"}, "color": "#00c896", "fontWeight": "600"},
                                            {"if": {"filter_query": '{inst_swing} = "WATCH"', "column_id": "inst_swing"}, "color": "#f5a623"},
                                            {"if": {"filter_query": '{inst_swing} = "AVOID"', "column_id": "inst_swing"}, "color": "#e05252"},
                                            {"if": {"filter_query": '{inst_conviction} >= 70', "column_id": "inst_conviction"}, "color": "#00c896", "fontWeight": "600"},
                                            {"if": {"filter_query": '{inst_conviction} >= 45 && {inst_conviction} < 70', "column_id": "inst_conviction"}, "color": "#f5a623"},
                                            {"if": {"filter_query": '{inst_conviction} < 45', "column_id": "inst_conviction"}, "color": "#e05252"},
                                        ],
                                    ),
                                    html.Div(
                                        id="option-save-status",
                                        className="mt-2",
                                        style={"color": cfg.text_muted, "fontSize": "0.78rem"},
                                    ),
                                ],
                            ),
                        ],
                    ),

                    # ==============================================================
                    # SECTION: EOD REVIEW (legacy stub — content moved to
                    # _build_live_signals_section sub-tabs)
                    # ==============================================================
                    html.Div(id="eod-section", hidden=True),

                    # ==============================================================
                    # SECTION: ASK KUBERA (legacy — kept for callback IDs)
                    # TradeGPT replaces this; use the floating panel or ISR chat
                    # ==============================================================
                    html.Div(
                        id="ask-kubera-section",
                        hidden=True,
                        children=[
                            # Keep IDs so kubera_callbacks.py doesn't crash
                            dcc.Input(id="ask-kubera-symbol", type="hidden", value=""),
                            dbc.Button(id="ask-kubera-run", style={"display": "none"}, n_clicks=0),
                            html.Div(id="ask-kubera-status", style={"display": "none"}),
                            html.Div(id="ask-kubera-quick-summary", style={"display": "none"}),
                            dcc.Markdown(id="ask-kubera-report", style={"display": "none"}),
                        ],
                    ),

                    # ---- INTELLIGENCE SECTION ----
                    _build_intelligence_section(),

                    # ---- SNIPER BOARD SECTION (consolidated trade ideas) ----
                    _build_sniper_board_section(),

                    # ---- LIVE SCANNER SECTION (wraps Scanner + AI Signals + Intraday ML + EOD) ----
                    _build_live_signals_section(),

                    # ---- FORECAST SECTION (predictive — model-driven outlook) ----
                    _build_forecast_section(),

                    # ---- PERFORMANCE SECTION (merged Paper + My Trades) ----
                    _build_performance_section(),

                    # ---- LEGACY SECTIONS (hidden, but fully rendered for callbacks) ----
                    _build_stock_ideas_section_legacy(),
                    _build_options_ideas_section_legacy(),
                    html.Div(id="research-section", hidden=True, children=[
                        # Research inputs (referenced by stock_report_callbacks)
                        dcc.Input(id="research-ticker-input", type="hidden", value=""),
                        html.Button(id="research-search-btn", style={"display": "none"}, n_clicks=0),
                        html.Span(id="research-quarter-badge", style={"display": "none"}),
                        html.Div(id="research-isr-content", style={"display": "none"}),
                        dcc.Loading(id="research-loading", children=html.Div()),
                    ]),
                    html.Div(id="my-trades-section", hidden=True),
                    # Old paper-section stays in place (above) for backward compat

                    # ---- INDIVIDUAL STOCK REPORT (hidden until ticker selected) ----
                    _build_stock_report_section(),

                    # ---- DETAIL VIEW CONTAINER (hidden by default) ----
                    html.Div(id="detail-view-container", style={"display": "none"}),

                    # ---- Stub for back-to-table-btn (populated by callback) ----
                    html.Button(id="back-to-table-btn", style={"display": "none"}),
                ],
            ),

            # ── TRADEGPT SIDEBAR ───────────────────────────────────────────────
            # Full-height right sidebar, accessible from any tab
            html.Div(
                id="tradegpt-floating-btn",
                n_clicks=0,
                style={
                    "position": "fixed", "bottom": "24px", "right": "24px",
                    "width": "56px", "height": "56px", "borderRadius": "50%",
                    "backgroundColor": cfg.accent_primary, "color": "#fff",
                    "display": "flex", "alignItems": "center", "justifyContent": "center",
                    "cursor": "pointer", "zIndex": "9999",
                    "boxShadow": "0 4px 20px rgba(88,166,255,0.35)",
                    "fontSize": "1.3rem", "transition": "all 0.3s",
                },
                children=[
                    html.I(className="fas fa-robot", style={"marginRight": "0px"}),
                ],
            ),
            html.Div(
                id="tradegpt-floating-panel",
                style={
                    "position": "fixed", "top": "0", "right": "0",
                    "width": "440px", "height": "100vh",
                    "backgroundColor": cfg.bg_color,
                    "borderLeft": f"1px solid {cfg.border_color}",
                    "zIndex": "9998",
                    "boxShadow": "-4px 0 24px rgba(0,0,0,0.4)",
                    "display": "none",
                    "flexDirection": "column",
                    "transition": "transform 0.3s ease",
                },
                children=[
                    # Header
                    html.Div(
                        style={
                            "padding": "14px 18px",
                            "borderBottom": f"1px solid {cfg.border_color}",
                            "background": "rgba(0,0,0,0.2)",
                        },
                        children=[
                            html.Div(
                                style={"display": "flex", "alignItems": "center",
                                       "justifyContent": "space-between", "marginBottom": "10px"},
                                children=[
                                    html.Div([
                                        html.I(className="fas fa-robot me-2",
                                               style={"color": cfg.accent_primary, "fontSize": "1.1rem"}),
                                        html.Span("TradeGPT",
                                                  style={"fontWeight": "700", "color": cfg.text_color,
                                                         "fontSize": "1.05rem", "letterSpacing": "0.02em"}),
                                        html.Span(" Intelligence Chat",
                                                  style={"color": cfg.text_muted, "fontSize": "0.78rem",
                                                         "marginLeft": "6px"}),
                                    ]),
                                    html.Div(
                                        id="tradegpt-close-btn",
                                        n_clicks=0,
                                        style={"cursor": "pointer", "color": cfg.text_muted,
                                               "fontSize": "1.1rem", "padding": "2px 6px"},
                                        children=html.I(className="fas fa-times"),
                                    ),
                                ],
                            ),
                            # Ticker input row
                            html.Div(
                                style={"display": "flex", "gap": "8px", "alignItems": "center"},
                                children=[
                                    dbc.Input(
                                        id="tradegpt-float-ticker",
                                        type="text",
                                        placeholder="Enter ticker...",
                                        style={
                                            "flex": "1", "textTransform": "uppercase",
                                            "backgroundColor": cfg.card_color,
                                            "color": cfg.text_color, "fontSize": "0.85rem",
                                            "border": f"1px solid {cfg.border_color}",
                                            "padding": "6px 12px", "borderRadius": "6px",
                                        },
                                    ),
                                    html.Div(
                                        id="tradegpt-float-status",
                                        style={"fontSize": "0.72rem", "color": cfg.accent_primary},
                                    ),
                                ],
                            ),
                        ],
                    ),
                    # Quick action buttons
                    html.Div(
                        style={"padding": "8px 14px", "display": "flex", "gap": "5px",
                               "flexWrap": "wrap",
                               "borderBottom": f"1px solid {cfg.border_color}"},
                        children=[
                            html.Button(
                                label, id={"type": "tradegpt-quick", "index": idx},
                                className="btn btn-outline-primary btn-sm",
                                style={"fontSize": "0.68rem", "borderColor": cfg.accent_primary,
                                       "color": cfg.accent_primary, "padding": "2px 8px",
                                       "borderRadius": "12px"},
                            )
                            for idx, label in enumerate([
                                "Full Analysis", "Entry Setup",
                                "Risk Check", "Options Play",
                            ])
                        ],
                    ),
                    # Messages area — fills remaining height
                    html.Div(
                        id="tradegpt-float-messages",
                        style={
                            "flex": "1", "overflowY": "auto",
                            "padding": "14px",
                        },
                        children=[
                            html.Div(
                                "Enter a ticker and ask anything about its institutional flows, insider activity, ML signals, or trade setups.",
                                style={"color": "#666", "fontSize": "0.82rem",
                                       "fontStyle": "italic", "lineHeight": "1.5"},
                            ),
                        ],
                    ),
                    # Input area — pinned to bottom
                    html.Div(
                        style={
                            "padding": "12px 14px",
                            "borderTop": f"1px solid {cfg.border_color}",
                            "background": "rgba(0,0,0,0.15)",
                        },
                        children=dbc.InputGroup([
                            dbc.Input(
                                id="tradegpt-float-input",
                                type="text",
                                placeholder="Ask about this stock...",
                                debounce=False,
                                style={
                                    "backgroundColor": cfg.card_color,
                                    "color": cfg.text_color,
                                    "border": f"1px solid {cfg.border_color}",
                                    "fontSize": "0.85rem",
                                },
                            ),
                            dcc.Loading(
                                id="tradegpt-float-loading",
                                type="dot",
                                color=cfg.accent_primary,
                                children=dbc.Button(
                                    [html.I(className="fas fa-paper-plane")],
                                    id="tradegpt-float-send",
                                    color="primary",
                                    style={
                                        "background": cfg.accent_primary,
                                        "border": "none",
                                    },
                                ),
                            ),
                        ]),
                    ),
                ],
            ),
        ],
    )


def _build_sniper_board_section():
    """Lazily import and build the Sniper Board layout."""
    from signal_scanner.dashboard.layouts.sniper_board_view import build_sniper_board_layout
    return build_sniper_board_layout()


def _build_performance_section():
    """Lazily import and build the Performance layout."""
    from signal_scanner.dashboard.layouts.performance_view import build_performance_layout
    return build_performance_layout()


def _build_forecast_section():
    """Lazily import and build the Forecast layout."""
    from signal_scanner.dashboard.layouts.forecast_view import build_forecast_layout
    return build_forecast_layout()


def _build_stock_ideas_section_legacy():
    """Render full Stock Ideas layout (hidden) so callback IDs exist."""
    from signal_scanner.dashboard.layouts.reports_view import build_stock_ideas_layout
    layout = build_stock_ideas_layout()
    layout.hidden = True
    return layout


def _build_options_ideas_section_legacy():
    """Render full Options Ideas layout (hidden) so callback IDs exist."""
    from signal_scanner.dashboard.layouts.reports_view import build_options_ideas_layout
    layout = build_options_ideas_layout()
    layout.hidden = True
    return layout


def _build_live_signals_section():
    """Build Intraday section with sub-tabs: Signals, Convergence, Intraday ML, Intraday Sniper, Options Board, EOD Review."""
    return html.Div(
        id="live-signals-section",
        hidden=True,
        className="kb-animate-in",
        children=[
            html.Div(
                className="kb-section-header",
                children=[
                    # Header changes dynamically based on which nav tab was clicked
                    html.H2(id="intraday-section-title", children=[
                        "Intraday",
                        html.Span("LIVE", className="kb-section-badge"),
                        rules_tooltip("intraday"),
                    ]),
                    html.P(
                        id="intraday-section-desc",
                        children="Intraday confluence + ML strategies (15-min cycle)",
                        className="kb-section-desc",
                    ),
                ],
            ),
            # Visible sub-tabs — Confluence vs ML.
            # Backed by the hidden ls-tabs controller below; clicks here
            # propagate via a callback in callbacks.py.
            dbc.Tabs(
                id="intraday-subtabs",
                active_tab="sub-ml",  # Drishti v1: ML is the default + only visible tab
                className="mb-3",
                children=[
                    # Confluence (Sniper) hidden — vestigial surface, nothing populates it.
                    # Reversible: remove tab_style to bring back.
                    dbc.Tab(label="Confluence (Sniper)", tab_id="sub-confluence",
                            tab_style={"display": "none"}),
                    dbc.Tab(label="ML (VWAP_MR / FPB / ORB_V2)", tab_id="sub-ml"),
                ],
            ),
            # Hidden tabs controller — keeps existing show/hide logic working.
            # Sync-via-callback wires intraday-subtabs -> ls-tabs.value.
            dcc.Tabs(
                id="ls-tabs",
                value="tab-intraday-sniper",
                style={"display": "none"},
                children=[
                    dcc.Tab(label="Scanner", value="tab-scanner"),
                    dcc.Tab(label="Convergence", value="tab-ai-signals"),
                    dcc.Tab(label="Intraday ML", value="tab-intraday-ml"),
                    dcc.Tab(label="Intraday Sniper", value="tab-intraday-sniper"),
                    dcc.Tab(label="Options Board", value="tab-options-flow"),
                    dcc.Tab(label="EOD Review", value="tab-eod-review"),
                ],
            ),
            # ============================================================
            # Scanner sub-tab — full scanner content
            # ============================================================
            html.Div(id="ls-scanner-container", children=[
                # ---- FILTER ROW ----
                dbc.Row(
                    className="mb-4 g-3",
                    children=[
                        dbc.Col([
                            html.Label("Watchlist", className="kb-label", style={"marginBottom": "4px", "display": "block"}),
                            dcc.Dropdown(
                                id="watchlist-dropdown",
                                className="dash-dropdown",
                                options=[],
                                value=None,
                                placeholder="Select watchlist...",
                                clearable=False,
                            ),
                        ], md=2),
                        dbc.Col([
                            html.Label("Signal", className="kb-label", style={"marginBottom": "4px", "display": "block"}),
                            dcc.Dropdown(
                                id="signal-filter",
                                className="dash-dropdown",
                                options=[
                                    {"label": "All", "value": "ALL"},
                                    {"label": "LONG", "value": "LONG"},
                                    {"label": "SHORT", "value": "SHORT"},
                                    {"label": "NEUTRAL", "value": "NEUTRAL"},
                                ],
                                value="ALL",
                                clearable=False,
                            ),
                        ], md=2),
                        dbc.Col([
                            html.Label("Min Score", className="kb-label", style={"marginBottom": "4px", "display": "block"}),
                            dcc.Slider(
                                id="score-slider",
                                min=0, max=100, step=5, value=0,
                                marks={i: {"label": str(i), "style": {"color": cfg.text_muted}}
                                       for i in range(0, 101, 25)},
                                tooltip={"placement": "bottom"},
                            ),
                        ], md=3),
                        dbc.Col([
                            html.Label("Sector", className="kb-label", style={"marginBottom": "4px", "display": "block"}),
                            dcc.Dropdown(
                                id="sector-filter",
                                className="dash-dropdown",
                                options=[],
                                value=None,
                                multi=True,
                                clearable=False,
                                placeholder="All sectors",
                            ),
                        ], md=2),
                        dbc.Col([
                            html.Label("Extra Columns", className="kb-label", style={"marginBottom": "4px", "display": "block"}),
                            dcc.Dropdown(
                                id="column-picker",
                                className="dash-dropdown",
                                options=COLUMN_PICKER_OPTIONS,
                                value=[],
                                multi=True,
                                clearable=False,
                                placeholder="Add columns...",
                            ),
                        ], md=3),
                    ],
                ),

                html.Div(
                    id="active-filters-hint",
                    className="mb-2",
                    style={
                        "color": cfg.text_muted,
                        "fontSize": "0.78rem",
                        "paddingLeft": "4px",
                    },
                ),
                html.Div(
                    className="mb-3",
                    style={"paddingLeft": "4px"},
                    children=[
                        dcc.Checklist(
                            id="price-cap-filter",
                            options=[{"label": "Price <= $10 only", "value": "LE10"}],
                            value=[],
                            inputStyle={"marginRight": "6px", "marginLeft": "0px"},
                            labelStyle={"color": cfg.text_muted, "fontSize": "0.78rem", "display": "inline-block"},
                            style={"color": cfg.text_color},
                        )
                    ],
                ),

                # ---- STATS CARDS ----
                dcc.Store(id="tile-filter-store", data={"signal": "ALL", "rec": None}),
                dbc.Row(
                    className="mb-4 g-3",
                    children=[
                        dbc.Col(_stat_card("stats-total", "SIGNALS", "0", cfg.text_color, "fa-satellite-dish", 1, clickable=True), md=2),
                        dbc.Col(_stat_card("stats-long", "LONG", "0", cfg.accent_long, "fa-arrow-trend-up", 2, clickable=True), md=2),
                        dbc.Col(_stat_card("stats-short", "SHORT", "0", cfg.accent_short, "fa-arrow-trend-down", 3, clickable=True), md=2),
                        dbc.Col(_stat_card("stats-avg", "AVG SCORE", "0", cfg.accent_primary, "fa-gauge-high", 4), md=2),
                        dbc.Col(_stat_card("stats-buy", "BUY", "0", "#00ff88", "fa-circle-check", 5, clickable=True), md=2),
                        dbc.Col(_stat_card("stats-sell", "SELL", "0", "#ff4488", "fa-circle-xmark", 6, clickable=True), md=2),
                    ],
                ),

                # ---- QUALIFIED BUY/SELL ----
                html.Div(
                    className="kb-card mb-4",
                    children=[
                        html.H4(
                            "Criteria-Met Recommendations",
                            style={"color": cfg.accent_primary, "marginBottom": "4px"},
                        ),
                        html.P(
                            "Signals meeting BUY/SELL quality thresholds",
                            className="kb-section-desc",
                            style={"marginBottom": "12px"},
                        ),
                        dash_table.DataTable(
                            id="qualified-recs-table",
                            columns=[
                                {"name": "Symbol", "id": "symbol"},
                                {"name": "Rec", "id": "recommendation"},
                                {"name": "Signal", "id": "signal"},
                                {"name": "Score", "id": "score", "type": "numeric"},
                                {"name": "Price", "id": "price", "type": "numeric", "format": dash_table.FormatTemplate.money(2)},
                                {"name": "R:R", "id": "rr_ratio", "type": "numeric"},
                                {"name": "GEX", "id": "gex_status"},
                                {"name": "Regime", "id": "market_regime"},
                                {"name": "Updated", "id": "last_updated"},
                            ],
                            data=[],
                            page_size=8,
                            sort_action="native",
                            style_table={"overflowX": "auto"},
                            style_header=TABLE_HEADER_STYLE,
                            style_cell=TABLE_CELL_STYLE,
                            style_data_conditional=[
                                {"if": {"filter_query": '{recommendation} = "BUY"', "column_id": "recommendation"}, "color": cfg.accent_long, "fontWeight": "bold"},
                                {"if": {"filter_query": '{recommendation} = "SELL"', "column_id": "recommendation"}, "color": cfg.accent_short, "fontWeight": "bold"},
                            ],
                        ),
                    ],
                ),

                # ---- SIGNAL TABLE ----
                html.Div(
                    className="kb-card mb-4",
                    children=[
                        dash_table.DataTable(
                            id="signal-table",
                            columns=ALL_COLUMNS,
                            hidden_columns=EXTRA_COLUMN_IDS,
                            data=[],
                            sort_action="native",
                            sort_mode="single",
                            sort_by=[{"column_id": "score", "direction": "desc"}],
                            filter_action="native",
                            page_size=40,
                            tooltip_header={
                                c["id"]: {"value": COLUMN_TOOLTIPS.get(c["id"], c.get("name", "")), "type": "text"}
                                for c in ALL_COLUMNS
                            },
                            tooltip_delay=300,
                            tooltip_duration=None,
                            css=[{"selector": ".show-hide", "rule": "display: none"}],
                            style_table={"overflowX": "auto"},
                            style_header=TABLE_HEADER_STYLE,
                            style_cell=TABLE_CELL_STYLE,
                            style_data_conditional=_build_conditional_styles(),
                            style_filter=TABLE_FILTER_STYLE,
                        ),
                    ],
                ),

                # ---- RECOMMENDATION HISTORY ----
                html.Div(
                    className="kb-card mb-4",
                    children=[
                        html.H4(
                            "Recommendation History",
                            style={"color": cfg.accent_primary, "marginBottom": "4px"},
                        ),
                        html.P(
                            "Persisted signal history with timestamps",
                            className="kb-section-desc",
                            style={"marginBottom": "12px"},
                        ),
                        dash_table.DataTable(
                            id="recommendation-history-table",
                            columns=[
                                {"name": "Time (UTC)", "id": "timestamp"},
                                {"name": "Symbol", "id": "symbol"},
                                {"name": "TF", "id": "timeframe"},
                                {"name": "Rec", "id": "recommendation"},
                                {"name": "Signal", "id": "signal"},
                                {"name": "Score", "id": "score", "type": "numeric"},
                                {"name": "Price", "id": "price", "type": "numeric", "format": dash_table.FormatTemplate.money(2)},
                                {"name": "R:R", "id": "rr_ratio", "type": "numeric"},
                                {"name": "GEX", "id": "gex_status"},
                                {"name": "Regime", "id": "market_regime"},
                            ],
                            data=[],
                            page_size=12,
                            sort_action="native",
                            style_table={"overflowX": "auto"},
                            style_header=TABLE_HEADER_STYLE,
                            style_cell=TABLE_CELL_STYLE,
                            style_data_conditional=[
                                {"if": {"filter_query": '{recommendation} = "BUY"', "column_id": "recommendation"}, "color": cfg.accent_long, "fontWeight": "bold"},
                                {"if": {"filter_query": '{recommendation} = "SELL"', "column_id": "recommendation"}, "color": cfg.accent_short, "fontWeight": "bold"},
                                {"if": {"filter_query": '{recommendation} = "HOLD"', "column_id": "recommendation"}, "color": "#888"},
                            ],
                        ),
                    ],
                ),

                # ---- SCANNER STATUS BAR ----
                html.Div(
                    className="kb-card",
                    children=[
                        html.Div(
                            id="scanner-status-panel",
                            style={"color": cfg.text_muted, "fontSize": "0.78rem"},
                        ),
                    ],
                ),
            ]),

            # ============================================================
            # AI Signals sub-tab
            # ============================================================
            html.Div(id="ls-ai-container", hidden=True, children=[
                html.Div(className="kb-card mb-4", children=[
                    html.H4("Convergence Signals (Diagnostic)", style={"color": cfg.accent_primary, "marginBottom": "4px"}),
                    html.P([
                        "Diagnostic view — raw confirmation signals before Sniper Board consolidation. ",
                        html.Strong("Not a decision surface. "),
                        "Trade ideas are on the Sniper Board, where these signals appear as convergence badges.",
                    ], className="kb-section-desc", style={"marginBottom": "12px"}),
                    dbc.Row([
                        dbc.Col([
                            html.Label("Lookback:", className="kb-label", style={"marginBottom": "4px", "display": "block"}),
                            dcc.Dropdown(id="ai-signals-lookback", className="dash-dropdown", options=[
                                {"label": "7 days", "value": 7}, {"label": "30 days", "value": 30},
                                {"label": "90 days", "value": 90},
                            ], value=30, clearable=False, style={"width": "120px"}),
                        ], width=2),
                        dbc.Col([
                            html.Label("Signal Type:", className="kb-label", style={"marginBottom": "4px", "display": "block"}),
                            dcc.Dropdown(id="ai-signals-type", className="dash-dropdown", options=[
                                {"label": "All Signals", "value": "ALL"},
                                {"label": "Accumulation Breakout", "value": "ACCUMULATION_BREAKOUT"},
                                {"label": "Insider Buying Surge", "value": "INSIDER_BUYING_SURGE"},
                                {"label": "Sector Rotation", "value": "SECTOR_ROTATION"},
                                {"label": "Smart Money Convergence", "value": "SMART_MONEY_CONVERGENCE"},
                                {"label": "Contrarian Opportunity", "value": "CONTRARIAN_OPPORTUNITY"},
                                {"label": "Exit Warning", "value": "EXIT_WARNING"},
                                {"label": "High Conviction Convergence", "value": "HIGH_CONVICTION_PREDICTION"},
                                {"label": "Swing Confluence (Proven)", "value": "SWING_CONFLUENCE"},
                            ], value="ALL", clearable=False, style={"width": "240px"}),
                        ], width=3),
                    ], className="mb-3"),
                    html.Div(id="ai-signals-cards", children=[
                        html.P("No signals detected. Run aggregation to generate signals.",
                               style={"color": cfg.text_muted, "textAlign": "center", "padding": "40px"}),
                    ]),
                ]),
            ]),

            # ============================================================
            # Intraday ML sub-tab — live ML model performance
            # ============================================================
            html.Div(id="ls-intraday-ml-container", hidden=True, children=[
                # Live Intraday Ideas table
                html.Div(className="kb-card mb-3", children=[
                    html.H4("Intraday Ideas", style={"color": cfg.accent_primary, "marginBottom": "4px"}),
                    html.P("Live context-momentum and pattern-based ideas. NEW = actionable. ENTERED = trade taken.",
                           style={"color": cfg.text_muted, "fontSize": "0.80rem", "marginBottom": "12px"}),
                    dash_table.DataTable(
                        id="intraday-ideas-table",
                        columns=[
                            {"name": "✦", "id": "rule_match"},
                            {"name": "State", "id": "state"},
                            {"name": "Symbol", "id": "symbol"},
                            {"name": "Side", "id": "side"},
                            {"name": "Source", "id": "source"},
                            {"name": "Entry $", "id": "entry_price", "type": "numeric",
                             "format": dash_table.FormatTemplate.money(2)},
                            {"name": "Stop $", "id": "stop_loss", "type": "numeric",
                             "format": dash_table.FormatTemplate.money(2)},
                            {"name": "T1 $", "id": "target_1", "type": "numeric",
                             "format": dash_table.FormatTemplate.money(2)},
                            {"name": "Conv", "id": "conviction", "type": "numeric"},
                            {"name": "RS", "id": "rs"},
                            {"name": "VolP", "id": "vol_pressure"},
                            {"name": "VWAP", "id": "vwap_sigma"},
                            {"name": "Seen", "id": "first_seen"},
                            {"name": "Trade#", "id": "trade_id"},
                        ],
                        data=[],
                        page_size=15,
                        sort_action="native",
                        style_table={"overflowX": "auto"},
                        style_header=TABLE_HEADER_STYLE,
                        style_cell={**TABLE_CELL_STYLE, "textAlign": "center"},
                        style_cell_conditional=[
                            {"if": {"column_id": "symbol"}, "textAlign": "left",
                             "fontWeight": "700", "color": "#4da3ff", "cursor": "pointer"},
                        ],
                        style_data_conditional=[
                            # Rule-match highlight — ideas that pass your trade rules
                            {"if": {"filter_query": '{rule_match} = "✦"'},
                             "backgroundColor": "rgba(255,212,59,0.08)",
                             "borderLeft": "3px solid #ffd43b"},
                            {"if": {"filter_query": '{rule_match} = "✦"',
                                    "column_id": "rule_match"},
                             "color": "#ffd43b", "fontWeight": "bold"},
                            {"if": {"filter_query": '{state} = "NEW"', "column_id": "state"},
                             "color": "#00ff88", "fontWeight": "bold"},
                            {"if": {"filter_query": '{state} = "ENTERED"', "column_id": "state"},
                             "color": "#ffd43b"},
                            {"if": {"filter_query": '{state} = "ACTIVE"', "column_id": "state"},
                             "color": "#4dc9ff"},
                        ],
                    ),
                ]),

                html.Div(className="kb-card mb-3", children=[
                    html.H4([
                        "Intraday ML Models",
                        html.Span(" — Live performance monitoring", style={"color": cfg.text_muted, "fontSize": "0.82rem", "fontWeight": "400"}),
                    ], style={"color": cfg.accent_primary, "marginBottom": "12px"}),

                    # Strategy performance cards
                    dbc.Row(className="g-3 mb-4", children=[
                        dbc.Col(md=3, children=[
                            html.Div(className="kb-card", style={"textAlign": "center", "padding": "16px", "border": f"1px solid {cfg.border_color}"}, children=[
                                html.Div("VWAP_MR", style={"fontSize": "0.72rem", "color": "#888", "textTransform": "uppercase", "letterSpacing": "0.08em", "marginBottom": "6px"}),
                                html.Div("AUC: 0.825", style={"fontSize": "1.3rem", "fontWeight": "800", "color": "#00c896", "fontFamily": "'JetBrains Mono', monospace"}),
                                html.Div("Top 5%: 52.9% 2R hit rate", style={"fontSize": "0.75rem", "color": cfg.text_muted, "marginTop": "4px"}),
                                html.Div(id="ml-vwap-live-stats", style={"marginTop": "8px", "fontSize": "0.78rem", "color": cfg.text_color}),
                            ]),
                        ]),
                        dbc.Col(md=3, children=[
                            html.Div(className="kb-card", style={"textAlign": "center", "padding": "16px", "border": f"1px solid {cfg.border_color}"}, children=[
                                html.Div("FPB", style={"fontSize": "0.72rem", "color": "#888", "textTransform": "uppercase", "letterSpacing": "0.08em", "marginBottom": "6px"}),
                                html.Div("AUC: 0.856", style={"fontSize": "1.3rem", "fontWeight": "800", "color": "#4dc9ff", "fontFamily": "'JetBrains Mono', monospace"}),
                                html.Div("Top 10%: 33.5% 2R hit rate", style={"fontSize": "0.75rem", "color": cfg.text_muted, "marginTop": "4px"}),
                                html.Div(id="ml-fpb-live-stats", style={"marginTop": "8px", "fontSize": "0.78rem", "color": cfg.text_color}),
                            ]),
                        ]),
                        dbc.Col(md=3, children=[
                            html.Div(className="kb-card", style={"textAlign": "center", "padding": "16px", "border": f"1px solid {cfg.border_color}"}, children=[
                                html.Div("ORB_V2", style={"fontSize": "0.72rem", "color": "#888", "textTransform": "uppercase", "letterSpacing": "0.08em", "marginBottom": "6px"}),
                                html.Div("AUC: 0.731", style={"fontSize": "1.3rem", "fontWeight": "800", "color": "#e88b4d", "fontFamily": "'JetBrains Mono', monospace"}),
                                html.Div("Top 10%: 36.8% 2R hit rate", style={"fontSize": "0.75rem", "color": cfg.text_muted, "marginTop": "4px"}),
                                html.Div(id="ml-orb-v2-live-stats", style={"marginTop": "8px", "fontSize": "0.78rem", "color": cfg.text_color}),
                            ]),
                        ]),
                        dbc.Col(md=3, children=[
                            html.Div(className="kb-card", style={"textAlign": "center", "padding": "16px", "border": f"1px solid {cfg.border_color}"}, children=[
                                html.Div("LIVE P&L", style={"fontSize": "0.72rem", "color": "#888", "textTransform": "uppercase", "letterSpacing": "0.08em", "marginBottom": "6px"}),
                                html.Div(id="ml-live-pnl", children="—", style={"fontSize": "1.3rem", "fontWeight": "800", "color": "#f5a623", "fontFamily": "'JetBrains Mono', monospace"}),
                                html.Div(id="ml-live-pnl-detail", style={"fontSize": "0.75rem", "color": cfg.text_muted, "marginTop": "4px"}),
                            ]),
                        ]),
                    ]),

                    # Recent ML trades table
                    html.Div("RECENT ML TRADES", className="kb-card-title", style={"marginBottom": "10px"}),
                    dash_table.DataTable(
                        id="ml-recent-trades-table",
                        columns=[
                            {"name": "Time", "id": "entry_time"},
                            {"name": "Symbol", "id": "symbol"},
                            {"name": "Strategy", "id": "strategy"},
                            {"name": "ML Prob", "id": "ml_prob", "type": "numeric"},
                            {"name": "Grade", "id": "grade"},
                            {"name": "Entry", "id": "entry_price", "type": "numeric"},
                            {"name": "Stop", "id": "stop_price", "type": "numeric"},
                            {"name": "Status", "id": "status"},
                            {"name": "P&L", "id": "pnl"},
                        ],
                        data=[],
                        page_size=15,
                        style_header=TABLE_HEADER_STYLE,
                        style_cell=TABLE_CELL_STYLE,
                        style_data_conditional=[
                            {"if": {"filter_query": '{strategy} = "VWAP_MR"', "column_id": "strategy"}, "color": "#00c896", "fontWeight": "600"},
                            {"if": {"filter_query": '{strategy} = "FPB"', "column_id": "strategy"}, "color": "#4dc9ff", "fontWeight": "600"},
                            {"if": {"filter_query": '{strategy} = "ORB_V2"', "column_id": "strategy"}, "color": "#e88b4d", "fontWeight": "600"},
                            {"if": {"filter_query": '{grade} = "A+"', "column_id": "grade"}, "color": "#00d26a", "fontWeight": "700"},
                            {"if": {"filter_query": '{grade} = "A"', "column_id": "grade"}, "color": "#4dc9ff", "fontWeight": "700"},
                            {"if": {"row_index": "odd"}, "backgroundColor": "#0d1117"},
                        ],
                        style_as_list_view=True,
                    ),

                    # Operator health strip
                    html.Div(
                        id="ml-health-strip",
                        className="mt-3",
                        style={"fontSize": "0.75rem", "color": cfg.text_muted, "padding": "8px 12px",
                               "backgroundColor": cfg.card_color_elevated, "borderRadius": "6px",
                               "border": f"1px solid {cfg.border_color}"},
                    ),
                ]),
            ]),

            # ============================================================
            # Intraday Sniper sub-tab - elite intraday buckets, tracked separately
            # ============================================================
            html.Div(id="ls-intraday-sniper-container", hidden=True, children=[
                html.Div(className="kb-card mb-3", children=[
                    html.H4([
                        "Intraday Sniper",
                        html.Span(
                            " - elite buckets kept separate from broad Intraday ML",
                            style={"color": cfg.text_muted, "fontSize": "0.82rem", "fontWeight": "400"},
                        ),
                    ], style={"color": cfg.accent_primary, "marginBottom": "12px"}),
                    html.P(
                        "High-selectivity intraday buckets only. These are tracked independently so their live frequency, "
                        "hit rate, and P&L are not diluted by broader Intraday ML activity.",
                        className="kb-section-desc",
                        style={"marginBottom": "14px"},
                    ),
                    dbc.Row(className="g-3 mb-4", children=[
                        dbc.Col(md=4, children=[
                            html.Div(className="kb-card", style={"textAlign": "center", "padding": "16px", "border": f"1px solid {cfg.border_color}"}, children=[
                                html.Div("VWAP_MR SNIPER", style={"fontSize": "0.72rem", "color": "#888", "textTransform": "uppercase", "letterSpacing": "0.08em", "marginBottom": "6px"}),
                                html.Div(id="sniper-vwap-stats", children="No trades", style={"fontSize": "1.05rem", "fontWeight": "800", "color": "#00c896", "fontFamily": "'JetBrains Mono', monospace"}),
                                html.Div(id="sniper-vwap-detail", style={"fontSize": "0.75rem", "color": cfg.text_muted, "marginTop": "4px"}),
                            ]),
                        ]),
                        dbc.Col(md=4, children=[
                            html.Div(className="kb-card", style={"textAlign": "center", "padding": "16px", "border": f"1px solid {cfg.border_color}"}, children=[
                                html.Div("FPB SNIPER", style={"fontSize": "0.72rem", "color": "#888", "textTransform": "uppercase", "letterSpacing": "0.08em", "marginBottom": "6px"}),
                                html.Div(id="sniper-fpb-stats", children="No trades", style={"fontSize": "1.05rem", "fontWeight": "800", "color": "#4dc9ff", "fontFamily": "'JetBrains Mono', monospace"}),
                                html.Div(id="sniper-fpb-detail", style={"fontSize": "0.75rem", "color": cfg.text_muted, "marginTop": "4px"}),
                            ]),
                        ]),
                        dbc.Col(md=4, children=[
                            html.Div(className="kb-card", style={"textAlign": "center", "padding": "16px", "border": f"1px solid {cfg.border_color}"}, children=[
                                html.Div("SNIPER P&L", style={"fontSize": "0.72rem", "color": "#888", "textTransform": "uppercase", "letterSpacing": "0.08em", "marginBottom": "6px"}),
                                html.Div(id="sniper-live-pnl", children="—", style={"fontSize": "1.3rem", "fontWeight": "800", "color": "#f5a623", "fontFamily": "'JetBrains Mono', monospace"}),
                                html.Div(id="sniper-live-pnl-detail", style={"fontSize": "0.75rem", "color": cfg.text_muted, "marginTop": "4px"}),
                            ]),
                        ]),
                    ]),
                    html.Div("RECENT SNIPER TRADES", className="kb-card-title", style={"marginBottom": "10px"}),
                    dash_table.DataTable(
                        id="sniper-recent-trades-table",
                        columns=[
                            {"name": "Time", "id": "entry_time"},
                            {"name": "Symbol", "id": "symbol"},
                            {"name": "Strategy", "id": "strategy"},
                            {"name": "Bucket", "id": "bucket"},
                            {"name": "Entry", "id": "entry_price", "type": "numeric"},
                            {"name": "Stop", "id": "stop_price", "type": "numeric"},
                            {"name": "Status", "id": "status"},
                            {"name": "P&L", "id": "pnl"},
                        ],
                        data=[],
                        page_size=15,
                        style_header=TABLE_HEADER_STYLE,
                        style_cell=TABLE_CELL_STYLE,
                        style_data_conditional=[
                            {"if": {"filter_query": '{strategy} = "VWAP_MR"', "column_id": "strategy"}, "color": "#00c896", "fontWeight": "600"},
                            {"if": {"filter_query": '{strategy} = "FPB"', "column_id": "strategy"}, "color": "#4dc9ff", "fontWeight": "600"},
                            {"if": {"row_index": "odd"}, "backgroundColor": "#0d1117"},
                        ],
                        style_as_list_view=True,
                    ),
                    html.Div(
                        id="sniper-health-strip",
                        className="mt-3",
                        style={"fontSize": "0.75rem", "color": cfg.text_muted, "padding": "8px 12px",
                               "backgroundColor": cfg.card_color_elevated, "borderRadius": "6px",
                               "border": f"1px solid {cfg.border_color}"},
                    ),
                ]),
            ]),

            # ============================================================
            # Options Flow sub-tab
            # ============================================================
            html.Div(id="ls-options-flow-container", hidden=True, children=[
                html.Div(className="kb-card mb-4", children=[
                    html.H4(["Options Board", rules_tooltip("options")], style={"color": cfg.accent_primary, "marginBottom": "4px"}),
                    html.P(
                        "Contract-level options intelligence from Polygon Options Starter. "
                        "Top OI contracts across Sniper universe. Delayed 15 min.",
                        className="kb-section-desc", style={"marginBottom": "12px"},
                    ),
                    dash_table.DataTable(
                        id="options-flow-table",
                        columns=[
                            {"name": "Symbol", "id": "symbol"},
                            {"name": "Type", "id": "contract_type"},
                            {"name": "Expiry", "id": "expiry_date"},
                            {"name": "Strike", "id": "strike", "type": "numeric",
                             "format": dash_table.FormatTemplate.money(2)},
                            {"name": "Side", "id": "signal"},
                            {"name": "OI", "id": "open_interest", "type": "numeric"},
                            {"name": "Volume", "id": "volume", "type": "numeric"},
                            {"name": "IV", "id": "implied_volatility", "type": "numeric"},
                            {"name": "Delta", "id": "delta", "type": "numeric"},
                            {"name": "Bid", "id": "bid", "type": "numeric",
                             "format": dash_table.FormatTemplate.money(2)},
                            {"name": "Ask", "id": "ask", "type": "numeric",
                             "format": dash_table.FormatTemplate.money(2)},
                            {"name": "Score", "id": "score", "type": "numeric"},
                            {"name": "Date", "id": "snapshot_date"},
                        ],
                        data=[],
                        page_size=20,
                        sort_action="native",
                        style_table={"overflowX": "auto"},
                        style_header=TABLE_HEADER_STYLE,
                        style_cell=TABLE_CELL_STYLE,
                        style_data_conditional=[
                            {"if": {"column_id": "symbol"},
                             "color": "#4da3ff", "cursor": "pointer",
                             "fontWeight": "700"},
                            {"if": {"filter_query": '{signal} = "LONG"', "column_id": "signal"},
                             "color": cfg.accent_long, "fontWeight": "bold"},
                            {"if": {"filter_query": '{signal} = "SHORT"', "column_id": "signal"},
                             "color": cfg.accent_short, "fontWeight": "bold"},
                        ],
                    ),
                ]),
            ]),

            # ============================================================
            # EOD Review sub-tab — full EOD content
            # ============================================================
            html.Div(id="ls-eod-container", hidden=True, children=[
                dbc.Row(
                    className="mb-4 g-3",
                    children=[
                        dbc.Col(_stat_card("eod-days", "ANALYZED DAYS", "0", cfg.text_color, "fa-calendar-days", 1), md=2),
                        dbc.Col(_stat_card("eod-win-rate", "AVG WIN %", "0", "#00ff88", "fa-bullseye", 2), md=2),
                        dbc.Col(_stat_card("eod-pnl", "TOTAL P&L", "$0", "#ffd43b", "fa-dollar-sign", 3), md=2),
                        dbc.Col(_stat_card("eod-losses", "TOTAL LOSSES", "0", cfg.accent_short, "fa-arrow-down", 4), md=2),
                        dbc.Col(_stat_card("eod-top-reason", "TOP LOSS REASON", "-", cfg.accent_neutral, "fa-magnifying-glass", 5), md=2),
                        dbc.Col(_stat_card("eod-alert-win", "ALERT WIN %", "0%", cfg.accent_primary, "fa-bell", 6), md=2),
                    ],
                ),
                html.Div(
                    id="eod-alert-meta",
                    className="mb-2",
                    style={"color": cfg.text_muted, "fontSize": "0.78rem"},
                ),
                html.Div(
                    id="eod-save-status",
                    className="mb-2",
                    style={"color": cfg.text_muted, "fontSize": "0.78rem"},
                ),
                html.Div(
                    className="kb-card",
                    children=[
                        dash_table.DataTable(
                            id="eod-analysis-table",
                            columns=[
                                {"name": "Date", "id": "trade_date", "editable": False},
                                {"name": "Trades", "id": "total_trades", "type": "numeric", "editable": False},
                                {"name": "Wins", "id": "wins", "type": "numeric", "editable": False},
                                {"name": "Losses", "id": "losses", "type": "numeric", "editable": False},
                                {"name": "Win %", "id": "win_rate", "type": "numeric", "editable": False},
                                {"name": "P&L", "id": "realized_pnl", "type": "numeric", "editable": False},
                                {"name": "Avg Loss", "id": "avg_loss", "type": "numeric", "editable": False},
                                {"name": "Max Loss", "id": "max_loss", "type": "numeric", "editable": False},
                                {"name": "Top Loss Reason", "id": "top_loss_reason", "editable": False},
                                {"name": "Action", "id": "action_status", "presentation": "dropdown", "editable": True},
                                {"name": "Review Notes", "id": "action_notes", "editable": True},
                                {"name": "Suggested Actions", "id": "suggested_actions", "editable": False},
                            ],
                            data=[],
                            page_size=10,
                            sort_action="native",
                            editable=True,
                            dropdown={
                                "action_status": {
                                    "options": [
                                        {"label": "PENDING", "value": "PENDING"},
                                        {"label": "IMPLEMENT", "value": "IMPLEMENT"},
                                        {"label": "WATCH", "value": "WATCH"},
                                        {"label": "IGNORE", "value": "IGNORE"},
                                    ]
                                }
                            },
                            style_table={"overflowX": "auto"},
                            style_header=TABLE_HEADER_STYLE,
                            style_cell={**TABLE_CELL_STYLE, "whiteSpace": "normal"},
                            style_data_conditional=[
                                {"if": {"filter_query": "{realized_pnl} > 0", "column_id": "realized_pnl"}, "color": cfg.accent_long},
                                {"if": {"filter_query": "{realized_pnl} < 0", "column_id": "realized_pnl"}, "color": cfg.accent_short},
                                {"if": {"filter_query": "{win_rate} >= 60", "column_id": "win_rate"}, "color": cfg.accent_long},
                                {"if": {"filter_query": "{win_rate} < 45", "column_id": "win_rate"}, "color": cfg.accent_short},
                                {"if": {"filter_query": '{action_status} = "IMPLEMENT"', "column_id": "action_status"}, "color": cfg.accent_long, "fontWeight": "bold"},
                                {"if": {"filter_query": '{action_status} = "WATCH"', "column_id": "action_status"}, "color": cfg.accent_neutral, "fontWeight": "bold"},
                                {"if": {"filter_query": '{action_status} = "IGNORE"', "column_id": "action_status"}, "color": "#aaa"},
                                {"if": {"filter_query": '{action_status} = "PENDING"', "column_id": "action_status"}, "color": cfg.accent_primary},
                            ],
                        ),
                    ],
                ),
            ]),
        ],
    )


def _build_stock_report_section():
    """Lazily import and build the Individual Stock Report layout."""
    from signal_scanner.dashboard.layouts.stock_report_view import build_stock_report_section
    return build_stock_report_section()


def _build_intelligence_section():
    """Build the Intelligence Layer with 8 report tabs."""
    from signal_scanner.dashboard.layouts.intelligence_reports import build_intelligence_layout
    return build_intelligence_layout()


def _stat_card(
    card_id: str, title: str, value: str, color: str, icon: str = "fa-chart-bar",
    stagger: int = 1,
    clickable: bool = False,
):
    """Build a modern stat card with icon, value, and label.

    When clickable=True the card is wrapped in an html.Div with n_clicks so
    callbacks can use it as a filter trigger (dbc.Card dropped n_clicks in v2.0+).
    """
    children = [
        html.I(className=f"fas {icon} kb-stat-icon", style={"color": color}),
        html.Div(
            value,
            id=card_id,
            className="kb-stat-value",
            style={"color": color},
        ),
        html.P(title, className="kb-stat-label"),
    ]

    if clickable:
        # dbc.Card dropped n_clicks in v2.0+; use html.Div with kb-stat-card class instead
        # (all visual styling comes from our kb-stat-card CSS, not DBC defaults)
        return html.Div(
            id=f"{card_id}-tile",
            n_clicks=0,
            className=f"kb-stat-card kb-animate-in kb-stagger-{stagger}",
            style={"cursor": "pointer"},
            children=children,
        )

    return dbc.Card(
        className=f"kb-stat-card kb-animate-in kb-stagger-{stagger}",
        children=children,
    )


def _build_conditional_styles() -> list:
    """Build rich conditional formatting for the DataTable."""
    styles = [
        # ---- Clickable symbol ----
        {"if": {"column_id": "symbol"}, "color": "#4da3ff", "cursor": "pointer",
         "fontWeight": "700", "textDecoration": "underline"},

        # ---- Institutional Phase column ----
        {"if": {"filter_query": '{inst_phase} = "ACTIVE ACCUM"', "column_id": "inst_phase"},
         "color": "#00c896", "fontWeight": "700", "backgroundColor": "#0a1f17"},
        {"if": {"filter_query": '{inst_phase} = "EARLY ACCUM"', "column_id": "inst_phase"},
         "color": "#4dc9ff", "fontWeight": "600"},
        {"if": {"filter_query": '{inst_phase} = "LATE ACCUM"', "column_id": "inst_phase"},
         "color": "#f5a623"},
        {"if": {"filter_query": '{inst_phase} = "EXPANSION"', "column_id": "inst_phase"},
         "color": "#a78bfa"},
        {"if": {"filter_query": '{inst_phase} = "DISTRIBUTION"', "column_id": "inst_phase"},
         "color": "#e05252", "fontWeight": "600"},
        {"if": {"filter_query": '{inst_phase} = "DORMANT"', "column_id": "inst_phase"},
         "color": "#555"},

        # ---- Institutional Conviction column ----
        {"if": {"filter_query": "{inst_conviction} >= 70", "column_id": "inst_conviction"},
         "color": "#00c896", "fontWeight": "700"},
        {"if": {"filter_query": "{inst_conviction} >= 45 && {inst_conviction} < 70",
                "column_id": "inst_conviction"}, "color": "#f5a623"},
        {"if": {"filter_query": "{inst_conviction} < 45", "column_id": "inst_conviction"},
         "color": "#e05252"},

        # ---- Institutional Swing Signal column ----
        {"if": {"filter_query": '{inst_swing} = "BUY"', "column_id": "inst_swing"},
         "color": "#00c896", "fontWeight": "700", "backgroundColor": "#0a1f17"},
        {"if": {"filter_query": '{inst_swing} = "WATCH"', "column_id": "inst_swing"},
         "color": "#f5a623"},
        {"if": {"filter_query": '{inst_swing} = "AVOID"', "column_id": "inst_swing"},
         "color": "#e05252", "fontWeight": "600"},
        {"if": {"filter_query": '{inst_swing} = "SHORT"', "column_id": "inst_swing"},
         "color": "#e05252", "backgroundColor": "#330d1a", "fontWeight": "700"},

        # ---- Signal column: colored background badges ----
        {
            "if": {"filter_query": '{signal} = "LONG"', "column_id": "signal"},
            "backgroundColor": "#0d3320",
            "color": "#00ff88",
            "fontWeight": "bold",
            "textAlign": "center",
        },
        {
            "if": {"filter_query": '{signal} = "SHORT"', "column_id": "signal"},
            "backgroundColor": "#330d1a",
            "color": "#ff4488",
            "fontWeight": "bold",
            "textAlign": "center",
        },
        {
            "if": {"filter_query": '{signal} = "NEUTRAL"', "column_id": "signal"},
            "backgroundColor": "#33290d",
            "color": cfg.accent_neutral,
            "textAlign": "center",
        },

        # ---- Recommendation column: vibrant badges ----
        {
            "if": {"filter_query": '{recommendation} = "BUY"', "column_id": "recommendation"},
            "backgroundColor": "#0d3320",
            "color": "#00ff88",
            "fontWeight": "bold",
            "textAlign": "center",
        },
        {
            "if": {"filter_query": '{recommendation} = "SELL"', "column_id": "recommendation"},
            "backgroundColor": "#330d1a",
            "color": "#ff4488",
            "fontWeight": "bold",
            "textAlign": "center",
        },
        {
            "if": {"filter_query": '{recommendation} = "HOLD"', "column_id": "recommendation"},
            "color": "#888",
            "textAlign": "center",
        },

        # ---- Score column: gradient coloring ----
        {
            "if": {"filter_query": "{score} >= 80", "column_id": "score"},
            "backgroundColor": "#0d3320",
            "color": "#00ff88",
            "fontWeight": "bold",
        },
        {
            "if": {"filter_query": "{score} >= 60 && {score} < 80", "column_id": "score"},
            "backgroundColor": "#2e2a0d",
            "color": cfg.accent_neutral,
            "fontWeight": "bold",
        },
        {
            "if": {"filter_query": "{score} < 60", "column_id": "score"},
            "color": "#666",
        },

        # ---- MTF agreement: colored by strength ----
        {
            "if": {"filter_query": '{mtf_agreement} contains "3/3"', "column_id": "mtf_agreement"},
            "backgroundColor": "#0d3320",
            "color": "#00ff88",
            "fontWeight": "bold",
            "textAlign": "center",
        },
        {
            "if": {"filter_query": '{mtf_agreement} contains "2/3"', "column_id": "mtf_agreement"},
            "backgroundColor": "#2e2a0d",
            "color": cfg.accent_neutral,
            "textAlign": "center",
        },
        {
            "if": {"filter_query": '{mtf_agreement} contains "1/3"', "column_id": "mtf_agreement"},
            "color": "#888",
            "textAlign": "center",
        },
        {
            "if": {"filter_query": '{mtf_agreement} contains "0/"', "column_id": "mtf_agreement"},
            "color": "#555",
            "textAlign": "center",
        },

        # ---- R:R ratio coloring ----
        {
            "if": {"filter_query": "{rr_ratio} >= 2", "column_id": "rr_ratio"},
            "color": "#00ff88",
            "fontWeight": "bold",
        },
        {
            "if": {"filter_query": "{rr_ratio} >= 1.5 && {rr_ratio} < 2", "column_id": "rr_ratio"},
            "color": cfg.accent_neutral,
        },
        {
            "if": {"filter_query": "{rr_ratio} < 1.5", "column_id": "rr_ratio"},
            "color": "#ff4488",
        },

        # ---- Trend direction ----
        {
            "if": {"filter_query": '{trend_direction} = "UP"', "column_id": "trend_direction"},
            "color": cfg.accent_long,
            "fontWeight": "bold",
        },
        {
            "if": {"filter_query": '{trend_direction} = "DOWN"', "column_id": "trend_direction"},
            "color": cfg.accent_short,
            "fontWeight": "bold",
        },
        {
            "if": {"filter_query": '{trend_direction} = "SIDE"', "column_id": "trend_direction"},
            "color": "#555",
        },

        # ---- Stop/Target coloring ----
        {
            "if": {"column_id": "stop_loss"},
            "color": "#ff6b6b",
        },
        {
            "if": {"column_id": "target_1"},
            "color": "#51cf66",
        },
        {
            "if": {"column_id": "target_2"},
            "color": "#00ff88",
            "fontWeight": "bold",
        },

        # ---- RSI coloring ----
        {
            "if": {"filter_query": "{rsi} > 70", "column_id": "rsi"},
            "color": "#ff6b6b",
        },
        {
            "if": {"filter_query": "{rsi} < 30", "column_id": "rsi"},
            "color": "#51cf66",
        },
        {
            "if": {"filter_query": "{rsi} >= 30 && {rsi} <= 70", "column_id": "rsi"},
            "color": "#aaa",
        },

        # ---- ADX coloring ----
        {
            "if": {"filter_query": "{adx} >= 25 && {adx} < 50", "column_id": "adx"},
            "color": cfg.accent_long,
        },
        {
            "if": {"filter_query": "{adx} >= 50", "column_id": "adx"},
            "color": "#ff6b6b",
        },
        {
            "if": {"filter_query": "{adx} < 20", "column_id": "adx"},
            "color": "#555",
        },

        # ---- Volume ratio ----
        {
            "if": {"filter_query": "{volume_ratio} >= 2", "column_id": "volume_ratio"},
            "color": "#00ff88",
            "fontWeight": "bold",
        },
        {
            "if": {"filter_query": "{volume_ratio} >= 1.3 && {volume_ratio} < 2", "column_id": "volume_ratio"},
            "color": cfg.accent_neutral,
        },

        # ---- VWAP status ----
        {
            "if": {"filter_query": '{vwap_status} contains "ABOVE"', "column_id": "vwap_status"},
            "color": cfg.accent_long,
        },
        {
            "if": {"filter_query": '{vwap_status} contains "BELOW"', "column_id": "vwap_status"},
            "color": cfg.accent_short,
        },

        # ---- GEX status ----
        {
            "if": {"filter_query": '{gex_status} contains "ABOVE"', "column_id": "gex_status"},
            "color": "#b388ff",
        },
        {
            "if": {"filter_query": '{gex_status} contains "BELOW"', "column_id": "gex_status"},
            "color": "#ff8a80",
        },

        # ---- Relative strength ----
        {
            "if": {"filter_query": "{relative_strength} > 0", "column_id": "relative_strength"},
            "color": cfg.accent_long,
        },
        {
            "if": {"filter_query": "{relative_strength} < 0", "column_id": "relative_strength"},
            "color": cfg.accent_short,
        },

        # ---- Market regime ----
        {
            "if": {"filter_query": '{market_regime} = "RISK_ON"', "column_id": "market_regime"},
            "color": "#00ff88",
        },
        {
            "if": {"filter_query": '{market_regime} = "RISK_OFF"', "column_id": "market_regime"},
            "color": "#ff4488",
        },
        {
            "if": {"filter_query": '{market_regime} = "NEUTRAL"', "column_id": "market_regime"},
            "color": cfg.accent_neutral,
        },

        # ---- Signal age ----
        {
            "if": {"filter_query": "{signal_age} >= 3", "column_id": "signal_age"},
            "color": "#00ff88",
            "fontWeight": "bold",
        },
        {
            "if": {"filter_query": "{signal_age} = 1", "column_id": "signal_age"},
            "color": "#888",
        },

        # ---- Signal momentum ----
        {
            "if": {"filter_query": '{signal_momentum} = "STRENGTHENING"', "column_id": "signal_momentum"},
            "color": "#00ff88",
            "fontWeight": "bold",
        },
        {
            "if": {"filter_query": '{signal_momentum} = "WEAKENING"', "column_id": "signal_momentum"},
            "color": "#ff006e",
            "fontWeight": "bold",
        },
        {
            "if": {"filter_query": "{score_delta} > 5", "column_id": "score_delta"},
            "color": "#00ff88",
        },
        {
            "if": {"filter_query": "{score_delta} < -5", "column_id": "score_delta"},
            "color": "#ff006e",
        },

        # ---- Stock state / confirmations ----
        {
            "if": {"filter_query": '{stock_state} = "NEW"', "column_id": "stock_state"},
            "color": cfg.accent_primary,
            "fontWeight": "bold",
        },
        {
            "if": {"filter_query": '{stock_state} = "CONFIRMED"', "column_id": "stock_state"},
            "color": cfg.accent_long,
            "fontWeight": "bold",
        },
        {
            "if": {"filter_query": '{stock_state} = "VERY_STRONG"', "column_id": "stock_state"},
            "color": "#00ff88",
            "fontWeight": "bold",
        },
        {
            "if": {"filter_query": "{recommendation_confirms} >= 5", "column_id": "recommendation_confirms"},
            "color": "#00ff88",
            "fontWeight": "bold",
        },
        {
            "if": {"filter_query": "{recommendation_confirms} >= 2 && {recommendation_confirms} < 5", "column_id": "recommendation_confirms"},
            "color": cfg.accent_neutral,
            "fontWeight": "bold",
        },

        # ---- Conditions column: compact ----
        {
            "if": {"column_id": "trade_conditions"},
            "fontSize": "11px",
            "whiteSpace": "normal",
            "maxWidth": "300px",
        },

        # ---- Alternating row backgrounds for readability ----
        {
            "if": {"row_index": "odd"},
            "backgroundColor": "#10131a",
        },

        # ---- Highlight high-conviction rows (score >= 80 + BUY/SELL) ----
        {
            "if": {"filter_query": '{score} >= 80 && {recommendation} = "BUY"'},
            "borderLeft": "3px solid #00ff88",
        },
        {
            "if": {"filter_query": '{score} >= 80 && {recommendation} = "SELL"'},
            "borderLeft": "3px solid #ff4488",
        },
    ]

    return styles
