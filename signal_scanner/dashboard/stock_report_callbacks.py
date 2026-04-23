"""Individual Stock Report (ISR) callbacks.

Handles:
    - Populating all ISR sections when selected-ticker store changes
    - Ask Kubera AI report trigger (lazy, on button click)
    - Back button navigation

Data sources (all local DuckDB):
    intelligence_scores   — phase, conviction, cascade, insider, distribution
    agg_qoq_changes       — QoQ institutional flow metrics
    fact_13f_positions    — top institutions holding the stock
    dim_manager_tiers     — tier classification for holders
    agg_sector_rotation   — sector rotation flow strength
    fact_form4_transactions — insider activity
    dim_issuer            — company name, sector
"""

from __future__ import annotations

import traceback
from datetime import datetime, timedelta

import duckdb
import plotly.graph_objects as go
from dash import ALL, Input, Output, State, callback_context, html, no_update
from dash.exceptions import PreventUpdate
from loguru import logger

from signal_scanner.institutional_intel.config import WAREHOUSE_PATH
from signal_scanner.config import DashboardConfig

cfg = DashboardConfig()


def _conn():
    from signal_scanner.institutional_intel.config import safe_duckdb_connect
    return safe_duckdb_connect(read_only=True)


def _safe(val, default=0):
    try:
        if val is None:
            return default
        return type(default)(val)
    except (TypeError, ValueError):
        return default


def _fmt_pct(val) -> str:
    v = _safe(val, 0.0)
    sign = "+" if v > 0 else ""
    return f"{sign}{v:.1f}%"


def _fmt_k(val) -> str:
    v = _safe(val, 0.0)
    if abs(v) >= 1_000_000:
        return f"${v/1_000_000:.1f}B"
    if abs(v) >= 1_000:
        return f"${v/1_000:.1f}M"
    return f"${v:.0f}K"


def _pct_color(val) -> str:
    v = _safe(val, 0.0)
    if v > 0:
        return "#00c896"
    if v < 0:
        return "#e05252"
    return "#888"


def _chat_bubble(text: str, is_user: bool = True) -> html.Div:
    """Render a chat message bubble."""
    from dash import dcc
    if is_user:
        return html.Div(
            style={"textAlign": "right", "marginBottom": "8px"},
            children=html.Div(
                text,
                style={
                    "display": "inline-block", "maxWidth": "85%",
                    "backgroundColor": "rgba(88,166,255,0.15)",
                    "borderRadius": "12px 12px 2px 12px",
                    "padding": "8px 14px", "fontSize": "0.84rem",
                    "color": "#e0e0e0", "textAlign": "left",
                },
            ),
        )
    else:
        return html.Div(
            style={"textAlign": "left", "marginBottom": "8px"},
            children=html.Div(
                dcc.Markdown(text, style={"color": "#e0e0e0", "fontSize": "0.84rem", "lineHeight": "1.6"}),
                style={
                    "display": "inline-block", "maxWidth": "85%",
                    "backgroundColor": "rgba(255,255,255,0.05)",
                    "borderRadius": "12px 12px 12px 2px",
                    "padding": "8px 14px",
                },
            ),
        )


_MIN_COMPLETE_QOQ_TICKERS = 500
_MIN_INTEL_QUALITY_SCORE = 75.0


def _quarter_to_period(quarter: str | None) -> str | None:
    if not quarter or "-Q" not in quarter:
        return None
    try:
        year_s, q_s = quarter.split("-Q", 1)
        year = int(year_s)
        q = int(q_s)
    except (TypeError, ValueError):
        return None
    if q == 1:
        return f"{year}-03-31"
    if q == 2:
        return f"{year}-06-30"
    if q == 3:
        return f"{year}-09-30"
    if q == 4:
        return f"{year}-12-31"
    return None


def _select_isr_qoq_quarter(conn: duckdb.DuckDBPyConnection, ticker: str) -> str | None:
    """Prefer latest quarter with broad market coverage, then fallback to ticker latest."""
    row = conn.execute(
        """
        SELECT q.current_quarter
        FROM agg_qoq_changes q
        JOIN (
            SELECT current_quarter
            FROM agg_qoq_changes
            GROUP BY current_quarter
            HAVING COUNT(*) >= ?
        ) cq ON q.current_quarter = cq.current_quarter
        WHERE q.ticker = ?
        ORDER BY q.current_quarter DESC
        LIMIT 1
        """,
        [_MIN_COMPLETE_QOQ_TICKERS, ticker],
    ).fetchone()
    if row and row[0]:
        return row[0]
    row = conn.execute(
        "SELECT MAX(current_quarter) FROM agg_qoq_changes WHERE ticker = ?",
        [ticker],
    ).fetchone()
    return row[0] if row and row[0] else None


def _select_isr_intel_quarter(
    conn: duckdb.DuckDBPyConnection,
    ticker: str,
    preferred_quarter: str | None,
) -> str | None:
    """Prefer ticker-quarter aligned clean intel, then clean latest, then any latest."""
    if preferred_quarter:
        row = conn.execute(
            """
            SELECT report_quarter
            FROM intelligence_scores
            WHERE ticker = ? AND report_quarter = ?
              AND COALESCE(data_quality_score, 100.0) >= ?
            LIMIT 1
            """,
            [ticker, preferred_quarter, _MIN_INTEL_QUALITY_SCORE],
        ).fetchone()
        if row and row[0]:
            return row[0]

    row = conn.execute(
        """
        SELECT report_quarter
        FROM intelligence_scores
        WHERE ticker = ?
          AND COALESCE(data_quality_score, 100.0) >= ?
        ORDER BY report_quarter DESC
        LIMIT 1
        """,
        [ticker, _MIN_INTEL_QUALITY_SCORE],
    ).fetchone()
    if row and row[0]:
        return row[0]

    row = conn.execute(
        "SELECT MAX(report_quarter) FROM intelligence_scores WHERE ticker = ?",
        [ticker],
    ).fetchone()
    if row and row[0]:
        return row[0]
    return preferred_quarter


def _select_isr_report_period(
    conn: duckdb.DuckDBPyConnection,
    ticker: str,
    preferred_quarter: str | None,
) -> str | None:
    """Use quarter-aligned period when possible, otherwise fallback to ticker latest period."""
    preferred_period = _quarter_to_period(preferred_quarter)
    if preferred_period:
        exists = conn.execute(
            """
            SELECT 1
            FROM fact_13f_positions
            WHERE ticker = ? AND report_period = ?::DATE
            LIMIT 1
            """,
            [ticker, preferred_period],
        ).fetchone()
        if exists:
            return preferred_period

    row = conn.execute(
        "SELECT MAX(report_period) FROM fact_13f_positions WHERE ticker = ?",
        [ticker],
    ).fetchone()
    return str(row[0]) if row and row[0] else None


