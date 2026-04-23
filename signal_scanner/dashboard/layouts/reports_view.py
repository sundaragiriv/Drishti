"""Stock Ideas dashboard layout (formerly Reports Hub).

Organized by trading timeframe:
  - Intraday Setups (live scanner → scored with trigger badges)
  - Swing Ideas (intelligence_scores swing_signal)
  - Longterm (Platinum 10/10 | Ultimate 8-9/10 | Gold 6-7/10)
  - Short Squeeze (squeeze candidates with GOLDEN badge)
  - Custom Screen (Build Your Own)
"""

import dash_bootstrap_components as dbc
from dash import dash_table, dcc, html

from signal_scanner.config import DashboardConfig

cfg = DashboardConfig()

# Import shared table styles from main_view
from signal_scanner.dashboard.layouts.main_view import TABLE_HEADER_STYLE, TABLE_CELL_STYLE

# ---------------------------------------------------------------------------
# Column definitions per sub-tab
# ---------------------------------------------------------------------------
_ACTION_COL = {"name": "", "id": "action", "presentation": "markdown"}

INTRADAY_COLUMNS = [
    _ACTION_COL,
    {"name": "Symbol", "id": "symbol"},
    {"name": "Signal", "id": "signal"},
    {"name": "Setup Score", "id": "setup_score", "type": "numeric"},
    {"name": "Triggers", "id": "trigger_badges", "presentation": "markdown"},
    {"name": "Session", "id": "session_time"},
    {"name": "State", "id": "stock_state"},
    {"name": "Price", "id": "price", "type": "numeric",
     "format": dash_table.FormatTemplate.money(2)},
    {"name": "Stop", "id": "stop_loss", "type": "numeric",
     "format": dash_table.FormatTemplate.money(2)},
    {"name": "T1", "id": "target_1", "type": "numeric",
     "format": dash_table.FormatTemplate.money(2)},
    {"name": "T2", "id": "target_2", "type": "numeric",
     "format": dash_table.FormatTemplate.money(2)},
    {"name": "R:R", "id": "rr_ratio", "type": "numeric"},
    {"name": "MTF", "id": "mtf_agreement"},
    {"name": "GEX", "id": "gex_status"},
    {"name": "VWAP", "id": "vwap_status"},
    {"name": "Phase", "id": "inst_phase"},
    {"name": "Conv.", "id": "inst_conviction", "type": "numeric"},
    {"name": "Momentum", "id": "signal_momentum"},
    {"name": "ML Score", "id": "ml_score", "type": "numeric"},
    {"name": "ML Grade", "id": "ml_grade"},
    {"name": "Strategy", "id": "ml_strategy"},
]

SWING_COLUMNS = [
    _ACTION_COL,
    {"name": "Ticker", "id": "ticker"},
    {"name": "Company", "id": "company"},
    {"name": "Swing Signal", "id": "swing_signal"},
    {"name": "EV%", "id": "expected_value", "type": "numeric"},
    {"name": "Entry Zone", "id": "swing_entry_zone"},
    {"name": "Target", "id": "swing_target"},
    {"name": "Stop", "id": "swing_stop"},
    {"name": "Options", "id": "swing_options_suggestion"},
    {"name": "Phase", "id": "accum_phase"},
    {"name": "Conv.", "id": "conviction_score", "type": "numeric"},
    {"name": "ML v2", "id": "ml_score_v2", "type": "numeric"},
    {"name": "Tier-1", "id": "tier1_manager_count", "type": "numeric"},
    {"name": "Insider", "id": "insider_cluster_detected"},
    {"name": "Squeeze", "id": "squeeze_score", "type": "numeric"},
    {"name": "% Inst Chg", "id": "inst_count_change_pct", "type": "numeric"},
    {"name": "% Shares Chg", "id": "shares_change_pct", "type": "numeric"},
    {"name": "Streak", "id": "count_up_streak", "type": "numeric"},
    {"name": "Price", "id": "price", "type": "numeric",
     "format": dash_table.FormatTemplate.money(2)},
]

