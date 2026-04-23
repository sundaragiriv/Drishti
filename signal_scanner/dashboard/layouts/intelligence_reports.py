"""Intelligence Layer Reports — 8-report decision-support system.

Reports:
  1. Overview — strongest setups, changes, warnings
  2. Institutional Report — sponsorship quality
  3. Sector Rotation — capital flow across sectors
  4. Sector Strength — breadth and health
  5. Top Stocks by Sector — actionable names per sector
  6. Theme Tracker — cross-sector themes
  7. Market Drivers — pressure, catalysts, why-now
  8. Mean Reversion — stretched vs compressed timing
"""

from __future__ import annotations

import dash_bootstrap_components as dbc
from dash import dash_table, dcc, html

from signal_scanner.config import DashboardConfig

cfg = DashboardConfig()

# Shared styles
_HEADER = {"color": cfg.accent_primary, "marginBottom": "4px", "fontSize": "1.2rem", "fontWeight": "700"}
_DESC = {"color": cfg.text_muted, "fontSize": "0.82rem", "marginBottom": "16px"}
_CARD = {"backgroundColor": cfg.card_color, "borderRadius": "8px", "padding": "16px", "marginBottom": "16px"}
_TABLE_HEADER = {"backgroundColor": "#0d1117", "color": cfg.accent_primary, "fontWeight": "600", "fontSize": "0.72rem", "textTransform": "uppercase"}
_TABLE_CELL = {"backgroundColor": cfg.card_color, "color": cfg.text_color, "fontSize": "0.80rem", "border": f"1px solid {cfg.border_color}", "padding": "8px"}
_FRESHNESS = {"fontSize": "0.68rem", "color": "#888", "fontStyle": "italic"}


def build_intelligence_layout() -> html.Div:
    """Build the full Intelligence Layer with 8 report tabs."""
    return html.Div(
        id="intelligence-section",
        hidden=True,
        className="kb-animate-in",
        children=[
            # Header
            html.Div(
                className="kb-section-header",
                children=[
                    html.H2("Intelligence Layer",
                             style={"color": "#fff", "fontWeight": "700"}),
                    html.P("Decision support, evidence, and market context",
                           style=_DESC),
                ],
            ),

            # Report tabs
            dbc.Tabs(
                id="intel-sub-tabs",
                active_tab="tab-overview",
                style={"marginBottom": "16px"},
                children=[
                    dbc.Tab(label="Overview", tab_id="tab-overview",
                            label_style={"color": cfg.text_muted},
                            active_label_style={"color": cfg.accent_primary}),
                    dbc.Tab(label="Institutional", tab_id="tab-institutional",
                            label_style={"color": cfg.text_muted},
                            active_label_style={"color": cfg.accent_primary}),
                    dbc.Tab(label="Sector Rotation", tab_id="tab-sector-rotation",
                            label_style={"color": cfg.text_muted},
                            active_label_style={"color": cfg.accent_primary}),
                    dbc.Tab(label="Sector Strength", tab_id="tab-sector-strength",
                            label_style={"color": cfg.text_muted},
                            active_label_style={"color": cfg.accent_primary}),
                    dbc.Tab(label="Top by Sector", tab_id="tab-top-sector",
                            label_style={"color": cfg.text_muted},
                            active_label_style={"color": cfg.accent_primary}),
                    dbc.Tab(label="Themes", tab_id="tab-themes",
                            label_style={"color": cfg.text_muted},
                            active_label_style={"color": cfg.accent_primary}),
                    dbc.Tab(label="Market Drivers", tab_id="tab-drivers",
                            label_style={"color": cfg.text_muted},
                            active_label_style={"color": cfg.accent_primary}),
                    dbc.Tab(label="Mean Reversion", tab_id="tab-mean-rev",
                            label_style={"color": cfg.text_muted},
                            active_label_style={"color": cfg.accent_primary}),
                ],
            ),

            # Tab content — populated by callbacks
            html.Div(id="intel-tab-content"),

            # Data refresh
            dcc.Store(id="intel-data-store", data={}),

            # Sector selector for Top by Sector tab
            dcc.Store(id="intel-selected-sector", data=None),
        ],
    )


# ---------------------------------------------------------------------------
# Per-report layout builders (called by callbacks)
# ---------------------------------------------------------------------------

