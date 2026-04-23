"""Detail view — symbol drill-down with charts, trade params, and GEX overlay.

V2: ATR-based scaled targets (T1/T2), regime context, prior day levels,
enhanced trade parameter card, visual polish.
"""

from typing import Dict, List, Optional

import dash_bootstrap_components as dbc
import plotly.graph_objects as go
from dash import dcc, html
from plotly.subplots import make_subplots

from signal_scanner.config import DashboardConfig

cfg = DashboardConfig()


def build_detail_view(
    symbol: str,
    signal_data: Optional[Dict] = None,
    history: Optional[List[Dict]] = None,
    price_df=None,
) -> html.Div:
    """Build the detail view layout for a symbol."""
    data = signal_data or {}
    score = data.get("score", 0)
    signal = data.get("signal", "NEUTRAL")
    price = data.get("price", 0)
    rec = data.get("recommendation", "HOLD")
    mtf = data.get("mtf_agreement", "")
    regime = data.get("market_regime", "")

    signal_color = {
        "LONG": "#00ff88",
        "SHORT": "#ff4488",
    }.get(signal, cfg.accent_neutral)

    rec_color = {
        "BUY": "#00ff88",
        "SELL": "#ff4488",
    }.get(rec, "#888")

    score_color = "#00ff88" if score >= 80 else (cfg.accent_neutral if score >= 60 else "#888")

    return html.Div(
        style={"backgroundColor": cfg.bg_color, "padding": "20px"},
        children=[
            # ---- HEADER ----
            dbc.Row([
                dbc.Col([
                    html.Div([
                        html.H2(
                            symbol,
                            style={
                                "color": cfg.accent_cyan,
                                "fontWeight": "700",
                                "margin": "0",
                                "display": "inline",
                            },
                        ),
                        html.Span(
                            f"  {score}/100",
                            style={
                                "color": score_color,
                                "fontSize": "22px",
                                "fontWeight": "bold",
                                "marginLeft": "16px",
                            },
                        ),
                        html.Span(
                            signal,
                            style={
                                "color": signal_color,
                                "backgroundColor": f"{signal_color}20",
                                "padding": "4px 16px",
                                "borderRadius": "4px",
                                "fontSize": "14px",
                                "fontWeight": "bold",
                                "marginLeft": "12px",
                                "border": f"1px solid {signal_color}",
                            },
                        ),
                        html.Span(
                            rec,
                            style={
                                "color": rec_color,
                                "backgroundColor": f"{rec_color}15",
                                "padding": "4px 12px",
                                "borderRadius": "4px",
                                "fontSize": "13px",
                                "fontWeight": "bold",
                                "marginLeft": "8px",
                                "border": f"1px solid {rec_color}40",
                            },
                        ),
                        html.Span(
                            f"MTF {mtf}",
                            style={
                                "color": "#aaa",
                                "fontSize": "12px",
                                "marginLeft": "12px",
                            },
                        ) if mtf else html.Span(),
                        html.Span(
                            regime.replace("_", " "),
                            style={
                                "color": {"RISK_ON": "#00ff88", "RISK_OFF": "#ff4488"}.get(regime, "#888"),
                                "fontSize": "11px",
                                "marginLeft": "12px",
                                "padding": "2px 8px",
                                "border": f"1px solid {cfg.border_color}",
                                "borderRadius": "4px",
                            },
                        ) if regime else html.Span(),
                    ]),
                ], width="auto"),
                dbc.Col([
                    dbc.Button(
                        [html.I(className="fas fa-arrow-left me-2"), "Back to Signals"],
                        id="back-to-table-btn",
                        className="kb-btn-secondary ms-2",
                        size="sm",
                    ),
                ], width="auto", className="ms-auto"),
            ], align="center", className="mb-3"),

            # ---- TRADE PARAMETERS (big cards) ----
            _build_trade_params(data, signal, signal_color),

            # ---- PRICE CHART ----
            dbc.Card(
                style={"backgroundColor": cfg.card_color, "border": f"1px solid {cfg.border_color}", "padding": "12px"},
                className="mb-3",
                children=[
                    html.H5("Price Chart", style={"color": cfg.text_color}),
                    dcc.Graph(
                        id="detail-price-chart",
                        figure=_build_price_chart(data, price_df),
                        config={"displayModeBar": True, "scrollZoom": True},
                        style={"height": "450px"},
                    ),
                ],
            ),

            # ---- CONFLUENCE BREAKDOWN ----
            dbc.Card(
                style={"backgroundColor": cfg.card_color, "border": f"1px solid {cfg.border_color}", "padding": "16px"},
                className="mb-3",
                children=[
                    html.H5("Confluence Breakdown", style={"color": cfg.text_color}),
                    _build_confluence_breakdown(data),
                ],
            ),

            # ---- KEY LEVELS ----
            dbc.Row(className="mb-3", children=[
                dbc.Col(_level_card("Zero Gamma", data.get("zero_gamma_level"), price, "#b388ff"), md=2),
                dbc.Col(_level_card("Resistance", data.get("gamma_wall_up"), price, cfg.accent_short), md=2),
                dbc.Col(_level_card("Support", data.get("gamma_wall_down"), price, cfg.accent_long), md=2),
                dbc.Col(_level_card("SMA 200", data.get("sma_200"), price, "#4dabf7"), md=2),
                dbc.Col(_level_card("Prev High", data.get("prior_day_high"), price, "#ffa94d"), md=2),
                dbc.Col(_level_card("Prev Low", data.get("prior_day_low"), price, "#ffa94d"), md=2),
            ]),

            # ---- SIGNAL HISTORY ----
            dbc.Card(
                style={"backgroundColor": cfg.card_color, "border": f"1px solid {cfg.border_color}", "padding": "12px"},
                children=[
                    html.H5("Recent Signal History", style={"color": cfg.text_color}),
                    html.Div(id="detail-signal-history", children=_build_history_table(history)),
                ],
            ),
        ],
    )


