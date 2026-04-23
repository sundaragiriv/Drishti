"""Intelligence Dashboard Callbacks.

Handles all interactivity for:
  - Intelligence Command Center (Screen A)
  - Accumulation Radar (Screen B)
  - Sector Rotation Clock (Screen C)
  - Stock Deep Dive (Screen D)
  - Intelligence stats bar
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import dash_bootstrap_components as dbc
import duckdb
import plotly.graph_objects as go
from dash import Input, Output, State, callback_context, html, no_update
from dash.exceptions import PreventUpdate
from loguru import logger

from signal_scanner.config import DashboardConfig
from signal_scanner.dashboard.layouts.intelligence_view import (
    PHASE_COLORS, SIGNAL_COLORS, _build_accumulation_radar,
    _build_command_center, _build_sector_clock, _build_deep_dive,
    _empty_radar_figure, _empty_sector_figure,
)
from signal_scanner.institutional_intel.config import WAREHOUSE_PATH

cfg = DashboardConfig()


def _get_conn():
    """Get a read-only DuckDB connection (returns None if locked)."""
    from signal_scanner.institutional_intel.config import safe_duckdb_connect
    return safe_duckdb_connect(read_only=True)


def _latest_quarter(conn: duckdb.DuckDBPyConnection) -> Optional[str]:
    from signal_scanner.institutional_intel.config import get_active_quarter
    q = get_active_quarter(conn)
    if q:
        return q
    row = conn.execute(
        "SELECT MAX(report_quarter) FROM intelligence_scores"
    ).fetchone()
    return row[0] if row else None


def _best_clean_quarter(conn: duckdb.DuckDBPyConnection) -> Optional[str]:
    """Return the canonical active quarter via get_active_quarter.

    Falls back to _latest_quarter() if nothing matches.
    """
    from signal_scanner.institutional_intel.config import get_active_quarter
    q = get_active_quarter(conn)
    return q if q else _latest_quarter(conn)


def _get_clean_quarter_options(conn: duckdb.DuckDBPyConnection) -> List[Dict]:
    """Return dropdown options for all non-contaminated quarters (quality > 0).

    Includes sparse quarters (quality=50) with an 'early data' label so users
    can select the most recent quarter even before all filers have reported.
    Excludes only contaminated quarters (quality=0) which have data integrity issues.
    """
    try:
        rows = conn.execute("""
            SELECT report_quarter, COUNT(*) as tickers, MAX(data_quality_score) as quality
            FROM intelligence_scores
            WHERE data_quality_score > 0
            GROUP BY report_quarter
            ORDER BY report_quarter DESC
        """).fetchall()
        options = []
        for q, cnt, quality in rows:
            if quality >= 75:
                label = f"{q}  ({cnt:,} tickers)"
            else:
                label = f"{q}  ({cnt:,} tickers — early data)"
            options.append({"label": label, "value": q})
        return options
    except Exception:
        rows = conn.execute("""
            SELECT report_quarter, COUNT(*) as tickers
            FROM intelligence_scores
            GROUP BY report_quarter
            ORDER BY report_quarter DESC
        """).fetchall()
        return [{"label": f"{q}  ({cnt:,})", "value": q} for q, cnt in rows]


def register_intelligence_callbacks(app) -> None:
    """Register all intelligence dashboard callbacks."""

    # ------------------------------------------------------------------
    # Sub-tab content switching
    # ------------------------------------------------------------------
    @app.callback(
        Output("intel-tab-content", "children"),
        Input("intel-sub-tabs", "active_tab"),
    )
    def render_intel_tab(active_tab: str):
        if active_tab == "tab-command":
            return _build_command_center()
        elif active_tab == "tab-radar":
            return _build_accumulation_radar()
        elif active_tab == "tab-sector":
            return _build_sector_clock()
        elif active_tab == "tab-deepdive":
            return _build_deep_dive()
        return html.Div("Select a tab above.")

    # ------------------------------------------------------------------
    # Quarter selector — populate options on section load
    # ------------------------------------------------------------------
    @app.callback(
        Output("intel-quarter-selector", "options"),
        Output("intel-quarter-selector", "value"),
        Input("intelligence-section", "hidden"),
    )
    def populate_intel_quarter_selector(hidden: bool):
        if hidden:
            raise PreventUpdate
        try:
            conn = _get_conn()
            if conn is None:
                return [], None
            try:
                options = _get_clean_quarter_options(conn)
                default = _best_clean_quarter(conn)
                return options, default
            finally:
                conn.close()
        except Exception as e:
            logger.error("Quarter selector error: {}", e)
            return [], None

    # ------------------------------------------------------------------
    # Stats bar — populate on section load / quarter change
    # ------------------------------------------------------------------
    @app.callback(
        Output("intel-stat-active-accum",    "children"),
        Output("intel-stat-early-accum",     "children"),
        Output("intel-stat-high-conviction", "children"),
        Output("intel-stat-cluster-insider", "children"),
        Output("intel-stat-distribution",    "children"),
        Output("intel-stat-triple-lock",     "children"),
        Output("intel-stat-quarter",         "children"),
        Input("intelligence-section", "hidden"),
        Input("intel-quarter-selector", "value"),
    )
    def update_intel_stats(hidden: bool, selected_quarter: Optional[str]):
        if hidden:
            raise PreventUpdate
        try:
            conn = _get_conn()
            if conn is None:
                return "—", "—", "—", "—", "—", "—", "—"
            try:
                quarter = selected_quarter or _best_clean_quarter(conn)
                if not quarter:
                    return "0", "0", "0", "0", "0", "0", "—"

                row = conn.execute("""
                    SELECT
                        SUM(CASE WHEN accum_phase = 'ACTIVE_ACCUM' THEN 1 ELSE 0 END),
                        SUM(CASE WHEN accum_phase = 'EARLY_ACCUM'  THEN 1 ELSE 0 END),
                        SUM(CASE WHEN conviction_score >= 70        THEN 1 ELSE 0 END),
                        SUM(CASE WHEN insider_cluster_detected = TRUE THEN 1 ELSE 0 END),
                        SUM(CASE WHEN distribution_warning = TRUE  THEN 1 ELSE 0 END),
                        SUM(CASE WHEN triple_lock = TRUE           THEN 1 ELSE 0 END)
                    FROM intelligence_scores WHERE report_quarter = ?
                """, [quarter]).fetchone()

                if not row:
                    return "0", "0", "0", "0", "0", "0", quarter

                return (
                    str(int(row[0] or 0)), str(int(row[1] or 0)),
                    str(int(row[2] or 0)), str(int(row[3] or 0)),
                    str(int(row[4] or 0)), str(int(row[5] or 0)),
                    quarter,
                )
            finally:
                conn.close()
        except Exception as e:
            logger.error("Intel stats error: {}", e)
            return "—", "—", "—", "—", "—", "—", "—"

    # ------------------------------------------------------------------
    # Command Center — top conviction setups table
    # ------------------------------------------------------------------
    @app.callback(
        Output("intel-command-table",      "data"),
        Output("intel-distribution-table", "data"),
        Input("intel-sub-tabs", "active_tab"),
        Input("intelligence-section", "hidden"),
        Input("intel-quarter-selector", "value"),
    )
    def update_command_center(active_tab: str, hidden: bool, selected_quarter: Optional[str]):
        if hidden or active_tab != "tab-command":
            raise PreventUpdate
        try:
            conn = _get_conn()
            if conn is None:
                return [], []
            try:
                quarter = selected_quarter or _best_clean_quarter(conn)
                if not quarter:
                    return [], []

                # Top conviction setups
                # Use DISTINCT ON or ROW_NUMBER to deduplicate dim_issuer joins.
                top_df = conn.execute("""
                    WITH di_dedup AS (
                        SELECT ticker,
                               FIRST(issuer_name) AS issuer_name,
                               FIRST(sector) AS sector
                        FROM dim_issuer
                        WHERE ticker IS NOT NULL AND ticker != ''
                        GROUP BY ticker
                    )
                    SELECT
                        CASE WHEN COALESCE(i.triple_lock, FALSE) THEN '🔒' ELSE '' END AS triple_lock,
                        i.ticker,
                        COALESCE(di.issuer_name, i.ticker) AS company,
                        COALESCE(di.sector, '') AS sector,
                        i.accum_phase,
                        ROUND(i.conviction_score, 1)::DECIMAL(5,1) AS conviction_score,
                        ROUND(COALESCE(i.ml_score_v2, 0), 1)::DECIMAL(5,1) AS ml_score_v2,
                        i.expected_impact_quarters,
                        i.accum_phase_quarters,
                        i.tier1_manager_count,
                        CAST(COALESCE(i.inst_f4_distinct_60d, 0) AS INTEGER) AS inst_f4_distinct_60d,
                        ROUND(COALESCE(i.price_momentum_90d, 0), 1)::DECIMAL(5,1) AS price_momentum_90d,
                        i.cascade_stage,
                        CASE WHEN i.insider_cluster_detected THEN 'Yes ✓' ELSE 'No' END AS insider_cluster_detected,
                        i.day_bias,
                        i.swing_signal,
                        i.longterm_signal
                    FROM intelligence_scores i
                    LEFT JOIN di_dedup di ON i.ticker = di.ticker
                    WHERE i.report_quarter = ?
                      AND i.accum_phase IN ('EARLY_ACCUM', 'ACTIVE_ACCUM', 'LATE_ACCUM')
                    ORDER BY
                        COALESCE(i.triple_lock, FALSE) DESC,
                        i.conviction_score DESC
                    LIMIT 100
                """, [quarter]).fetchdf()

                # Distribution warnings
                dist_df = conn.execute("""
                    WITH di_dedup AS (
                        SELECT ticker,
                               FIRST(issuer_name) AS issuer_name,
                               FIRST(sector) AS sector
                        FROM dim_issuer
                        WHERE ticker IS NOT NULL AND ticker != ''
                        GROUP BY ticker
                    )
                    SELECT
                        i.ticker,
                        COALESCE(di.issuer_name, i.ticker) AS company,
                        COALESCE(di.sector, '') AS sector,
                        i.distribution_severity,
                        ROUND(i.conviction_score, 1) AS conviction_score,
                        i.accum_phase,
                        i.swing_signal,
                        i.longterm_signal
                    FROM intelligence_scores i
                    LEFT JOIN di_dedup di ON i.ticker = di.ticker
                    WHERE i.report_quarter = ?
                      AND i.distribution_warning = TRUE
                    ORDER BY
                        CASE i.distribution_severity
                            WHEN 'SEVERE'   THEN 1
                            WHEN 'MODERATE' THEN 2
                            ELSE 3
                        END,
                        i.conviction_score DESC
                    LIMIT 50
                """, [quarter]).fetchdf()

                return (
                    top_df.to_dict("records") if not top_df.empty else [],
                    dist_df.to_dict("records") if not dist_df.empty else [],
                )
            finally:
                conn.close()
        except Exception as e:
            logger.error("Command center error: {}", e)
            return [], []

    # ------------------------------------------------------------------
    # Accumulation Radar — scatter plot
    # ------------------------------------------------------------------
    @app.callback(
        Output("intel-radar-chart", "figure"),
        Input("intel-sub-tabs", "active_tab"),
        Input("intel-radar-phase-filter", "value"),
        Input("intelligence-section", "hidden"),
        Input("intel-quarter-selector", "value"),
    )
    def update_radar(active_tab: str, phase_filter: str, hidden: bool, selected_quarter: Optional[str]):
        if hidden or active_tab != "tab-radar":
            raise PreventUpdate
        try:
            conn = _get_conn()
            if conn is None:
                return _empty_radar_figure()
            try:
                quarter = selected_quarter or _best_clean_quarter(conn)
                if not quarter:
                    return _empty_radar_figure()

                where_clause = ""
                if phase_filter == "BUY_ZONE":
                    where_clause = "AND i.accum_phase IN ('EARLY_ACCUM', 'ACTIVE_ACCUM')"
                elif phase_filter == "DIST":
                    where_clause = "AND i.distribution_warning = TRUE"

                df = conn.execute(f"""
                    SELECT
                        i.ticker,
                        COALESCE(di.issuer_name, i.ticker) AS company,
                        COALESCE(di.sector, 'Unknown') AS sector,
                        i.accum_phase,
                        COALESCE(i.accum_strength_score, 0) AS accum_strength_score,
                        COALESCE(q.avg_price_change_pct, 0) AS avg_price_change_pct,
                        COALESCE(i.conviction_score, 0) AS conviction_score,
                        i.swing_signal,
                        i.expected_impact_quarters
                    FROM intelligence_scores i
                    LEFT JOIN dim_issuer di ON i.ticker = di.ticker
                    LEFT JOIN agg_qoq_changes q
                        ON i.ticker = q.ticker AND i.report_quarter = q.current_quarter
                    WHERE i.report_quarter = ? {where_clause}
                      AND i.accum_phase IS NOT NULL
                """, [quarter]).fetchdf()
            finally:
                conn.close()

            if df.empty:
                return _empty_radar_figure()

            fig = _empty_radar_figure()

            # Group by phase for legend
            for phase in df["accum_phase"].unique():
                phase_df = df[df["accum_phase"] == phase]
                color = PHASE_COLORS.get(str(phase), cfg.text_muted)
                fig.add_trace(go.Scatter(
                    x=phase_df["accum_strength_score"],
                    y=phase_df["avg_price_change_pct"],
                    mode="markers",
                    name=str(phase).replace("_", " "),
                    marker=dict(
                        color=color,
                        size=[max(6, min(20, float(s) / 8)) for s in phase_df["conviction_score"]],
                        opacity=0.8,
                        line=dict(color=cfg.bg_color, width=1),
                    ),
                    text=phase_df["ticker"],
                    customdata=list(zip(
                        phase_df["company"],
                        phase_df["conviction_score"],
                        phase_df["swing_signal"].fillna("N/A"),
                        phase_df["expected_impact_quarters"].fillna(3),
                    )),
                    hovertemplate=(
                        "<b>%{text}</b><br>"
                        "%{customdata[0]}<br>"
                        "Accum Strength: %{x:.0f}<br>"
                        "Price Response: %{y:.1f}%<br>"
                        "Conviction: %{customdata[1]:.0f}/100<br>"
                        "Swing: %{customdata[2]}<br>"
                        "Lag: %{customdata[3]}Q<extra></extra>"
                    ),
                ))

            fig.update_layout(title=dict(
                text=f"Accumulation Radar — {quarter}",
                font={"color": cfg.accent_primary, "size": 13},
                x=0.5,
            ))
            return fig

        except Exception as e:
            logger.error("Radar chart error: {}", e)
            return _empty_radar_figure()

    # ------------------------------------------------------------------
    # Sector Clock — horizontal bar chart
    # ------------------------------------------------------------------
    @app.callback(
        Output("intel-sector-clock-chart", "figure"),
        Output("intel-cycle-phase-badge",  "children"),
        Output("intel-favored-sectors",    "children"),
        Output("intel-sector-perf-table",  "data"),
        Input("intel-sub-tabs", "active_tab"),
        Input("intelligence-section", "hidden"),
    )
    def update_sector_clock(active_tab: str, hidden: bool):
        if hidden or active_tab != "tab-sector":
            raise PreventUpdate
        try:
            from signal_scanner.institutional_intel.intelligence.sector_rotation import (
                get_sector_rotation_summary,
            )
            conn = _get_conn()
            if conn is None:
                return _empty_sector_figure(), "DuckDB unavailable", "", []
            # summary is a dict: {sectors: [...], cycle_phase: str, flow_by_cycle: {...}, ...}
            summary = get_sector_rotation_summary(conn)
            conn.close()

            sector_list = summary.get("sectors", []) if summary else []
            if not sector_list:
                return _empty_sector_figure(), "No sector data available", "", []

            sectors   = [r["sector"]                        for r in sector_list]
            flow_pcts = [float(r.get("flow_pct") or 0)      for r in sector_list]
            colors    = ["#00ff88" if f > 0 else "#ff4488"  for f in flow_pcts]
            streaks   = [int(r.get("inflow_streak") or 0)   for r in sector_list]

            fig = go.Figure(go.Bar(
                x=flow_pcts,
                y=sectors,
                orientation="h",
                marker=dict(color=colors, opacity=0.85),
                text=[f"{f:+.1f}% ({s}Q streak)" if s > 0 else f"{f:+.1f}%"
                      for f, s in zip(flow_pcts, streaks)],
                textposition="outside",
                textfont=dict(color=cfg.text_color, size=10),
                hovertemplate="<b>%{y}</b><br>Net Flow: %{x:+.1f}%<extra></extra>",
            ))
            fig.update_layout(
                paper_bgcolor=cfg.card_color,
                plot_bgcolor=cfg.bg_color,
                font={"color": cfg.text_color, "size": 11},
                margin={"l": 130, "r": 80, "t": 30, "b": 40},
                xaxis=dict(title="Net Institutional Flow % QoQ",
                           gridcolor=cfg.border_color, zeroline=True,
                           zerolinecolor=cfg.accent_primary),
                yaxis=dict(gridcolor=cfg.border_color),
                showlegend=False,
            )

            # cycle_phase comes directly from the summary dict (already computed)
            cycle_phase = str(summary.get("cycle_phase", "unknown")).replace("_", " ").title()
            inflow_sectors  = [s["sector"] for s in summary.get("top_inflow_sectors",  [])]
            outflow_sectors = [s["sector"] for s in summary.get("top_outflow_sectors", [])]

            badge = html.Div([
                html.Span("Cycle Phase: ", style={"color": cfg.text_muted, "fontSize": "0.8rem"}),
                html.Span(
                    cycle_phase,
                    style={"color": cfg.accent_primary, "fontWeight": "700", "fontSize": "0.85rem"},
                ),
            ])

            favored_div = html.Div([
                html.Span("Top Inflow: ", style={"color": cfg.text_muted, "fontSize": "0.8rem"}),
                html.Span(
                    ", ".join(inflow_sectors) if inflow_sectors else "—",
                    style={"color": "#00ff88", "fontWeight": "600", "fontSize": "0.82rem"},
                ),
                html.Span("  |  Top Outflow: ",
                          style={"color": cfg.text_muted, "fontSize": "0.8rem", "marginLeft": "12px"}),
                html.Span(
                    ", ".join(outflow_sectors) if outflow_sectors else "—",
                    style={"color": "#ff4488", "fontWeight": "600", "fontSize": "0.82rem"},
                ),
            ])

            # Build table data from sector_list
            table_data = []
            for s in sector_list:
                flow = float(s.get("flow_pct") or 0)
                net_k = float(s.get("net_flow_k") or 0)
                signal = "INFLOW" if flow > 0.5 else ("OUTFLOW" if flow < -0.5 else "FLAT")
                table_data.append({
                    "sector": s["sector"],
                    "flow_pct": round(flow, 1),
                    "net_flow_m": round(net_k / 1000, 0),  # K → M
                    "ticker_count": int(s.get("ticker_count") or 0),
                    "inflow_streak": int(s.get("inflow_streak") or 0),
                    "cycle_phase": str(s.get("cycle_phase", "")).replace("_", " ").title(),
                    "signal": signal,
                })

            return fig, badge, favored_div, table_data

        except Exception as e:
            logger.error("Sector clock error: {}", e)
            return _empty_sector_figure(), "Error loading sector data", "", []

    # ------------------------------------------------------------------
    # Sector Detail — click-to-expand top 20 symbols
    # ------------------------------------------------------------------
    @app.callback(
        Output("intel-sector-detail-panel", "hidden"),
        Output("intel-sector-detail-title", "children"),
        Output("intel-sector-detail-table", "data"),
        Input("intel-sector-perf-table", "active_cell"),
        State("intel-sector-perf-table", "data"),
        prevent_initial_call=True,
    )
    def show_sector_detail(active_cell, table_data):
        if not active_cell or not table_data:
            raise PreventUpdate
        row_idx = active_cell.get("row", 0)
        if row_idx >= len(table_data):
            raise PreventUpdate
        sector_name = table_data[row_idx].get("sector", "")
        if not sector_name:
            raise PreventUpdate

        try:
            conn = _get_conn()
            if conn is None:
                return True, "", []
            q = _best_clean_quarter(conn)
            rows = conn.execute("""
                SELECT s.ticker, q.current_price, s.conviction_score, s.ml_score_v2,
                       s.accum_phase, s.insider_effect_score, s.squeeze_score,
                       s.price_momentum_90d
                FROM intelligence_scores s
                JOIN agg_qoq_changes q
                    ON s.ticker = q.ticker AND s.report_quarter = q.current_quarter
                JOIN dim_issuer di ON s.ticker = di.ticker
                WHERE di.sector = ?
                  AND s.report_quarter = ?
                ORDER BY s.conviction_score DESC
                LIMIT 20
            """, [sector_name, q]).fetchall()
            conn.close()

            detail = []
            for r in rows:
                detail.append({
                    "ticker": r[0],
                    "current_price": round(float(r[1] or 0), 2),
                    "conviction_score": round(float(r[2] or 0)),
                    "ml_score_v2": round(float(r[3] or 0)),
                    "accum_phase": str(r[4] or ""),
                    "insider_effect_score": round(float(r[5] or 0)),
                    "squeeze_score": round(float(r[6] or 0)),
                    "price_momentum_90d": round(float(r[7] or 0), 1),
                })

            title = f"TOP 20 — {sector_name.upper()}"
            return False, title, detail

        except Exception as e:
            logger.error("Sector detail error: {}", e)
            return True, "", []

    # ------------------------------------------------------------------
    # Stock Deep Dive
    # ------------------------------------------------------------------
    @app.callback(
        Output("intel-deep-dive-chart", "figure"),
        Output("intel-deep-dive-badge", "children"),
        Output("intel-deep-dive-stats", "children"),
        Input("intel-deep-dive-run", "n_clicks"),
        State("intel-deep-dive-ticker", "value"),
        prevent_initial_call=True,
    )
    def update_deep_dive(n_clicks: int, ticker_input: str):
        if not ticker_input or not ticker_input.strip():
            raise PreventUpdate

        ticker = ticker_input.upper().strip()
        try:
            conn = _get_conn()
            if conn is None:
                empty_fig = go.Figure(layout={
                    "paper_bgcolor": cfg.card_color,
                    "plot_bgcolor": cfg.bg_color,
                    "annotations": [{"text": "DuckDB temporarily unavailable",
                                     "x": 0.5, "y": 0.5, "xref": "paper", "yref": "paper",
                                     "showarrow": False, "font": {"color": cfg.text_muted}}]
                })
                return empty_fig, html.Span("DuckDB locked — try again shortly", style={"color": "#ff4488"}), ""

            try:
                # Get accumulation history (last 8 quarters)
                hist_df = conn.execute("""
                    SELECT
                        q.current_quarter AS quarter,
                        q.inst_count_current,
                        q.inst_count_change,
                        COALESCE(q.avg_price_change_pct, 0) AS price_pct,
                        i.accum_phase,
                        ROUND(COALESCE(i.conviction_score, 0), 1) AS conviction_score
                    FROM agg_qoq_changes q
                    LEFT JOIN intelligence_scores i
                        ON q.ticker = i.ticker AND q.current_quarter = i.report_quarter
                    WHERE q.ticker = ?
                    ORDER BY q.current_quarter DESC LIMIT 8
                """, [ticker]).fetchdf()

                # Insider overlay
                insider_df = conn.execute("""
                    SELECT transaction_date, direction, shares, insider_role
                    FROM fact_form4_transactions
                    WHERE ticker = ?
                      AND transaction_date >= CURRENT_DATE - INTERVAL 730 DAY
                    ORDER BY transaction_date
                """, [ticker]).fetchdf()

                # Latest intelligence
                intel_row = conn.execute("""
                    SELECT
                        accum_phase, conviction_score, expected_impact_quarters,
                        lag_confidence, swing_signal, longterm_signal,
                        distribution_warning, day_bias, accum_phase_quarters
                    FROM intelligence_scores
                    WHERE ticker = ?
                    ORDER BY report_quarter DESC LIMIT 1
                """, [ticker]).fetchone()
            finally:
                conn.close()

            if hist_df.empty:
                empty_fig = go.Figure(layout={
                    "paper_bgcolor": cfg.card_color,
                    "plot_bgcolor": cfg.bg_color,
                    "annotations": [{"text": f"No data found for {ticker}",
                                     "x": 0.5, "y": 0.5, "xref": "paper", "yref": "paper",
                                     "showarrow": False, "font": {"color": cfg.text_muted}}]
                })
                return empty_fig, html.Span(f"No data for {ticker}", style={"color": "#ff4488"}), ""

            hist_df = hist_df.iloc[::-1].reset_index(drop=True)  # chronological order
            quarters = hist_df["quarter"].tolist()

            fig = go.Figure()

            # Inst count bars
            bar_colors = [PHASE_COLORS.get(str(p), cfg.text_muted) for p in hist_df["accum_phase"]]
            fig.add_trace(go.Bar(
                x=quarters,
                y=hist_df["inst_count_current"],
                name="Inst. Holders",
                marker_color=bar_colors,
                opacity=0.75,
                yaxis="y1",
                hovertemplate="<b>%{x}</b><br>Holders: %{y}<extra></extra>",
            ))

            # Price change overlay
            fig.add_trace(go.Scatter(
                x=quarters,
                y=hist_df["price_pct"],
                name="Price Change %",
                mode="lines+markers",
                line=dict(color="#ffd43b", width=2),
                marker=dict(size=6),
                yaxis="y2",
                hovertemplate="<b>%{x}</b><br>Price Chg: %{y:.1f}%<extra></extra>",
            ))

            # Conviction score overlay
            fig.add_trace(go.Scatter(
                x=quarters,
                y=hist_df["conviction_score"],
                name="Conviction Score",
                mode="lines+markers",
                line=dict(color=cfg.accent_primary, width=1.5, dash="dot"),
                marker=dict(size=5),
                yaxis="y3",
                hovertemplate="<b>%{x}</b><br>Conviction: %{y:.0f}/100<extra></extra>",
            ))

            fig.update_layout(
                paper_bgcolor=cfg.card_color,
                plot_bgcolor=cfg.bg_color,
                font={"color": cfg.text_color, "size": 11},
                margin={"l": 50, "r": 80, "t": 50, "b": 50},
                title=dict(text=f"{ticker} — Accumulation Timeline",
                           font={"color": cfg.accent_primary, "size": 13}, x=0.5),
                legend=dict(bgcolor=cfg.card_color, bordercolor=cfg.border_color,
                            font={"color": cfg.text_color, "size": 10}),
                xaxis=dict(gridcolor=cfg.border_color),
                yaxis=dict(title="Inst. Holders", gridcolor=cfg.border_color),
                yaxis2=dict(title="Price Chg %", overlaying="y", side="right",
                            showgrid=False, zeroline=True, zerolinecolor=cfg.border_color),
                yaxis3=dict(title="Conviction", overlaying="y", side="right",
                            position=0.97, showgrid=False, range=[0, 105]),
            )

            # Phase badge
            if intel_row:
                phase = str(intel_row[0] or "UNKNOWN")
                conviction = float(intel_row[1] or 0)
                lag = int(intel_row[2] or 2)
                lag_conf = str(intel_row[3] or "LOW")
                swing = str(intel_row[4] or "—")
                lt = str(intel_row[5] or "—")
                dist = bool(intel_row[6])
                day_bias = str(intel_row[7] or "NEUTRAL")
                streak = int(intel_row[8] or 0)
                phase_color = PHASE_COLORS.get(phase, cfg.text_muted)

                badge = html.Div([
                    html.Span(
                        phase.replace("_", " "),
                        style={"backgroundColor": phase_color + "22",
                               "color": phase_color, "padding": "3px 10px",
                               "borderRadius": "12px", "fontWeight": "700",
                               "fontSize": "0.8rem", "border": f"1px solid {phase_color}"},
                    ),
                    html.Span(f"  Conviction: {conviction:.0f}/100",
                              style={"color": cfg.accent_primary, "marginLeft": "10px",
                                     "fontWeight": "600", "fontSize": "0.85rem"}),
                    html.Span(f"  Lag: {lag}Q ({lag_conf})",
                              style={"color": cfg.text_muted, "marginLeft": "8px", "fontSize": "0.8rem"}),
                ], style={"display": "flex", "alignItems": "center"})

                stats = dbc.Row([
                    dbc.Col([
                        html.Div([
                            html.P("Streak", style={"color": cfg.text_muted, "fontSize": "0.7rem", "margin": 0}),
                            html.P(f"{streak}Q", style={"color": cfg.accent_primary, "fontWeight": "700", "fontSize": "1.1rem", "margin": 0}),
                        ], className="kb-stat-card", style={"padding": "10px", "textAlign": "center"}),
                    ], md=2),
                    dbc.Col([
                        html.Div([
                            html.P("Day Bias", style={"color": cfg.text_muted, "fontSize": "0.7rem", "margin": 0}),
                            html.P(day_bias, style={"color": SIGNAL_COLORS.get(day_bias, cfg.text_color), "fontWeight": "700", "fontSize": "0.9rem", "margin": 0}),
                        ], className="kb-stat-card", style={"padding": "10px", "textAlign": "center"}),
                    ], md=2),
                    dbc.Col([
                        html.Div([
                            html.P("Swing Signal", style={"color": cfg.text_muted, "fontSize": "0.7rem", "margin": 0}),
                            html.P(swing, style={"color": SIGNAL_COLORS.get(swing, cfg.text_color), "fontWeight": "700", "fontSize": "0.9rem", "margin": 0}),
                        ], className="kb-stat-card", style={"padding": "10px", "textAlign": "center"}),
                    ], md=2),
                    dbc.Col([
                        html.Div([
                            html.P("Long Term", style={"color": cfg.text_muted, "fontSize": "0.7rem", "margin": 0}),
                            html.P(lt, style={"color": SIGNAL_COLORS.get(lt, cfg.text_color), "fontWeight": "700", "fontSize": "0.9rem", "margin": 0}),
                        ], className="kb-stat-card", style={"padding": "10px", "textAlign": "center"}),
                    ], md=2),
                    dbc.Col([
                        html.Div([
                            html.P("Dist. Warning", style={"color": cfg.text_muted, "fontSize": "0.7rem", "margin": 0}),
                            html.P("⚠ YES" if dist else "Clear", style={"color": "#ff4488" if dist else "#00ff88", "fontWeight": "700", "fontSize": "0.9rem", "margin": 0}),
                        ], className="kb-stat-card", style={"padding": "10px", "textAlign": "center"}),
                    ], md=2),
                ], className="g-2")
            else:
                badge = html.Span(f"{ticker} — No intelligence data yet. Run pipeline --stage intelligence.",
                                  style={"color": cfg.text_muted})
                stats = html.Div()

            return fig, badge, stats

        except Exception as e:
            logger.error("Deep dive error for {}: {}", ticker, e)
            empty_fig = go.Figure(layout={"paper_bgcolor": cfg.card_color, "plot_bgcolor": cfg.bg_color})
            return empty_fig, html.Span(f"Error: {e}", style={"color": "#ff4488"}), ""

    # -----------------------------------------------------------------------
    # Ticker click → Individual Stock Report
    # -----------------------------------------------------------------------
    @app.callback(
        Output("selected-ticker", "data", allow_duplicate=True),
        Input("intel-command-table",      "active_cell"),
        Input("intel-distribution-table", "active_cell"),
        State("intel-command-table",      "data"),
        State("intel-distribution-table", "data"),
        prevent_initial_call=True,
    )
    def intel_table_ticker_click(cmd_cell, dist_cell, cmd_data, dist_data):
        ctx = callback_context
        if not ctx.triggered:
            return no_update
        trigger = ctx.triggered[0]["prop_id"].split(".")[0]
        if trigger == "intel-command-table" and cmd_cell and cmd_data:
            if cmd_cell.get("column_id") == "ticker":
                try:
                    return cmd_data[cmd_cell["row"]]["ticker"]
                except (IndexError, KeyError):
                    pass
        if trigger == "intel-distribution-table" and dist_cell and dist_data:
            if dist_cell.get("column_id") == "ticker":
                try:
                    return dist_data[dist_cell["row"]]["ticker"]
                except (IndexError, KeyError):
                    pass
        return no_update