def _holder_counts_from_positions(
    conn: duckdb.DuckDBPyConnection,
    ticker: str,
    period: str,
) -> dict:
    """Distinct manager tier breakdown for a single ticker period (equity rows only)."""
    rows = conn.execute(
        """
        SELECT
            COALESCE(t.tier, 3) AS tier,
            COUNT(DISTINCT f.manager_cik) AS manager_count
        FROM fact_13f_positions f
        LEFT JOIN dim_manager_tiers t ON f.manager_cik = t.manager_cik
        WHERE f.ticker = ?
          AND f.report_period = ?::DATE
          AND f.shares > 0
          AND COALESCE(f.put_call, '') = ''
        GROUP BY COALESCE(t.tier, 3)
        """,
        [ticker, period],
    ).fetchall()

    out = {"tier1": 0, "tier2": 0, "tier3": 0, "total": 0}
    for tier_val, manager_count in rows:
        tier = int(tier_val or 3)
        count = int(manager_count or 0)
        if tier == 1:
            out["tier1"] = count
        elif tier == 2:
            out["tier2"] = count
        else:
            out["tier3"] += count
        out["total"] += count
    return out


# ---------------------------------------------------------------------------
# Phase badge helpers
# ---------------------------------------------------------------------------
_PHASE_COLORS = {
    "ACTIVE_ACCUM":  "#00c896",
    "EARLY_ACCUM":   "#4dc9ff",
    "LATE_ACCUM":    "#f5a623",
    "EXPANSION":     "#a78bfa",
    "DISTRIBUTION":  "#e05252",
    "DECLINE":       "#888",
    "DORMANT":       "#555",
}

_PHASE_LABELS = {
    "ACTIVE_ACCUM":  "Active Accum",
    "EARLY_ACCUM":   "Early Accum",
    "LATE_ACCUM":    "Late Accum",
    "EXPANSION":     "Expansion",
    "DISTRIBUTION":  "Distribution",
    "DECLINE":       "Decline",
    "DORMANT":       "Dormant",
}

_VERDICT_COLORS = {
    "BUY":        "#00c896",
    "ACCUMULATE": "#4dc9ff",
    "WATCH":      "#f5a623",
    "AVOID":      "#e05252",
    "SHORT":      "#e05252",
    "HOLD":       "#a78bfa",
}


def _build_kpi_html(score, label, color, pct=None):
    """Build KPI card inner HTML: score value, sub-label, and mini progress bar."""
    bar_pct = pct if pct is not None else max(0, min(100, score or 0))
    return html.Div([
        html.Div(
            f"{score:.1f}" if isinstance(score, float) else
            f"{score:.0f}" if isinstance(score, (int, float)) and score is not None else "—",
            style={"fontSize": "1.3rem", "fontWeight": "800", "color": color,
                   "fontFamily": "'JetBrains Mono', monospace"}),
        html.Div(label, style={"fontSize": "0.68rem", "color": "#888", "marginTop": "2px"}),
        html.Div(style={"marginTop": "6px", "height": "3px", "backgroundColor": "#1e2230",
                        "borderRadius": "2px", "overflow": "hidden"}, children=[
            html.Div(style={"height": "100%", "width": f"{bar_pct:.0f}%",
                            "backgroundColor": color, "borderRadius": "2px"}),
        ]),
    ])


