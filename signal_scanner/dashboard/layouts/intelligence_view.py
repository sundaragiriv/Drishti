"""Intelligence Dashboard — Accumulation Radar, Command Center, Sector Clock.

Screens:
  A: Intelligence Command Center — top conviction setups
  B: Accumulation Radar — scatter plot of accumulation vs price response
  C: Sector Rotation Clock — sector inflow/outflow visual
  D: Individual Stock Deep Dive — per-ticker accumulation timeline
"""

from __future__ import annotations

import dash_bootstrap_components as dbc
import plotly.graph_objects as go
from dash import dash_table, dcc, html

from signal_scanner.config import DashboardConfig
from signal_scanner.dashboard.layouts.main_view import TABLE_CELL_STYLE, TABLE_HEADER_STYLE

cfg = DashboardConfig()

# Phase color map
PHASE_COLORS = {
    "ACTIVE_ACCUM":  "#00ff88",   # bright green  — primary buy zone
    "EARLY_ACCUM":   "#7bc47f",   # medium green  — early stage
    "LATE_ACCUM":    "#ffd43b",   # gold          — late but ok
    "EXPANSION":     "#ff8c00",   # orange        — fully priced
    "DORMANT":       "#555577",   # muted blue    — no activity
    "DISTRIBUTION":  "#ff4488",   # pink/red      — smart money exiting
    "DECLINE":       "#cc0000",   # red           — avoid
}

PHASE_LABELS = {
    "ACTIVE_ACCUM":  "Active Accum",
    "EARLY_ACCUM":   "Early Accum",
    "LATE_ACCUM":    "Late Accum",
    "EXPANSION":     "Expansion",
    "DORMANT":       "Dormant",
    "DISTRIBUTION":  "Distribution",
    "DECLINE":       "Decline",
}

SIGNAL_COLORS = {
    "BUY":       "#00ff88",
    "ACCUMULATE": "#7bc47f",
    "WATCH":     "#ffd43b",
    "HOLD":      "#aaaaaa",
    "AVOID":     "#ff8c00",
    "REDUCE":    "#ff4488",
    "EXIT":      "#cc0000",
    "SHORT":     "#cc0000",
    "LONG_ONLY": "#00ff88",
    "SHORT_ONLY":"#ff4488",
    "NEUTRAL":   "#aaaaaa",
}

_CARD_STYLE = {
    "backgroundColor": cfg.card_color,
    "border": f"1px solid {cfg.border_color}",
    "borderRadius": "8px",
    "padding": "16px",
}

_SECTION_TITLE_STYLE = {
    "color": cfg.accent_primary,
    "fontWeight": "700",
    "fontSize": "0.75rem",
    "letterSpacing": "0.1em",
    "textTransform": "uppercase",
    "marginBottom": "12px",
}


# ---------------------------------------------------------------------------
# Screen B: Accumulation Radar (scatter plot)
# ---------------------------------------------------------------------------

def _build_accumulation_radar() -> html.Div:
    return html.Div([
        html.Div([
            html.P("ACCUMULATION RADAR", style=_SECTION_TITLE_STYLE),
            html.P(
                "Bottom-right = BUY ZONE (strong accumulation, price not yet responded). "
                "Click any point to analyze that ticker.",
                style={"color": cfg.text_muted, "fontSize": "0.78rem", "marginBottom": "12px"},
            ),
        ]),
        dcc.Loading(
            type="circle",
            color=cfg.accent_primary,
            children=dcc.Graph(
                id="intel-radar-chart",
                style={"height": "520px"},
                config={"displayModeBar": False},
                figure=_empty_radar_figure(),
            ),
        ),
        html.Div([
            html.Span("Filter: ", style={"color": cfg.text_muted, "fontSize": "0.8rem"}),
            dcc.Dropdown(
                id="intel-radar-phase-filter",
                className="dash-dropdown",
                options=[
                    {"label": "All Phases", "value": "ALL"},
                    {"label": "Buy Zone Only (Early + Active Accum)", "value": "BUY_ZONE"},
                    {"label": "Distribution Warnings", "value": "DIST"},
                ],
                value="ALL",
                clearable=False,
                style={
                    "width": "260px",
                    "display": "inline-block",
                    "fontSize": "0.8rem",
                },
            ),
        ], style={"marginTop": "8px"}),
    ], style=_CARD_STYLE)


