"""Drishti v2 callbacks — regime hero card + Road-to-10M equity tracker.

Visible-impact components that replace the old terminal pill banner as the
first thing the eye lands on. The legacy banner is kept below for status
detail (readiness/EOD age/kill switch); these two cards do the "what state
are we in / how much have we made" job at a glance.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import plotly.graph_objects as go
from dash import Input, Output, html, no_update

from signal_scanner.config import ScannerConfig

# ---------------------------------------------------------------------------
# Regime → visual mapping (Phosphor duotone icons + actionable guidance).
# State 0 CRASH | 1 DISTRIBUTION | 2 ACCUMULATION | 3 MEAN_REVERSION | 4 BULL_TREND
# ---------------------------------------------------------------------------
REGIME_DISPLAY = {
    0: {
        "label": "CRASH",
        "card_cls": "regime-crash",
        "icon_cls": "ph-duotone ph-warning",
        "icon_color": "#fb7185",
        "guidance": "Sit out. All entries blocked — wait for state to change.",
        "long": False, "short": True,
    },
    1: {
        "label": "DISTRIBUTING",
        "card_cls": "regime-distribute",
        "icon_cls": "ph-duotone ph-trend-down",
        "icon_color": "#f59e0b",
        "guidance": "Defensive. SHORTs allowed; LONGs blocked.",
        "long": False, "short": True,
    },
    2: {
        "label": "ACCUMULATING",
        "card_cls": "regime-accumulate",
        "icon_cls": "ph-duotone ph-arrow-fat-up",
        "icon_color": "#fbbf24",
        "guidance": "LONGs allowed with tight stops. Favor higher-conviction setups.",
        "long": True, "short": False,
    },
    3: {
        "label": "MEAN-REVERSION",
        "card_cls": "regime-mean-rev",
        "icon_cls": "ph-duotone ph-arrows-clockwise",
        "icon_color": "#38bdf8",
        "guidance": "Two-sided. Both LONG and SHORT allowed — favor pullback entries.",
        "long": True, "short": True,
    },
    4: {
        "label": "BULL TREND",
        "card_cls": "regime-bull",
        "icon_cls": "ph-duotone ph-trend-up",
        "icon_color": "#34d399",
        "guidance": "Trend day. LONGs primary; let winners run.",
        "long": True, "short": False,
    },
}

DEFAULT_GOAL = 10_000_000.0


def _regime_now():
    """Load the HMM regime once per callback tick."""
    try:
        from signal_scanner.institutional_intel.intelligence.regime_hmm import DailyRegimeHMM
        hmm = DailyRegimeHMM()
        hmm.load()
        if hmm._model is None:
            return None, {}
        state, probs, name = hmm.current_regime()
        return int(state), {"probs": probs, "name": name}
    except Exception:
        return None, {}


def _equity_series():
    """Cumulative realized P&L series from paper_trades.

    Returns (timestamps, cumulative_pnl, latest_value, day_delta).
    """
    try:
        cfg = ScannerConfig()
        conn = sqlite3.connect(f"file:{cfg.db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT closed_at, realized_pnl
            FROM paper_trades
            WHERE status = 'CLOSED' AND closed_at IS NOT NULL
            ORDER BY closed_at ASC
            LIMIT 5000
        """).fetchall()
        conn.close()
    except Exception:
        return [], [], 0.0, 0.0

    if not rows:
        return [], [], 0.0, 0.0

    timestamps = []
    cum = 0.0
    series = []
    prev_day_end = 0.0
    last_day = None
    for r in rows:
        try:
            pnl = float(r["realized_pnl"] or 0)
            cum += pnl
            ts = r["closed_at"]
            timestamps.append(ts)
            series.append(cum)
            day = ts[:10] if ts else None
            if last_day is None:
                last_day = day
            if day != last_day:
                prev_day_end = series[-2] if len(series) >= 2 else 0.0
                last_day = day
        except Exception:
            continue

    latest = series[-1] if series else 0.0
    day_delta = latest - prev_day_end
    return timestamps, series, latest, day_delta