def register_stock_report_callbacks(app) -> None:

    # -----------------------------------------------------------------------
    # 1. Populate all ISR sections when selected-ticker changes
    # -----------------------------------------------------------------------
    @app.callback(
        # Header
        Output("isr-ticker-label",   "children"),
        Output("isr-company-label",  "children"),
        Output("isr-sector-label",   "children"),
        Output("isr-phase-badge",    "children"),
        Output("isr-phase-badge",    "style"),
        Output("isr-verdict-badge",  "children"),
        Output("isr-verdict-badge",  "style"),
        # Chart links
        Output("isr-tradingview-link", "href"),
        Output("isr-yahoo-link",       "href"),
        # Scorecards
        Output("isr-sc-conviction",  "children"),
        Output("isr-sc-phase",       "children"),
        Output("isr-sc-cascade",     "children"),
        Output("isr-sc-tier1",       "children"),
        Output("isr-sc-insider",     "children"),
        Output("isr-sc-distribution","children"),
        # Overview panels
        Output("isr-qoq-grid",          "children"),
        Output("isr-holder-breakdown",  "children"),
        Output("isr-sector-rotation",   "children"),
        # Tables + chart
        Output("isr-institutions-table", "data"),
        Output("isr-phase-chart",        "figure"),
        Output("isr-insider-table",      "data"),
        # Trade ideas
        Output("isr-trade-ideas",        "children"),
        # Intelligence KPI cards
        Output("isr-kpi-insider",   "children"),
        Output("isr-kpi-momentum",  "children"),
        Output("isr-kpi-ev",        "children"),
        Output("isr-kpi-alignment", "children"),
        Output("isr-kpi-squeeze",   "children"),
        Output("isr-kpi-ml",        "children"),
        Output("isr-kpi-cascade",   "children"),
        Output("isr-kpi-quality",   "children"),
        # Recommendation system (Phase G)
        Output("isr-rec-verdict",    "children"),
        Output("isr-rec-verdict",    "style"),
        Output("isr-rec-confidence", "children"),
        Output("isr-rec-horizon",    "children"),
        Output("isr-rec-composite",  "children"),
        Output("isr-rec-why-now",    "children"),
        Output("isr-rec-weakens",    "children"),
        Output("isr-rec-scorecard",  "children"),
        Input("selected-ticker", "data"),
    )
    def populate_isr(ticker_data):
        if not ticker_data:
            empty_fig = go.Figure()
            empty_fig.update_layout(
                paper_bgcolor="#0d1117", plot_bgcolor="#0d1117",
                font_color="#888",
                annotations=[{"text": "Select a ticker", "showarrow": False,
                               "font": {"size": 14, "color": "#555"}}],
            )
            _e = html.Div()
            _rec_empty = ("—", {"fontSize": "1.4rem", "fontWeight": "800", "color": "#888"}, "—", "—", "—", "", "", [])
            return (
                "—", "", "", "—", {}, "—", {},
                "#", "#",
                "—", "—", "—", "—", "—", "—",
                [], [], [],
                [], empty_fig, [],
                [],
                _e, _e, _e, _e, _e, _e, _e, _e,
                *_rec_empty,
            )

        ticker = str(ticker_data).upper().strip()

        # Guard: reject invalid tickers before any DB hit
        JUNK_TICKERS = {"N/A", "NONE", "NULL", "NA", ""}
        if ticker in JUNK_TICKERS or len(ticker) > 5:
            empty_fig = go.Figure()
            empty_fig.update_layout(
                paper_bgcolor="#0d1117", plot_bgcolor="#0d1117",
                font_color="#888",
                annotations=[{"text": f"Invalid ticker: {ticker}", "showarrow": False,
                               "font": {"size": 14, "color": "#e05252"}}],
            )
            _e = html.Div()
            _rec_empty = ("—", {"fontSize": "1.4rem", "fontWeight": "800", "color": "#888"}, "—", "—", "—", "", "", [])
            return (
                ticker, "Invalid ticker", "", "—", {}, "—", {},
                "#", "#",
                "—", "—", "—", "—", "—", "—",
                [], [], [],
                [], empty_fig, [],
                [html.P(f"Ticker '{ticker}' is not valid.", style={"color": "#e05252"})],
                _e, _e, _e, _e, _e, _e, _e, _e,
                *_rec_empty,
            )

        try:
            conn = _conn()
            if conn is None:
                lock_fig = _empty_chart("Data temporarily unavailable")
                _e = html.Div()
                return (
                    ticker, "Data temporarily unavailable", "", "—", {}, "—", {},
                    "#", "#",
                    "—", "—", "—", "—", "—", "—",
                    [html.Div("Institutional warehouse is busy. Try again in a minute.",
                              style={"color": "#888", "fontSize": "0.80rem"})],
                    [], [],
                    [], lock_fig, [],
                    [html.P("DuckDB is locked by an active ingestion process.",
                            style={"color": "#888"})],
                    _e, _e, _e, _e, _e, _e, _e, _e,
                    *("—", {"fontSize": "1.4rem", "fontWeight": "800", "color": "#888"}, "—", "—", "—", "", "", []),
                )
            qoq_quarter = _select_isr_qoq_quarter(conn, ticker)
            intel_quarter = _select_isr_intel_quarter(conn, ticker, qoq_quarter)
            period = _select_isr_report_period(conn, ticker, qoq_quarter)

            logger.debug(
                "ISR selection ticker={} qoq_quarter={} intel_quarter={} period={}",
                ticker,
                qoq_quarter,
                intel_quarter,
                period,
            )

            # ── Intelligence scores ───────────────────────────────────────
            intel = {}
            if intel_quarter:
                row = conn.execute("""
                    SELECT accum_phase, accum_phase_quarters, accum_strength_score,
                           cascade_stage, new_initiations_count, copycat_score,
                           tier1_manager_count, tier2_manager_count, manager_quality_score,
                           max_manager_concentration, concentrated_managers_count,
                           insider_cluster_detected, insider_net_buy_count,
                           ceo_cfo_buying, insider_score,
                           distribution_warning, distribution_severity,
                           divergence_active, divergence_magnitude,
                           conviction_score, day_bias, swing_signal, longterm_signal,
                           swing_entry_zone, swing_target, swing_stop,
                           swing_options_suggestion, longterm_thesis,
                           longterm_target_quarter, longterm_options_suggestion,
                           lag_confidence, lag_rationale, expected_impact_quarters,
                           ml_score_v2, expected_value,
                           squeeze_score, short_squeeze_score, days_to_cover,
                           trend_score, price_momentum_90d, price_above_200sma,
                           institutional_pressure, insider_effect_score,
                           data_quality_score
                    FROM intelligence_scores
                    WHERE ticker = ? AND report_quarter = ?
                """, [ticker, intel_quarter]).fetchone()
                if row:
                    cols = [
                        "accum_phase", "accum_phase_quarters", "accum_strength_score",
                        "cascade_stage", "new_initiations_count", "copycat_score",
                        "tier1_manager_count", "tier2_manager_count", "manager_quality_score",
                        "max_manager_concentration", "concentrated_managers_count",
                        "insider_cluster_detected", "insider_net_buy_count",
                        "ceo_cfo_buying", "insider_score",
                        "distribution_warning", "distribution_severity",
                        "divergence_active", "divergence_magnitude",
                        "conviction_score", "day_bias", "swing_signal", "longterm_signal",
                        "swing_entry_zone", "swing_target", "swing_stop",
                        "swing_options_suggestion", "longterm_thesis",
                        "longterm_target_quarter", "longterm_options_suggestion",
                        "lag_confidence", "lag_rationale", "expected_impact_quarters",
                        "ml_score_v2", "expected_value",
                        "squeeze_score", "short_squeeze_score", "days_to_cover",
                        "trend_score", "price_momentum_90d", "price_above_200sma",
                        "institutional_pressure", "insider_effect_score",
                        "data_quality_score",
                    ]
                    intel = dict(zip(cols, row))

            # ── Company / sector ─────────────────────────────────────────
            issuer_row = conn.execute(
                "SELECT issuer_name, sector FROM dim_issuer WHERE ticker = ? LIMIT 1",
                [ticker]
            ).fetchone()
            company = issuer_row[0] if issuer_row else ticker
            sector  = issuer_row[1] if issuer_row and issuer_row[1] else "Unknown"

            # ── QoQ changes ───────────────────────────────────────────────
            qoq = {}
            if qoq_quarter:
                qrow = conn.execute("""
                    SELECT inst_count_current, inst_count_prior,
                           inst_count_change_pct, shares_change_pct,
                           value_change_pct, value_current_usd_k,
                           count_up_streak
                    FROM agg_qoq_changes
                    WHERE ticker = ? AND current_quarter = ?
                """, [ticker, qoq_quarter]).fetchone()
                if qrow:
                    qoq = {
                        "inst_count_current": qrow[0],
                        "inst_count_prior":   qrow[1],
                        "count_change_pct":   qrow[2],
                        "shares_change_pct":  qrow[3],
                        "value_change_pct":   qrow[4],
                        "value_current_usd_k": qrow[5],
                        "count_up_streak":    qrow[6],
                    }

            # ── Phase history (8 quarters) ────────────────────────────────
            phase_hist = conn.execute("""
                SELECT current_quarter, inst_count_current, inst_count_change_pct
                FROM agg_qoq_changes
                WHERE ticker = ?
                ORDER BY current_quarter DESC
                LIMIT 8
            """, [ticker]).fetchdf()

            # ── Top institutions ──────────────────────────────────────────
            inst_rows = []
            holder_counts = {"tier1": 0, "tier2": 0, "tier3": 0, "total": 0}
            if period:
                holder_counts = _holder_counts_from_positions(conn, ticker, period)
                inst_df = conn.execute("""
                    SELECT
                        f.manager_cik,
                        MAX(f.manager_name) AS manager_name,
                        SUM(f.shares) AS shares,
                        SUM(f.value_usd_thousands) AS value_usd_thousands,
                        COALESCE(MAX(t.tier), 3) AS tier
                    FROM fact_13f_positions f
                    LEFT JOIN dim_manager_tiers t ON f.manager_cik = t.manager_cik
                    WHERE f.ticker = ? AND f.report_period = ?::DATE
                      AND f.shares > 0
                      AND COALESCE(f.put_call, '') = ''
                    GROUP BY f.manager_cik
                    ORDER BY value_usd_thousands DESC
                    LIMIT 20
                """, [ticker, period]).fetchdf()

                mgr_totals = {}
                if not inst_df.empty:
                    ciks = [str(c) for c in inst_df["manager_cik"].dropna().tolist()]
                    if ciks:
                        placeholders = ", ".join(["?"] * len(ciks))
                        mgr_total_rows = conn.execute(f"""
                            SELECT manager_cik, SUM(value_usd_thousands) AS total_aum
                            FROM fact_13f_positions
                            WHERE report_period = ?::DATE
                              AND shares > 0
                              AND COALESCE(put_call, '') = ''
                              AND manager_cik IN ({placeholders})
                            GROUP BY manager_cik
                        """, [period, *ciks]).fetchdf()
                        for _, r in mgr_total_rows.iterrows():
                            mgr_totals[str(r["manager_cik"])] = float(r["total_aum"] or 0)

                for _, r in inst_df.iterrows():
                    cik = str(r["manager_cik"])
                    total_aum = mgr_totals.get(cik, 0)
                    pct = round(float(r["value_usd_thousands"] or 0) / total_aum * 100, 2) \
                          if total_aum > 0 else 0.0
                    inst_rows.append({
                        "manager_name":       str(r["manager_name"] or ""),
                        "tier":               int(r["tier"] or 3),
                        "shares":             int(r["shares"] or 0),
                        "value_usd_thousands": round(float(r["value_usd_thousands"] or 0), 0),
                        "pct_portfolio":       pct,
                    })

            # ── Sector rotation ───────────────────────────────────────────
            sector_rot = {}
            sector_quarter = qoq_quarter or intel_quarter
            if sector_quarter and sector != "Unknown":
                sr = conn.execute("""
                    SELECT flow_pct, net_flow_k, inflow_streak, ticker_count
                    FROM agg_sector_rotation
                    WHERE sector = ? AND report_quarter = ?
                """, [sector, sector_quarter]).fetchone()
                if sr:
                    sector_rot = {
                        "flow_pct":      sr[0],
                        "net_flow_k":    sr[1],
                        "inflow_streak": sr[2],
                        "ticker_count":  sr[3],
                    }

            # ── Insider activity (last 180 days) ──────────────────────────
            cutoff = (datetime.utcnow() - timedelta(days=180)).strftime("%Y-%m-%d")
            insider_df = conn.execute("""
                SELECT transaction_date, insider_name, insider_role,
                       transaction_code, shares, price,
                       ROUND(shares * COALESCE(price, 0), 0) AS dollar_value,
                       ownership_after
                FROM fact_form4_transactions
                WHERE ticker = ? AND transaction_date >= ?
                ORDER BY transaction_date DESC
                LIMIT 20
            """, [ticker, cutoff]).fetchdf()
            insider_rows = insider_df.to_dict("records") if not insider_df.empty else []

            conn.close()

        except Exception as e:
            logger.error("ISR populate error for {}: {}", ticker, traceback.format_exc())
            empty_fig = _empty_chart("Error loading data")
            _e = html.Div()
            return (
                ticker, "Error loading data", "", "ERR", {}, "ERR", {},
                "#", "#",
                "—", "—", "—", "—", "—", "—",
                [], [], [],
                [], empty_fig, [],
                [],
                _e, _e, _e, _e, _e, _e, _e, _e,
            )

        # ── Build outputs ─────────────────────────────────────────────────

        # Phase badge
        phase = intel.get("accum_phase", "DORMANT") or "DORMANT"
        phase_color = _PHASE_COLORS.get(phase, "#555")
        phase_label = _PHASE_LABELS.get(phase, phase)
        phase_badge_style = {
            "backgroundColor": phase_color + "22",
            "color": phase_color,
            "border": f"1px solid {phase_color}",
            "borderRadius": "4px", "padding": "4px 10px",
            "fontSize": "0.72rem", "fontWeight": "700",
            "textTransform": "uppercase", "letterSpacing": "0.08em",
            "marginRight": "8px",
        }

        # Verdict badge
        swing = intel.get("swing_signal", "WATCH") or "WATCH"
        lt    = intel.get("longterm_signal", "WATCH") or "WATCH"
        verdict = lt if lt not in ("WATCH", None) else swing
        verdict_color = _VERDICT_COLORS.get(verdict, "#888")
        verdict_badge_style = {
            "backgroundColor": verdict_color + "22",
            "color": verdict_color,
            "border": f"1px solid {verdict_color}",
            "borderRadius": "4px", "padding": "4px 10px",
            "fontSize": "0.72rem", "fontWeight": "700",
            "textTransform": "uppercase", "letterSpacing": "0.08em",
        }

        # Scorecards
        conviction = intel.get("conviction_score", 0) or 0
        phase_streak = intel.get("accum_phase_quarters", 0) or 0
        cascade = intel.get("cascade_stage", 0) or 0
        tier1 = intel.get("tier1_manager_count", 0) or 0
        insider_score = intel.get("insider_score", 0) or 0
        dist_sev = intel.get("distribution_severity") or "NONE"

        # QoQ grid
        qoq_grid = _build_qoq_grid(qoq)

        # Holder breakdown
        tier1_cnt = intel.get("tier1_manager_count", 0) or 0
        tier2_cnt = intel.get("tier2_manager_count", 0) or 0
        total_cnt = int(qoq.get("inst_count_current") or 0)
        if total_cnt <= 0:
            total_cnt = int(holder_counts.get("total", 0) or 0)
        if tier1_cnt <= 0:
            tier1_cnt = int(holder_counts.get("tier1", 0) or 0)
        if tier2_cnt <= 0:
            tier2_cnt = int(holder_counts.get("tier2", 0) or 0)
        tier3_cnt = max(0, total_cnt - tier1_cnt - tier2_cnt)
        holder_breakdown = _build_holder_breakdown(tier1_cnt, tier2_cnt, tier3_cnt, total_cnt)

        # Sector rotation panel
        sector_panel = _build_sector_rotation_panel(sector, sector_rot)

        # Phase history chart
        phase_fig = _build_phase_chart(ticker, phase_hist)

        # Trade ideas
        trade_ideas = _build_trade_ideas(intel, ticker)

        # ISR expansion blocks (Phase G spec)
        try:
            from signal_scanner.dashboard.isr_blocks import (
                build_interconnected_block, build_drivers_block,
                build_evidence_block, build_mean_reversion_block,
                build_buy_summary_block,
            )
            trade_ideas.extend([
                build_buy_summary_block(ticker, intel),
                build_interconnected_block(ticker),
                build_drivers_block(ticker),
                build_mean_reversion_block(ticker),
                build_evidence_block(ticker),
            ])
        except Exception as e:
            logger.debug("ISR expansion blocks error: {}", e)

        # Chart links
        tv_url = f"https://www.tradingview.com/chart/?symbol={ticker}"
        yf_url = f"https://finance.yahoo.com/quote/{ticker}/chart"

        # ── Intelligence KPI computation ────────────────────────────────

        # KPI 1: Insider Signal Strength
        insider_effect = _safe(intel.get("insider_effect_score"), 0)
        insider_score_val = _safe(intel.get("insider_score"), 0)
        cluster = bool(intel.get("insider_cluster_detected"))
        kpi_insider = insider_effect * 0.5 + insider_score_val * 0.3 + (20 if cluster else 0)
        kpi_insider = min(100, kpi_insider)
        kpi_insider_color = "#00c896" if kpi_insider >= 70 else "#f5a623" if kpi_insider >= 45 else "#e05252"
        kpi_insider_label = "Cluster active" if cluster else f"Effect: {insider_effect:.0f}"
        kpi_insider_html = _build_kpi_html(kpi_insider, kpi_insider_label, kpi_insider_color)

        # KPI 2: Institutional Momentum
        pressure = _safe(intel.get("institutional_pressure"), 0)
        accum_str = _safe(intel.get("accum_strength_score"), 0)
        kpi_momentum = pressure * 0.5 + accum_str * 0.5
        kpi_momentum = min(100, kpi_momentum)
        streak = _safe(qoq.get("count_up_streak"), 0)
        arrow = "\u2191" if streak >= 2 else "\u2193" if streak <= 0 else "\u2192"
        kpi_mom_color = "#00c896" if kpi_momentum >= 70 else "#f5a623" if kpi_momentum >= 45 else "#e05252"
        kpi_mom_label = f"{arrow} Streak: {streak}Q"
        kpi_momentum_html = _build_kpi_html(kpi_momentum, kpi_mom_label, kpi_mom_color)

        # KPI 3: Risk-Adjusted EV
        ev_raw = _safe(intel.get("expected_value"), 0.0)
        conv = _safe(intel.get("conviction_score"), 0.0)
        dist = bool(intel.get("distribution_warning"))
        kpi_ev = ev_raw * (conv / 100) if conv > 0 else 0
        if dist:
            kpi_ev *= 0.7  # 30% discount
        kpi_ev_color = "#00c896" if kpi_ev > 3 else "#f5a623" if kpi_ev > 0 else "#e05252"
        kpi_ev_label = f"Raw EV: {ev_raw:.1f}% | Conv: {conv:.0f}"
        kpi_ev_html = _build_kpi_html(
            round(kpi_ev, 1), kpi_ev_label, kpi_ev_color,
            pct=min(100, max(0, kpi_ev * 10 + 50)),
        )

        # KPI 4: Tech-Inst Alignment
        trend = _safe(intel.get("trend_score"), 50)
        mom90 = _safe(intel.get("price_momentum_90d"), 0.0)
        sma200 = _safe(intel.get("price_above_200sma"), 0)
        sma_bonus = 15 if sma200 else -10
        mom_norm = min(30, max(-30, mom90)) / 30 * 30  # normalize to +/-30
        kpi_align = trend * 0.45 + (mom_norm + 30) * 0.35 + (50 + sma_bonus) * 0.20
        kpi_align = max(0, min(100, kpi_align))
        aligned = kpi_align >= 55
        kpi_align_color = "#00c896" if kpi_align >= 65 else "#f5a623" if kpi_align >= 45 else "#e05252"
        kpi_align_label = ("Aligned \u2713" if aligned else "Diverging \u2717") + f" | Mom: {mom90:+.1f}%"
        kpi_align_html = _build_kpi_html(kpi_align, kpi_align_label, kpi_align_color)

        # KPI 5: Squeeze Setup
        sq = _safe(intel.get("short_squeeze_score"), 0)
        dtc = _safe(intel.get("days_to_cover"), 0.0)
        phase_str = intel.get("accum_phase", "") or ""
        is_accum = phase_str in ("ACTIVE_ACCUM", "LATE_ACCUM", "EARLY_ACCUM")
        kpi_squeeze = sq * 0.6 + min(100, conv * 0.4)
        if not is_accum:
            kpi_squeeze *= 0.5
        kpi_squeeze = min(100, kpi_squeeze)
        kpi_sq_color = "#00c896" if kpi_squeeze >= 60 else "#f5a623" if kpi_squeeze >= 30 else "#888"
        kpi_sq_label = f"DTC: {dtc:.1f}d" + (" | Accumulating" if is_accum else " | Not accumulating")
        kpi_squeeze_html = _build_kpi_html(kpi_squeeze, kpi_sq_label, kpi_sq_color)

        # KPI 6: ML Confidence
        ml = _safe(intel.get("ml_score_v2"), 0)
        if ml >= 90:
            ml_label = "Top 5% \u2014 Very High"
        elif ml >= 80:
            ml_label = "Top 10% \u2014 High"
        elif ml >= 60:
            ml_label = "Top 20% \u2014 Above Avg"
        elif ml > 0:
            ml_label = "Below median"
        else:
            ml_label = "No ML score"
        kpi_ml_color = "#00c896" if ml >= 80 else "#4dc9ff" if ml >= 60 else "#f5a623" if ml > 0 else "#555"
        kpi_ml_html = _build_kpi_html(ml, ml_label, kpi_ml_color)

        # KPI 7: Cascade Momentum
        cascade_val = _safe(intel.get("cascade_stage"), 0)
        copycat = _safe(intel.get("copycat_score"), 0)
        initiations = _safe(intel.get("new_initiations_count"), 0)
        kpi_cascade = copycat * 0.5 + (cascade_val / 3 * 100) * 0.3 + min(100, initiations * 5) * 0.2
        kpi_cascade = min(100, kpi_cascade)
        stage_labels = {0: "No cascade", 1: "Stage 1 \u2014 Early", 2: "Stage 2 \u2014 Active", 3: "Stage 3 \u2014 Broad"}
        kpi_cas_color = "#a78bfa" if cascade_val >= 2 else "#4dc9ff" if cascade_val >= 1 else "#888"
        kpi_cas_label = stage_labels.get(cascade_val, f"Stage {cascade_val}") + f" | {initiations} new"
        kpi_cascade_html = _build_kpi_html(kpi_cascade, kpi_cas_label, kpi_cas_color)

        # KPI 8: Data Quality
        dq = _safe(intel.get("data_quality_score"), 0)
        impact_q = _safe(intel.get("expected_impact_quarters"), 0)
        phase_q = _safe(intel.get("accum_phase_quarters"), 0)
        if impact_q <= 1:
            timeline = "EARLY \u2014 forming"
        elif impact_q <= 2:
            timeline = "DEVELOPING"
        else:
            timeline = "MATURE \u2014 late"
        kpi_dq_color = "#00c896" if dq >= 80 else "#f5a623" if dq >= 60 else "#e05252"
        kpi_dq_label = f"{timeline} | Phase: {phase_q}Q"
        kpi_quality_html = _build_kpi_html(dq, kpi_dq_label, kpi_dq_color)

        return (
            # Header
            ticker, company, f"Sector: {sector}",
            phase_label, phase_badge_style,
            verdict,     verdict_badge_style,
            # Chart links
            tv_url, yf_url,
            # Scorecards
            f"{conviction:.0f}",
            f"{phase_streak}Q",
            f"Stage {cascade}",
            str(tier1),
            f"{insider_score:.0f}",
            dist_sev,
            # Overview
            qoq_grid, holder_breakdown, sector_panel,
            # Tables + chart
            inst_rows, phase_fig, insider_rows,
            # Trade ideas
            trade_ideas,
            # Intelligence KPI cards
            kpi_insider_html, kpi_momentum_html, kpi_ev_html, kpi_align_html,
            kpi_squeeze_html, kpi_ml_html, kpi_cascade_html, kpi_quality_html,
            # Recommendation system (Phase G)
            *_build_recommendation_outputs(intel, ticker),
        )