def _empty_radar_figure() -> go.Figure:
    fig = go.Figure()
    fig.update_layout(
        paper_bgcolor=cfg.card_color,
        plot_bgcolor=cfg.bg_color,
        font={"color": cfg.text_color, "size": 11},
        xaxis=dict(
            title=dict(text="Accumulation Strength Score →", font={"color": cfg.text_muted, "size": 10}),
            gridcolor=cfg.border_color,
            zeroline=False,
            range=[0, 105],
        ),
        yaxis=dict(
            title=dict(text="Price Response (QoQ %) →", font={"color": cfg.text_muted, "size": 10}),
            gridcolor=cfg.border_color,
            zeroline=True,
            zerolinecolor=cfg.border_color,
        ),
        margin={"l": 50, "r": 20, "t": 40, "b": 50},
        annotations=[
            dict(x=80, y=-15, text="◀ BUY ZONE ▶", showarrow=False,
                 font={"color": "#00ff88", "size": 11}, opacity=0.6),
            dict(x=80, y=40, text="EXPANSION",  showarrow=False,
                 font={"color": "#ff8c00", "size": 10}, opacity=0.5),
            dict(x=15, y=30, text="DISTRIBUTION", showarrow=False,
                 font={"color": "#ff4488", "size": 10}, opacity=0.5),
            dict(x=15, y=-15, text="DORMANT", showarrow=False,
                 font={"color": cfg.text_muted, "size": 10}, opacity=0.5),
        ],
        legend=dict(
            bgcolor=cfg.card_color,
            bordercolor=cfg.border_color,
            font={"color": cfg.text_color, "size": 10},
        ),
    )
    return fig


# ---------------------------------------------------------------------------
# Screen A: Command Center
# ---------------------------------------------------------------------------

COMMAND_CENTER_COLUMNS = [
    {"name": "🔒",           "id": "triple_lock"},
    {"name": "Ticker",      "id": "ticker"},
    {"name": "Company",     "id": "company"},
    {"name": "Sector",      "id": "sector"},
    {"name": "Phase",       "id": "accum_phase"},
    {"name": "Conviction",  "id": "conviction_score",   "type": "numeric"},
    {"name": "ML v2",       "id": "ml_score_v2",        "type": "numeric"},
    {"name": "Lag (Q)",     "id": "expected_impact_quarters", "type": "numeric"},
    {"name": "Streak (Q)",  "id": "accum_phase_quarters","type": "numeric"},
    {"name": "Tier-1 Mgrs", "id": "tier1_manager_count","type": "numeric"},
    {"name": "F4 Insiders", "id": "inst_f4_distinct_60d", "type": "numeric"},
    {"name": "Mom 90d%",    "id": "price_momentum_90d", "type": "numeric"},
    {"name": "Cascade",     "id": "cascade_stage",      "type": "numeric"},
    {"name": "Insider",     "id": "insider_cluster_detected"},
    {"name": "Day Bias",    "id": "day_bias"},
    {"name": "Swing",       "id": "swing_signal"},
    {"name": "Long Term",   "id": "longterm_signal"},
]

DISTRIBUTION_COLUMNS = [
    {"name": "Ticker",      "id": "ticker"},
    {"name": "Company",     "id": "company"},
    {"name": "Sector",      "id": "sector"},
    {"name": "Severity",    "id": "distribution_severity"},
    {"name": "Conviction",  "id": "conviction_score", "type": "numeric"},
    {"name": "Phase",       "id": "accum_phase"},
    {"name": "Swing",       "id": "swing_signal"},
    {"name": "Long Term",   "id": "longterm_signal"},
]


