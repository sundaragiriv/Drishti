"""ISR Expansion Blocks — new sections for the Individual Stock Report.

Blocks:
  1. Interconnected Stocks — peer relationships + support verdict
  2. Market Drivers Summary — pressure + catalysts + why-now
  3. Evidence Quality — data freshness + confidence
  4. Buy-Only Intelligence Summary — company context for top names
  5. Mean Reversion — stretch/compression state
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import dash_bootstrap_components as dbc
from dash import html
from loguru import logger

from signal_scanner.config import DashboardConfig

cfg = DashboardConfig()

_BLOCK_STYLE = {
    "backgroundColor": "#0d1117",
    "border": f"1px solid {cfg.border_color}",
    "borderRadius": "8px",
    "padding": "16px",
    "marginBottom": "12px",
}
_LABEL = {"fontSize": "0.65rem", "color": "#888", "textTransform": "uppercase",
          "letterSpacing": "0.08em", "marginBottom": "4px"}
_VALUE = {"fontSize": "0.90rem", "fontWeight": "700", "color": cfg.text_color}


def build_interconnected_block(ticker: str) -> html.Div:
    """Interconnected Stocks block for ISR."""
    try:
        from signal_scanner.institutional_intel.config import safe_duckdb_connect
        conn = safe_duckdb_connect(read_only=True)
        if not conn:
            return html.Div()

        try:
            # Get interconnected features
            row = conn.execute("""
                SELECT peer_count, peers_in_accum, peers_with_insider,
                       peer_avg_conviction, sector_breadth_20d,
                       peer_avg_ret_5d, peer_momentum_spread
                FROM fact_interconnected_features
                WHERE ticker = ?
                ORDER BY trade_date DESC LIMIT 1
            """, [ticker]).fetchone()

            # Get related companies
            related = conn.execute("""
                SELECT related_ticker FROM dim_related_companies
                WHERE ticker = ? LIMIT 10
            """, [ticker]).fetchall()
        finally:
            conn.close()

        if not row:
            return html.Div(style=_BLOCK_STYLE, children=[
                html.H6("Interconnected Stocks", style={"color": cfg.accent_primary, "marginBottom": "8px"}),
                html.P("No peer data available for this ticker", style={"color": cfg.text_muted}),
            ])

        pc, pa, pi, pac, sb, pr5, pms = row
        peers = [r[0] for r in (related or [])]

        # Support verdict
        if (pa or 0) >= 3 and (sb or 0) > 0.5:
            verdict = "Supported"
            v_color = "#00c896"
        elif (pa or 0) >= 1:
            verdict = "Partial Support"
            v_color = "#ffd43b"
        elif (pms or 0) > 0.02:
            verdict = "Leading (peers lagging)"
            v_color = "#4dc9ff"
        elif (pms or 0) < -0.02:
            verdict = "Lagging Follower"
            v_color = "#e05252"
        else:
            verdict = "Isolated"
            v_color = "#888"

        return html.Div(style=_BLOCK_STYLE, children=[
            html.H6("Interconnected Stocks", style={"color": cfg.accent_primary, "marginBottom": "8px"}),
            dbc.Row([
                dbc.Col([
                    html.Div("VERDICT", style=_LABEL),
                    html.Div(verdict, style={**_VALUE, "color": v_color}),
                ], md=2),
                dbc.Col([
                    html.Div("PEERS", style=_LABEL),
                    html.Div(str(pc or 0), style=_VALUE),
                ], md=1),
                dbc.Col([
                    html.Div("IN ACCUM", style=_LABEL),
                    html.Div(str(pa or 0), style=_VALUE),
                ], md=1),
                dbc.Col([
                    html.Div("BREADTH", style=_LABEL),
                    html.Div(f"{(sb or 0) * 100:.0f}%", style=_VALUE),
                ], md=2),
                dbc.Col([
                    html.Div("PEER AVG CONV", style=_LABEL),
                    html.Div(f"{pac or 0:.0f}", style=_VALUE),
                ], md=2),
                dbc.Col([
                    html.Div("RELATED", style=_LABEL),
                    html.Div(", ".join(peers[:5]) if peers else "—",
                             style={"fontSize": "0.78rem", "color": "#4da3ff"}),
                ], md=4),
            ]),
        ])
    except Exception as e:
        logger.debug("ISR interconnected block error: {}", e)
        return html.Div()


def build_drivers_block(ticker: str) -> html.Div:
    """Market Drivers summary block for ISR."""
    try:
        from signal_scanner.institutional_intel.config import safe_duckdb_connect
        conn = safe_duckdb_connect(read_only=True)
        if not conn:
            return html.Div()

        try:
            row = conn.execute("""
                SELECT squeeze_score, short_squeeze_score, dark_pool_pct_avg,
                       short_volume_ratio_avg, institutional_pressure
                FROM intelligence_scores
                WHERE ticker = ? AND report_quarter = (
                    SELECT MAX(report_quarter) FROM intelligence_scores WHERE data_quality_score >= 75
                )
            """, [ticker]).fetchone()

            # Recent insider buys
            insiders = conn.execute("""
                SELECT COUNT(*) FROM fact_form4_transactions
                WHERE ticker = ? AND transaction_code = 'P'
                  AND transaction_date >= CURRENT_DATE - INTERVAL '30' DAY
            """, [ticker]).fetchone()
        finally:
            conn.close()

        if not row:
            return html.Div()

        sq, ssq, dp, svr, ip = row
        insider_count = insiders[0] if insiders else 0

        # Driver summary
        drivers = []
        if (sq or 0) >= 60:
            drivers.append(f"Short squeeze pressure ({sq:.0f})")
        if (ip or 0) >= 60:
            drivers.append(f"Institutional pressure ({ip:.0f})")
        if insider_count > 0:
            drivers.append(f"Insider buying ({insider_count} txns in 30d)")
        if (dp or 0) >= 45:
            drivers.append(f"Dark pool activity ({dp:.0f}%)")

        return html.Div(style=_BLOCK_STYLE, children=[
            html.H6("Market Drivers", style={"color": "#ff8c00", "marginBottom": "8px"}),
            dbc.Row([
                dbc.Col([
                    html.Div("SQUEEZE", style=_LABEL),
                    html.Div(f"{sq or 0:.0f}", style=_VALUE),
                ], md=2),
                dbc.Col([
                    html.Div("DARK POOL", style=_LABEL),
                    html.Div(f"{dp or 0:.0f}%", style=_VALUE),
                ], md=2),
                dbc.Col([
                    html.Div("INST PRESSURE", style=_LABEL),
                    html.Div(f"{ip or 0:.0f}", style=_VALUE),
                ], md=2),
                dbc.Col([
                    html.Div("INSIDERS (30D)", style=_LABEL),
                    html.Div(str(insider_count), style=_VALUE),
                ], md=2),
                dbc.Col([
                    html.Div("STRONGEST DRIVER", style=_LABEL),
                    html.Div(drivers[0] if drivers else "No strong driver",
                             style={"fontSize": "0.80rem", "color": "#ff8c00" if drivers else "#888"}),
                ], md=4),
            ]),
        ])
    except Exception as e:
        logger.debug("ISR drivers block error: {}", e)
        return html.Div()


def build_evidence_block(ticker: str) -> html.Div:
    """Evidence Quality block for ISR."""
    try:
        from signal_scanner.institutional_intel.config import safe_duckdb_connect
        conn = safe_duckdb_connect(read_only=True)
        if not conn:
            return html.Div()

        try:
            row = conn.execute("""
                SELECT data_quality_score, report_quarter, computed_at
                FROM intelligence_scores
                WHERE ticker = ? AND report_quarter = (
                    SELECT MAX(report_quarter) FROM intelligence_scores WHERE data_quality_score >= 50
                )
            """, [ticker]).fetchone()

            # Check option data exists
            opt = conn.execute("""
                SELECT COUNT(*), MAX(snapshot_date) FROM fact_options_contracts
                WHERE underlying = ?
            """, [ticker]).fetchone()
        finally:
            conn.close()

        if not row:
            return html.Div()

        dq, quarter, computed = row
        opt_count = opt[0] if opt else 0
        opt_date = str(opt[1]) if opt and opt[1] else "None"

        quality_color = "#00c896" if (dq or 0) >= 80 else "#ffd43b" if (dq or 0) >= 60 else "#e05252"

        return html.Div(style=_BLOCK_STYLE, children=[
            html.H6("Evidence Quality", style={"color": cfg.text_muted, "marginBottom": "8px"}),
            dbc.Row([
                dbc.Col([
                    html.Div("DATA QUALITY", style=_LABEL),
                    html.Div(f"{dq or 0:.0f}", style={**_VALUE, "color": quality_color}),
                ], md=2),
                dbc.Col([
                    html.Div("QUARTER", style=_LABEL),
                    html.Div(str(quarter), style=_VALUE),
                ], md=2),
                dbc.Col([
                    html.Div("COMPUTED", style=_LABEL),
                    html.Div(str(computed)[:16] if computed else "—",
                             style={"fontSize": "0.75rem", "color": cfg.text_muted}),
                ], md=3),
                dbc.Col([
                    html.Div("OPTIONS DATA", style=_LABEL),
                    html.Div(f"{opt_count} contracts (as of {opt_date})" if opt_count > 0 else "No options data",
                             style={"fontSize": "0.75rem", "color": "#a78bfa" if opt_count > 0 else "#888"}),
                ], md=5),
            ]),
        ])
    except Exception as e:
        return html.Div()


def build_mean_reversion_block(ticker: str) -> html.Div:
    """Mean Reversion block for ISR."""
    try:
        from signal_scanner.institutional_intel.config import safe_duckdb_connect
        conn = safe_duckdb_connect(read_only=True)
        if not conn:
            return html.Div()

        try:
            row = conn.execute("""
                SELECT rsi_14, price_vs_sma200_pct, price_vs_sma50_pct,
                       ROUND((close - sma_20) / NULLIF(sma_20, 0) * 100, 1)
                FROM fact_swing_features
                WHERE ticker = ?
                ORDER BY trade_date DESC LIMIT 1
            """, [ticker]).fetchone()
        finally:
            conn.close()

        if not row:
            return html.Div()

        rsi, vs200, vs50, vs20 = row
        rsi = rsi or 50
        vs200 = vs200 or 0
        vs50 = vs50 or 0
        vs20 = vs20 or 0

        if rsi < 30 and vs200 > 0:
            verdict = "Oversold in Uptrend"
            v_color = "#00c896"
        elif rsi > 70 and vs200 > 10:
            verdict = "Extended"
            v_color = "#e05252"
        elif rsi > 70 and vs200 < 0:
            verdict = "Overbought in Weak Trend"
            v_color = "#ff8c00"
        elif rsi < 30 and vs200 < -10:
            verdict = "Oversold in Downtrend"
            v_color = "#888"
        else:
            verdict = "Neutral"
            v_color = cfg.text_muted

        return html.Div(style=_BLOCK_STYLE, children=[
            html.H6("Mean Reversion", style={"color": "#4dc9ff", "marginBottom": "8px"}),
            dbc.Row([
                dbc.Col([
                    html.Div("VERDICT", style=_LABEL),
                    html.Div(verdict, style={**_VALUE, "color": v_color}),
                ], md=3),
                dbc.Col([
                    html.Div("RSI", style=_LABEL),
                    html.Div(f"{rsi:.0f}", style=_VALUE),
                ], md=1),
                dbc.Col([
                    html.Div("VS 20SMA", style=_LABEL),
                    html.Div(f"{vs20:+.1f}%", style=_VALUE),
                ], md=2),
                dbc.Col([
                    html.Div("VS 50SMA", style=_LABEL),
                    html.Div(f"{vs50:+.1f}%", style=_VALUE),
                ], md=2),
                dbc.Col([
                    html.Div("VS 200SMA", style=_LABEL),
                    html.Div(f"{vs200:+.1f}%", style=_VALUE),
                ], md=2),
            ]),
        ])
    except Exception as e:
        return html.Div()


def build_buy_summary_block(ticker: str, intel: dict) -> html.Div:
    """Buy-Only Intelligence Summary — only for top-tier names."""
    conv = float(intel.get("conviction_score") or 0)
    signal = str(intel.get("swing_signal") or "")

    # Only render for BUY names with decent conviction
    if signal not in ("BUY",) or conv < 60:
        return html.Div()

    phase = str(intel.get("accum_phase") or "DORMANT")
    sector = ""
    company = ""
    try:
        from signal_scanner.institutional_intel.config import safe_duckdb_connect
        conn = safe_duckdb_connect(read_only=True)
        if conn:
            try:
                r = conn.execute(
                    "SELECT issuer_name, sector FROM dim_issuer WHERE ticker = ? LIMIT 1",
                    [ticker],
                ).fetchone()
                if r:
                    company = r[0] or ""
                    sector = r[1] or ""
            finally:
                conn.close()
    except Exception:
        pass

    # Build plain-language summary
    what_it_is = f"{company}" if company else ticker
    what_drives = f"{sector}" if sector else "Unknown sector"

    why_buy = []
    if phase in ("ACTIVE_ACCUM", "LATE_ACCUM"):
        why_buy.append(f"Institutional accumulation ({phase.replace('_', ' ').title()})")
    if conv >= 70:
        why_buy.append(f"High conviction ({conv:.0f})")
    if intel.get("triple_lock"):
        why_buy.append("Triple Lock convergence")
    if intel.get("insider_cluster_detected"):
        why_buy.append("Insider buying cluster")

    return html.Div(style={**_BLOCK_STYLE, "borderLeft": "3px solid #00c896"}, children=[
        html.H6("Why This Is A Buy", style={"color": "#00c896", "marginBottom": "8px"}),
        html.P(f"{what_it_is} — {what_drives}",
               style={"fontSize": "0.85rem", "color": cfg.text_color, "marginBottom": "6px"}),
        html.Ul([html.Li(r, style={"fontSize": "0.80rem", "color": "#ddd"}) for r in why_buy],
                style={"paddingLeft": "16px", "margin": "0"}),
    ])