def _build_recommendation_outputs(intel: dict, ticker: str) -> tuple:
    """Build the 8 recommendation outputs for ISR."""
    try:
        from signal_scanner.institutional_intel.intelligence.isr_recommendation import compute_recommendation
        rec = compute_recommendation(intel, ticker)

        verdict = rec["verdict"]
        verdict_colors = {
            "Strong Buy": "#00ff88", "Buy": "#00c896", "Watch": "#ffd43b",
            "Neutral": "#888", "Avoid": "#e05252", "Strong Avoid": "#ff4488",
        }
        verdict_style = {
            "fontSize": "1.4rem", "fontWeight": "800",
            "color": verdict_colors.get(verdict, "#888"),
        }

        confidence = rec["confidence"]
        horizon = rec["horizon"]
        composite = f"{rec['composite_score']:.0f}"

        # Why Now (join as bullet points)
        why_items = rec["why_now"]
        why_text = " | ".join(why_items) if why_items else "No strong current reasons"

        # Weakens
        weak_items = rec["weakens"]
        weak_text = ("Risks: " + " | ".join(weak_items)) if weak_items else ""

        # Scorecard chips
        from dash import html
        scorecard_chips = []
        sc_colors = {
            "Thesis Strength": "#f5a623", "Setup Quality": "#4dc9ff",
            "Predictive Edge": "#a78bfa", "Pressure / Positioning": "#ff8c00",
            "Sector / Theme": "#00c896", "Interconnected": "#4da3ff",
            "Options Quality": "#a78bfa", "Risk / Liquidity": "#888",
        }
        for name, score in rec["scorecard"].items():
            color = sc_colors.get(name, "#888")
            scorecard_chips.append(
                html.Div(
                    style={"display": "flex", "alignItems": "center", "gap": "4px",
                           "padding": "3px 8px", "borderRadius": "4px",
                           "border": f"1px solid {color}33", "backgroundColor": f"{color}11"},
                    children=[
                        html.Span(name, style={"fontSize": "0.60rem", "color": "#888"}),
                        html.Span(str(score), style={"fontSize": "0.80rem", "fontWeight": "700",
                                                      "color": color,
                                                      "fontFamily": "'JetBrains Mono', monospace"}),
                    ],
                )
            )

        return (verdict, verdict_style, confidence, horizon, composite,
                why_text, weak_text, scorecard_chips)
    except Exception as e:
        from loguru import logger
        logger.debug("ISR recommendation error: {}", e)
        return ("—", {"fontSize": "1.4rem", "fontWeight": "800", "color": "#888"},
                "—", "—", "—", "", "", [])


    # -----------------------------------------------------------------------
    # 2. TradeGPT ISR Chat — conversational AI (replaces Ask Kubera)
    # -----------------------------------------------------------------------
    @app.callback(
        Output("isr-chat-messages", "children", allow_duplicate=True),
        Output("isr-chat-input", "value"),
        Input("isr-chat-send-btn", "n_clicks"),
        Input("isr-chat-input", "n_submit"),
        Input({"type": "isr-quick-q", "index": ALL}, "n_clicks"),
        Input("isr-chat-clear-btn", "n_clicks"),
        State("isr-chat-input", "value"),
        State("isr-chat-messages", "children"),
        State("selected-ticker", "data"),
        prevent_initial_call=True,
    )
    def isr_chat_handler(send_clicks, n_submit, quick_clicks, clear_clicks, input_val, current_messages, ticker_data):
        from dash import ctx as dash_ctx

        triggered_id = dash_ctx.triggered_id

        # Clear chat
        if triggered_id == "isr-chat-clear-btn":
            if ticker_data:
                ticker = str(ticker_data).upper().strip()
                try:
                    from signal_scanner.institutional_intel.intelligence.trade_gpt import get_trade_gpt
                    get_trade_gpt().clear_session(f"isr-{ticker}")
                except Exception:
                    pass
            return [html.Div("Ask me anything about this stock...",
                            style={"color": "#666", "fontSize": "0.82rem", "fontStyle": "italic"})], ""

        # Quick question buttons
        quick_prompts = [
            "Give me a full analysis of this stock with entry/exit levels for all timeframes.",
            "What's the best entry setup right now? Include specific price levels and stop loss.",
            "What are the key risk factors that could invalidate the current thesis?",
            "What options strategy would you recommend for this stock? Include strike and expiry guidance.",
        ]
        user_msg = None
        if isinstance(triggered_id, dict) and triggered_id.get("type") == "isr-quick-q":
            idx = triggered_id.get("index", 0)
            user_msg = quick_prompts[idx] if idx < len(quick_prompts) else None
        else:
            user_msg = input_val

        if not user_msg or not user_msg.strip():
            raise PreventUpdate

        ticker = str(ticker_data).upper().strip() if ticker_data else None
        if not ticker:
            return (current_messages or []) + [
                _chat_bubble("Please select a stock first.", is_user=False)
            ], ""

        # Build context — always load for current ticker
        session_id = f"isr-{ticker}"
        context = None
        try:
            from signal_scanner.institutional_intel.intelligence.trade_gpt import get_trade_gpt
            gpt = get_trade_gpt()

            # Load context if this ticker doesn't have a briefing yet
            active = gpt.get_active_ticker(session_id)
            if active != ticker or session_id not in gpt.conversations:
                try:
                    from signal_scanner.institutional_intel.intelligence.kubera_context import build_stock_context
                    conn = _conn()
                    if conn:
                        context = build_stock_context(ticker, conn)
                        conn.close()
                        logger.debug("ISR context loaded for {}: {} fields", ticker, len(context) if context else 0)
                except Exception as e:
                    logger.warning("Context build failed for {}: {}", ticker, e)
                    context = {}  # Pass empty dict so chat() knows we tried

            response = gpt.chat(session_id, user_msg.strip(), ticker=ticker, context=context)
        except Exception as e:
            logger.error("TradeGPT ISR error for {}: {}", ticker, e)
            response = f"**Error**: {e}"

        # Build message list (replace placeholder if present)
        msgs = current_messages or []
        # Remove placeholder text
        if msgs and len(msgs) == 1:
            first = msgs[0]
            if isinstance(first, dict) and "italic" in str(first.get("props", {}).get("style", {})):
                msgs = []

        msgs.append(_chat_bubble(user_msg.strip(), is_user=True))
        msgs.append(_chat_bubble(response, is_user=False))

        return msgs, ""

    # -----------------------------------------------------------------------
    # 3. Back button — clear selected ticker, restore previous section
    # -----------------------------------------------------------------------
    @app.callback(
        Output("selected-ticker", "data", allow_duplicate=True),
        Input("isr-back-btn", "n_clicks"),
        prevent_initial_call=True,
    )
    def isr_back(n_clicks):
        return None

    # -----------------------------------------------------------------------
    # 3b. Jump-to-ticker — switch ISR to a new symbol without leaving
    # -----------------------------------------------------------------------
    @app.callback(
        Output("selected-ticker", "data", allow_duplicate=True),
        Output("isr-jump-ticker", "value"),
        Input("isr-jump-btn", "n_clicks"),
        Input("isr-jump-ticker", "n_submit"),
        State("isr-jump-ticker", "value"),
        prevent_initial_call=True,
    )
    def isr_jump_to_ticker(n_clicks, n_submit, ticker):
        if not ticker or not ticker.strip():
            raise PreventUpdate
        return ticker.strip().upper(), ""

    # -----------------------------------------------------------------------
    # 3c. Auto-clear chat when ticker changes
    # -----------------------------------------------------------------------
    @app.callback(
        Output("isr-chat-messages", "children", allow_duplicate=True),
        Input("selected-ticker", "data"),
        prevent_initial_call=True,
    )
    def isr_chat_auto_clear(ticker_data):
        """Reset chat messages when the user switches to a different ticker."""
        return [html.Div(
            "Ask me anything about this stock...",
            style={"color": "#666", "fontSize": "0.82rem", "fontStyle": "italic"},
        )]

    # -----------------------------------------------------------------------
    # 4. Research tab — ISR symbol lookup
    # -----------------------------------------------------------------------
    @app.callback(
        Output("selected-ticker", "data", allow_duplicate=True),
        Output("research-quarter-badge", "children"),
        Input("research-search-btn", "n_clicks"),
        Input("research-ticker-input", "n_submit"),
        State("research-ticker-input", "value"),
        prevent_initial_call=True,
    )
    def research_isr_lookup(n_clicks, n_submit, ticker):
        if not ticker or not ticker.strip():
            raise PreventUpdate
        ticker = ticker.strip().upper()
        # Show active quarter info
        quarter_info = ""
        try:
            conn = _conn()
            if conn:
                from signal_scanner.institutional_intel.config import get_active_quarter
                q = get_active_quarter(conn)
                conn.close()
                if q:
                    quarter_info = f"Active quarter: {q}"
        except Exception:
            pass
        return ticker, quarter_info


