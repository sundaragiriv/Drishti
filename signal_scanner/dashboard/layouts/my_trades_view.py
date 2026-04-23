"""My Trades tab — manual trade entry, tracking, and P&L."""

import dash_bootstrap_components as dbc
from dash import dash_table, dcc, html

from signal_scanner.config import DashboardConfig

cfg = DashboardConfig()

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

_INPUT_STYLE = {
    "backgroundColor": cfg.card_color,
    "color": cfg.text_color,
    "border": f"1px solid {cfg.border_color}",
    "borderRadius": "6px",
    "padding": "6px 10px",
    "fontSize": "0.82rem",
    "width": "100%",
}


def _stat_tile(card_id: str, title: str, value: str, color: str, icon: str) -> dbc.Card:
    return dbc.Card(
        className="kb-stat-card text-center",
        style={"borderTop": f"3px solid {color}"},
        children=dbc.CardBody(
            style={"padding": "14px 10px"},
            children=[
                html.I(className=f"fas {icon}", style={"color": color, "fontSize": "1rem", "marginBottom": "6px"}),
                html.Div(value, id=card_id, style={"color": color, "fontSize": "1.3rem", "fontWeight": "700"}),
                html.Div(title, style={"color": cfg.text_muted, "fontSize": "0.7rem", "textTransform": "uppercase", "letterSpacing": "0.05em"}),
            ],
        ),
    )