LONGTERM_COLUMNS = [
    _ACTION_COL,
    {"name": "#", "id": "rank", "type": "numeric"},
    {"name": "Ticker", "id": "ticker"},
    {"name": "Company", "id": "company"},
    {"name": "Tier", "id": "tier"},
    {"name": "EV%", "id": "expected_value", "type": "numeric"},
    {"name": "Confirms", "id": "confirmation_count", "type": "numeric"},
    {"name": "Missing", "id": "missing"},
    {"name": "Phase", "id": "accum_phase"},
    {"name": "Conv.", "id": "conviction_score", "type": "numeric"},
    {"name": "ML v2", "id": "ml_score_v2", "type": "numeric"},
    {"name": "Tier-1", "id": "tier1_manager_count", "type": "numeric"},
    {"name": "Insider", "id": "insider_cluster_detected"},
    {"name": "Cascade", "id": "cascade_stage", "type": "numeric"},
    {"name": "Squeeze", "id": "squeeze_score", "type": "numeric"},
    {"name": "% Inst Chg", "id": "inst_count_change_pct", "type": "numeric"},
    {"name": "% Shares Chg", "id": "shares_change_pct", "type": "numeric"},
    {"name": "Mom 90d", "id": "price_momentum_90d", "type": "numeric"},
    {"name": "Price", "id": "price", "type": "numeric",
     "format": dash_table.FormatTemplate.money(2)},
    {"name": "LT Signal", "id": "longterm_signal"},
    {"name": "LEAPS", "id": "longterm_options_suggestion"},
]

SQUEEZE_COLUMNS = [
    _ACTION_COL,
    {"name": "Ticker", "id": "ticker"},
    {"name": "Golden", "id": "golden_squeeze"},
    {"name": "Base Squeeze", "id": "base_squeeze", "type": "numeric"},
    {"name": "Enhanced", "id": "short_squeeze_score", "type": "numeric"},
    {"name": "DTC", "id": "days_to_cover", "type": "numeric"},
    {"name": "Short Vol%", "id": "short_volume_ratio_avg", "type": "numeric"},
    {"name": "Dark Pool%", "id": "dark_pool_pct_avg", "type": "numeric"},
    {"name": "Phase", "id": "accum_phase"},
    {"name": "Conv.", "id": "conviction_score", "type": "numeric"},
    {"name": "Insider", "id": "insider_cluster_detected"},
    {"name": "Tier-1", "id": "tier1_manager_count", "type": "numeric"},
    {"name": "Price", "id": "price", "type": "numeric",
     "format": dash_table.FormatTemplate.money(2)},
]

SCREEN_COLUMNS = [
    {"name": "Ticker", "id": "ticker"},
    {"name": "Company", "id": "company"},
    {"name": "Sector", "id": "sector"},
    {"name": "Price", "id": "current_price", "type": "numeric",
     "format": dash_table.FormatTemplate.money(2)},
    {"name": "Inst# Current", "id": "inst_count_current", "type": "numeric"},
    {"name": "Inst# Prior", "id": "inst_count_prior", "type": "numeric"},
    {"name": "Count Change", "id": "inst_count_change", "type": "numeric"},
    {"name": "% Count Chg", "id": "inst_count_change_pct", "type": "numeric"},
    {"name": "Shares Current", "id": "shares_current", "type": "numeric"},
    {"name": "% Shares Chg", "id": "shares_change_pct", "type": "numeric"},
    {"name": "Value ($K)", "id": "value_current_usd_k", "type": "numeric"},
    {"name": "% Value Chg", "id": "value_change_pct", "type": "numeric"},
    {"name": "Count Streak", "id": "count_up_streak", "type": "numeric"},
    {"name": "Shares Streak", "id": "shares_up_streak", "type": "numeric"},
]