def _build_command_center() -> html.Div:
    return html.Div([
        # Top conviction setups
        dbc.Row([
            dbc.Col([
                html.Div([
                    html.P("TOP CONVICTION SETUPS", style=_SECTION_TITLE_STYLE),
                    html.P(
                        "Ranked by conviction score. Phase = EARLY or ACTIVE ACCUM only. "
                        "Click ticker → Deep Dive.",
                        style={"color": cfg.text_muted, "fontSize": "0.78rem", "marginBottom": "10px"},
                    ),
                    dcc.Loading(
                        type="dot",
                        color=cfg.accent_primary,
                        children=dash_table.DataTable(
                            id="intel-command-table",
                            columns=COMMAND_CENTER_COLUMNS,
                            data=[],
                            page_size=20,
                            sort_action="native",
                            filter_action="native",
                            style_table={"overflowX": "auto"},
                            style_header=TABLE_HEADER_STYLE,
                            style_cell={**TABLE_CELL_STYLE, "minWidth": "70px"},
                            style_data_conditional=[
                                # Clickable ticker column
                                {"if": {"column_id": "ticker"}, "color": "#4da3ff", "cursor": "pointer", "fontWeight": "700", "textDecoration": "underline"},
                                # Phase coloring
                                *[
                                    {
                                        "if": {"filter_query": f'{{accum_phase}} = "{phase}"',
                                               "column_id": "accum_phase"},
                                        "color": color, "fontWeight": "bold",
                                    }
                                    for phase, color in PHASE_COLORS.items()
                                ],
                                # Signal coloring
                                *[
                                    {
                                        "if": {"filter_query": f'{{swing_signal}} = "{sig}"',
                                               "column_id": "swing_signal"},
                                        "color": SIGNAL_COLORS.get(sig, cfg.text_color),
                                        "fontWeight": "bold",
                                    }
                                    for sig in ["BUY", "WATCH", "AVOID", "SHORT"]
                                ],
                                # Insider cluster highlight
                                {
                                    "if": {"filter_query": '{insider_cluster_detected} = "True"',
                                           "column_id": "insider_cluster_detected"},
                                    "color": "#00ff88", "fontWeight": "bold",
                                },
                                # Day bias coloring
                                {
                                    "if": {"filter_query": '{day_bias} = "LONG_ONLY"',
                                           "column_id": "day_bias"},
                                    "color": "#00ff88",
                                },
                                {
                                    "if": {"filter_query": '{day_bias} = "SHORT_ONLY"',
                                           "column_id": "day_bias"},
                                    "color": "#ff4488",
                                },
                                # Triple Lock row — gold highlight (all 3 signals)
                                {
                                    "if": {"filter_query": '{triple_lock} = "🔒"'},
                                    "backgroundColor": "#1a1500",
                                    "border": "1px solid #ffd700",
                                },
                                # Triple Lock icon column colour
                                {
                                    "if": {"filter_query": '{triple_lock} = "🔒"',
                                           "column_id": "triple_lock"},
                                    "color": "#ffd700", "fontWeight": "bold", "fontSize": "1.0rem",
                                },
                                # ML v2 column color gradient via thresholds
                                {
                                    "if": {"filter_query": "{ml_score_v2} >= 65",
                                           "column_id": "ml_score_v2"},
                                    "color": "#00ff88", "fontWeight": "bold",
                                },
                                {
                                    "if": {"filter_query": "{ml_score_v2} < 35 && {ml_score_v2} > 0",
                                           "column_id": "ml_score_v2"},
                                    "color": "#ff4488",
                                },
                                # Positive momentum highlight
                                {
                                    "if": {"filter_query": "{price_momentum_90d} >= 10",
                                           "column_id": "price_momentum_90d"},
                                    "color": "#00ff88",
                                },
                                {
                                    "if": {"filter_query": "{price_momentum_90d} < -10",
                                           "column_id": "price_momentum_90d"},
                                    "color": "#ff4488",
                                },
                                # High conviction highlight row
                                {
                                    "if": {"filter_query": "{conviction_score} >= 70"},
                                    "backgroundColor": "#0a1a10",
                                },
                            ],
                            style_data={"backgroundColor": cfg.bg_color, "color": cfg.text_color},
                        ),
                    ),
                ], style=_CARD_STYLE),
            ], md=12),
        ], className="mb-3"),

        # Distribution warnings
        dbc.Row([
            dbc.Col([
                html.Div([
                    html.P("DISTRIBUTION WARNINGS — Potential Exits / Shorts",
                           style={**_SECTION_TITLE_STYLE, "color": "#ff4488"}),
                    dcc.Loading(
                        type="dot",
                        color="#ff4488",
                        children=dash_table.DataTable(
                            id="intel-distribution-table",
                            columns=DISTRIBUTION_COLUMNS,
                            data=[],
                            page_size=10,
                            sort_action="native",
                            style_table={"overflowX": "auto"},
                            style_header={**TABLE_HEADER_STYLE, "color": "#ff4488"},
                            style_cell=TABLE_CELL_STYLE,
                            style_data_conditional=[
                                {"if": {"column_id": "ticker"}, "color": "#4da3ff", "cursor": "pointer", "fontWeight": "700", "textDecoration": "underline"},
                                {"if": {"filter_query": '{distribution_severity} = "SEVERE"',
                                        "column_id": "distribution_severity"},
                                 "color": "#cc0000", "fontWeight": "bold"},
                                {"if": {"filter_query": '{distribution_severity} = "MODERATE"',
                                        "column_id": "distribution_severity"},
                                 "color": "#ff4488", "fontWeight": "bold"},
                                {"if": {"filter_query": '{distribution_severity} = "MILD"',
                                        "column_id": "distribution_severity"},
                                 "color": "#ffd43b"},
                            ],
                            style_data={"backgroundColor": cfg.bg_color, "color": cfg.text_color},
                        ),
                    ),
                ], style={**_CARD_STYLE, "borderColor": "#ff448830"}),
            ], md=12),
        ]),
    ])