def build_my_trades_layout() -> html.Div:
    """Build the complete My Trades section."""
    return html.Div(
        id="my-trades-section",
        hidden=True,
        className="kb-animate-in",
        children=[
            # ---- Header ----
            html.Div(
                className="kb-section-header",
                children=[
                    html.H2([
                        "My Trades",
                        html.Span("MANUAL TRACKER", className="kb-section-badge"),
                    ]),
                    html.P(
                        "Track your real trades, monitor P&L, and get exit signals",
                        className="kb-section-desc",
                    ),
                ],
            ),

            # ---- Stat tiles ----
            dbc.Row(
                className="mb-4 g-3",
                children=[
                    dbc.Col(_stat_tile("mt-open-count", "OPEN", "0", cfg.text_color, "fa-folder-open"), md=2),
                    dbc.Col(_stat_tile("mt-closed-count", "CLOSED", "0", cfg.accent_primary, "fa-check-circle"), md=2),
                    dbc.Col(_stat_tile("mt-win-rate", "WIN %", "0%", "#00ff88", "fa-trophy"), md=2),
                    dbc.Col(_stat_tile("mt-realized-pnl", "REALIZED P&L", "$0", "#ffd43b", "fa-coins"), md=2),
                    dbc.Col(_stat_tile("mt-wins", "WINS", "0", cfg.accent_long, "fa-arrow-up"), md=2),
                    dbc.Col(_stat_tile("mt-losses", "LOSSES", "0", cfg.accent_short, "fa-arrow-down"), md=2),
                ],
            ),

            # ---- Entry form ----
            html.Div(
                className="kb-card mb-3",
                children=[
                    html.H5(
                        [html.I(className="fas fa-plus-circle me-2"), "Enter New Trade"],
                        style={"color": cfg.accent_primary, "marginBottom": "12px"},
                    ),
                    dbc.Row(
                        className="g-2 mb-2",
                        children=[
                            dbc.Col([
                                html.Label("Symbol", className="kb-label", style={"fontSize": "0.72rem"}),
                                dcc.Input(id="mt-entry-symbol", type="text", placeholder="AAPL", style=_INPUT_STYLE),
                            ], md=2),
                            dbc.Col([
                                html.Label("Side", className="kb-label", style={"fontSize": "0.72rem"}),
                                dcc.Dropdown(
                                    id="mt-entry-side",
                                    options=[{"label": "LONG", "value": "LONG"}, {"label": "SHORT", "value": "SHORT"}],
                                    value="LONG",
                                    clearable=False,
                                    className="dash-dropdown",
                                ),
                            ], md=1),
                            dbc.Col([
                                html.Label("Instrument", className="kb-label", style={"fontSize": "0.72rem"}),
                                dcc.Dropdown(
                                    id="mt-entry-instrument",
                                    options=[
                                        {"label": "Stock", "value": "STOCK"},
                                        {"label": "Call Option", "value": "CALL"},
                                        {"label": "Put Option", "value": "PUT"},
                                    ],
                                    value="STOCK",
                                    clearable=False,
                                    className="dash-dropdown",
                                ),
                            ], md=2),
                            dbc.Col([
                                html.Label("Entry Price", className="kb-label", style={"fontSize": "0.72rem"}),
                                dcc.Input(id="mt-entry-price", type="number", placeholder="0.00", style=_INPUT_STYLE),
                            ], md=2),
                            dbc.Col([
                                html.Label("Qty", className="kb-label", style={"fontSize": "0.72rem"}),
                                dcc.Input(id="mt-entry-qty", type="number", placeholder="100", value=100, style=_INPUT_STYLE),
                            ], md=1),
                            dbc.Col([
                                html.Label("Stop Loss", className="kb-label", style={"fontSize": "0.72rem"}),
                                dcc.Input(id="mt-entry-stop", type="number", placeholder="0.00", style=_INPUT_STYLE),
                            ], md=2),
                            dbc.Col([
                                html.Label("Target", className="kb-label", style={"fontSize": "0.72rem"}),
                                dcc.Input(id="mt-entry-target", type="number", placeholder="0.00", style=_INPUT_STYLE),
                            ], md=2),
                        ],
                    ),
                    dbc.Row(
                        className="g-2 align-items-end",
                        children=[
                            dbc.Col([
                                html.Label("Source", className="kb-label", style={"fontSize": "0.72rem"}),
                                dcc.Dropdown(
                                    id="mt-entry-source",
                                    options=[
                                        {"label": "Stock Ideas", "value": "MANUAL_STOCK_IDEAS"},
                                        {"label": "AI Signals", "value": "MANUAL_AI_SIGNALS"},
                                        {"label": "Options Ideas", "value": "MANUAL_OPTIONS"},
                                        {"label": "Scanner", "value": "MANUAL_SCANNER"},
                                        {"label": "External", "value": "MANUAL_EXTERNAL"},
                                    ],
                                    value="MANUAL_STOCK_IDEAS",
                                    clearable=False,
                                    className="dash-dropdown",
                                ),
                            ], md=2),
                            dbc.Col([
                                html.Label("Notes", className="kb-label", style={"fontSize": "0.72rem"}),
                                dcc.Input(id="mt-entry-notes", type="text", placeholder="Optional notes...", style=_INPUT_STYLE),
                            ], md=4),
                            # Option fields (hidden by default)
                            dbc.Col([
                                html.Label("Opt Expiry", className="kb-label", style={"fontSize": "0.72rem"}),
                                dcc.Input(id="mt-entry-opt-expiry", type="text", placeholder="2026-03-21", style=_INPUT_STYLE),
                            ], md=2, id="mt-opt-expiry-col", style={"display": "none"}),
                            dbc.Col([
                                html.Label("Opt Strike", className="kb-label", style={"fontSize": "0.72rem"}),
                                dcc.Input(id="mt-entry-opt-strike", type="number", placeholder="150.00", style=_INPUT_STYLE),
                            ], md=2, id="mt-opt-strike-col", style={"display": "none"}),
                            dbc.Col([
                                dbc.Button(
                                    [html.I(className="fas fa-play me-2"), "Enter Trade"],
                                    id="mt-enter-btn",
                                    className="kb-btn-primary",
                                    n_clicks=0,
                                    style={"width": "100%"},
                                ),
                            ], md=2),
                        ],
                    ),
                    html.Div(
                        id="mt-entry-status",
                        className="mt-2",
                        style={"color": cfg.text_muted, "fontSize": "0.78rem"},
                    ),
                ],
            ),

            # ---- Exit form (hidden until row selected) ----
            html.Div(
                id="mt-exit-panel",
                className="kb-card mb-3",
                hidden=True,
                children=[
                    html.H5(
                        [html.I(className="fas fa-sign-out-alt me-2"), "Exit Trade"],
                        style={"color": cfg.accent_short, "marginBottom": "12px"},
                    ),
                    html.Div(id="mt-exit-info", style={"color": cfg.text_color, "fontSize": "0.85rem", "marginBottom": "10px"}),
                    dcc.Store(id="mt-exit-trade-id", data=None),
                    dbc.Row(
                        className="g-2 align-items-end",
                        children=[
                            dbc.Col([
                                html.Label("Exit Price", className="kb-label", style={"fontSize": "0.72rem"}),
                                dcc.Input(id="mt-exit-price", type="number", placeholder="0.00", style=_INPUT_STYLE),
                            ], md=2),
                            dbc.Col([
                                html.Label("Exit Qty", className="kb-label", style={"fontSize": "0.72rem"}),
                                dcc.Input(id="mt-exit-qty", type="number", placeholder="Full position",
                                          disabled=True, style={**_INPUT_STYLE, "opacity": "0.5"}),
                            ], md=2),
                            dbc.Col([
                                html.Label("Notes", className="kb-label", style={"fontSize": "0.72rem"}),
                                dcc.Input(id="mt-exit-notes", type="text", placeholder="Exit reason...", style=_INPUT_STYLE),
                            ], md=3),
                            dbc.Col([
                                dbc.Button(
                                    [html.I(className="fas fa-stop-circle me-2"), "Close Position"],
                                    id="mt-exit-btn",
                                    className="kb-btn-primary",
                                    n_clicks=0,
                                    style={"width": "100%", "backgroundColor": cfg.accent_short, "borderColor": cfg.accent_short},
                                ),
                            ], md=2),
                            dbc.Col([
                                dbc.Button(
                                    "Cancel",
                                    id="mt-exit-cancel-btn",
                                    className="kb-btn-primary",
                                    outline=True,
                                    n_clicks=0,
                                    style={"width": "100%"},
                                ),
                            ], md=1),
                        ],
                    ),
                    html.Div(
                        id="mt-exit-status",
                        className="mt-2",
                        style={"color": cfg.text_muted, "fontSize": "0.78rem"},
                    ),
                ],
            ),

            # ---- Trades table ----
            html.Div(
                className="kb-card",
                children=[
                    html.Div(
                        style={"display": "flex", "justifyContent": "space-between", "alignItems": "center", "marginBottom": "10px"},
                        children=[
                            html.H5("Trade Ledger", style={"color": cfg.accent_primary, "margin": 0}),
                            dcc.Dropdown(
                                id="mt-status-filter",
                                options=[
                                    {"label": "All", "value": "ALL"},
                                    {"label": "Open", "value": "OPEN"},
                                    {"label": "Closed", "value": "CLOSED"},
                                ],
                                value="ALL",
                                clearable=False,
                                className="dash-dropdown",
                                style={"width": "140px"},
                            ),
                        ],
                    ),
                    dash_table.DataTable(
                        id="mt-trades-table",
                        columns=[
                            {"name": "ID", "id": "id", "type": "numeric"},
                            {"name": "Opened", "id": "opened_at"},
                            {"name": "Symbol", "id": "symbol"},
                            {"name": "Side", "id": "side"},
                            {"name": "Type", "id": "instrument_type"},
                            {"name": "Status", "id": "status"},
                            {"name": "Entry", "id": "entry_price", "type": "numeric", "format": dash_table.FormatTemplate.money(2)},
                            {"name": "Qty", "id": "quantity", "type": "numeric"},
                            {"name": "Stop", "id": "stop_loss", "type": "numeric", "format": dash_table.FormatTemplate.money(2)},
                            {"name": "Target", "id": "target_1", "type": "numeric", "format": dash_table.FormatTemplate.money(2)},
                            {"name": "Exit", "id": "exit_price", "type": "numeric", "format": dash_table.FormatTemplate.money(2)},
                            {"name": "Alert", "id": "alert_state"},
                            {"name": "P&L", "id": "realized_pnl", "type": "numeric"},
                            {"name": "P&L %", "id": "realized_pnl_pct", "type": "numeric"},
                            {"name": "Source", "id": "recommendation_source"},
                            {"name": "Exit Reason", "id": "exit_reason"},
                            {"name": "Closed", "id": "closed_at"},
                        ],
                        hidden_columns=["id"],
                        data=[],
                        row_selectable="single",
                        page_size=15,
                        sort_action="native",
                        style_table={"overflowX": "auto"},
                        style_header=TABLE_HEADER_STYLE,
                        style_cell=TABLE_CELL_STYLE,
                        style_data_conditional=[
                            {"if": {"column_id": "symbol"}, "color": "#4da3ff", "fontWeight": "700"},
                            {"if": {"filter_query": '{side} = "LONG"', "column_id": "side"}, "color": cfg.accent_long, "fontWeight": "bold"},
                            {"if": {"filter_query": '{side} = "SHORT"', "column_id": "side"}, "color": cfg.accent_short, "fontWeight": "bold"},
                            {"if": {"filter_query": '{status} = "OPEN"', "column_id": "status"}, "color": cfg.accent_neutral, "fontWeight": "bold"},
                            {"if": {"filter_query": '{status} = "CLOSED"', "column_id": "status"}, "color": "#aaa"},
                            {"if": {"filter_query": "{realized_pnl} > 0", "column_id": "realized_pnl"}, "color": cfg.accent_long, "fontWeight": "bold"},
                            {"if": {"filter_query": "{realized_pnl} < 0", "column_id": "realized_pnl"}, "color": cfg.accent_short, "fontWeight": "bold"},
                            {"if": {"filter_query": "{realized_pnl_pct} > 0", "column_id": "realized_pnl_pct"}, "color": cfg.accent_long},
                            {"if": {"filter_query": "{realized_pnl_pct} < 0", "column_id": "realized_pnl_pct"}, "color": cfg.accent_short},
                            # Alert column styling
                            {"if": {"filter_query": '{alert_state} contains "EXIT"', "column_id": "alert_state"}, "color": cfg.accent_short, "fontWeight": "bold"},
                            {"if": {"filter_query": '{alert_state} contains "WARN"', "column_id": "alert_state"}, "color": cfg.accent_neutral, "fontWeight": "bold"},
                            {"if": {"filter_query": '{alert_state} = "OK"', "column_id": "alert_state"}, "color": cfg.accent_long},
                        ],
                    ),
                ],
            ),
        ],
    )
