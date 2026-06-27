"""Performance tab — merged Paper Trading + My Trades with regime-stratified analytics.

Sub-tabs:
  - Open Positions: live P&L, current price, distance to stop/T1/T2
  - Closed Trades: full journal with entry/exit, P&L, regime, source
  - Analytics: win rate, expectancy, profit factor, Sharpe (filterable)
  - Manual Trades: real trade entry + tracking (from My Trades)
  - Release Notes: chronological log of system changes (docs/RELEASE_NOTES.md)
"""

from pathlib import Path

import dash_bootstrap_components as dbc
from dash import dash_table, dcc, html

from signal_scanner.config import DashboardConfig
from signal_scanner.dashboard.layouts.main_view import TABLE_HEADER_STYLE, TABLE_CELL_STYLE
from signal_scanner.dashboard.layouts.my_trades_view import build_my_trades_layout
from signal_scanner.dashboard.trade_rules import rules_tooltip

cfg = DashboardConfig()

_RELEASE_NOTES_PATH = Path(__file__).resolve().parents[3] / "docs" / "RELEASE_NOTES.md"


def _load_release_notes() -> str:
    """Load docs/RELEASE_NOTES.md. Returns a friendly fallback if missing."""
    try:
        return _RELEASE_NOTES_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        return "# Release Notes\n\n_No `docs/RELEASE_NOTES.md` file found._"
    except Exception as e:
        return f"# Release Notes\n\n_Failed to read RELEASE_NOTES.md: {e}_"


