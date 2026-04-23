"""Ask Kubera dashboard callbacks.

Handles the Ask Kubera section:
    - Quick rule-based summary (instant, no API)
    - Full AI-powered Kubera Intelligence Report via Claude API
"""

from __future__ import annotations

from dash import Input, Output, State, callback_context
from dash.exceptions import PreventUpdate

from signal_scanner.config import DashboardConfig

cfg = DashboardConfig()


def register_kubera_callbacks(app) -> None:
    """Register Ask Kubera callbacks with the Dash app."""

    @app.callback(
        Output("ask-kubera-quick-summary", "children"),
        Output("ask-kubera-report", "children"),
        Output("ask-kubera-status", "children"),
        Input("ask-kubera-run", "n_clicks"),
        State("ask-kubera-symbol", "value"),
        prevent_initial_call=True,
    )
    def run_ask_kubera(n_clicks, symbol_input):
        """Generate Kubera Intelligence Report for the given ticker.

        Step 1: Build stock context from warehouse (fast, rule-based)
        Step 2: Generate quick summary (instant fallback)
        Step 3: Call Claude API for full Kubera report (requires ANTHROPIC_API_KEY)
        """
        if not n_clicks:
            raise PreventUpdate

        symbol = str(symbol_input or "").strip().upper()
        if not symbol:
            return (
                "Enter a ticker symbol above.",
                "No symbol entered.",
                "",
            )

        # ---- Connect to institutional warehouse ----
        try:
            import duckdb
            from signal_scanner.institutional_intel.config import WAREHOUSE_PATH
            conn = duckdb.connect(str(WAREHOUSE_PATH), read_only=True)
        except Exception as e:
            return (
                f"Warehouse connection failed: {e}",
                f"**Error**: Cannot connect to institutional data warehouse.\n\n{e}",
                "Error",
            )

        try:
            # ---- Build stock context ----
            from signal_scanner.institutional_intel.intelligence.kubera_context import (
                build_stock_context,
            )
            context = build_stock_context(ticker=symbol, conn=conn)
        except Exception as e:
            conn.close()
            return (
                f"Context build failed for {symbol}: {e}",
                f"**Error building context for {symbol}**: {e}",
                "Error",
            )

        # ---- Quick summary (rule-based, always shown) ----
        try:
            from signal_scanner.institutional_intel.intelligence.kubera_engine import (
                generate_quick_summary,
            )
            quick_summary = generate_quick_summary(context)
        except Exception as e:
            quick_summary = f"Quick summary unavailable: {e}"

        # ---- Full AI report via Claude API ----
        status_msg = ""
        try:
            from signal_scanner.institutional_intel.intelligence.kubera_engine import (
                generate_kubera_report,
            )
            report = generate_kubera_report(context)
            if report.startswith("**Error**"):
                status_msg = "AI report failed — showing quick summary only"
            else:
                status_msg = f"Report generated for {symbol}"
        except Exception as e:
            report = f"**Error generating Kubera report**: {e}"
            status_msg = "AI report error"
        finally:
            try:
                conn.close()
            except Exception:
                pass

        return quick_summary, report, status_msg