# ---------------------------------------------------------------------------
# Screen C: Sector Rotation Clock (bar chart — more practical than circular)
# ---------------------------------------------------------------------------

def _build_sector_clock() -> html.Div:
    return html.Div([
        html.P("SECTOR ROTATION CLOCK", style=_SECTION_TITLE_STYLE),
        html.P(
            "Net institutional capital flows by sector. Green = inflow, Red = outflow.",
            style={"color": cfg.text_muted, "fontSize": "0.78rem", "marginBottom": "12px"},
        ),
        dcc.Loading(
            type="circle",
            color=cfg.accent_primary,
            children=dcc.Graph(
                id="intel-sector-clock-chart",
                style={"height": "380px"},
                config={"displayModeBar": False},
                figure=_empty_sector_figure(),
            ),
        ),
        dbc.Row([
            dbc.Col([
                html.Div(id="intel-cycle-phase-badge", style={"marginTop": "12px"}),
            ]),
            dbc.Col([
                html.Div(id="intel-favored-sectors", style={"marginTop": "12px"}),
            ]),
        ]),

        # ── Sector Performance Table ──────────────────────────────────
        html.Hr(style={"borderColor": cfg.border_color, "margin": "16px 0 12px"}),
        html.P("SECTOR PERFORMANCE BREAKDOWN", style=_SECTION_TITLE_STYLE),
        html.P(
            "Click any sector to see top 20 holdings ranked by conviction.",
            style={"color": cfg.text_muted, "fontSize": "0.78rem", "marginBottom": "8px"},
        ),
        dash_table.DataTable(
            id="intel-sector-perf-table",
            columns=[
                {"name": "Sector", "id": "sector"},
                {"name": "Flow %", "id": "flow_pct", "type": "numeric",
                 "format": {"specifier": "+.1f"}},
                {"name": "Net Flow ($M)", "id": "net_flow_m", "type": "numeric",
                 "format": {"specifier": ",.0f"}},
                {"name": "Tickers", "id": "ticker_count", "type": "numeric"},
                {"name": "Streak (Q)", "id": "inflow_streak", "type": "numeric"},
                {"name": "Cycle Phase", "id": "cycle_phase"},
                {"name": "Signal", "id": "signal"},
            ],
            data=[],
            page_size=30,
            sort_action="native",
            style_table={"overflowX": "auto"},
            style_header=TABLE_HEADER_STYLE,
            style_cell={**TABLE_CELL_STYLE, "minWidth": "80px"},
            style_data_conditional=[
                {"if": {"column_id": "sector"}, "color": "#4da3ff",
                 "cursor": "pointer", "fontWeight": "700", "textDecoration": "underline"},
                {"if": {"filter_query": '{signal} = "INFLOW"', "column_id": "signal"},
                 "color": "#00ff88", "fontWeight": "bold"},
                {"if": {"filter_query": '{signal} = "OUTFLOW"', "column_id": "signal"},
                 "color": "#ff4488", "fontWeight": "bold"},
                {"if": {"filter_query": '{signal} = "FLAT"', "column_id": "signal"},
                 "color": "#aaaaaa"},
                {"if": {"filter_query": '{flow_pct} > 0', "column_id": "flow_pct"},
                 "color": "#00ff88"},
                {"if": {"filter_query": '{flow_pct} < 0', "column_id": "flow_pct"},
                 "color": "#ff4488"},
            ],
        ),

        # ── Sector Detail Panel (hidden until click) ─────────────────
        html.Div(
            id="intel-sector-detail-panel",
            hidden=True,
            children=[
                html.Hr(style={"borderColor": cfg.border_color, "margin": "16px 0 12px"}),
                html.P(id="intel-sector-detail-title", style=_SECTION_TITLE_STYLE),
                dash_table.DataTable(
                    id="intel-sector-detail-table",
                    columns=[
                        {"name": "Ticker", "id": "ticker"},
                        {"name": "Price", "id": "current_price", "type": "numeric",
                         "format": {"specifier": "$.2f"}},
                        {"name": "Conviction", "id": "conviction_score", "type": "numeric",
                         "format": {"specifier": ".0f"}},
                        {"name": "ML v2", "id": "ml_score_v2", "type": "numeric",
                         "format": {"specifier": ".0f"}},
                        {"name": "Phase", "id": "accum_phase"},
                        {"name": "Insider Eff", "id": "insider_effect_score", "type": "numeric",
                         "format": {"specifier": ".0f"}},
                        {"name": "Squeeze", "id": "squeeze_score", "type": "numeric",
                         "format": {"specifier": ".0f"}},
                        {"name": "Momentum", "id": "price_momentum_90d", "type": "numeric",
                         "format": {"specifier": "+.1f"}},
                    ],
                    data=[],
                    page_size=20,
                    sort_action="native",
                    style_table={"overflowX": "auto"},
                    style_header=TABLE_HEADER_STYLE,
                    style_cell={**TABLE_CELL_STYLE, "minWidth": "75px"},
                    style_data_conditional=[
                        {"if": {"column_id": "ticker"}, "color": "#4da3ff",
                         "fontWeight": "700"},
                        *[
                            {"if": {"filter_query": f'{{accum_phase}} = "{phase}"',
                                    "column_id": "accum_phase"},
                             "color": color}
                            for phase, color in PHASE_COLORS.items()
                        ],
                    ],
                ),
            ],
        ),
    ], style=_CARD_STYLE)