def _report_table(table_id: str, columns: list, page_size: int = 25) -> dash_table.DataTable:
    """Create a styled report DataTable."""
    return dash_table.DataTable(
        id=table_id,
        columns=columns,
        data=[],
        page_size=page_size,
        sort_action="native",
        filter_action="native",
        style_table={"overflowX": "auto"},
        style_header={
            **TABLE_HEADER_STYLE,
            "textAlign": "center",
        },
        style_cell={
            **TABLE_CELL_STYLE,
            "textAlign": "center",
            "minWidth": "80px",
        },
        style_data_conditional=[
            # Action column — clickable enter button
            {"if": {"column_id": "action"}, "color": cfg.accent_primary, "cursor": "pointer", "fontWeight": "700", "textAlign": "center", "minWidth": "50px", "maxWidth": "60px"},
            # Ticker link style
            {"if": {"column_id": "ticker"}, "color": "#4da3ff", "cursor": "pointer", "fontWeight": "700", "textDecoration": "underline"},
            {"if": {"column_id": "symbol"}, "color": "#4da3ff", "cursor": "pointer", "fontWeight": "700", "textDecoration": "underline"},
            # Signal coloring
            {"if": {"filter_query": '{signal} = "LONG"', "column_id": "signal"}, "color": cfg.accent_long, "fontWeight": "700"},
            {"if": {"filter_query": '{signal} = "SHORT"', "column_id": "signal"}, "color": cfg.accent_short, "fontWeight": "700"},
            {"if": {"filter_query": '{swing_signal} = "BUY"', "column_id": "swing_signal"}, "color": cfg.accent_long, "fontWeight": "700"},
            {"if": {"filter_query": '{swing_signal} = "SHORT"', "column_id": "swing_signal"}, "color": cfg.accent_short, "fontWeight": "700"},
            # Options flow sentiment coloring
            {"if": {"filter_query": '{pc_sentiment} = "BULLISH"', "column_id": "pc_sentiment"}, "color": cfg.accent_long, "fontWeight": "700"},
            {"if": {"filter_query": '{pc_sentiment} = "BEARISH"', "column_id": "pc_sentiment"}, "color": cfg.accent_short, "fontWeight": "700"},
            # Positive/negative coloring
            {"if": {"filter_query": "{shares_change_pct} > 0", "column_id": "shares_change_pct"}, "color": cfg.accent_long},
            {"if": {"filter_query": "{shares_change_pct} < 0", "column_id": "shares_change_pct"}, "color": cfg.accent_short},
            {"if": {"filter_query": "{inst_count_change_pct} > 0", "column_id": "inst_count_change_pct"}, "color": cfg.accent_long},
            {"if": {"filter_query": "{inst_count_change_pct} < 0", "column_id": "inst_count_change_pct"}, "color": cfg.accent_short},
            # Tier coloring
            {"if": {"filter_query": '{tier} = "PLATINUM"', "column_id": "tier"}, "color": "#00d26a", "fontWeight": "700"},
            {"if": {"filter_query": '{tier} = "ULTIMATE"', "column_id": "tier"}, "color": cfg.accent_primary, "fontWeight": "700"},
            {"if": {"filter_query": '{tier} = "GOLD"', "column_id": "tier"}, "color": "#ffc107", "fontWeight": "700"},
            # Confirmation count coloring
            {"if": {"filter_query": "{confirmation_count} = 10", "column_id": "confirmation_count"}, "color": "#00d26a", "fontWeight": "700"},
            {"if": {"filter_query": "{confirmation_count} >= 8 && {confirmation_count} <= 9", "column_id": "confirmation_count"}, "color": cfg.accent_primary, "fontWeight": "700"},
            # Setup score gradient
            {"if": {"filter_query": "{setup_score} >= 80", "column_id": "setup_score"}, "color": "#00d26a", "fontWeight": "700"},
            {"if": {"filter_query": "{setup_score} >= 60 && {setup_score} < 80", "column_id": "setup_score"}, "color": cfg.accent_primary, "fontWeight": "700"},
            {"if": {"filter_query": "{setup_score} < 60", "column_id": "setup_score"}, "color": "#ffc107"},
            # Golden squeeze — highlight entire row gold
            {"if": {"filter_query": '{golden_squeeze} = "True"'}, "backgroundColor": "rgba(255,193,7,0.15)"},
            {"if": {"filter_query": '{golden_squeeze} = "True"', "column_id": "golden_squeeze"}, "color": "#ffc107", "fontWeight": "700"},
            # Base squeeze color bands
            {"if": {"filter_query": "{base_squeeze} >= 60", "column_id": "base_squeeze"}, "color": cfg.accent_primary, "fontWeight": "700"},
            {"if": {"filter_query": "{base_squeeze} < 30", "column_id": "base_squeeze"}, "color": "#666"},
            # Enhanced squeeze score
            {"if": {"filter_query": "{short_squeeze_score} >= 60", "column_id": "short_squeeze_score"}, "color": cfg.accent_primary, "fontWeight": "700"},
            # EV column coloring
            {"if": {"filter_query": "{expected_value} > 3", "column_id": "expected_value"}, "color": "#00d26a", "fontWeight": "700"},
            {"if": {"filter_query": "{expected_value} > 0 && {expected_value} <= 3", "column_id": "expected_value"}, "color": cfg.accent_long},
            {"if": {"filter_query": "{expected_value} < 0", "column_id": "expected_value"}, "color": cfg.accent_short},
            # Stock state
            {"if": {"filter_query": '{stock_state} = "VERY_STRONG"', "column_id": "stock_state"}, "color": "#00d26a", "fontWeight": "700"},
            {"if": {"filter_query": '{stock_state} = "CONFIRMED"', "column_id": "stock_state"}, "color": cfg.accent_primary},
            # ML Score coloring
            {"if": {"filter_query": "{ml_score} >= 75", "column_id": "ml_score"}, "color": "#00d26a", "fontWeight": "700"},
            {"if": {"filter_query": "{ml_score} >= 65 && {ml_score} < 75", "column_id": "ml_score"}, "color": cfg.accent_primary, "fontWeight": "600"},
            {"if": {"filter_query": "{ml_score} < 65", "column_id": "ml_score"}, "color": "#ffc107"},
            # ML Grade coloring
            {"if": {"filter_query": '{ml_grade} = "A+"', "column_id": "ml_grade"}, "color": "#00d26a", "fontWeight": "700"},
            {"if": {"filter_query": '{ml_grade} = "A"', "column_id": "ml_grade"}, "color": "#4dc9ff", "fontWeight": "700"},
            {"if": {"filter_query": '{ml_grade} = "B"', "column_id": "ml_grade"}, "color": "#ffc107"},
            {"if": {"filter_query": '{ml_grade} = "C"', "column_id": "ml_grade"}, "color": "#888"},
            # ML overlay score on options
            {"if": {"filter_query": "{ml_overlay_score} >= 80", "column_id": "ml_overlay_score"}, "color": "#00d26a", "fontWeight": "700"},
            {"if": {"filter_query": "{ml_overlay_score} >= 60 && {ml_overlay_score} < 80", "column_id": "ml_overlay_score"}, "color": cfg.accent_primary},
        ],
        markdown_options={"html": True},
    )