def build_overview(data: dict) -> list:
    """Overview report: KPI strip + top setups + changes + warnings."""
    quarter = data.get("quarter", "?")
    freshness = data.get("freshness", "")

    kpis = data.get("kpis", {})
    top_setups = data.get("top_setups", [])
    improving = data.get("improving", [])
    deteriorating = data.get("deteriorating", [])

    return [
        # Freshness
        html.Div(f"Quarter: {quarter} | {freshness}", style=_FRESHNESS),

        # KPI strip
        html.Div(
            style={"display": "flex", "gap": "12px", "flexWrap": "wrap", "marginBottom": "16px"},
            children=[
                _kpi_chip("Active Accum", str(kpis.get("active_accum", 0)), "#00c896"),
                _kpi_chip("Early Accum", str(kpis.get("early_accum", 0)), "#4dc9ff"),
                _kpi_chip("High Conviction", str(kpis.get("high_conviction", 0)), "#f5a623"),
                _kpi_chip("Insider Clusters", str(kpis.get("insider_clusters", 0)), "#a78bfa"),
                _kpi_chip("Dist. Warnings", str(kpis.get("dist_warnings", 0)), "#e05252"),
                _kpi_chip("Triple Lock", str(kpis.get("triple_lock", 0)), "#ffd43b"),
            ],
        ),

        # Top conviction setups
        html.Div(style=_CARD, children=[
            html.H5("Top Conviction Setups", style={**_HEADER, "fontSize": "1.0rem"}),
            _build_table("overview-top-table", top_setups,
                         ["ticker", "conviction", "phase", "ml_v2", "insider", "pressure", "squeeze"]),
        ]),

        # Improving names
        html.Div(style=_CARD, children=[
            html.H5("Improving / New Entrants", style={**_HEADER, "fontSize": "1.0rem", "color": "#00c896"}),
            _build_table("overview-improving-table", improving,
                         ["ticker", "conviction", "phase", "change_reason"]) if improving else
            html.P("No significant improvements detected", style={"color": cfg.text_muted}),
        ]),

        # Deteriorating
        html.Div(style=_CARD, children=[
            html.H5("Deteriorations / Warnings", style={**_HEADER, "fontSize": "1.0rem", "color": "#e05252"}),
            _build_table("overview-warnings-table", deteriorating,
                         ["ticker", "conviction", "phase", "warning"]) if deteriorating else
            html.P("No significant deteriorations", style={"color": cfg.text_muted}),
        ]),
    ]


def build_institutional(data: dict) -> list:
    """Institutional Report: sponsorship quality view."""
    phase_dist = data.get("phase_distribution", {})
    top_quality = data.get("top_quality", [])

    return [
        html.Div(f"Data as of: {data.get('freshness', '')}", style=_FRESHNESS),

        # Phase distribution
        html.Div(style=_CARD, children=[
            html.H5("Accumulation Phase Distribution", style={**_HEADER, "fontSize": "1.0rem"}),
            html.Div(
                style={"display": "flex", "gap": "12px", "flexWrap": "wrap"},
                children=[_kpi_chip(k, str(v), "#4dc9ff") for k, v in phase_dist.items()],
            ),
        ]),

        # Top institutional quality
        html.Div(style=_CARD, children=[
            html.H5("Strongest Institutional Sponsorship", style={**_HEADER, "fontSize": "1.0rem"}),
            _build_table("inst-quality-table", top_quality,
                         ["ticker", "conviction", "phase", "tier1_mgrs", "manager_quality", "insider_score", "cascade"]),
        ]),
    ]


def build_sector_rotation(data: dict) -> list:
    """Sector Rotation: capital flow."""
    sectors = data.get("sectors", [])
    return [
        html.Div(f"Data as of: {data.get('freshness', '')}", style=_FRESHNESS),
        html.Div(style=_CARD, children=[
            html.H5("Sector Capital Flow", style={**_HEADER, "fontSize": "1.0rem"}),
            _build_table("sector-rotation-table", sectors,
                         ["sector", "net_flow_pct", "inflow_streak", "tickers", "signal"]),
        ]),
    ]


def build_sector_strength(data: dict) -> list:
    """Sector Strength: breadth and health."""
    sectors = data.get("sectors", [])
    return [
        html.Div(f"Data as of: {data.get('freshness', '')}", style=_FRESHNESS),
        html.Div(style=_CARD, children=[
            html.H5("Sector Breadth & Strength", style={**_HEADER, "fontSize": "1.0rem"}),
            _build_table("sector-strength-table", sectors,
                         ["sector", "breadth_pct", "avg_rsi", "above_200sma_pct", "avg_momentum", "tickers"]),
        ]),
    ]