def _empty_sector_figure() -> go.Figure:
    fig = go.Figure()
    fig.update_layout(
        paper_bgcolor=cfg.card_color,
        plot_bgcolor=cfg.bg_color,
        font={"color": cfg.text_color, "size": 11},
        margin={"l": 120, "r": 20, "t": 30, "b": 40},
        xaxis=dict(title="Net Flow % QoQ", gridcolor=cfg.border_color, zeroline=True,
                   zerolinecolor=cfg.accent_primary),
        yaxis=dict(gridcolor=cfg.border_color),
    )
    return fig


# ---------------------------------------------------------------------------
# Screen D: Individual Stock Deep Dive
# ---------------------------------------------------------------------------

def _build_deep_dive() -> html.Div:
    return html.Div([
        dbc.Row([
            dbc.Col([
                dcc.Input(
                    id="intel-deep-dive-ticker",
                    type="text",
                    placeholder="Enter ticker (e.g., NVDA)",
                    debounce=True,
                    style={
                        "backgroundColor": cfg.card_color,
                        "color": cfg.text_color,
                        "border": f"1px solid {cfg.border_color}",
                        "borderRadius": "4px",
                        "padding": "8px 12px",
                        "fontSize": "0.9rem",
                        "width": "200px",
                    },
                ),
                dbc.Button(
                    [html.I(className="fas fa-search me-2"), "Analyze"],
                    id="intel-deep-dive-run",
                    className="kb-btn-primary ms-2",
                    n_clicks=0,
                ),
            ], md=6),
            dbc.Col([
                html.Div(id="intel-deep-dive-badge"),
            ], md=6),
        ], className="mb-3"),

        dcc.Loading(
            type="circle",
            color=cfg.accent_primary,
            children=[
                # Phase timeline chart
                dcc.Graph(
                    id="intel-deep-dive-chart",
                    style={"height": "380px"},
                    config={"displayModeBar": False},
                    figure=go.Figure(layout={
                        "paper_bgcolor": cfg.card_color,
                        "plot_bgcolor": cfg.bg_color,
                        "font": {"color": cfg.text_color},
                    }),
                ),
            ],
        ),

        # Stats row
        dbc.Row([
            dbc.Col([
                html.Div(id="intel-deep-dive-stats"),
            ]),
        ], className="mt-3"),
    ], style=_CARD_STYLE)