# ---------------------------------------------------------------------------
# UI builder helpers
# ---------------------------------------------------------------------------

def _empty_chart(msg: str = "") -> go.Figure:
    fig = go.Figure()
    fig.update_layout(
        paper_bgcolor="#0d1117", plot_bgcolor="#0d1117",
        font_color="#888", margin={"l": 10, "r": 10, "t": 10, "b": 10},
    )
    if msg:
        fig.add_annotation(text=msg, showarrow=False,
                           font={"size": 13, "color": "#555"}, x=0.5, y=0.5)
    return fig


def _build_qoq_grid(qoq: dict) -> list:
    if not qoq:
        return [html.Div("No QoQ data available.", style={"color": "#555", "fontSize": "0.80rem"})]

    rows = [
        ("Institutions", qoq.get("inst_count_current"), qoq.get("count_change_pct"), "count"),
        ("Shares",       None,                           qoq.get("shares_change_pct"), "pct"),
        ("Value",        qoq.get("value_current_usd_k"), qoq.get("value_change_pct"),  "value"),
    ]

    items = []
    for label, absolute, pct, mode in rows:
        pct_str = _fmt_pct(pct)
        pct_col = _pct_color(pct)
        abs_str = ""
        if mode == "count" and absolute is not None:
            abs_str = f"{int(absolute):,} holders"
        elif mode == "value" and absolute is not None:
            abs_str = _fmt_k(absolute)

        items.append(
            html.Div(
                style={"display": "flex", "justifyContent": "space-between",
                       "padding": "8px 0", "borderBottom": "1px solid #1e2230"},
                children=[
                    html.Span(label, style={"color": "#888", "fontSize": "0.80rem"}),
                    html.Div([
                        html.Span(abs_str,
                                  style={"color": cfg.text_color, "fontSize": "0.80rem",
                                         "marginRight": "8px"}),
                        html.Span(pct_str,
                                  style={"color": pct_col, "fontWeight": "700",
                                         "fontSize": "0.82rem"}),
                    ]),
                ],
            )
        )
    return items