def build_performance_layout() -> html.Div:
    """Build the Performance section layout."""
    return html.Div(
        id="performance-section",
        hidden=True,
        className="kb-animate-in",
        children=[
            # Section header
            html.Div(
                className="kb-section-header",
                children=[
                    html.H2([
                        "P&L Ledger",
                        html.Span("ALL TRADES", className="kb-section-badge"),
                        rules_tooltip("performance"),
                    ]),
                    html.P(
                        "One truth for all trade outcomes — paper, manual, and automated",
                        className="kb-section-desc",
                    ),
                ],
            ),

            # KPI row
            dbc.Row(
                className="mb-4 g-3",
                children=[
                    dcc.Store(id="perf-tile-filter-store", data={"status": None}),
                    dbc.Col(_perf_tile("perf-open", "OPEN", "0",
                                       cfg.text_color, "fa-folder-open",
                                       clickable=True), md=2),
                    dbc.Col(_perf_tile("perf-closed", "CLOSED", "0",
                                       cfg.accent_primary, "fa-folder-closed",
                                       clickable=True), md=2),
                    dbc.Col(_perf_tile("perf-win-rate", "WIN %", "0",
                                       "#00ff88", "fa-trophy"), md=2),
                    dbc.Col(_perf_tile("perf-unrealized", "UNREALIZED P&L", "$0",
                                       "#4dc9ff", "fa-chart-line"), md=2),
                    dbc.Col(_perf_tile("perf-pnl", "REALIZED P&L", "$0",
                                       "#ffd43b", "fa-coins"), md=2),
                    dbc.Col(_perf_tile("perf-expectancy", "EXPECTANCY", "$0",
                                       cfg.accent_primary, "fa-calculator"), md=2),
                ],
            ),

            # Sub-tabs: Open | Closed | Analytics | Manual Trades | Release Notes
            dbc.Tabs(
                id="performance-subtabs",
                active_tab="perf-open-tab",
                className="mb-3",
                children=[
                    dbc.Tab(label="Open Positions", tab_id="perf-open-tab"),
                    dbc.Tab(label="Closed Trades", tab_id="perf-closed-tab"),
                    dbc.Tab(label="Analytics", tab_id="perf-analytics-tab"),
                    dbc.Tab(label="Manual Trades", tab_id="perf-manual-tab"),
                    dbc.Tab(label="Release Notes", tab_id="perf-releases-tab"),
                ],
            ),

            # Sub-tab content container
            html.Div(id="performance-subtab-content", children=[
                # Open positions table (default visible)
                html.Div(
                    id="perf-open-content",
                    className="kb-card",
                    children=[
                        dash_table.DataTable(
                            id="perf-open-table",
                            columns=[
                                {"name": "Opened", "id": "opened_at"},
                                {"name": "Symbol", "id": "symbol"},
                                {"name": "Side", "id": "side"},
                                {"name": "Entry $", "id": "entry_price", "type": "numeric",
                                 "format": dash_table.FormatTemplate.money(2)},
                                {"name": "Stop $", "id": "stop_loss", "type": "numeric",
                                 "format": dash_table.FormatTemplate.money(2)},
                                {"name": "Current $", "id": "current_price", "type": "numeric",
                                 "format": dash_table.FormatTemplate.money(2)},
                                {"name": "Gain %", "id": "gain_pct"},
                                {"name": "Gain $", "id": "gain_dollar"},
                                {"name": "T1", "id": "target_1", "type": "numeric",
                                 "format": dash_table.FormatTemplate.money(2)},
                                {"name": "T2", "id": "target_2", "type": "numeric",
                                 "format": dash_table.FormatTemplate.money(2)},
                                {"name": "R:R", "id": "entry_rr_ratio", "type": "numeric"},
                                {"name": "Source", "id": "recommendation_source"},
                                {"name": "Regime", "id": "entry_market_regime"},
                            ],
                            data=[],
                            page_size=15,
                            sort_action="native",
                            row_selectable="single",
                            selected_rows=[],
                            style_table={"overflowX": "auto"},
                            style_header=TABLE_HEADER_STYLE,
                            style_cell=TABLE_CELL_STYLE,
                            style_data_conditional=[
                                {"if": {"column_id": "symbol"},
                                 "color": "#4da3ff", "cursor": "pointer",
                                 "fontWeight": "700", "textDecoration": "underline"},
                                {"if": {"filter_query": '{side} = "LONG"', "column_id": "side"},
                                 "color": cfg.accent_long, "fontWeight": "bold"},
                                {"if": {"filter_query": '{side} = "SHORT"', "column_id": "side"},
                                 "color": cfg.accent_short, "fontWeight": "bold"},
                                # Gain %: green if positive, red if negative
                                {"if": {"filter_query": "{gain_pct_raw} > 0", "column_id": "gain_pct"},
                                 "color": cfg.accent_long, "fontWeight": "700"},
                                {"if": {"filter_query": "{gain_pct_raw} < 0", "column_id": "gain_pct"},
                                 "color": cfg.accent_short, "fontWeight": "700"},
                                {"if": {"filter_query": "{gain_pct_raw} > 0", "column_id": "gain_dollar"},
                                 "color": cfg.accent_long, "fontWeight": "700"},
                                {"if": {"filter_query": "{gain_pct_raw} < 0", "column_id": "gain_dollar"},
                                 "color": cfg.accent_short, "fontWeight": "700"},
                                {"if": {"filter_query": "{gain_pct_raw} > 0", "column_id": "current_price"},
                                 "color": cfg.accent_long},
                                {"if": {"filter_query": "{gain_pct_raw} < 0", "column_id": "current_price"},
                                 "color": cfg.accent_short},
                            ],
                        ),
                        # Close trade panel (shown when row selected)
                        html.Div(
                            id="perf-close-panel",
                            hidden=True,
                            className="mt-3",
                            style={"borderLeft": f"3px solid {cfg.accent_short}",
                                   "padding": "12px", "backgroundColor": "#1a1a2e"},
                            children=[
                                html.Div(id="perf-close-info",
                                         style={"marginBottom": "10px",
                                                "fontWeight": "bold"}),
                                dbc.Row([
                                    dbc.Col([
                                        dbc.Label("Exit Price"),
                                        dbc.Input(id="perf-close-price", type="number",
                                                  step=0.01, placeholder="Exit price"),
                                    ], md=3),
                                    dbc.Col([
                                        dbc.Label("Exit Reason"),
                                        dbc.Input(id="perf-close-reason", type="text",
                                                  value="MANUAL_EXIT",
                                                  placeholder="Reason"),
                                    ], md=3),
                                    dbc.Col([
                                        html.Br(),
                                        html.Button("Close Trade",
                                                    id="perf-close-btn",
                                                    className="btn btn-danger btn-sm",
                                                    n_clicks=0),
                                    ], md=2),
                                    dbc.Col([
                                        html.Br(),
                                        html.Button("Cancel",
                                                    id="perf-close-cancel",
                                                    className="btn btn-secondary btn-sm",
                                                    n_clicks=0),
                                    ], md=2),
                                ]),
                                html.Div(id="perf-close-status",
                                         className="mt-2",
                                         style={"color": "#aaa"}),
                                dcc.Store(id="perf-close-trade-id", data=None),
                            ],
                        ),
                    ],
                ),

                # Closed trades table
                html.Div(
                    id="perf-closed-content",
                    hidden=True,
                    className="kb-card",
                    children=[
                        # Regime filter for closed trades
                        html.Div(
                            style={"display": "flex", "gap": "12px",
                                   "marginBottom": "12px", "alignItems": "center"},
                            children=[
                                html.Span("Filter by Regime:", className="kb-label"),
                                dcc.Dropdown(
                                    id="perf-regime-filter",
                                    options=[
                                        {"label": "All Regimes", "value": "ALL"},
                                        {"label": "Bull Trend", "value": "BULL_TREND"},
                                        {"label": "Mean Reversion", "value": "MEAN_REVERSION"},
                                        {"label": "Accumulation", "value": "ACCUMULATION"},
                                        {"label": "Distribution", "value": "DISTRIBUTION"},
                                        {"label": "Crash", "value": "CRASH"},
                                    ],
                                    value="ALL",
                                    clearable=False,
                                    style={"width": "160px", "fontSize": "0.78rem"},
                                ),
                                html.Span("Strategy:", className="kb-label"),
                                dcc.Dropdown(
                                    id="perf-strategy-filter",
                                    options=[
                                        {"label": "All", "value": "ALL"},
                                        {"label": "IDEA_SWING", "value": "IDEA_SWING"},
                                        {"label": "SCANNER_MTF", "value": "SCANNER_MTF"},
                                        {"label": "VWAP_MR", "value": "VWAP_MR"},
                                        {"label": "FPB", "value": "FPB"},
                                        {"label": "ORB_V2", "value": "ORB_V2"},
                                        {"label": "MANUAL", "value": "MANUAL"},
                                    ],
                                    value="ALL",
                                    clearable=False,
                                    style={"width": "160px", "fontSize": "0.78rem"},
                                ),
                            ],
                        ),
                        dash_table.DataTable(
                            id="perf-closed-table",
                            columns=[
                                {"name": "Opened", "id": "opened_at"},
                                {"name": "Closed", "id": "closed_at"},
                                {"name": "Symbol", "id": "symbol"},
                                {"name": "Strategy", "id": "strategy_type"},
                                {"name": "Side", "id": "side"},
                                {"name": "Entry", "id": "entry_price", "type": "numeric",
                                 "format": dash_table.FormatTemplate.money(2)},
                                {"name": "Exit", "id": "exit_price", "type": "numeric",
                                 "format": dash_table.FormatTemplate.money(2)},
                                {"name": "Exit Reason", "id": "exit_reason"},
                                {"name": "Regime", "id": "entry_market_regime"},
                                {"name": "P&L", "id": "realized_pnl", "type": "numeric",
                                 "format": dash_table.FormatTemplate.money(2)},
                                {"name": "P&L %", "id": "realized_pnl_pct", "type": "numeric"},
                            ],
                            data=[],
                            page_size=15,
                            sort_action="native",
                            style_table={"overflowX": "auto"},
                            style_header=TABLE_HEADER_STYLE,
                            style_cell=TABLE_CELL_STYLE,
                            style_data_conditional=[
                                {"if": {"column_id": "symbol"},
                                 "color": "#4da3ff", "fontWeight": "700"},
                                {"if": {"filter_query": "{realized_pnl} > 0", "column_id": "realized_pnl"},
                                 "color": cfg.accent_long},
                                {"if": {"filter_query": "{realized_pnl} < 0", "column_id": "realized_pnl"},
                                 "color": cfg.accent_short},
                                {"if": {"filter_query": "{realized_pnl_pct} > 0", "column_id": "realized_pnl_pct"},
                                 "color": cfg.accent_long},
                                {"if": {"filter_query": "{realized_pnl_pct} < 0", "column_id": "realized_pnl_pct"},
                                 "color": cfg.accent_short},
                            ],
                        ),
                    ],
                ),

                # Analytics panel
                html.Div(
                    id="perf-analytics-content",
                    hidden=True,
                    className="kb-card",
                    children=[
                        html.H5("Regime-Stratified Performance",
                                style={"color": cfg.accent_primary,
                                       "marginBottom": "16px"}),
                        # Analytics cards row
                        dbc.Row(
                            className="g-3 mb-4",
                            children=[
                                dbc.Col(
                                    _analytics_card("perf-overall-wr", "Overall WR",
                                                    "0%", "#00ff88"),
                                    md=3),
                                dbc.Col(
                                    _analytics_card("perf-regime-wr", "With-Regime WR",
                                                    "0%", cfg.accent_primary),
                                    md=3),
                                dbc.Col(
                                    _analytics_card("perf-profit-factor", "Profit Factor",
                                                    "0.0", "#ffd43b"),
                                    md=3),
                                dbc.Col(
                                    _analytics_card("perf-sharpe", "Sharpe Ratio",
                                                    "0.0", cfg.accent_neutral),
                                    md=3),
                            ],
                        ),
                        # Regime breakdown table
                        dash_table.DataTable(
                            id="perf-regime-breakdown",
                            columns=[
                                {"name": "Regime", "id": "regime"},
                                {"name": "Trades", "id": "n_trades", "type": "numeric"},
                                {"name": "Win Rate", "id": "win_rate"},
                                {"name": "Avg P&L", "id": "avg_pnl", "type": "numeric"},
                                {"name": "Profit Factor", "id": "profit_factor", "type": "numeric"},
                                {"name": "Best Trade", "id": "best_trade"},
                                {"name": "Worst Trade", "id": "worst_trade"},
                            ],
                            data=[],
                            style_table={"overflowX": "auto"},
                            style_header=TABLE_HEADER_STYLE,
                            style_cell=TABLE_CELL_STYLE,
                        ),
                    ],
                ),

                # Manual trades — full My Trades entry form + tracker
                html.Div(
                    id="perf-manual-content",
                    hidden=True,
                    # Use the children from build_my_trades_layout() directly (without
                    # the outer my-trades-section wrapper which is reserved for legacy nav)
                    children=build_my_trades_layout().children,
                ),

                # Release notes — markdown rendered from docs/RELEASE_NOTES.md
                html.Div(
                    id="perf-releases-content",
                    hidden=True,
                    className="kb-card",
                    style={"padding": "20px", "maxHeight": "70vh", "overflowY": "auto"},
                    children=[
                        dcc.Markdown(
                            id="perf-releases-md",
                            children=_load_release_notes(),
                            style={"fontSize": "0.92rem", "lineHeight": "1.55"},
                        ),
                    ],
                ),
            ]),
        ],
    )


def _perf_tile(card_id: str, title: str, value: str, color: str,
               icon: str, clickable: bool = False):
    """Build a performance stat tile."""
    children = [
        html.I(className=f"fas {icon} kb-stat-icon",
               style={"color": color}),
        html.Div(value, id=card_id, className="kb-stat-value",
                 style={"color": color}),
        html.P(title, className="kb-stat-label"),
    ]
    if clickable:
        return html.Div(
            id=f"{card_id}-tile",
            n_clicks=0,
            className="kb-stat-card kb-animate-in",
            style={"cursor": "pointer"},
            children=children,
        )
    return dbc.Card(
        className="kb-stat-card kb-animate-in",
        children=children,
    )


def _analytics_card(card_id: str, title: str, value: str, color: str):
    """Small analytics metric card."""
    return dbc.Card(
        className="kb-stat-card",
        style={"borderTop": f"3px solid {color}", "textAlign": "center"},
        children=[
            html.Div(value, id=card_id,
                     style={"color": color, "fontSize": "1.5rem",
                            "fontWeight": "700"}),
            html.Div(title, style={"color": cfg.text_muted,
                                   "fontSize": "0.72rem",
                                   "textTransform": "uppercase"}),
        ],
    )