# ---------------------------------------------------------------------------
# Intelligence Stats Bar
# ---------------------------------------------------------------------------

def _build_intel_stats_bar() -> html.Div:
    return dbc.Row([
        dbc.Col(html.Div([
            html.I(className="fas fa-layer-group kb-stat-icon", style={"color": "#00ff88"}),
            html.Div("0", id="intel-stat-active-accum", className="kb-stat-value", style={"color": "#00ff88"}),
            html.P("Active Accum", className="kb-stat-label"),
        ], className="kb-stat-card kb-animate-in"), md=2),

        dbc.Col(html.Div([
            html.I(className="fas fa-seedling kb-stat-icon", style={"color": "#7bc47f"}),
            html.Div("0", id="intel-stat-early-accum", className="kb-stat-value", style={"color": "#7bc47f"}),
            html.P("Early Accum", className="kb-stat-label"),
        ], className="kb-stat-card kb-animate-in"), md=2),

        dbc.Col(html.Div([
            html.I(className="fas fa-fire kb-stat-icon", style={"color": cfg.accent_primary}),
            html.Div("0", id="intel-stat-high-conviction", className="kb-stat-value", style={"color": cfg.accent_primary}),
            html.P("High Conviction", className="kb-stat-label"),
        ], className="kb-stat-card kb-animate-in"), md=2),

        dbc.Col(html.Div([
            html.I(className="fas fa-users kb-stat-icon", style={"color": "#b388ff"}),
            html.Div("0", id="intel-stat-cluster-insider", className="kb-stat-value", style={"color": "#b388ff"}),
            html.P("Insider Clusters", className="kb-stat-label"),
        ], className="kb-stat-card kb-animate-in"), md=2),

        dbc.Col(html.Div([
            html.I(className="fas fa-triangle-exclamation kb-stat-icon", style={"color": "#ff4488"}),
            html.Div("0", id="intel-stat-distribution", className="kb-stat-value", style={"color": "#ff4488"}),
            html.P("Dist. Warnings", className="kb-stat-label"),
        ], className="kb-stat-card kb-animate-in"), md=2),

        dbc.Col(html.Div([
            html.I(className="fas fa-lock kb-stat-icon", style={"color": "#ffd700"}),
            html.Div("0", id="intel-stat-triple-lock", className="kb-stat-value", style={"color": "#ffd700"}),
            html.P("Triple Lock", className="kb-stat-label"),
        ], className="kb-stat-card kb-animate-in"), md=1),

        dbc.Col(html.Div([
            html.I(className="fas fa-calendar kb-stat-icon", style={"color": cfg.text_muted}),
            html.Div("—", id="intel-stat-quarter", className="kb-stat-value", style={"color": cfg.text_muted, "fontSize": "1.1rem"}),
            html.P("Quarter", className="kb-stat-label"),
        ], className="kb-stat-card kb-animate-in"), md=1),
    ], className="mb-4 g-3")


