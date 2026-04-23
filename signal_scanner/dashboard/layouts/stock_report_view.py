"""Individual Stock Report (ISR) layout.

Full intelligence view for a single ticker — shown when any ticker is
clicked across signals, paper trades, options, reports, or intelligence tables.

Sections:
    1. Header bar: ticker, company, phase badge, verdict, back button
    2. Scorecard row: 6 key metrics
    3. Institutional overview: QoQ stats + top institutions held by count
    4. Sector rotation strength
    5. Institutions table: top 20 holders by value
    6. Phase history chart
    7. Potential trade ideas: Day / Swing / Long Term with entry/exit criteria
    8. Insider activity table
    9. Ask Kubera section (lazy AI trigger)
"""

from __future__ import annotations

import dash_bootstrap_components as dbc
from dash import dash_table, dcc, html

from signal_scanner.config import DashboardConfig

cfg = DashboardConfig()

# Shared style reused from main_view
TABLE_HEADER_STYLE = {
    "backgroundColor": cfg.card_color_elevated,
    "color": cfg.accent_primary,
    "fontWeight": "600",
    "border": "none",
    "borderBottom": f"2px solid {cfg.border_color}",
    "fontSize": "0.72rem",
    "textTransform": "uppercase",
    "letterSpacing": "0.06em",
    "padding": "10px 8px",
    "fontFamily": "'Inter', -apple-system, sans-serif",
}

TABLE_CELL_STYLE = {
    "color": cfg.text_color,
    "border": "none",
    "borderBottom": f"1px solid #1e2230",
    "padding": "7px 8px",
    "fontSize": "0.80rem",
    "fontFamily": "'JetBrains Mono', 'Consolas', monospace",
}


def _score_badge(value, high=75, mid=45):
    """Color a score: green ≥ high, yellow ≥ mid, red below."""
    if value is None:
        return "#555"
    if value >= high:
        return "#00c896"
    if value >= mid:
        return "#f5a623"
    return "#e05252"


def _phase_color(phase: str) -> str:
    return {
        "ACTIVE_ACCUM": "#00c896",
        "EARLY_ACCUM":  "#4dc9ff",
        "LATE_ACCUM":   "#f5a623",
        "EXPANSION":    "#a78bfa",
        "DISTRIBUTION": "#e05252",
        "DECLINE":      "#888",
        "DORMANT":      "#555",
    }.get(phase or "", "#555")


def _kpi_card(card_id: str, title: str) -> html.Div:
    """KPI card with a single container filled by the callback."""
    return html.Div(
        className="kb-card",
        style={"padding": "12px 10px", "position": "relative"},
        children=[
            html.Div(title,
                     style={"fontSize": "0.60rem", "color": "#888",
                            "textTransform": "uppercase", "letterSpacing": "0.08em",
                            "marginBottom": "4px"}),
            html.Div(id=card_id),  # Single container — callback fills this
        ],
    )