def build_top_by_sector(data: dict, sector: str = None) -> list:
    """Top Stocks by Sector."""
    sectors_list = data.get("available_sectors", [])
    stocks = data.get("stocks", [])
    return [
        html.Div(f"Data as of: {data.get('freshness', '')}", style=_FRESHNESS),

        # Sector selector
        html.Div(style={"marginBottom": "12px"}, children=[
            html.Label("Select Sector:", style={"fontSize": "0.75rem", "color": "#888", "marginRight": "8px"}),
            dcc.Dropdown(
                id="top-sector-dropdown",
                options=[{"label": s, "value": s} for s in sectors_list[:30]],
                value=sector or (sectors_list[0] if sectors_list else None),
                clearable=False,
                style={"width": "300px", "display": "inline-block"},
            ),
        ]),

        html.Div(style=_CARD, children=[
            html.H5(f"Top Stocks: {sector or 'All'}", style={**_HEADER, "fontSize": "1.0rem"}),
            _build_table("top-sector-table", stocks,
                         ["ticker", "conviction", "phase", "ml_v2", "pressure", "squeeze", "options"]),
        ]),
    ]


def build_themes(data: dict) -> list:
    """Theme Tracker."""
    themes = data.get("themes", [])
    return [
        html.Div(f"Data as of: {data.get('freshness', '')}", style=_FRESHNESS),
        html.Div(style=_CARD, children=[
            html.H5("Active Market Themes", style={**_HEADER, "fontSize": "1.0rem"}),
            _build_table("theme-table", themes,
                         ["theme", "strength", "leaders", "breadth", "trend"]),
        ]),
    ]


def build_market_drivers(data: dict) -> list:
    """Market Drivers: pressure + catalysts + driver summary."""
    pressure = data.get("pressure", [])
    catalysts = data.get("catalysts", [])

    return [
        html.Div(f"Data as of: {data.get('freshness', '')}", style=_FRESHNESS),

        # Pressure
        html.Div(style=_CARD, children=[
            html.H5("Pressure & Positioning", style={**_HEADER, "fontSize": "1.0rem", "color": "#ff8c00"}),
            _build_table("drivers-pressure-table", pressure,
                         ["ticker", "squeeze", "short_squeeze", "dark_pool_pct", "svr_trend", "ctb"]),
        ]),

        # Catalysts
        html.Div(style=_CARD, children=[
            html.H5("Recent Catalysts", style={**_HEADER, "fontSize": "1.0rem", "color": "#4dc9ff"}),
            _build_table("drivers-catalysts-table", catalysts,
                         ["ticker", "catalyst_type", "date", "detail"]),
        ]),
    ]


def build_mean_reversion(data: dict) -> list:
    """Mean Reversion: stretch/compression timing."""
    stocks = data.get("stocks", [])
    market = data.get("market_summary", "")

    return [
        html.Div(f"Data as of: {data.get('freshness', '')}", style=_FRESHNESS),

        # Market-level summary
        html.Div(style={**_CARD, "borderLeft": "3px solid #4dc9ff"}, children=[
            html.H5("Market Mean Reversion", style={**_HEADER, "fontSize": "1.0rem"}),
            html.P(market or "Market state analysis requires daily price data",
                   style={"color": cfg.text_color, "fontSize": "0.85rem"}),
        ]),

        # Stock-level
        html.Div(style=_CARD, children=[
            html.H5("Stock Mean Reversion Signals", style={**_HEADER, "fontSize": "1.0rem"}),
            _build_table("mean-rev-table", stocks,
                         ["ticker", "verdict", "price_vs_20sma", "price_vs_50sma", "price_vs_200sma", "rsi_14"]),
        ]),
    ]


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _kpi_chip(label: str, value: str, color: str) -> html.Div:
    return html.Div(
        style={"display": "flex", "alignItems": "center", "gap": "6px",
               "padding": "4px 10px", "borderRadius": "4px",
               "border": f"1px solid {color}33", "backgroundColor": f"{color}11"},
        children=[
            html.Span(label, style={"fontSize": "0.65rem", "color": "#888",
                                     "textTransform": "uppercase", "letterSpacing": "0.05em"}),
            html.Span(value, style={"fontSize": "0.9rem", "fontWeight": "800", "color": color,
                                     "fontFamily": "'JetBrains Mono', monospace"}),
        ],
    )


def _build_table(table_id: str, data: list, columns: list) -> dash_table.DataTable:
    return dash_table.DataTable(
        id=table_id,
        columns=[{"name": c.replace("_", " ").title(), "id": c} for c in columns],
        data=data,
        page_size=20,
        sort_action="native",
        style_table={"overflowX": "auto"},
        style_header=_TABLE_HEADER,
        style_cell={**_TABLE_CELL, "textAlign": "center"},
        style_cell_conditional=[
            {"if": {"column_id": "ticker"}, "textAlign": "left", "fontWeight": "700",
             "color": "#4da3ff", "cursor": "pointer", "textDecoration": "underline"},
        ],
    )
