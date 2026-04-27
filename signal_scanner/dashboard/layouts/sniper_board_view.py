"""Swing Snipers — Consolidated EV-ranked trade ideas.

Single table merging Swing, Triple Lock, Squeeze, Options, and AI signals.
Sorted by EV Score descending. Visual language: icons, badges, color-coded R:R.
Decision in 3 seconds — detail on click-expand.
"""

import dash_bootstrap_components as dbc
from dash import dash_table, dcc, html

from signal_scanner.config import DashboardConfig
from signal_scanner.dashboard.layouts.main_view import TABLE_HEADER_STYLE, TABLE_CELL_STYLE

cfg = DashboardConfig()

# ---------------------------------------------------------------------------
# Swing Snipers column definitions
# ---------------------------------------------------------------------------
SNIPER_COLUMNS = [
    {"name": "#", "id": "rank", "type": "numeric"},
    {"name": "Tier", "id": "tier"},
    {"name": "Symbol", "id": "symbol"},
    {"name": "Status", "id": "daily_status"},
    {"name": "Side", "id": "side"},
    {"name": "Now $", "id": "current_price", "type": "numeric",
     "format": dash_table.FormatTemplate.money(2)},
    {"name": "Thesis $", "id": "thesis_price", "type": "numeric",
     "format": dash_table.FormatTemplate.money(2)},
    {"name": "Dist %", "id": "distance_pct", "type": "numeric"},
    {"name": "Stop", "id": "stop_price", "type": "numeric",
     "format": dash_table.FormatTemplate.money(2)},
    {"name": "T1", "id": "target_1", "type": "numeric",
     "format": dash_table.FormatTemplate.money(2)},
    {"name": "R:R", "id": "rr_ratio", "type": "numeric"},
    {"name": "Conv", "id": "_conviction", "type": "numeric"},
    {"name": "Source", "id": "source_badge"},
    {"name": "Confirmed", "id": "convergence"},
    {"name": "Pressure", "id": "flow", "type": "numeric"},
    {"name": "Fresh", "id": "thesis_freshness", "type": "numeric"},
    {"name": "Opts", "id": "options_available"},
]


