"""Forecast — what the models see.

A honest read of the production models:
  - ml_signal_v2  (28-feat conviction scorer, val AUC 0.560)
  - HMM regime model (5-state, walk-forward Sharpe 3.47)
  - Triple Lock filter (conviction>=70 + ml_v2>=70 + F4>=1 + accum)

NO new training, NO new features. Everything here reads from
`intelligence_scores` and existing model artifacts. The 60-day prove-it
window holds the models FIXED — observed performance reflects live edge,
not a moving target.
"""

from dash import dash_table, dcc, html

from signal_scanner.config import DashboardConfig
from signal_scanner.dashboard.trade_rules import rules_tooltip
from signal_scanner.dashboard.layouts.main_view import (
    TABLE_CELL_STYLE,
    TABLE_HEADER_STYLE,
)

cfg = DashboardConfig()


def _model_health_row(label: str, detail: str) -> html.Div:
    return html.Div(
        style={"display": "flex", "padding": "6px 0",
               "borderBottom": "1px solid var(--kb-border)"},
        children=[
            html.Span(label, style={"flex": "0 0 220px",
                                    "color": "var(--kb-text-muted)",
                                    "fontFamily": "var(--kb-font-mono)",
                                    "fontSize": "0.78rem",
                                    "letterSpacing": "0.04em",
                                    "textTransform": "uppercase"}),
            html.Span(detail, style={"flex": 1,
                                     "color": "var(--kb-text)",
                                     "fontFamily": "var(--kb-font-mono)",
                                     "fontSize": "0.82rem"}),
        ],
    )