def build_stock_ideas_layout() -> html.Div:
    """Construct the Stock Ideas layout (replaces old Reports Hub)."""
    return html.Div(
        id="stock-ideas-section",
        hidden=True,
        className="kb-animate-in",
        children=[
            # Header
            html.Div(
                className="kb-section-header",
                children=[
                    html.Div(
                        style={"display": "flex", "alignItems": "center", "justifyContent": "space-between"},
                        children=[
                            html.Div([
                                html.H2([
                                    "Stock Ideas",
                                    html.Span("MULTI-TIMEFRAME", className="kb-section-badge"),
                                ]),
                                html.P(
                                    "Intraday setups, swing trades, and long-term conviction picks powered by scanner + institutional intelligence",
                                    className="kb-section-desc",
                                ),
                            ]),
                            html.Div(
                                style={"display": "flex", "alignItems": "center", "gap": "12px"},
                                children=[
                                    html.Span("Quarter:", className="kb-label"),
                                    dcc.Dropdown(
                                        id="si-quarter-dropdown",
                                        options=[],
                                        value=None,
                                        placeholder="Latest",
                                        className="dash-dropdown",
                                        style={"width": "160px", "fontSize": "13px"},
                                        clearable=False,
                                    ),
                                ],
                            ),
                        ],
                    ),
                ],
            ),

            # Stat cards
            dbc.Row(
                className="mb-4 g-3",
                children=[
                    dbc.Col(_stat_card("si-stat-intraday", "Intraday Setups", "0", "fa-bolt"), width=2),
                    dbc.Col(_stat_card("si-stat-swing", "Swing Ideas", "0", "fa-chart-line"), width=2),
                    dbc.Col(_stat_card("si-stat-platinum", "Platinum (10/10)", "0", "fa-crown"), width=2),
                    dbc.Col(_stat_card("si-stat-ultimate", "Ultimate (8-9)", "0", "fa-star"), width=2),
                    dbc.Col(_stat_card("si-stat-gold", "Gold (6-7)", "0", "fa-medal"), width=2),
                    dbc.Col(_stat_card("si-stat-squeeze", "Squeeze Watch", "0", "fa-compress-arrows-alt"), width=2),
                ],
            ),

            # Sub-tabs
            dcc.Tabs(
                id="si-tabs",
                value="tab-intraday",
                style={"marginBottom": "16px"},
                children=[
                    dcc.Tab(label="Intraday Setups", value="tab-intraday"),
                    dcc.Tab(label="Swing Ideas", value="tab-swing"),
                    dcc.Tab(label="Longterm", value="tab-longterm"),
                    dcc.Tab(label="Short Squeeze", value="tab-squeeze"),
                    dcc.Tab(label="Custom Screen", value="tab-custom"),
                ],
            ),

            # --- INTRADAY SETUPS ---
            html.Div(id="si-intraday-container", className="kb-card mb-4", children=[
                html.H4([
                    "Intraday Setups",
                    html.Span(" — Scanner signals scored with institutional overlay", style={"color": cfg.text_muted, "fontSize": "0.82rem", "fontWeight": "400"}),
                ], style={"color": cfg.accent_primary, "marginBottom": "4px"}),
                html.P(
                    "Setup Score (0-100): GEX + VWAP + Sweep + FVG + RSI-Div + MTF + Momentum + Session + Institutional bonus. "
                    "Trigger badges show which confirmations are active.",
                    className="kb-section-desc", style={"marginBottom": "12px"},
                ),
                _report_table("si-intraday-table", INTRADAY_COLUMNS),
            ]),

            # --- SWING IDEAS ---
            html.Div(id="si-swing-container", hidden=True, className="kb-card mb-4", children=[
                html.H4([
                    "Swing Ideas (2-8 Weeks)",
                    html.Span(" — Intelligence-driven swing entries", style={"color": cfg.text_muted, "fontSize": "0.82rem", "fontWeight": "400"}),
                ], style={"color": cfg.accent_primary, "marginBottom": "4px"}),
                html.P(
                    "Stocks where institutional intelligence signals a BUY or SHORT swing opportunity. Sorted by conviction score.",
                    className="kb-section-desc", style={"marginBottom": "12px"},
                ),
                _report_table("si-swing-table", SWING_COLUMNS),
            ]),

            # --- LONGTERM ---
            html.Div(id="si-longterm-container", hidden=True, className="kb-card mb-4", children=[
                html.H4([
                    "Longterm Conviction",
                    html.Span(" — 10-Confirmation ranking system", style={"color": cfg.text_muted, "fontSize": "0.82rem", "fontWeight": "400"}),
                ], style={"color": cfg.accent_primary, "marginBottom": "4px"}),
                html.P(
                    "Phase + Inst Growth + Shares Accum + Insider + Tier-1 + ML+Conv + SMA200 + Price Mom + Cascade + No Distribution",
                    className="kb-section-desc", style={"marginBottom": "8px"},
                ),
                dbc.Row([
                    dbc.Col([
                        html.Label("Tier:", className="kb-label", style={"marginBottom": "4px", "display": "block"}),
                        dcc.Dropdown(id="si-longterm-tier", className="dash-dropdown", options=[
                            {"label": "All (6+)", "value": "all"},
                            {"label": "Platinum (10/10)", "value": "platinum"},
                            {"label": "Ultimate (8-9)", "value": "ultimate"},
                            {"label": "Gold (6-7)", "value": "gold"},
                        ], value="all", clearable=False, style={"width": "180px"}),
                    ], width=2),
                ], className="mb-3"),
                _report_table("si-longterm-table", LONGTERM_COLUMNS),
            ]),

            # --- SHORT SQUEEZE ---
            html.Div(id="si-squeeze-container", hidden=True, className="kb-card mb-4", children=[
                html.H4([
                    "Short Squeeze Watch",
                    html.Span(" — Squeeze candidates from institutional data", style={"color": cfg.text_muted, "fontSize": "0.82rem", "fontWeight": "400"}),
                ], style={"color": cfg.accent_primary, "marginBottom": "4px"}),
                html.P(
                    "GOLDEN SQUEEZE: score >= 60 + active accumulation + conviction >= 50. These have institutional backing behind the squeeze setup.",
                    className="kb-section-desc", style={"marginBottom": "12px"},
                ),
                _report_table("si-squeeze-table", SQUEEZE_COLUMNS),
            ]),

            # --- CUSTOM SCREEN ---
            html.Div(id="si-custom-container", hidden=True, className="kb-card mb-4", children=[
                html.H4("Build Your Own Screen", style={"color": cfg.accent_primary, "marginBottom": "4px"}),
                html.P("Create custom screens using institutional data filters.", className="kb-section-desc", style={"marginBottom": "12px"}),
                dbc.Row([
                    dbc.Col([
                        html.Label("Min Price:", className="kb-label", style={"marginBottom": "4px", "display": "block"}),
                        dcc.Input(id="screen-min-price", type="number", value=None, placeholder="e.g. 5",
                                  style={"width": "90px", "fontSize": "12px", "backgroundColor": cfg.card_color,
                                         "color": cfg.text_color, "border": f"1px solid {cfg.border_color}",
                                         "borderRadius": "6px", "padding": "6px 8px"}),
                    ], width=1),
                    dbc.Col([
                        html.Label("Max Price:", className="kb-label", style={"marginBottom": "4px", "display": "block"}),
                        dcc.Input(id="screen-max-price", type="number", value=None, placeholder="e.g. 100",
                                  style={"width": "90px", "fontSize": "12px", "backgroundColor": cfg.card_color,
                                         "color": cfg.text_color, "border": f"1px solid {cfg.border_color}",
                                         "borderRadius": "6px", "padding": "6px 8px"}),
                    ], width=1),
                    dbc.Col([
                        html.Label("Min Inst Count:", className="kb-label", style={"marginBottom": "4px", "display": "block"}),
                        dcc.Input(id="screen-min-inst", type="number", value=None, placeholder="e.g. 10",
                                  style={"width": "100px", "fontSize": "12px", "backgroundColor": cfg.card_color,
                                         "color": cfg.text_color, "border": f"1px solid {cfg.border_color}",
                                         "borderRadius": "6px", "padding": "6px 8px"}),
                    ], width=2),
                    dbc.Col([
                        html.Label("Min % Shares Chg:", className="kb-label", style={"marginBottom": "4px", "display": "block"}),
                        dcc.Input(id="screen-min-shares-pct", type="number", value=None, placeholder="e.g. 20",
                                  style={"width": "100px", "fontSize": "12px", "backgroundColor": cfg.card_color,
                                         "color": cfg.text_color, "border": f"1px solid {cfg.border_color}",
                                         "borderRadius": "6px", "padding": "6px 8px"}),
                    ], width=2),
                    dbc.Col([
                        html.Label("Min % Count Chg:", className="kb-label", style={"marginBottom": "4px", "display": "block"}),
                        dcc.Input(id="screen-min-count-pct", type="number", value=None, placeholder="e.g. 10",
                                  style={"width": "100px", "fontSize": "12px", "backgroundColor": cfg.card_color,
                                         "color": cfg.text_color, "border": f"1px solid {cfg.border_color}",
                                         "borderRadius": "6px", "padding": "6px 8px"}),
                    ], width=2),
                    dbc.Col([
                        html.Label("Min Streak:", className="kb-label", style={"marginBottom": "4px", "display": "block"}),
                        dcc.Dropdown(id="screen-min-streak", className="dash-dropdown", options=[
                            {"label": "Any", "value": 0}, {"label": "2+", "value": 2},
                            {"label": "3+", "value": 3}, {"label": "4+", "value": 4},
                        ], value=0, clearable=False, style={"width": "80px"}),
                    ], width=2),
                    dbc.Col([
                        html.Label("\u00A0", className="kb-label", style={"marginBottom": "4px", "display": "block"}),
                        html.Button(
                            "Run Screen", id="screen-run-btn", n_clicks=0,
                            className="kb-btn-primary",
                        ),
                    ], width=2),
                ], className="mb-3"),
                _report_table("si-custom-table", SCREEN_COLUMNS),
            ]),
        ],
    )