def build_sniper_board_layout() -> html.Div:
    """Build the Swing Snipers section layout."""
    return html.Div(
        id="sniper-board-section",
        hidden=False,  # Default landing page — visible on load
        className="kb-animate-in",
        children=[
            # Section header
            html.Div(
                className="kb-section-header",
                style={"display": "flex", "alignItems": "center",
                       "justifyContent": "space-between"},
                children=[
                    html.Div([
                        # Objective question — the one thing this dashboard exists to answer.
                        # Stays as the page H2 so the user sees it the moment they land.
                        html.H2([
                            "What stocks will make me money today?",
                        ], style={"fontWeight": "700", "marginBottom": "4px"}),
                        html.Div(
                            style={"display": "flex", "gap": "16px", "alignItems": "center"},
                            children=[
                                html.P(
                                    "Top setups, EV-ranked, after costs. Empty answer = sit out.",
                                    className="kb-section-desc",
                                    style={"margin": "0"},
                                ),
                                # Data freshness timestamp
                                html.Span(
                                    id="sniper-freshness-badge",
                                    style={
                                        "fontSize": "11px",
                                        "padding": "2px 8px",
                                        "borderRadius": "4px",
                                        "background": "rgba(212, 160, 23, 0.15)",
                                        "color": "#D4A017",
                                        "border": "1px solid rgba(212, 160, 23, 0.3)",
                                        "whiteSpace": "nowrap",
                                    },
                                ),
                                # Degraded mode banner
                                html.Span(
                                    id="sniper-degraded-banner",
                                    style={
                                        "fontSize": "11px",
                                        "padding": "2px 8px",
                                        "borderRadius": "4px",
                                        "background": "rgba(255, 0, 110, 0.15)",
                                        "color": "#ff4488",
                                        "border": "1px solid rgba(255, 0, 110, 0.3)",
                                        "display": "none",
                                    },
                                ),
                            ],
                        ),
                    ]),
                    # Filter controls
                    html.Div(
                        style={"display": "flex", "gap": "12px",
                               "alignItems": "center"},
                        children=[
                            # Regime toggle
                            html.Div(
                                style={"display": "flex", "alignItems": "center",
                                       "gap": "6px"},
                                children=[
                                    html.Span("Regime-Aligned",
                                              className="kb-label",
                                              style={"fontSize": "0.75rem"}),
                                    dbc.Switch(
                                        id="sniper-regime-toggle",
                                        value=False,
                                        className="kb-switch",
                                    ),
                                ],
                            ),
                            # Side filter
                            dbc.RadioItems(
                                id="sniper-side-filter",
                                options=[
                                    {"label": "ALL", "value": "ALL"},
                                    {"label": "LONG", "value": "LONG"},
                                    {"label": "SHORT", "value": "SHORT"},
                                ],
                                value="ALL",
                                inline=True,
                                className="kb-radio-pills",
                                inputClassName="btn-check",
                                labelClassName="btn btn-sm btn-outline-secondary",
                                labelCheckedClassName="btn btn-sm btn-primary",
                            ),
                            # Source filter
                            dcc.Dropdown(
                                id="sniper-source-filter",
                                options=[
                                    {"label": "All Sources", "value": "ALL"},
                                    {"label": "Swing", "value": "SWING"},
                                    {"label": "Triple Lock", "value": "TRIPLE_LOCK"},
                                    {"label": "Squeeze", "value": "SQUEEZE"},
                                    {"label": "Pullback", "value": "PULLBACK"},
                                    {"label": "Convergence", "value": "CONVERGENCE"},
                                    {"label": "Confluence", "value": "CONFLUENCE"},
                                    {"label": "Breakout", "value": "BREAKOUT"},
                                    {"label": "Distribution", "value": "DISTRIBUTION"},
                                ],
                                value="ALL",
                                clearable=False,
                                style={
                                    "width": "140px",
                                    "backgroundColor": cfg.card_color,
                                    "color": cfg.text_color,
                                    "fontSize": "0.78rem",
                                },
                            ),
                        ],
                    ),
                ],
            ),

            # KPI strip (compact)
            html.Div(
                style={"display": "flex", "gap": "16px", "marginBottom": "12px",
                       "flexWrap": "wrap", "alignItems": "center"},
                children=[
                    _kpi_chip("sniper-total", "SETUPS", "0", cfg.accent_primary),
                    _kpi_chip("sniper-long", "LONG", "0", cfg.accent_long),
                    _kpi_chip("sniper-short", "SHORT", "0", cfg.accent_short),
                    _kpi_chip("sniper-triple", "TRIPLE LOCK", "0", "#ffd43b"),
                    _kpi_chip("sniper-avg-rr", "AVG R:R", "0", "#00ff88"),
                    _kpi_chip("sniper-regime", "REGIME", "---", cfg.accent_neutral),
                ],
            ),

            # Data refresh store
            dcc.Store(id="sniper-data-store", data=[]),
            dcc.Interval(
                id="sniper-refresh-interval",
                interval=60 * 1000,  # refresh every 60s
                n_intervals=0,
            ),

            # ---- THE ANSWER — top 10 setups, the actionable list ----
            html.Div(
                className="kb-card",
                style={"marginBottom": "16px",
                       "border": "1px solid rgba(212, 160, 23, 0.4)",
                       "background": "linear-gradient(180deg, rgba(212,160,23,0.06) 0%, rgba(212,160,23,0.0) 100%)"},
                children=[
                    html.Div(
                        style={"display": "flex", "alignItems": "center",
                               "gap": "10px", "marginBottom": "8px",
                               "padding": "0 4px"},
                        children=[
                            html.I(className="fas fa-bullseye",
                                   style={"color": "#D4A017", "fontSize": "14px"}),
                            html.Span("TODAY'S TOP 10",
                                      style={"fontWeight": "700",
                                             "letterSpacing": "0.08em",
                                             "fontSize": "0.78rem",
                                             "color": "#D4A017"}),
                            html.Span(id="sniper-top10-summary",
                                      style={"fontSize": "0.78rem",
                                             "color": "rgba(255,255,255,0.65)",
                                             "marginLeft": "auto"}),
                        ],
                    ),
                    dash_table.DataTable(
                        id="sniper-top10-table",
                        columns=[
                            {"name": "#",       "id": "rank",       "type": "numeric"},
                            {"name": "Symbol",  "id": "symbol"},
                            {"name": "Side",    "id": "side"},
                            {"name": "Now",     "id": "current_price",
                             "type": "numeric",
                             "format": dash_table.FormatTemplate.money(2)},
                            {"name": "Stop",    "id": "stop_price",
                             "type": "numeric",
                             "format": dash_table.FormatTemplate.money(2)},
                            {"name": "T1",      "id": "target_1",
                             "type": "numeric",
                             "format": dash_table.FormatTemplate.money(2)},
                            {"name": "R:R",     "id": "rr_ratio",   "type": "numeric"},
                            {"name": "Conv",    "id": "_conviction","type": "numeric"},
                            {"name": "Source",  "id": "source_badge"},
                            {"name": "Status",  "id": "daily_status"},
                        ],
                        data=[],
                        page_size=10,
                        sort_action="none",
                        row_selectable=False,
                        style_table={"overflowX": "auto"},
                        style_header=TABLE_HEADER_STYLE,
                        style_cell=TABLE_CELL_STYLE,
                        style_data_conditional=[
                            {"if": {"column_id": "symbol"},
                             "color": "#4da3ff", "fontWeight": "700"},
                            {"if": {"filter_query": "{side} = 'LONG'", "column_id": "side"},
                             "color": cfg.accent_long, "fontWeight": "600"},
                            {"if": {"filter_query": "{side} = 'SHORT'", "column_id": "side"},
                             "color": cfg.accent_short, "fontWeight": "600"},
                        ],
                    ),
                ],
            ),

            # Main sniper table
            html.Div(
                className="kb-card",
                children=[
                    dash_table.DataTable(
                        id="sniper-board-table",
                        columns=SNIPER_COLUMNS,
                        data=[],
                        page_size=20,
                        sort_action="native",
                        sort_by=[{"column_id": "ev_score", "direction": "desc"}],
                        row_selectable=False,
                        style_table={"overflowX": "auto"},
                        style_header=TABLE_HEADER_STYLE,
                        style_cell={
                            **TABLE_CELL_STYLE,
                            "textAlign": "center",
                        },
                        style_cell_conditional=[
                            {"if": {"column_id": "symbol"},
                             "textAlign": "left", "fontWeight": "700"},
                            {"if": {"column_id": "source_badge"},
                             "textAlign": "center"},
                        ],
                        style_data_conditional=[
                            # Symbol clickable
                            {"if": {"column_id": "symbol"},
                             "color": "#4da3ff", "cursor": "pointer",
                             "textDecoration": "underline"},
                            # Side colors
                            {"if": {"filter_query": '{side} = "LONG"',
                                    "column_id": "side"},
                             "color": cfg.accent_long, "fontWeight": "bold"},
                            {"if": {"filter_query": '{side} = "SHORT"',
                                    "column_id": "side"},
                             "color": cfg.accent_short, "fontWeight": "bold"},
                            # R:R color coding
                            {"if": {"filter_query": "{rr_ratio} >= 2.5",
                                    "column_id": "rr_ratio"},
                             "color": "#00ff88", "fontWeight": "bold"},
                            {"if": {"filter_query": "{rr_ratio} >= 2 && {rr_ratio} < 2.5",
                                    "column_id": "rr_ratio"},
                             "color": "#ffd43b"},
                            {"if": {"filter_query": "{rr_ratio} < 2",
                                    "column_id": "rr_ratio"},
                             "color": "#ff4488"},
                            # EV score highlight
                            {"if": {"filter_query": "{ev_score} >= 5",
                                    "column_id": "ev_score"},
                             "color": "#00ff88", "fontWeight": "bold"},
                            # Source badge colors
                            {"if": {"filter_query": '{source_badge} = "TRIPLE_LOCK"',
                                    "column_id": "source_badge"},
                             "color": "#ffd43b", "fontWeight": "bold",
                             "backgroundColor": "#1a1500"},
                            {"if": {"filter_query": '{source_badge} = "SQUEEZE"',
                                    "column_id": "source_badge"},
                             "color": "#ff8c00", "fontWeight": "bold"},
                            {"if": {"filter_query": '{source_badge} = "SWING"',
                                    "column_id": "source_badge"},
                             "color": "#b388ff"},
                            {"if": {"filter_query": '{source_badge} = "OPTIONS"',
                                    "column_id": "source_badge"},
                             "color": "#4da3ff"},
                            {"if": {"filter_query": '{source_badge} = "AI"',
                                    "column_id": "source_badge"},
                             "color": "#ff69b4"},
                            # Regime badge colors
                            {"if": {"filter_query": '{regime_badge} = "TRENDING"',
                                    "column_id": "regime_badge"},
                             "color": "#00ff88", "fontWeight": "bold"},
                            {"if": {"filter_query": '{regime_badge} = "MEAN-REV"',
                                    "column_id": "regime_badge"},
                             "color": "#4da3ff", "fontWeight": "bold"},
                            {"if": {"filter_query": '{regime_badge} = "ACCUM"',
                                    "column_id": "regime_badge"},
                             "color": "#ffd43b", "fontWeight": "bold"},
                            {"if": {"filter_query": '{regime_badge} = "DISTRIB"',
                                    "column_id": "regime_badge"},
                             "color": "#ff8c00", "fontWeight": "bold"},
                            {"if": {"filter_query": '{regime_badge} = "CRASH"',
                                    "column_id": "regime_badge"},
                             "color": "#ff4488", "fontWeight": "bold",
                             "backgroundColor": "#1a0000"},
                            # Daily status colors
                            {"if": {"filter_query": '{daily_status} = "RECONFIRMED"',
                                    "column_id": "daily_status"},
                             "color": "#00ff88", "fontWeight": "bold",
                             "backgroundColor": "#001a0d"},
                            {"if": {"filter_query": '{daily_status} = "ACTIVE"',
                                    "column_id": "daily_status"},
                             "color": "#4da3ff", "fontWeight": "bold"},
                            {"if": {"filter_query": '{daily_status} = "STRETCHED"',
                                    "column_id": "daily_status"},
                             "color": "#ffd43b"},
                            {"if": {"filter_query": '{daily_status} = "STALE"',
                                    "column_id": "daily_status"},
                             "color": "#666"},
                            {"if": {"filter_query": '{daily_status} = "MISSED"',
                                    "column_id": "daily_status"},
                             "color": "#555", "fontStyle": "italic"},
                            {"if": {"filter_query": '{daily_status} = "INVALIDATED"',
                                    "column_id": "daily_status"},
                             "color": "#ff4488", "textDecoration": "line-through"},
                            # Distance % coloring
                            {"if": {"filter_query": "{distance_pct} > 10",
                                    "column_id": "distance_pct"},
                             "color": "#ffd43b"},
                            {"if": {"filter_query": "{distance_pct} > 18",
                                    "column_id": "distance_pct"},
                             "color": "#ff4488"},
                            {"if": {"filter_query": "{distance_pct} < -10",
                                    "column_id": "distance_pct"},
                             "color": "#ff4488"},
                            # Tier colors
                            {"if": {"filter_query": '{tier} = "Platinum"',
                                    "column_id": "tier"},
                             "color": "#e5e4e2", "fontWeight": "bold",
                             "backgroundColor": "#1a1a2e"},
                            {"if": {"filter_query": '{tier} = "Gold"',
                                    "column_id": "tier"},
                             "color": "#ffd700", "fontWeight": "bold"},
                            {"if": {"filter_query": '{tier} = "Silver"',
                                    "column_id": "tier"},
                             "color": "#c0c0c0"},
                            {"if": {"filter_query": '{tier} = "Bronze"',
                                    "column_id": "tier"},
                             "color": "#cd7f32"},
                            {"if": {"filter_query": '{tier} = "Avoid"',
                                    "column_id": "tier"},
                             "color": "#ff4488", "fontStyle": "italic"},
                            # Options available
                            {"if": {"filter_query": '{options_available} = "Yes"',
                                    "column_id": "options_available"},
                             "color": "#a78bfa", "fontWeight": "bold"},
                        ],
                    ),
                ],
            ),

            # Expandable detail (shown when a row is clicked)
            html.Div(
                id="sniper-detail-panel",
                hidden=True,
                className="kb-card mt-3",
                style={"borderLeft": f"3px solid {cfg.accent_primary}",
                       "padding": "16px"},
                children=[
                    html.Div(
                        style={"display": "flex", "justifyContent": "space-between",
                               "alignItems": "center", "marginBottom": "12px"},
                        children=[
                            html.Div(style={"display": "flex", "alignItems": "center", "gap": "12px"},
                                     children=[
                                html.H4(id="sniper-detail-symbol",
                                        style={"color": cfg.accent_primary, "margin": "0"}),
                                html.Span(id="sniper-detail-idea-state",
                                          className="badge bg-secondary",
                                          style={"fontSize": "0.75rem"}),
                            ]),
                            html.Div(style={"display": "flex", "gap": "8px"}, children=[
                                html.Button(
                                    "Enter Trade",
                                    id="sniper-enter-trade",
                                    n_clicks=0,
                                    className="btn btn-sm btn-success",
                                ),
                                html.Button(
                                    "Watch",
                                    id="sniper-watch-idea",
                                    n_clicks=0,
                                    className="btn btn-sm btn-outline-warning",
                                ),
                                html.Button(
                                    "View Full ISR",
                                    id="sniper-goto-isr",
                                    n_clicks=0,
                                    className="btn btn-sm btn-outline-primary",
                                ),
                            ]),
                        ],
                    ),
                    # Hidden store for idea_id
                    dcc.Store(id="sniper-detail-idea-id", data=None),
                    # Feedback toast
                    dbc.Toast(
                        id="sniper-action-toast",
                        header="Action",
                        is_open=False,
                        dismissable=True,
                        duration=4000,
                        style={"position": "fixed", "top": 10, "right": 10, "width": 350},
                    ),
                    dbc.Row([
                        dbc.Col([
                            html.Span("Conviction", className="kb-label"),
                            html.Div(id="sniper-detail-conviction",
                                     className="kb-stat-value",
                                     style={"fontSize": "1.1rem"}),
                        ], md=2),
                        dbc.Col([
                            html.Span("ML Score", className="kb-label"),
                            html.Div(id="sniper-detail-ml",
                                     className="kb-stat-value",
                                     style={"fontSize": "1.1rem"}),
                        ], md=2),
                        dbc.Col([
                            html.Span("Phase", className="kb-label"),
                            html.Div(id="sniper-detail-phase",
                                     className="kb-stat-value",
                                     style={"fontSize": "1.1rem"}),
                        ], md=2),
                        dbc.Col([
                            html.Span("Pressure", className="kb-label"),
                            html.Div(id="sniper-detail-pressure",
                                     className="kb-stat-value",
                                     style={"fontSize": "1.1rem"}),
                        ], md=2),
                        dbc.Col([
                            html.Span("Squeeze", className="kb-label"),
                            html.Div(id="sniper-detail-squeeze",
                                     className="kb-stat-value",
                                     style={"fontSize": "1.1rem"}),
                        ], md=2),
                        dbc.Col([
                            html.Span("Insider Effect", className="kb-label"),
                            html.Div(id="sniper-detail-insider",
                                     className="kb-stat-value",
                                     style={"fontSize": "1.1rem"}),
                        ], md=2),
                    ]),
                ],
            ),

            # Trade entry modal — proper operator capture
            dbc.Modal(
                id="sniper-trade-modal",
                is_open=False,
                size="md",
                children=[
                    dbc.ModalHeader(dbc.ModalTitle(id="sniper-trade-modal-title")),
                    dbc.ModalBody([
                        dbc.Row([
                            dbc.Col([
                                dbc.Label("Entry Price"),
                                dbc.Input(id="sniper-trade-price", type="number",
                                          step=0.01, placeholder="Actual entry price"),
                            ], md=6),
                            dbc.Col([
                                dbc.Label("Quantity"),
                                dbc.Input(id="sniper-trade-qty", type="number",
                                          step=1, placeholder="Shares"),
                            ], md=6),
                        ], className="mb-3"),
                        dbc.Row([
                            dbc.Col([
                                dbc.Label("Stop Loss"),
                                dbc.Input(id="sniper-trade-stop", type="number",
                                          step=0.01, placeholder="Stop price"),
                            ], md=6),
                            dbc.Col([
                                dbc.Label("Target"),
                                dbc.Input(id="sniper-trade-target", type="number",
                                          step=0.01, placeholder="Target price"),
                            ], md=6),
                        ], className="mb-3"),
                        dbc.Row([
                            dbc.Col([
                                dbc.Label("Entry Time (optional)"),
                                dbc.Input(id="sniper-trade-time", type="text",
                                          placeholder="e.g. 2026-03-17 10:15"),
                            ], md=6),
                            dbc.Col([
                                dbc.Label("Notes (optional)"),
                                dbc.Input(id="sniper-trade-notes", type="text",
                                          placeholder="Trade notes"),
                            ], md=6),
                        ], className="mb-3"),
                    ]),
                    dbc.ModalFooter([
                        html.Button("Cancel", id="sniper-trade-cancel",
                                    className="btn btn-secondary", n_clicks=0),
                        html.Button("Confirm Entry", id="sniper-trade-confirm",
                                    className="btn btn-success", n_clicks=0),
                    ]),
                ],
            ),
        ],
    )


def _kpi_chip(card_id: str, title: str, value: str, color: str) -> html.Div:
    """Compact KPI chip — inline, not a big card."""
    return html.Div(
        style={
            "display": "flex", "alignItems": "center", "gap": "6px",
            "padding": "4px 10px", "borderRadius": "4px",
            "border": f"1px solid {color}33",
            "backgroundColor": f"{color}11",
        },
        children=[
            html.Span(title, style={"fontSize": "0.65rem", "color": "#888",
                                     "textTransform": "uppercase", "letterSpacing": "0.05em"}),
            html.Span(value, id=card_id, style={"fontSize": "0.9rem", "fontWeight": "800",
                                                  "color": color,
                                                  "fontFamily": "'JetBrains Mono', monospace"}),
        ],
    )


def _sniper_tile(card_id: str, title: str, value: str,
                 color: str, icon: str) -> dbc.Card:
    """Legacy stat tile — kept for backward compat."""
    return dbc.Card(
        className="kb-stat-card kb-animate-in",
        children=[
            html.I(className=f"fas {icon} kb-stat-icon",
                   style={"color": color}),
            html.Div(value, id=card_id, className="kb-stat-value",
                     style={"color": color}),
            html.P(title, className="kb-stat-label"),
        ],
    )