def _build_holder_breakdown(tier1: int, tier2: int, tier3: int, total: int) -> list:
    items = [
        ("Tier-1 (Top 20 funds)",   tier1, "#00c896"),
        ("Tier-2 (Major funds)",    tier2, "#4dc9ff"),
        ("Tier-3 (Other)",          tier3, "#888"),
        ("Total Institutions",      total, cfg.accent_primary),
    ]
    rows = []
    for label, val, color in items:
        rows.append(
            html.Div(
                style={"display": "flex", "justifyContent": "space-between",
                       "padding": "8px 0", "borderBottom": "1px solid #1e2230"},
                children=[
                    html.Span(label, style={"color": "#888", "fontSize": "0.80rem"}),
                    html.Span(str(val),
                              style={"color": color, "fontWeight": "700", "fontSize": "0.88rem"}),
                ],
            )
        )
    return rows


def _build_sector_rotation_panel(sector: str, sr: dict) -> list:
    if not sr:
        return [html.Div(f"No rotation data for sector: {sector}",
                         style={"color": "#555", "fontSize": "0.80rem"})]

    flow_pct   = _safe(sr.get("flow_pct"), 0.0)
    net_flow_k = _safe(sr.get("net_flow_k"), 0.0)
    streak     = _safe(sr.get("inflow_streak"), 0)
    tickers    = _safe(sr.get("ticker_count"), 0)

    # Strength label
    if flow_pct >= 5:
        strength, strength_color = "STRONG INFLOW", "#00c896"
    elif flow_pct >= 1:
        strength, strength_color = "MODERATE INFLOW", "#4dc9ff"
    elif flow_pct >= -1:
        strength, strength_color = "NEUTRAL", "#888"
    elif flow_pct >= -5:
        strength, strength_color = "MODERATE OUTFLOW", "#f5a623"
    else:
        strength, strength_color = "STRONG OUTFLOW", "#e05252"

    rows = [
        ("Sector", sector, cfg.text_color),
        ("Rotation",     strength,          strength_color),
        ("Net Flow",     _fmt_k(net_flow_k), _pct_color(net_flow_k)),
        ("Flow %",       _fmt_pct(flow_pct), _pct_color(flow_pct)),
        ("Inflow Streak", f"{streak}Q",      "#4dc9ff" if streak > 0 else "#888"),
        ("Tickers in Sector", str(tickers),  "#888"),
    ]

    items = []
    for label, val, color in rows:
        items.append(
            html.Div(
                style={"display": "flex", "justifyContent": "space-between",
                       "padding": "8px 0", "borderBottom": "1px solid #1e2230"},
                children=[
                    html.Span(label, style={"color": "#888", "fontSize": "0.80rem"}),
                    html.Span(str(val), style={"color": color, "fontWeight": "600",
                                               "fontSize": "0.82rem"}),
                ],
            )
        )
    return items