# ---------------------------------------------------------------------------
# Options Ideas layout (3 sub-tabs)
# ---------------------------------------------------------------------------
WEEKLY_OPTIONS_COLUMNS = [
    {"name": "Symbol", "id": "symbol"},
    {"name": "Direction", "id": "direction"},
    {"name": "Strike", "id": "strike", "type": "numeric"},
    {"name": "Expiry", "id": "expiry_guidance"},
    {"name": "Score", "id": "setup_score", "type": "numeric"},
    {"name": "Flags", "id": "flags", "presentation": "markdown"},
    {"name": "Price", "id": "price", "type": "numeric",
     "format": dash_table.FormatTemplate.money(2)},
    {"name": "Stop", "id": "stop_loss", "type": "numeric",
     "format": dash_table.FormatTemplate.money(2)},
    {"name": "T1", "id": "target_1", "type": "numeric",
     "format": dash_table.FormatTemplate.money(2)},
    {"name": "Signal", "id": "signal"},
    {"name": "Session", "id": "session_time"},
    {"name": "State", "id": "stock_state"},
    {"name": "GEX", "id": "gex_status"},
    {"name": "MTF", "id": "mtf_agreement"},
    {"name": "P/C", "id": "pc_ratio", "type": "numeric"},
    {"name": "Flow", "id": "pc_sentiment"},
]