def _build_trade_params(data: Dict, signal: str, signal_color: str) -> dbc.Card:
    """Build the trade parameters card with T1/T2 targets, R:R, ATR, conditions."""
    recommendation = data.get("recommendation", "HOLD")
    stop_loss = data.get("stop_loss")
    target_1 = data.get("target_1")
    target_2 = data.get("target_2")
    rr_ratio = data.get("rr_ratio")
    atr = data.get("atr")
    trade_conditions = data.get("trade_conditions", "")
    trend = data.get("trend_direction", "SIDEWAYS")
    price = data.get("price", 0)
    vwap_status = data.get("vwap_status", "")
    rel_str = data.get("relative_strength")
    signal_age = data.get("signal_age", 1)

    rec_color = {
        "BUY": "#00ff88",
        "SELL": "#ff4488",
    }.get(recommendation, "#888")

    trend_display = {"UPTREND": "UP", "DOWNTREND": "DOWN", "SIDEWAYS": "SIDE", "UP": "UP", "DOWN": "DOWN", "SIDE": "SIDE"}.get(trend, trend)
    trend_color = {"UP": cfg.accent_long, "UPTREND": cfg.accent_long, "DOWN": cfg.accent_short, "DOWNTREND": cfg.accent_short}.get(trend, "#555")

    rr_text = f"{rr_ratio}:1" if rr_ratio else "N/A"
    rr_color = "#00ff88" if rr_ratio and rr_ratio >= 2 else (cfg.accent_neutral if rr_ratio and rr_ratio >= 1.5 else "#ff4488")

    # Build condition items
    condition_items = []
    if trade_conditions:
        for cond in trade_conditions.split(" | "):
            cond = cond.strip()
            if not cond:
                continue
            if cond.startswith("CAUTION") or cond.startswith("Counter"):
                cond_color = "#ffd43b"
            elif cond.startswith("T1") or cond.startswith("T2"):
                cond_color = "#51cf66"
            else:
                cond_color = cfg.text_color
            condition_items.append(
                html.Li(cond, style={"color": cond_color, "fontSize": "12px", "padding": "2px 0"})
            )

    param_style = {"textAlign": "center", "padding": "8px"}
    label_style = {"color": "#666", "fontSize": "10px", "margin": "0", "fontWeight": "bold", "letterSpacing": "0.5px"}
    value_style_base = {"margin": "2px 0", "fontWeight": "700"}

    return dbc.Card(
        style={"backgroundColor": cfg.card_color, "border": f"1px solid {cfg.border_color}", "padding": "16px"},
        className="mb-3",
        children=[
            html.H5("Trade Setup", style={"color": cfg.text_color, "marginBottom": "12px"}),
            dbc.Row([
                dbc.Col(html.Div([
                    html.P("REC", style=label_style),
                    html.H4(recommendation, style={**value_style_base, "color": rec_color, "fontSize": "20px"}),
                ], style=param_style), md=1),
                dbc.Col(html.Div([
                    html.P("TREND", style=label_style),
                    html.H5(trend_display, style={**value_style_base, "color": trend_color}),
                ], style=param_style), md=1),
                dbc.Col(html.Div([
                    html.P("STOP", style=label_style),
                    html.H5(f"${stop_loss:.2f}" if stop_loss else "---", style={**value_style_base, "color": "#ff6b6b"}),
                ], style=param_style), md=2),
                dbc.Col(html.Div([
                    html.P("TARGET 1", style=label_style),
                    html.H5(f"${target_1:.2f}" if target_1 else "---", style={**value_style_base, "color": "#51cf66"}),
                    html.P("Take 50%", style={"color": "#555", "fontSize": "9px", "margin": "0"}),
                ], style=param_style), md=2),
                dbc.Col(html.Div([
                    html.P("TARGET 2", style=label_style),
                    html.H5(f"${target_2:.2f}" if target_2 else "---", style={**value_style_base, "color": "#00ff88"}),
                    html.P("Close / Trail", style={"color": "#555", "fontSize": "9px", "margin": "0"}),
                ], style=param_style), md=2),
                dbc.Col(html.Div([
                    html.P("R:R", style=label_style),
                    html.H5(rr_text, style={**value_style_base, "color": rr_color}),
                ], style=param_style), md=1),
                dbc.Col(html.Div([
                    html.P("ATR", style=label_style),
                    html.H5(f"${atr:.2f}" if atr else "---", style={**value_style_base, "color": "#aaa"}),
                ], style=param_style), md=1),
                dbc.Col(html.Div([
                    html.P("VWAP", style=label_style),
                    html.H5(
                        "Above" if "ABOVE" in vwap_status else ("Below" if "BELOW" in vwap_status else "---"),
                        style={**value_style_base, "color": cfg.accent_long if "ABOVE" in vwap_status else cfg.accent_short},
                    ),
                ], style=param_style), md=1),
                dbc.Col(html.Div([
                    html.P("AGE", style=label_style),
                    html.H5(str(signal_age), style={**value_style_base, "color": "#00ff88" if signal_age >= 3 else "#888"}),
                ], style=param_style), md=1),
                dbc.Col(html.Div([
                    html.P("MOMENTUM", style=label_style),
                    html.H5(
                        data.get("signal_momentum", "NEW"),
                        style={
                            **value_style_base,
                            "color": {"STRENGTHENING": "#00ff88", "WEAKENING": "#ff006e", "STABLE": "#888"}.get(
                                data.get("signal_momentum", ""), cfg.accent_cyan
                            ),
                            "fontSize": "12px",
                        },
                    ),
                ], style=param_style), md=2),
            ], className="mb-2"),
            # Relative strength badge
            html.Div([
                html.Span("RS vs SPY: ", style={"color": "#666", "fontSize": "11px"}),
                html.Span(
                    f"{rel_str:+.1f}%" if rel_str is not None else "N/A",
                    style={
                        "color": cfg.accent_long if rel_str and rel_str > 0 else cfg.accent_short,
                        "fontSize": "12px",
                        "fontWeight": "bold",
                    },
                ),
            ], style={"marginBottom": "8px"}) if rel_str is not None else html.Div(),
            # Trade conditions
            html.Div([
                html.P("TRADE CONDITIONS", style={**label_style, "margin": "0 0 4px 0"}),
                html.Ul(
                    condition_items if condition_items else [
                        html.Li("Standard entry", style={"color": "#555", "fontSize": "12px"})
                    ],
                    style={"listStyle": "none", "padding": "0", "margin": "0"},
                ),
            ]) if signal not in ("NEUTRAL",) else html.Div(),
        ],
    )