# ---------------------------------------------------------------------------
def register_drishti_callbacks(app) -> None:
    """Wire the Drishti v2 hero callbacks (regime card + equity tracker)."""

    @app.callback(
        Output("dr-regime-card", "className"),
        Output("dr-regime-icon", "className"),
        Output("dr-regime-icon", "style"),
        Output("dr-regime-state", "children"),
        Output("dr-regime-guidance", "children"),
        Output("dr-regime-chips", "children"),
        Input("refresh-interval", "n_intervals"),
        prevent_initial_call=False,
    )
    def update_regime_card(_n):
        state, _ctx = _regime_now()
        if state is None or state not in REGIME_DISPLAY:
            return ("dr-regime-card",
                    "ph-duotone ph-question",
                    {"color": "#a1a1aa"},
                    "REGIME N/A",
                    "HMM model not loaded — start the scanner to refit.",
                    [_side_chip("LONG", False), _side_chip("SHORT", False)])

        d = REGIME_DISPLAY[state]
        card_cls = f"dr-regime-card {d['card_cls']}"
        icon_cls = d["icon_cls"]
        icon_style = {"color": d["icon_color"]}
        state_text = d["label"]
        guidance = d["guidance"]
        chips = [_side_chip("LONG", d["long"]),
                 _side_chip("SHORT", d["short"])]
        return card_cls, icon_cls, icon_style, state_text, guidance, chips

    @app.callback(
        Output("dr-clusters-grid", "children"),
        Output("dr-clusters-summary", "children"),
        Input("sniper-refresh-interval", "n_intervals"),
        prevent_initial_call=False,
    )
    def update_director_clusters(_n):
        from signal_scanner.dashboard.director_clusters import get_recent_director_clusters
        try:
            clusters = get_recent_director_clusters(limit=12)
        except Exception:
            clusters = []
        if not clusters:
            return ([html.Div(
                "No Director clusters in the last 60 days.",
                style={"gridColumn": "1/-1", "color": "var(--dr-text-muted)",
                       "fontSize": "0.82rem", "padding": "12px 4px"})],
                    "0 active")
        active = sum(1 for c in clusters if c["in_window"])
        summary = f"{len(clusters)} shown · {active} active in 60d window"
        cards = [_cluster_card(c) for c in clusters]
        return cards, summary

    @app.callback(
        Output("dr-equity-value", "children"),
        Output("dr-equity-delta", "children"),
        Output("dr-equity-delta", "style"),
        Output("dr-equity-pct-of-goal", "children"),
        Output("dr-equity-spark", "figure"),
        Input("refresh-interval", "n_intervals"),
        prevent_initial_call=False,
    )
    def update_equity_card(_n):
        ts, series, latest, day_delta = _equity_series()
        if not series:
            value_text = "$0"
            delta_text = "no closed trades yet"
            delta_style = {"fontFamily": "var(--dr-font-mono)", "fontSize": "0.78rem",
                           "color": "var(--dr-text-muted)"}
            pct_text = "0.00% of $10M"
            fig = _empty_spark()
            return value_text, delta_text, delta_style, pct_text, fig

        value_text = f"${latest:,.2f}"
        sign = "+" if day_delta >= 0 else "-"
        delta_text = f"{sign}${abs(day_delta):,.0f} today"
        delta_color = "var(--dr-long-bright)" if day_delta >= 0 else "var(--dr-short-bright)"
        delta_style = {"fontFamily": "var(--dr-font-mono)", "fontSize": "0.78rem",
                       "color": delta_color, "fontWeight": "600"}

        pct = (latest / DEFAULT_GOAL) * 100 if DEFAULT_GOAL else 0
        pct_text = f"{pct:.4f}% of $10M"

        fig = _build_spark(ts, series, day_delta >= 0)
        return value_text, delta_text, delta_style, pct_text, fig


# ---------------------------------------------------------------------------
def _side_chip(label: str, allowed: bool):
    icon = "ph ph-check-circle" if allowed else "ph ph-prohibit-inset"
    cls = "dr-side-chip allowed" if allowed else "dr-side-chip blocked"
    return html.Span([html.I(className=icon), label], className=cls)


def _cluster_card(c: dict):
    """Render one Director-cluster edge card."""
    ret = c.get("return_since_cluster_pct", 0.0)
    ret_color = "var(--dr-long-bright)" if ret >= 0 else "var(--dr-short-bright)"
    ret_sign = "+" if ret >= 0 else ""
    days_left = c.get("days_remaining", 0)
    in_window = c.get("in_window", False)
    window_text = (f"{days_left}d left" if in_window
                   else ("WINDOW EXPIRED" if days_left < 0 else f"{days_left}d"))
    window_color = ("var(--dr-text-secondary)" if in_window
                    else "var(--dr-text-muted)")
    n_dir = c.get("n_directors", 0)
    n_ins = c.get("n_insiders", 0)

    return html.Div(
        className="dr-edge-card",
        style={"opacity": "1" if in_window else "0.55"},
        children=[
            html.Div(
                style={"display": "flex", "justifyContent": "space-between",
                       "alignItems": "baseline"},
                children=[
                    html.Span(c["ticker"], className="dr-edge-ticker"),
                    html.Span(f"{ret_sign}{ret:.1f}%",
                              className="dr-edge-stat",
                              style={"color": ret_color}),
                ],
            ),
            html.Div(
                className="dr-edge-meta",
                children=[
                    html.I(className="ph-fill ph-user-circle-gear",
                           style={"marginRight": "4px", "color": "var(--dr-gold)"}),
                    f"{n_dir} director{'s' if n_dir != 1 else ''}",
                    " · ",
                    f"{n_ins} total",
                ],
            ),
            html.Div(
                className="dr-edge-meta",
                style={"color": window_color, "marginTop": "1px",
                       "fontFamily": "var(--dr-font-mono)",
                       "fontSize": "0.70rem"},
                children=[
                    f"avg ${c['avg_buy_price']:.2f} → ${c['current_price']:.2f}  ·  {window_text}",
                ],
            ),
        ],
    )


def _empty_spark():
    fig = go.Figure()
    fig.update_layout(
        margin=dict(l=0, r=0, t=0, b=0),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        xaxis=dict(visible=False),
        yaxis=dict(visible=False),
        showlegend=False, height=44,
    )
    return fig


def _build_spark(timestamps, series, positive: bool):
    color = "#34d399" if positive else "#fb7185"
    fill = "rgba(52,211,153,0.15)" if positive else "rgba(251,113,133,0.15)"
    fig = go.Figure(go.Scatter(
        x=list(range(len(series))), y=series,
        mode="lines", line=dict(color=color, width=2, shape="spline"),
        fill="tozeroy", fillcolor=fill,
        hoverinfo="skip",
    ))
    fig.update_layout(
        margin=dict(l=0, r=0, t=0, b=0),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        xaxis=dict(visible=False, fixedrange=True),
        yaxis=dict(visible=False, fixedrange=True),
        showlegend=False, height=44,
    )
    return fig