def build_stock_report_section() -> html.Div:
    """Return the full ISR layout div (hidden by default)."""
    return html.Div(
        id="stock-report-section",
        hidden=True,
        style={"padding": "0 0 40px 0"},
        children=[

            # ── HEADER BAR ───────────────────────────────────────────────────
            html.Div(
                className="kb-section-header",
                style={"marginBottom": "0", "borderBottom": f"1px solid {cfg.border_color}",
                       "paddingBottom": "18px"},
                children=[
                    dbc.Row([
                        dbc.Col([
                            html.Button(
                                [html.I(className="fas fa-arrow-left me-2"), "Back"],
                                id="isr-back-btn",
                                className="btn btn-outline-secondary btn-sm me-3",
                                style={"fontSize": "0.78rem"},
                            ),
                            html.Span(id="isr-ticker-label",
                                      style={"fontSize": "1.6rem", "fontWeight": "800",
                                             "color": cfg.accent_primary, "letterSpacing": "0.05em"}),
                            html.Span(" — ", style={"color": "#555", "margin": "0 6px"}),
                            html.Span(id="isr-company-label",
                                      style={"fontSize": "1.1rem", "color": cfg.text_color,
                                             "fontWeight": "400"}),
                        ], width="auto", className="d-flex align-items-center"),

                        dbc.Col([
                            html.Span(id="isr-phase-badge",
                                      style={"borderRadius": "4px", "padding": "4px 10px",
                                             "fontSize": "0.72rem", "fontWeight": "700",
                                             "textTransform": "uppercase", "letterSpacing": "0.08em",
                                             "marginRight": "8px"}),
                            html.Span(id="isr-verdict-badge",
                                      style={"borderRadius": "4px", "padding": "4px 10px",
                                             "fontSize": "0.72rem", "fontWeight": "700",
                                             "textTransform": "uppercase", "letterSpacing": "0.08em"}),
                        ], width="auto", className="d-flex align-items-center ms-3"),

                        dbc.Col([
                            dbc.InputGroup([
                                dbc.Input(
                                    id="isr-jump-ticker",
                                    type="text",
                                    placeholder="Jump to ticker...",
                                    debounce=True,
                                    style={
                                        "backgroundColor": cfg.card_color,
                                        "color": cfg.text_color,
                                        "border": f"1px solid {cfg.border_color}",
                                        "fontSize": "0.82rem",
                                        "width": "120px",
                                        "textTransform": "uppercase",
                                    },
                                ),
                                dbc.Button(
                                    html.I(className="fas fa-search"),
                                    id="isr-jump-btn",
                                    color="primary",
                                    size="sm",
                                    style={"background": cfg.accent_primary, "border": "none"},
                                ),
                            ], size="sm", style={"width": "170px"}),
                        ], width="auto", className="d-flex align-items-center ms-3"),

                        dbc.Col([
                            html.Span(id="isr-sector-label",
                                      style={"color": "#888", "fontSize": "0.80rem", "marginRight": "12px"}),
                            html.A(
                                [html.I(className="fas fa-chart-line", style={"marginRight": "4px"}), "TradingView"],
                                id="isr-tradingview-link",
                                href="#",
                                target="_blank",
                                rel="noopener noreferrer",
                                style={
                                    "color": "#4da3ff", "fontSize": "0.75rem", "marginRight": "10px",
                                    "textDecoration": "none", "fontWeight": "600",
                                },
                            ),
                            html.A(
                                [html.I(className="fas fa-chart-area", style={"marginRight": "4px"}), "Yahoo"],
                                id="isr-yahoo-link",
                                href="#",
                                target="_blank",
                                rel="noopener noreferrer",
                                style={
                                    "color": "#7b61ff", "fontSize": "0.75rem",
                                    "textDecoration": "none", "fontWeight": "600",
                                },
                            ),
                        ], className="d-flex align-items-center ms-auto"),
                    ], align="center"),
                ],
            ),

            # ── G1: RECOMMENDATION BAR ────────────────────────────────────
            html.Div(
                id="isr-recommendation-bar",
                className="mt-3 mb-2",
                style={"padding": "16px", "borderRadius": "8px",
                       "backgroundColor": "#0d1117",
                       "border": f"1px solid {cfg.border_color}"},
                children=[
                    dbc.Row([
                        # Verdict
                        dbc.Col([
                            html.Div("VERDICT", style={"fontSize": "0.65rem", "color": "#888",
                                                        "textTransform": "uppercase", "letterSpacing": "0.1em"}),
                            html.Div(id="isr-rec-verdict",
                                     style={"fontSize": "1.4rem", "fontWeight": "800"}),
                        ], md=2),
                        # Confidence
                        dbc.Col([
                            html.Div("CONFIDENCE", style={"fontSize": "0.65rem", "color": "#888",
                                                           "textTransform": "uppercase", "letterSpacing": "0.1em"}),
                            html.Div(id="isr-rec-confidence",
                                     style={"fontSize": "1.1rem", "fontWeight": "700"}),
                        ], md=2),
                        # Horizon
                        dbc.Col([
                            html.Div("HORIZON", style={"fontSize": "0.65rem", "color": "#888",
                                                        "textTransform": "uppercase", "letterSpacing": "0.1em"}),
                            html.Div(id="isr-rec-horizon",
                                     style={"fontSize": "1.0rem", "color": cfg.text_color}),
                        ], md=2),
                        # Composite Score
                        dbc.Col([
                            html.Div("COMPOSITE", style={"fontSize": "0.65rem", "color": "#888",
                                                          "textTransform": "uppercase", "letterSpacing": "0.1em"}),
                            html.Div(id="isr-rec-composite",
                                     style={"fontSize": "1.1rem", "fontWeight": "700",
                                            "fontFamily": "'JetBrains Mono', monospace"}),
                        ], md=2),
                        # Why Now (brief)
                        dbc.Col([
                            html.Div("WHY NOW", style={"fontSize": "0.65rem", "color": "#888",
                                                        "textTransform": "uppercase", "letterSpacing": "0.1em"}),
                            html.Div(id="isr-rec-why-now",
                                     style={"fontSize": "0.78rem", "color": "#00c896"}),
                        ], md=4),
                    ]),
                    # Weakens strip
                    html.Div(
                        id="isr-rec-weakens",
                        style={"marginTop": "8px", "fontSize": "0.75rem", "color": "#e05252"},
                    ),
                ],
            ),

            # ── G4: RECOMMENDATION SCORECARD ───────────────────────────
            html.Div(
                id="isr-rec-scorecard",
                className="mb-2",
                style={"display": "flex", "gap": "8px", "flexWrap": "wrap"},
            ),

            # ── ORIGINAL SCORECARD ROW ────────────────────────────────────
            dbc.Row(
                id="isr-scorecard-row",
                className="g-2 mt-2 mb-3",
                children=[
                    dbc.Col(_score_card("isr-sc-conviction",  "Conviction",      "—", "#f5a623"), md=2),
                    dbc.Col(_score_card("isr-sc-phase",        "Phase Streak",    "—", "#4dc9ff"), md=2),
                    dbc.Col(_score_card("isr-sc-cascade",      "Cascade Stage",   "—", "#a78bfa"), md=2),
                    dbc.Col(_score_card("isr-sc-tier1",        "Tier-1 Managers", "—", "#00c896"), md=2),
                    dbc.Col(_score_card("isr-sc-insider",      "Insider Score",   "—", "#f5a623"), md=2),
                    dbc.Col(_score_card("isr-sc-distribution", "Distribution",    "—", "#e05252"), md=2),
                ],
            ),

            # ── INTELLIGENCE KPI ROW 1 ──────────────────────────────────
            dbc.Row(className="g-2 mt-1 mb-2", children=[
                dbc.Col(_kpi_card("isr-kpi-insider",   "Insider Signal"),    md=3),
                dbc.Col(_kpi_card("isr-kpi-momentum",  "Inst. Momentum"),   md=3),
                dbc.Col(_kpi_card("isr-kpi-ev",        "Risk-Adj EV"),      md=3),
                dbc.Col(_kpi_card("isr-kpi-alignment", "Tech-Inst Align"),  md=3),
            ]),

            # ── INTELLIGENCE KPI ROW 2 ──────────────────────────────────
            dbc.Row(className="g-2 mb-3", children=[
                dbc.Col(_kpi_card("isr-kpi-squeeze",   "Squeeze Setup"),    md=3),
                dbc.Col(_kpi_card("isr-kpi-ml",        "ML Confidence"),    md=3),
                dbc.Col(_kpi_card("isr-kpi-cascade",   "Cascade Momentum"), md=3),
                dbc.Col(_kpi_card("isr-kpi-quality",   "Data Quality"),     md=3),
            ]),

            # ── INSTITUTIONAL OVERVIEW ROW ────────────────────────────────────
            dbc.Row(className="g-3 mb-3", children=[

                # QoQ Changes
                dbc.Col(md=4, children=[
                    html.Div(
                        className="kb-card",
                        style={"height": "100%"},
                        children=[
                            html.Div("INSTITUTIONAL QoQ CHANGES",
                                     className="kb-card-title", style={"marginBottom": "12px"}),
                            html.Div(id="isr-qoq-grid"),
                        ],
                    ),
                ]),

                # Top institutions count breakdown
                dbc.Col(md=4, children=[
                    html.Div(
                        className="kb-card",
                        style={"height": "100%"},
                        children=[
                            html.Div("HOLDER BREAKDOWN",
                                     className="kb-card-title", style={"marginBottom": "12px"}),
                            html.Div(id="isr-holder-breakdown"),
                        ],
                    ),
                ]),

                # Sector rotation strength
                dbc.Col(md=4, children=[
                    html.Div(
                        className="kb-card",
                        style={"height": "100%"},
                        children=[
                            html.Div("SECTOR ROTATION STRENGTH",
                                     className="kb-card-title", style={"marginBottom": "12px"}),
                            html.Div(id="isr-sector-rotation"),
                        ],
                    ),
                ]),
            ]),

            # ── INSTITUTIONS TABLE + PHASE CHART ROW ─────────────────────────
            dbc.Row(className="g-3 mb-3", children=[

                dbc.Col(md=7, children=[
                    html.Div(className="kb-card", children=[
                        html.Div("TOP 20 INSTITUTIONS INVESTED",
                                 className="kb-card-title", style={"marginBottom": "10px"}),
                        dash_table.DataTable(
                            id="isr-institutions-table",
                            columns=[
                                {"name": "Manager",   "id": "manager_name"},
                                {"name": "Tier",      "id": "tier"},
                                {"name": "Shares",    "id": "shares",     "type": "numeric"},
                                {"name": "Value ($K)", "id": "value_usd_thousands", "type": "numeric"},
                                {"name": "% of Port", "id": "pct_portfolio", "type": "numeric"},
                            ],
                            data=[],
                            page_size=15,
                            style_header=TABLE_HEADER_STYLE,
                            style_cell=TABLE_CELL_STYLE,
                            style_data_conditional=[
                                {"if": {"filter_query": "{tier} = 1"},
                                 "color": "#00c896", "fontWeight": "600"},
                                {"if": {"filter_query": "{tier} = 2"},
                                 "color": "#4dc9ff"},
                                {"if": {"row_index": "odd"},
                                 "backgroundColor": "#0d1117"},
                            ],
                            style_as_list_view=True,
                        ),
                    ]),
                ]),

                dbc.Col(md=5, children=[
                    html.Div(className="kb-card", children=[
                        html.Div("ACCUMULATION PHASE HISTORY",
                                 className="kb-card-title", style={"marginBottom": "10px"}),
                        dcc.Graph(
                            id="isr-phase-chart",
                            config={"displayModeBar": False},
                            style={"height": "320px"},
                        ),
                    ]),
                ]),
            ]),

            # ── POTENTIAL TRADE IDEAS ─────────────────────────────────────────
            dbc.Row(className="g-3 mb-3", children=[
                dbc.Col(md=12, children=[
                    html.Div(className="kb-card", children=[
                        html.Div("POTENTIAL TRADE IDEAS",
                                 className="kb-card-title", style={"marginBottom": "12px"}),
                        dbc.Row(id="isr-trade-ideas", className="g-3"),
                    ]),
                ]),
            ]),

            # ── INSIDER ACTIVITY TABLE ────────────────────────────────────────
            dbc.Row(className="g-3 mb-3", children=[
                dbc.Col(md=12, children=[
                    html.Div(className="kb-card", children=[
                        html.Div("INSIDER ACTIVITY (LAST 180 DAYS)",
                                 className="kb-card-title", style={"marginBottom": "10px"}),
                        dash_table.DataTable(
                            id="isr-insider-table",
                            columns=[
                                {"name": "Date",       "id": "transaction_date"},
                                {"name": "Insider",    "id": "insider_name"},
                                {"name": "Role",       "id": "insider_role"},
                                {"name": "Type",       "id": "transaction_code"},
                                {"name": "Shares",     "id": "shares",  "type": "numeric"},
                                {"name": "Price",      "id": "price",   "type": "numeric"},
                                {"name": "$ Value",    "id": "dollar_value", "type": "numeric"},
                                {"name": "Own. After", "id": "ownership_after", "type": "numeric"},
                            ],
                            data=[],
                            page_size=10,
                            style_header=TABLE_HEADER_STYLE,
                            style_cell=TABLE_CELL_STYLE,
                            style_data_conditional=[
                                {"if": {"filter_query": '{transaction_code} = "P"'},
                                 "color": "#00c896", "fontWeight": "600"},
                                {"if": {"filter_query": '{transaction_code} = "S"'},
                                 "color": "#e05252"},
                                {"if": {"row_index": "odd"},
                                 "backgroundColor": "#0d1117"},
                            ],
                            style_as_list_view=True,
                        ),
                    ]),
                ]),
            ]),

            # ── TRADEGPT CHAT ─────────────────────────────────────────────────
            dbc.Row(className="g-3", children=[
                dbc.Col(md=12, children=[
                    html.Div(className="kb-card", children=[
                        dbc.Row([
                            dbc.Col([
                                html.Div("TradeGPT — AI CHAT",
                                         className="kb-card-title"),
                                html.Div(
                                    "Ask specific questions about this stock's institutional data, insider activity, technicals, and more.",
                                    style={"color": "#888", "fontSize": "0.80rem", "marginTop": "4px"},
                                ),
                            ]),
                            dbc.Col([
                                html.Button(
                                    [html.I(className="fas fa-eraser me-2"),
                                     "Clear Chat"],
                                    id="isr-chat-clear-btn",
                                    className="btn btn-outline-secondary btn-sm",
                                    style={"fontSize": "0.78rem"},
                                ),
                            ], width="auto", className="d-flex align-items-center"),
                        ], align="center", className="mb-2"),
                        # Quick question buttons
                        html.Div(
                            style={"display": "flex", "gap": "6px", "flexWrap": "wrap", "marginBottom": "10px"},
                            children=[
                                html.Button(
                                    label, id={"type": "isr-quick-q", "index": idx},
                                    className="btn btn-outline-primary btn-sm",
                                    style={"fontSize": "0.72rem", "borderColor": cfg.accent_primary,
                                           "color": cfg.accent_primary, "padding": "3px 10px"},
                                )
                                for idx, label in enumerate([
                                    "Full Analysis", "Entry Setup?",
                                    "Risk Factors", "Options Play",
                                ])
                            ],
                        ),
                        # Chat messages container
                        html.Div(
                            id="isr-chat-messages",
                            style={
                                "maxHeight": "400px", "overflowY": "auto",
                                "backgroundColor": "rgba(0,0,0,0.15)",
                                "borderRadius": "8px", "padding": "12px",
                                "marginBottom": "10px", "minHeight": "60px",
                            },
                            children=[
                                html.Div(
                                    "Ask me anything about this stock...",
                                    style={"color": "#666", "fontSize": "0.82rem",
                                           "fontStyle": "italic"},
                                ),
                            ],
                        ),
                        # Input row
                        dbc.Row([
                            dbc.Col([
                                dbc.Input(
                                    id="isr-chat-input",
                                    type="text",
                                    placeholder="Ask TradeGPT about this stock...",
                                    debounce=False,
                                    style={"backgroundColor": cfg.card_color,
                                           "color": cfg.text_color,
                                           "border": f"1px solid {cfg.border_color}",
                                           "fontSize": "0.85rem"},
                                ),
                            ]),
                            dbc.Col([
                                dcc.Loading(
                                    id="isr-chat-loading",
                                    type="dot",
                                    color=cfg.accent_primary,
                                    children=html.Button(
                                        [html.I(className="fas fa-paper-plane me-1"), "Send"],
                                        id="isr-chat-send-btn",
                                        className="btn btn-primary btn-sm",
                                        style={"background": cfg.accent_primary,
                                               "border": "none", "fontWeight": "600",
                                               "fontSize": "0.82rem", "width": "100%"},
                                    ),
                                ),
                            ], width=2),
                        ]),
                        # Hidden store for kubera report (backwards compat)
                        dcc.Markdown(
                            id="isr-kubera-report",
                            children="",
                            style={"display": "none"},
                        ),
                    ]),
                ]),
            ]),
        ],
    )


def _score_card(card_id: str, title: str, value: str, color: str) -> html.Div:
    return html.Div(
        className="kb-card",
        style={"textAlign": "center", "padding": "14px 10px"},
        children=[
            html.Div(title,
                     style={"fontSize": "0.65rem", "color": "#888",
                             "textTransform": "uppercase", "letterSpacing": "0.08em",
                             "marginBottom": "6px"}),
            html.Div(value,
                     id=card_id,
                     style={"fontSize": "1.5rem", "fontWeight": "800",
                             "color": color, "fontFamily": "'JetBrains Mono', monospace"}),
        ],
    )