def build_forecast_layout() -> html.Div:
    return html.Div(
        id="forecast-section",
        hidden=True,
        className="kb-animate-in",
        children=[
            # Section header — terminal-tight
            html.Div(
                className="kb-section-header",
                children=[
                    html.H2(
                        ["Forecast — what the models see", rules_tooltip("forecast")],
                        style={"fontWeight": "700", "marginBottom": "2px"},
                    ),
                    html.P(
                        "ml_signal_v2 · HMM regime · Triple Lock · honest evidence, no new training during prove-it window",
                        className="kb-section-desc",
                    ),
                ],
            ),

            # Refresh interval
            dcc.Interval(
                id="forecast-refresh-interval",
                interval=60 * 1000,
                n_intervals=0,
            ),

            # ─── ROW 1: HMM regime card ─────────────────────────────────
            html.Div(
                className="kb-card",
                style={"padding": "16px", "marginBottom": "14px"},
                children=[
                    html.Div(
                        style={"display": "flex", "alignItems": "center",
                               "gap": "10px", "marginBottom": "10px"},
                        children=[
                            html.I(className="fas fa-brain",
                                   style={"color": "var(--kb-gold)",
                                          "fontSize": "14px"}),
                            html.Span("TODAY'S HMM REGIME",
                                      style={"fontWeight": "700",
                                             "letterSpacing": "0.08em",
                                             "fontSize": "0.78rem",
                                             "color": "var(--kb-gold)"}),
                        ],
                    ),
                    html.Div(
                        id="forecast-regime-body",
                        style={"fontFamily": "var(--kb-font-mono)",
                               "fontSize": "0.85rem",
                               "color": "var(--kb-text)",
                               "lineHeight": "1.5"},
                        children="Loading regime...",
                    ),
                ],
            ),

            # ─── ROW 2: Top 10 by ML conviction ────────────────────────
            html.Div(
                className="kb-card",
                style={"padding": "16px", "marginBottom": "14px"},
                children=[
                    html.Div(
                        style={"display": "flex", "alignItems": "center",
                               "gap": "10px", "marginBottom": "10px"},
                        children=[
                            html.I(className="fas fa-bolt",
                                   style={"color": "#00ff88",
                                          "fontSize": "14px"}),
                            html.Span("TOP 10 BY ML CONVICTION (ml_signal_v2)",
                                      style={"fontWeight": "700",
                                             "letterSpacing": "0.08em",
                                             "fontSize": "0.78rem",
                                             "color": "#00ff88"}),
                            html.Span(id="forecast-ml-summary",
                                      style={"marginLeft": "auto",
                                             "fontSize": "0.75rem",
                                             "color": "var(--kb-text-muted)",
                                             "fontFamily": "var(--kb-font-mono)"}),
                        ],
                    ),
                    dash_table.DataTable(
                        id="forecast-ml-table",
                        columns=[
                            {"name": "#",         "id": "rank",          "type": "numeric"},
                            {"name": "Symbol",    "id": "symbol"},
                            {"name": "ML v2",     "id": "ml_score_v2",   "type": "numeric"},
                            {"name": "Conv",      "id": "conviction_score", "type": "numeric"},
                            {"name": "Phase",     "id": "accum_phase"},
                            {"name": "Side",      "id": "side"},
                            {"name": "Triple",    "id": "triple_lock"},
                            {"name": "F4 60d",    "id": "inst_f4_distinct_60d", "type": "numeric"},
                            {"name": "Mom 90d %", "id": "price_momentum_90d", "type": "numeric"},
                        ],
                        data=[],
                        sort_action="none",
                        row_selectable=False,
                        style_table={"overflowX": "auto"},
                        style_header=TABLE_HEADER_STYLE,
                        style_cell={**TABLE_CELL_STYLE, "textAlign": "center"},
                        style_cell_conditional=[
                            {"if": {"column_id": "symbol"},
                             "textAlign": "left", "fontWeight": "700"},
                        ],
                        style_data_conditional=[
                            {"if": {"column_id": "symbol"},
                             "color": "#4da3ff"},
                            {"if": {"filter_query": '{side} = "LONG"',
                                    "column_id": "side"},
                             "color": cfg.accent_long, "fontWeight": "600"},
                            {"if": {"filter_query": '{side} = "SHORT"',
                                    "column_id": "side"},
                             "color": cfg.accent_short, "fontWeight": "600"},
                            {"if": {"filter_query": "{triple_lock} = 'YES'",
                                    "column_id": "triple_lock"},
                             "color": "#ffd43b", "fontWeight": "700"},
                            {"if": {"filter_query": "{ml_score_v2} >= 90",
                                    "column_id": "ml_score_v2"},
                             "color": "#00ff88", "fontWeight": "700"},
                        ],
                    ),
                ],
            ),

            # ─── ROW 3: Triple Lock candidates ──────────────────────────
            html.Div(
                className="kb-card",
                style={"padding": "16px", "marginBottom": "14px"},
                children=[
                    html.Div(
                        style={"display": "flex", "alignItems": "center",
                               "gap": "10px", "marginBottom": "6px"},
                        children=[
                            html.I(className="fas fa-key",
                                   style={"color": "#ffd43b",
                                          "fontSize": "14px"}),
                            html.Span("TRIPLE LOCK CANDIDATES",
                                      style={"fontWeight": "700",
                                             "letterSpacing": "0.08em",
                                             "fontSize": "0.78rem",
                                             "color": "#ffd43b"}),
                            html.Span(id="forecast-triple-summary",
                                      style={"marginLeft": "auto",
                                             "fontSize": "0.75rem",
                                             "color": "var(--kb-text-muted)",
                                             "fontFamily": "var(--kb-font-mono)"}),
                        ],
                    ),
                    html.P(
                        "Filter: conv>=70 + ml_v2>=70 + F4 insiders>=1 + accum phase. "
                        "Historical edge: 59.8% WR on n=132 paper trades at 1R target.",
                        className="kb-section-desc",
                        style={"marginBottom": "10px", "marginTop": "0"},
                    ),
                    dash_table.DataTable(
                        id="forecast-triple-table",
                        columns=[
                            {"name": "Symbol", "id": "symbol"},
                            {"name": "Conv",   "id": "conviction_score", "type": "numeric"},
                            {"name": "ML v2",  "id": "ml_score_v2",      "type": "numeric"},
                            {"name": "F4 60d", "id": "inst_f4_distinct_60d", "type": "numeric"},
                            {"name": "Phase",  "id": "accum_phase"},
                            {"name": "Squeeze","id": "squeeze_score",    "type": "numeric"},
                        ],
                        data=[],
                        sort_action="native",
                        row_selectable=False,
                        style_table={"overflowX": "auto"},
                        style_header=TABLE_HEADER_STYLE,
                        style_cell={**TABLE_CELL_STYLE, "textAlign": "center"},
                        style_cell_conditional=[
                            {"if": {"column_id": "symbol"},
                             "textAlign": "left", "fontWeight": "700"},
                        ],
                        style_data_conditional=[
                            {"if": {"column_id": "symbol"},
                             "color": "#4da3ff"},
                        ],
                    ),
                ],
            ),

            # ─── ROW 4: Model health footer ─────────────────────────────
            html.Div(
                className="kb-card",
                style={"padding": "14px 18px", "marginBottom": "14px"},
                children=[
                    html.Div(
                        style={"display": "flex", "alignItems": "center",
                               "gap": "10px", "marginBottom": "8px"},
                        children=[
                            html.I(className="fas fa-stethoscope",
                                   style={"color": "var(--kb-text-muted)",
                                          "fontSize": "13px"}),
                            html.Span("MODEL HEALTH",
                                      style={"fontWeight": "700",
                                             "letterSpacing": "0.08em",
                                             "fontSize": "0.74rem",
                                             "color": "var(--kb-text-muted)"}),
                        ],
                    ),
                    html.Div(id="forecast-model-health"),
                    html.P(
                        "Models held FIXED during the 60-day prove-it window. "
                        "Refit only after window closes — see install-tasks.ps1 for the scheduled retrain.",
                        style={"fontSize": "0.72rem",
                               "color": "var(--kb-text-muted)",
                               "marginTop": "10px",
                               "marginBottom": "0",
                               "fontStyle": "italic"},
                    ),
                ],
            ),
        ],
    )