SWING_CONTRACT_COLUMNS = [
    {"name": "#", "id": "rank", "type": "numeric"},
    {"name": "Direction", "id": "direction"},
    {"name": "Ticker", "id": "ticker"},
    {"name": "Company", "id": "company"},
    {"name": "Sector", "id": "sector"},
    {"name": "Price", "id": "current_price", "type": "numeric",
     "format": dash_table.FormatTemplate.money(2)},
    {"name": "Strike", "id": "strike", "type": "numeric"},
    {"name": "Type", "id": "option_type"},
    {"name": "Expiry", "id": "expiry_guidance"},
    {"name": "Conviction", "id": "conviction_score", "type": "numeric"},
    {"name": "ML", "id": "ml_overlay_score", "type": "numeric"},
    {"name": "P/C", "id": "pc_ratio", "type": "numeric"},
    {"name": "Flow", "id": "pc_sentiment"},
    {"name": "Gamma Wall", "id": "gamma_wall", "type": "numeric"},
    {"name": "Put Wall", "id": "put_wall", "type": "numeric"},
    {"name": "Pressure", "id": "inst_pressure", "type": "numeric"},
    {"name": "Source", "id": "source"},
    {"name": "Rationale", "id": "rationale"},
    {"name": "% Shares Chg", "id": "shares_change_pct", "type": "numeric"},
    {"name": "Streak", "id": "streak", "type": "numeric"},
]