def _build_price_chart(data: Dict, price_df=None) -> go.Figure:
    """Build candlestick chart with GEX overlays and ATR targets."""
    fig = make_subplots(
        rows=2, cols=1,
        shared_xaxes=True,
        row_heights=[0.75, 0.25],
        vertical_spacing=0.03,
    )

    if price_df is not None and not price_df.empty:
        fig.add_trace(
            go.Candlestick(
                x=price_df.index,
                open=price_df["Open"],
                high=price_df["High"],
                low=price_df["Low"],
                close=price_df["Close"],
                name="Price",
                increasing_line_color="#00ff88",
                decreasing_line_color="#ff4488",
            ),
            row=1, col=1,
        )
        colors = [
            "#00ff88" if c >= o else "#ff4488"
            for c, o in zip(price_df["Close"], price_df["Open"])
        ]
        fig.add_trace(
            go.Bar(
                x=price_df.index,
                y=price_df["Volume"],
                name="Volume",
                marker_color=colors,
                opacity=0.4,
            ),
            row=2, col=1,
        )

    # Key level overlays
    sma = data.get("sma_200")
    zg = data.get("zero_gamma_level")
    wall_up = data.get("gamma_wall_up")
    wall_down = data.get("gamma_wall_down")

    if sma:
        fig.add_hline(y=sma, line_dash="dash", line_color="#4dabf7",
                      annotation_text=f"SMA200: {sma}", row=1, col=1)
    if zg:
        fig.add_hline(y=zg, line_dash="solid", line_color="#b388ff",
                      annotation_text=f"ZG: {zg}", row=1, col=1)
    if wall_up:
        fig.add_hline(y=wall_up, line_dash="dot", line_color="#ff4488",
                      annotation_text=f"Res: {wall_up}", row=1, col=1)
    if wall_down:
        fig.add_hline(y=wall_down, line_dash="dot", line_color="#00ff88",
                      annotation_text=f"Sup: {wall_down}", row=1, col=1)

    # Stop and target overlays
    sl = data.get("stop_loss")
    t1 = data.get("target_1")
    t2 = data.get("target_2")
    if sl:
        fig.add_hline(y=sl, line_dash="dashdot", line_color="#ff6b6b", line_width=2,
                      annotation_text=f"Stop: ${sl}", row=1, col=1)
    if t1:
        fig.add_hline(y=t1, line_dash="dashdot", line_color="#51cf66",
                      annotation_text=f"T1: ${t1}", row=1, col=1)
    if t2:
        fig.add_hline(y=t2, line_dash="dashdot", line_color="#00ff88", line_width=2,
                      annotation_text=f"T2: ${t2}", row=1, col=1)

    # Prior day levels
    pdh = data.get("prior_day_high")
    pdl = data.get("prior_day_low")
    if pdh:
        fig.add_hline(y=pdh, line_dash="dot", line_color="#ffa94d", opacity=0.5,
                      annotation_text=f"Prev H: {pdh}", row=1, col=1)
    if pdl:
        fig.add_hline(y=pdl, line_dash="dot", line_color="#ffa94d", opacity=0.5,
                      annotation_text=f"Prev L: {pdl}", row=1, col=1)

    fig.update_layout(
        template="plotly_dark",
        paper_bgcolor=cfg.card_color,
        plot_bgcolor=cfg.bg_color,
        font_color=cfg.text_color,
        showlegend=False,
        xaxis_rangeslider_visible=False,
        margin=dict(l=50, r=20, t=20, b=20),
    )
    fig.update_yaxes(title_text="Price", row=1, col=1)
    fig.update_yaxes(title_text="Volume", row=2, col=1)

    return fig