def _build_phase_chart(ticker: str, df) -> go.Figure:
    if df is None or df.empty:
        return _empty_chart("No history data")

    df = df.sort_values("current_quarter")
    quarters = df["current_quarter"].tolist()
    counts   = df["inst_count_current"].tolist()
    pcts     = df["inst_count_change_pct"].tolist()

    bar_colors = ["#00c896" if (p or 0) >= 0 else "#e05252" for p in pcts]

    fig = go.Figure()
    fig.add_trace(go.Bar(
        x=quarters, y=counts,
        marker_color=bar_colors,
        name="Institutions",
        hovertemplate="%{x}<br>%{y:,} holders<extra></extra>",
    ))

    fig.update_layout(
        paper_bgcolor="#0d1117",
        plot_bgcolor="#0d1117",
        font={"color": "#888", "size": 11},
        margin={"l": 40, "r": 10, "t": 10, "b": 40},
        xaxis={"gridcolor": "#1e2230", "linecolor": "#1e2230"},
        yaxis={"gridcolor": "#1e2230", "linecolor": "#1e2230"},
        showlegend=False,
    )
    return fig


def _build_trade_ideas(intel: dict, ticker: str) -> list:
    """Build three trade idea cards: Day / Swing / Long Term."""
    day_bias   = intel.get("day_bias")    or "NEUTRAL"
    swing_sig  = intel.get("swing_signal") or "WATCH"
    lt_sig     = intel.get("longterm_signal") or "WATCH"
    swing_entry = intel.get("swing_entry_zone") or "—"
    swing_tgt   = intel.get("swing_target") or "—"
    swing_stop  = intel.get("swing_stop") or "—"
    swing_opts  = intel.get("swing_options_suggestion") or "—"
    lt_thesis   = intel.get("longterm_thesis") or "—"
    lt_tgt_q    = intel.get("longterm_target_quarter") or "—"
    lt_opts     = intel.get("longterm_options_suggestion") or "—"
    lag         = intel.get("lag_confidence") or "LOW"
    lag_note    = intel.get("lag_rationale") or ""
    impact_q    = intel.get("expected_impact_quarters") or "—"
    phase       = intel.get("accum_phase") or "DORMANT"
    conviction  = _safe(intel.get("conviction_score"), 0.0)
    dist_warn   = intel.get("distribution_warning") or False

    ideas = [
        _trade_idea_card(
            "DAY TRADE",
            day_bias,
            [
                ("Bias",     day_bias),
                ("Phase",    phase),
                ("Options",  f"0DTE/Weekly — {swing_opts}"),
                ("Note",     "Follow intraday structure; institutional data is quarterly"),
            ],
            "#4dc9ff",
        ),
        _trade_idea_card(
            "SWING TRADE",
            swing_sig,
            [
                ("Signal",       swing_sig),
                ("Conviction",   f"{conviction:.0f}/100"),
                ("Entry Zone",   swing_entry),
                ("Target",       swing_tgt),
                ("Stop Loss",    swing_stop),
                ("Options",      f"30-45 DTE — {swing_opts}"),
                ("Hold",         "2–8 weeks"),
            ],
            "#00c896" if swing_sig in ("BUY", "STRONG_BUY") else
            "#e05252" if swing_sig in ("AVOID", "SHORT") else "#f5a623",
        ),
        _trade_idea_card(
            "LONG TERM",
            lt_sig,
            [
                ("Signal",         lt_sig),
                ("Thesis",         (lt_thesis[:120] + "…") if len(lt_thesis) > 120 else lt_thesis),
                ("Target Quarter", lt_tgt_q),
                ("Impact Est.",    f"{impact_q}Q from filing release"),
                ("Lag Confidence", lag),
                ("LEAPS",          f"90-180 DTE — {lt_opts}"),
                ("Exit Trigger",   (lag_note[:100] + "…") if len(lag_note) > 100 else lag_note),
                ("Risk",           "SEVERE distribution warning" if dist_warn else "Monitor quarterly"),
            ],
            "#00c896" if lt_sig in ("BUY", "ACCUMULATE") else
            "#e05252" if lt_sig in ("EXIT", "SHORT") else "#a78bfa",
        ),
    ]

    # --- Options Expression: from fact_options_contracts via OptionsIntelligence ---
    try:
        from signal_scanner.institutional_intel.config import safe_duckdb_connect as _odc
        from signal_scanner.institutional_intel.intelligence.options_intelligence import OptionsIntelligence
        _oc = _odc(read_only=True)
        if _oc:
            try:
                oeng = OptionsIntelligence(_oc)
                direction = "LONG" if swing_sig in ("BUY", "STRONG_BUY", "ACCUMULATE") else "SHORT"
                recs = oeng.recommend_expressions(ticker, direction, target_delta=0.40, max_results=4)
                summary = oeng.get_underlying_summary(ticker)
            finally:
                _oc.close()

            if recs:
                opt_rows = []
                for r in recs:
                    opt_rows.append((
                        f"{r['contract_type'].upper()} ${r['strike']:.0f}",
                        f"Exp {r['expiry']} | D={r['delta']} | OI={r['open_interest']} | Score={r['score']}",
                    ))
                # Add IV context line if available
                if summary.get("has_data"):
                    atm = summary.get("atm_iv")
                    skew = summary.get("call_put_skew")
                    pcr = summary.get("put_call_ratio")
                    ctx_parts = []
                    if atm:
                        ctx_parts.append(f"IV={atm:.1%}")
                    if skew:
                        ctx_parts.append(f"Skew={skew:.3f}")
                    if pcr:
                        ctx_parts.append(f"P/C={pcr:.2f}")
                    if ctx_parts:
                        opt_rows.append(("Context", " | ".join(ctx_parts)))

                ideas.append(
                    _trade_idea_card(
                        "OPTIONS EXPRESSION",
                        swing_sig,
                        opt_rows,
                        "#a78bfa",
                    ),
            )
    except Exception:
        pass  # options expression is best-effort

    return ideas