LEAPS_COLUMNS = [
    {"name": "Ticker", "id": "ticker"},
    {"name": "Company", "id": "company"},
    {"name": "Tier", "id": "tier"},
    {"name": "Confirms", "id": "confirmation_count", "type": "numeric"},
    {"name": "Direction", "id": "direction"},
    {"name": "Strike", "id": "strike", "type": "numeric"},
    {"name": "Expiry", "id": "expiry_guidance"},
    {"name": "Price", "id": "price", "type": "numeric",
     "format": dash_table.FormatTemplate.money(2)},
    {"name": "Conv.", "id": "conviction_score", "type": "numeric"},
    {"name": "LT Signal", "id": "longterm_signal"},
    {"name": "LEAPS Idea", "id": "leaps_suggestion"},
    {"name": "Phase", "id": "accum_phase"},
    {"name": "Tier-1", "id": "tier1_count", "type": "numeric"},
    {"name": "Insider", "id": "insider"},
    {"name": "Cascade", "id": "cascade", "type": "numeric"},
]


def build_options_ideas_layout() -> html.Div:
    """Construct the Options Ideas layout (3 sub-tabs)."""
    return html.Div(
        id="options-ideas-section",
        hidden=True,
        className="kb-animate-in",
        children=[
            # Header
            html.Div(
                className="kb-section-header",
                children=[
                    html.H2([
                        "Options Ideas",
                        html.Span("3 TIMEFRAMES", className="kb-section-badge"),
                    ]),
                    html.P(
                        "Weekly plays (0-7 DTE), swing contracts (14-45 DTE), and LEAPS (6-18m) with institutional conviction and trigger flags",
                        className="kb-section-desc",
                    ),
                ],
            ),

            # Sub-tabs
            dcc.Tabs(
                id="oi-tabs",
                value="tab-weekly",
                style={"marginBottom": "16px"},
                children=[
                    dcc.Tab(label="Weekly Plays (0-7 DTE)", value="tab-weekly"),
                    dcc.Tab(label="Swing Contracts (14-45 DTE)", value="tab-swing-contracts"),
                    dcc.Tab(label="LEAPS (6-18m)", value="tab-leaps"),
                ],
            ),

            # --- WEEKLY ---
            html.Div(id="oi-weekly-container", className="kb-card mb-4", children=[
                html.H4([
                    "Weekly Plays",
                    html.Span(" — GEX-informed 0-7 DTE from intraday setups", style={"color": cfg.text_muted, "fontSize": "0.82rem", "fontWeight": "400"}),
                ], style={"color": cfg.accent_primary, "marginBottom": "4px"}),
                html.P(
                    "Flags: GAMMA-SQZ (gamma squeeze potential) | 0DTE (same-day eligible) | SWEEP | FVG | VWAP-REV | RSI-DIV | INST+",
                    className="kb-section-desc", style={"marginBottom": "12px"},
                ),
                _report_table("oi-weekly-table", WEEKLY_OPTIONS_COLUMNS),
            ]),

            # --- SWING CONTRACTS ---
            html.Div(id="oi-swing-container", hidden=True, className="kb-card mb-4", children=[
                html.H4([
                    "Swing Contracts (14-45 DTE)",
                    html.Span(" — 4-pillar institutional ideas + Squeezer bar", style={"color": cfg.text_muted, "fontSize": "0.82rem", "fontWeight": "400"}),
                ], style={"color": cfg.accent_primary, "marginBottom": "4px"}),
                html.P(
                    "Institutional Pressure bar (0-100): Conviction + Squeeze + Tier-1 + Insider + Phase + Cascade. Higher pressure = stronger institutional backing.",
                    className="kb-section-desc", style={"marginBottom": "12px"},
                ),
                _report_table("oi-swing-table", SWING_CONTRACT_COLUMNS),
            ]),

            # --- LEAPS ---
            html.Div(id="oi-leaps-container", hidden=True, className="kb-card mb-4", children=[
                html.H4([
                    "LEAPS (6-18 Months)",
                    html.Span(" — From Ultimate/Platinum tier longterm picks", style={"color": cfg.text_muted, "fontSize": "0.82rem", "fontWeight": "400"}),
                ], style={"color": cfg.accent_primary, "marginBottom": "4px"}),
                html.P(
                    "8+ confirmation stocks with ATM-5% OTM CALL LEAPS. Expiry based on expected institutional impact timeline.",
                    className="kb-section-desc", style={"marginBottom": "12px"},
                ),
                _report_table("oi-leaps-table", LEAPS_COLUMNS),
            ]),
        ],
    )


def _stat_card(card_id: str, label: str, default_value: str, icon: str = "fa-chart-bar") -> dbc.Card:
    """Build a stat card with icon."""
    return dbc.Card(
        className="kb-stat-card kb-animate-in",
        children=[
            html.I(className=f"fas {icon} kb-stat-icon", style={"color": cfg.accent_primary}),
            html.Div(default_value, id=card_id, className="kb-stat-value", style={"color": cfg.text_color}),
            html.P(label, className="kb-stat-label"),
        ],
    )


# Backward compat: old function name still works
def build_reports_layout() -> html.Div:
    """Backward-compatible alias for build_stock_ideas_layout."""
    return build_stock_ideas_layout()