def _build_confluence_breakdown(data: Dict) -> html.Div:
    """Build visual confluence factor breakdown with gradient scoring info."""
    price = data.get("price", 0)
    sma = data.get("sma_200")
    zg = data.get("zero_gamma_level")
    rsi = data.get("rsi")
    adx = data.get("adx")
    vol_ratio = data.get("volume_ratio")
    vwap_status = data.get("vwap_status", "UNKNOWN")
    rsi_slope = data.get("rsi_slope")
    adx_slope = data.get("adx_slope")

    factors = []

    # SMA Position (15 pts)
    if sma and price:
        pct = ((price - sma) / sma) * 100 if sma > 0 else 0
        above = price > sma
        factors.append(_factor_row(
            "SMA Position",
            f"${price:.2f} {'above' if above else 'below'} ${sma:.2f} ({pct:+.1f}%)",
            15, above,
        ))
    else:
        factors.append(_factor_row("SMA Position", "No data", 15, None))

    # GEX (25 pts)
    gex_status = data.get("gex_status", "UNKNOWN")
    if zg:
        above = "ABOVE" in gex_status
        dist = abs((price - zg) / zg * 100) if zg > 0 else 0
        factors.append(_factor_row(
            "GEX Positioning",
            f"{'Above' if above else 'Below'} ZG ${zg:.2f} ({dist:.1f}% away)",
            25, above if gex_status != "UNKNOWN" else None,
        ))
    else:
        factors.append(_factor_row("GEX Positioning", "No options data", 25, None))

    # RSI (20 pts)
    if rsi is not None:
        bullish = rsi > 50
        slope_txt = ""
        if rsi_slope is not None:
            slope_txt = f" | slope {rsi_slope:+.1f}"
        factors.append(_factor_row(
            "RSI Momentum",
            f"RSI {rsi:.1f} ({'bullish' if bullish else 'bearish'}){slope_txt}",
            20, bullish,
        ))
    else:
        factors.append(_factor_row("RSI Momentum", "No data", 20, None))

    # Volume (15 pts)
    if vol_ratio is not None:
        confirmed = vol_ratio > 1.3
        factors.append(_factor_row(
            "Volume Confirm",
            f"{vol_ratio:.1f}x avg" + (" (institutional)" if vol_ratio > 2 else ""),
            15, confirmed,
        ))
    else:
        factors.append(_factor_row("Volume Confirm", "No data", 15, None))

    # Trend/ADX (15 pts)
    if adx is not None:
        strong = 25 <= adx < 50
        slope_txt = ""
        if adx_slope is not None:
            slope_txt = f" | slope {adx_slope:+.1f}"
        label = "trending" if adx >= 25 else ("choppy" if adx < 20 else "weak")
        factors.append(_factor_row(
            "Trend Strength",
            f"ADX {adx:.1f} ({label}){slope_txt}",
            15, strong,
        ))
    else:
        factors.append(_factor_row("Trend Strength", "No data", 15, None))

    # VWAP (10 pts)
    if vwap_status != "UNKNOWN":
        above = "ABOVE" in vwap_status
        factors.append(_factor_row(
            "VWAP Position",
            f"{'Above' if above else 'Below'} VWAP",
            10, above,
        ))
    else:
        factors.append(_factor_row("VWAP Position", "No data", 10, None))

    return html.Div(factors)