def _trade_idea_card(title: str, signal: str, rows: list, accent: str) -> "dbc.Col":
    from dash_bootstrap_components import Col

    sig_color = {
        "BUY": "#00c896", "STRONG_BUY": "#00c896", "ACCUMULATE": "#4dc9ff",
        "WATCH": "#f5a623", "AVOID": "#e05252", "SHORT": "#e05252",
        "EXIT": "#e05252", "LONG_ONLY": "#00c896", "SHORT_ONLY": "#e05252",
        "NEUTRAL": "#888", "HOLD": "#a78bfa", "REDUCE": "#f5a623",
    }.get(signal, "#888")

    return Col(md=4, children=[
        html.Div(
            style={
                "backgroundColor": "#0d1117",
                "border": f"1px solid {accent}33",
                "borderRadius": "8px",
                "padding": "16px",
                "height": "100%",
            },
            children=[
                html.Div(
                    style={"display": "flex", "justifyContent": "space-between",
                           "marginBottom": "12px"},
                    children=[
                        html.Span(title, style={"color": accent, "fontWeight": "700",
                                                 "fontSize": "0.70rem",
                                                 "textTransform": "uppercase",
                                                 "letterSpacing": "0.1em"}),
                        html.Span(signal,
                                  style={"color": sig_color, "fontWeight": "800",
                                         "fontSize": "0.80rem",
                                         "backgroundColor": sig_color + "22",
                                         "padding": "2px 8px",
                                         "borderRadius": "3px"}),
                    ],
                ),
                *[
                    html.Div(
                        style={"display": "flex", "justifyContent": "space-between",
                               "padding": "5px 0", "borderBottom": "1px solid #1e2230"},
                        children=[
                            html.Span(label, style={"color": "#666", "fontSize": "0.72rem"}),
                            html.Span(str(val or "—"),
                                      style={"color": cfg.text_color, "fontSize": "0.76rem",
                                             "maxWidth": "65%", "textAlign": "right",
                                             "wordBreak": "break-word"}),
                        ],
                    )
                    for label, val in rows
                ],
            ],
        ),
    ])
