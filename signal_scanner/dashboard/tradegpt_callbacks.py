"""TradeGPT chat callbacks — sidebar panel + ISR chat wiring.

Handles:
    - Toggle sidebar panel visibility (open/close)
    - Sidebar: send message, receive response, update messages
    - Quick action buttons
    - Auto-sync ticker from ISR to sidebar
"""

from __future__ import annotations

from dash import ALL, Input, Output, State, html, dcc, no_update
from dash.exceptions import PreventUpdate
from loguru import logger

from signal_scanner.config import DashboardConfig

cfg = DashboardConfig()


def _chat_bubble(text: str, is_user: bool = True) -> html.Div:
    """Render a chat message bubble."""
    if is_user:
        return html.Div(
            style={"textAlign": "right", "marginBottom": "10px"},
            children=html.Div(
                text,
                style={
                    "display": "inline-block", "maxWidth": "88%",
                    "backgroundColor": "rgba(88,166,255,0.15)",
                    "borderRadius": "14px 14px 2px 14px",
                    "padding": "10px 14px", "fontSize": "0.84rem",
                    "color": "#e0e0e0", "textAlign": "left",
                    "lineHeight": "1.5",
                },
            ),
        )
    else:
        return html.Div(
            style={"textAlign": "left", "marginBottom": "10px"},
            children=html.Div(
                dcc.Markdown(text, style={"color": "#e0e0e0", "fontSize": "0.84rem", "lineHeight": "1.6"}),
                style={
                    "display": "inline-block", "maxWidth": "88%",
                    "backgroundColor": "rgba(255,255,255,0.05)",
                    "borderRadius": "14px 14px 14px 2px",
                    "padding": "10px 14px",
                },
            ),
        )


def register_tradegpt_callbacks(app) -> None:
    """Register all TradeGPT sidebar callbacks."""

    # -----------------------------------------------------------------------
    # 1. Toggle sidebar visibility (open button + close button)
    # -----------------------------------------------------------------------
    @app.callback(
        Output("tradegpt-floating-panel", "style"),
        Output("tradegpt-floating-btn", "style"),
        Input("tradegpt-floating-btn", "n_clicks"),
        Input("tradegpt-close-btn", "n_clicks"),
        State("tradegpt-floating-panel", "style"),
        State("tradegpt-floating-btn", "style"),
        prevent_initial_call=True,
    )
    def toggle_sidebar(open_clicks, close_clicks, panel_style, btn_style):
        from dash import ctx as dash_ctx
        triggered = dash_ctx.triggered_id

        panel_s = dict(panel_style) if panel_style else {}
        btn_s = dict(btn_style) if btn_style else {}

        if triggered == "tradegpt-close-btn":
            # Close
            panel_s["display"] = "none"
            btn_s["display"] = "flex"
            return panel_s, btn_s

        # Toggle
        if panel_s.get("display") == "none":
            panel_s["display"] = "flex"
            btn_s["display"] = "none"  # Hide button when panel open
        else:
            panel_s["display"] = "none"
            btn_s["display"] = "flex"

        return panel_s, btn_s

    # -----------------------------------------------------------------------
    # 2. Sidebar: send message and get response
    # -----------------------------------------------------------------------
    @app.callback(
        Output("tradegpt-float-messages", "children"),
        Output("tradegpt-float-input", "value"),
        Output("tradegpt-float-status", "children"),
        Input("tradegpt-float-send", "n_clicks"),
        Input("tradegpt-float-input", "n_submit"),
        Input({"type": "tradegpt-quick", "index": ALL}, "n_clicks"),
        State("tradegpt-float-input", "value"),
        State("tradegpt-float-ticker", "value"),
        State("tradegpt-float-messages", "children"),
        prevent_initial_call=True,
    )
    def sidebar_chat_send(n_clicks, n_submit, quick_clicks, input_val, ticker_val, current_messages):
        from dash import ctx as dash_ctx
        triggered = dash_ctx.triggered_id

        # Quick action buttons
        quick_prompts = [
            "Give me a full analysis with entry/exit levels for all timeframes.",
            "What's the best entry setup right now? Include specific price, stop loss, and target.",
            "What are the key risk factors? What would invalidate the current thesis?",
            "What options strategy do you recommend? Include strike, expiry, and sizing guidance.",
        ]

        user_msg = None
        if isinstance(triggered, dict) and triggered.get("type") == "tradegpt-quick":
            idx = triggered.get("index", 0)
            user_msg = quick_prompts[idx] if idx < len(quick_prompts) else None
        else:
            user_msg = input_val

        if not user_msg or not user_msg.strip():
            raise PreventUpdate

        ticker = ticker_val.strip().upper() if ticker_val and ticker_val.strip() else None

        if not ticker:
            msgs = current_messages or []
            # Remove placeholder
            if msgs and len(msgs) == 1 and _is_placeholder(msgs[0]):
                msgs = []
            msgs.append(_chat_bubble(user_msg.strip(), is_user=True))
            msgs.append(_chat_bubble("Enter a ticker symbol in the field above to load intelligence data.", is_user=False))
            return msgs, "", ""

        session_id = f"float-{ticker}"
        context = None
        status = ""

        try:
            from signal_scanner.institutional_intel.intelligence.trade_gpt import get_trade_gpt
            gpt = get_trade_gpt()

            # Load context if ticker doesn't have a briefing yet
            active = gpt.get_active_ticker(session_id)
            if active != ticker or session_id not in gpt.conversations:
                try:
                    from signal_scanner.institutional_intel.config import safe_duckdb_connect
                    from signal_scanner.institutional_intel.intelligence.kubera_context import build_stock_context
                    conn = safe_duckdb_connect(read_only=True)
                    if conn:
                        context = build_stock_context(ticker, conn)
                        conn.close()
                        if context and len(context) > 1:
                            status = f"Briefing loaded for {ticker}"
                        else:
                            status = f"Limited data for {ticker}"
                            context = {}
                except Exception as e:
                    logger.debug("Sidebar context build failed for {}: {}", ticker, e)
                    status = "Data unavailable"
                    context = {}  # Pass empty so chat() knows we tried

            response = gpt.chat(session_id, user_msg.strip(), ticker=ticker, context=context)
        except Exception as e:
            logger.error("TradeGPT sidebar error: {}", e)
            response = f"**Error**: {e}"

        # Build message list
        msgs = current_messages or []
        if msgs and len(msgs) == 1 and _is_placeholder(msgs[0]):
            msgs = []

        msgs.append(_chat_bubble(user_msg.strip(), is_user=True))
        msgs.append(_chat_bubble(response, is_user=False))

        return msgs, "", status

    # -----------------------------------------------------------------------
    # 3. Sync ISR ticker to sidebar
    # -----------------------------------------------------------------------
    @app.callback(
        Output("tradegpt-float-ticker", "value"),
        Input("selected-ticker", "data"),
        prevent_initial_call=True,
    )
    def sync_ticker_to_sidebar(ticker_data):
        if not ticker_data:
            raise PreventUpdate
        return str(ticker_data).upper().strip()


def _is_placeholder(component) -> bool:
    """Check if a component is the initial placeholder text."""
    if isinstance(component, dict):
        style = component.get("props", {}).get("style", {})
        return "italic" in str(style)
    if hasattr(component, "style"):
        return "italic" in str(getattr(component, "style", {}))
    return False