def _factor_row(
    name: str, detail: str, max_pts: int, bullish: Optional[bool]
) -> html.Div:
    """Build a single confluence factor row with visual bar."""
    if bullish is True:
        color = "#00ff88"
        bar_width = "80%"
    elif bullish is False:
        color = "#ff4488"
        bar_width = "40%"
    else:
        color = "#555"
        bar_width = "10%"

    return html.Div(
        style={"padding": "6px 0", "borderBottom": "1px solid #1e2229"},
        children=[
            html.Div([
                html.Span(f"{name}", style={"color": cfg.text_color, "fontWeight": "600", "fontSize": "12px", "width": "140px", "display": "inline-block"}),
                html.Span(detail, style={"color": "#aaa", "fontSize": "12px"}),
                html.Span(f" /{max_pts}pts", style={"color": "#555", "fontSize": "11px", "float": "right"}),
            ]),
            # Visual bar
            html.Div(
                style={"marginTop": "3px", "backgroundColor": "#1e2229", "borderRadius": "2px", "height": "4px"},
                children=[
                    html.Div(style={"backgroundColor": color, "width": bar_width, "height": "4px", "borderRadius": "2px"}),
                ],
            ),
        ],
    )


def _level_card(title: str, level: Optional[float], price: float, color: str) -> dbc.Card:
    """Build a key level info card."""
    if level is not None:
        dist_pct = ((level - price) / price) * 100 if price > 0 else 0
        value_text = f"${level:.2f}"
        dist_text = f"({dist_pct:+.1f}%)"
    else:
        value_text = "---"
        dist_text = ""

    return dbc.Card(
        style={
            "backgroundColor": cfg.card_color,
            "border": f"1px solid {color}30",
            "padding": "10px",
            "textAlign": "center",
        },
        children=[
            html.P(title, style={"color": "#666", "fontSize": "10px", "margin": "0", "fontWeight": "bold", "letterSpacing": "0.5px"}),
            html.H5(value_text, style={"color": color, "margin": "2px 0 0 0", "fontWeight": "700"}),
            html.P(dist_text, style={"color": "#888", "fontSize": "10px", "margin": "0"}),
        ],
    )