# ---------------------------------------------------------------------------
# Full Intelligence Section (all screens tabbed)
# ---------------------------------------------------------------------------

def build_intelligence_section() -> html.Div:
    """Build the complete Intelligence tab content."""
    return html.Div(
        id="intelligence-section",
        hidden=True,
        className="kb-animate-in",
        children=[
            # Section header + quarter selector row
            dbc.Row([
                dbc.Col([
                    html.H2(
                        [html.I(className="fas fa-brain me-3"), "Intelligence Center"],
                        style={"color": cfg.accent_primary, "fontWeight": "800", "fontSize": "1.6rem"},
                    ),
                    html.P(
                        "Institutional accumulation phase analysis, conviction scoring, and data-backed trading signals.",
                        style={"color": cfg.text_muted, "marginBottom": "0"},
                    ),
                ], md=8),
                dbc.Col([
                    html.Div([
                        html.Label(
                            "Quarter",
                            style={"color": cfg.text_muted, "fontSize": "0.72rem",
                                   "letterSpacing": "0.08em", "textTransform": "uppercase",
                                   "marginBottom": "4px", "display": "block"},
                        ),
                        dcc.Dropdown(
                            id="intel-quarter-selector",
                            options=[],
                            placeholder="Loading...",
                            clearable=False,
                            className="dash-dropdown",
                            style={
                                "minWidth": "180px",
                            },
                        ),
                    ], style={"textAlign": "right"}),
                ], md=4, style={"display": "flex", "alignItems": "flex-end", "justifyContent": "flex-end"}),
            ], className="mb-4 align-items-start"),

            # Stats bar
            _build_intel_stats_bar(),

            # Sub-tabs
            dbc.Tabs(
                id="intel-sub-tabs",
                active_tab="tab-command",
                children=[
                    dbc.Tab(label="Command Center", tab_id="tab-command",
                            label_style={"color": cfg.text_muted},
                            active_label_style={"color": cfg.accent_primary}),
                    dbc.Tab(label="Accumulation Radar", tab_id="tab-radar",
                            label_style={"color": cfg.text_muted},
                            active_label_style={"color": cfg.accent_primary}),
                    dbc.Tab(label="Sector Clock", tab_id="tab-sector",
                            label_style={"color": cfg.text_muted},
                            active_label_style={"color": cfg.accent_primary}),
                    dbc.Tab(label="Stock Deep Dive", tab_id="tab-deepdive",
                            label_style={"color": cfg.text_muted},
                            active_label_style={"color": cfg.accent_primary}),
                ],
                style={"marginBottom": "20px"},
            ),

            # Tab content — shown/hidden based on active tab
            html.Div(id="intel-tab-content"),

            # Refresh store
            dcc.Store(id="intel-data-store", data={}),
        ],
    )