def _build_history_table(history: Optional[List[Dict]]) -> html.Div:
    """Build signal history table with T1/T2 columns."""
    if not history:
        return html.P("No history available", style={"color": "#888"})

    rows = []
    for h in history[:20]:
        signal = h.get("signal", "")
        score = h.get("score", 0)
        color = {"LONG": "#00ff88", "SHORT": "#ff4488"}.get(signal, "#888")

        ts = h.get("timestamp", "")
        if len(ts) > 19:
            ts = ts[:19]

        rec = h.get("recommendation", "")
        rec_color = {"BUY": "#00ff88", "SELL": "#ff4488"}.get(rec, "#888")

        sl = h.get("stop_loss")
        t1 = h.get("target_1")
        t2 = h.get("target_2")
        rr = h.get("rr_ratio")
        td_style = {"color": cfg.text_color, "padding": "4px 8px", "fontSize": "11px"}

        rows.append(
            html.Tr([
                html.Td(ts, style={"color": "#666", "fontSize": "11px", "padding": "4px 8px"}),
                html.Td(h.get("timeframe", ""), style=td_style),
                html.Td(signal, style={"color": color, "fontWeight": "bold", "padding": "4px 8px", "fontSize": "11px"}),
                html.Td(rec, style={"color": rec_color, "fontWeight": "bold", "padding": "4px 8px", "fontSize": "11px"}),
                html.Td(str(score), style={"color": color, "padding": "4px 8px", "fontSize": "11px"}),
                html.Td(f"${sl:.2f}" if sl else "", style={"color": "#ff6b6b", "padding": "4px 8px", "fontSize": "11px"}),
                html.Td(f"${t1:.2f}" if t1 else "", style={"color": "#51cf66", "padding": "4px 8px", "fontSize": "11px"}),
                html.Td(f"${t2:.2f}" if t2 else "", style={"color": "#00ff88", "padding": "4px 8px", "fontSize": "11px"}),
                html.Td(f"{rr}:1" if rr else "", style={"color": cfg.accent_cyan, "padding": "4px 8px", "fontSize": "11px"}),
            ])
        )

    header_style = {"color": cfg.accent_cyan, "fontSize": "10px", "padding": "4px 8px", "borderBottom": "1px solid #333", "fontWeight": "bold", "letterSpacing": "0.5px"}
    return html.Table(
        style={"width": "100%", "borderCollapse": "collapse"},
        children=[
            html.Thead(html.Tr([
                html.Th(col, style=header_style)
                for col in ["TIME", "TF", "SIGNAL", "REC", "SCORE", "STOP", "T1", "T2", "R:R"]
            ])),
            html.Tbody(rows),
        ],
    )
