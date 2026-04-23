"""Intelligence Layer Callbacks v2 — 8-report system.

Replaces the old 4-tab intelligence callbacks.
"""

from __future__ import annotations

from dash import Input, Output, State, html, no_update
from dash.exceptions import PreventUpdate
from loguru import logger


def register_intelligence_callbacks(app):
    """Register all Intelligence Layer report callbacks."""

    from signal_scanner.dashboard.layouts.intelligence_reports import (
        build_overview, build_institutional, build_sector_rotation,
        build_sector_strength, build_top_by_sector, build_themes,
        build_market_drivers, build_mean_reversion,
    )
    from signal_scanner.dashboard.intelligence_data import (
        get_overview_data, get_institutional_data, get_sector_rotation_data,
        get_sector_strength_data, get_top_by_sector_data, get_theme_data,
        get_market_drivers_data, get_mean_reversion_data,
    )

    @app.callback(
        Output("intel-tab-content", "children"),
        Input("intel-sub-tabs", "active_tab"),
        Input("refresh-interval", "n_intervals"),
        prevent_initial_call=False,
    )
    def render_intel_tab(active_tab, _n):
        try:
            if active_tab == "tab-overview":
                data = get_overview_data()
                return build_overview(data)

            elif active_tab == "tab-institutional":
                data = get_institutional_data()
                return build_institutional(data)

            elif active_tab == "tab-sector-rotation":
                data = get_sector_rotation_data()
                return build_sector_rotation(data)

            elif active_tab == "tab-sector-strength":
                data = get_sector_strength_data()
                return build_sector_strength(data)

            elif active_tab == "tab-top-sector":
                data = get_top_by_sector_data()
                return build_top_by_sector(data)

            elif active_tab == "tab-themes":
                data = get_theme_data()
                return build_themes(data)

            elif active_tab == "tab-drivers":
                data = get_market_drivers_data()
                return build_market_drivers(data)

            elif active_tab == "tab-mean-rev":
                data = get_mean_reversion_data()
                return build_mean_reversion(data)

            return html.Div("Select a report tab above.")

        except Exception as e:
            logger.warning("Intelligence report error: {}", e)
            return html.Div(
                f"Report loading error: {e}",
                style={"color": "#e05252", "padding": "20px"},
            )

    # Ticker click → ISR from any report table
    for table_id in [
        "overview-top-table", "overview-improving-table", "overview-warnings-table",
        "inst-quality-table", "sector-rotation-table", "sector-strength-table",
        "top-sector-table", "theme-table", "drivers-pressure-table",
        "drivers-catalysts-table", "mean-rev-table",
    ]:
        try:
            @app.callback(
                Output("selected-ticker", "data", allow_duplicate=True),
                Input(table_id, "active_cell"),
                State(table_id, "derived_virtual_data"),
                State(table_id, "data"),
                prevent_initial_call=True,
            )
            def _table_click(active_cell, virtual_data, data, _tid=table_id):
                if not active_cell:
                    raise PreventUpdate
                view = virtual_data if virtual_data else data
                if not view:
                    raise PreventUpdate
                row_idx = active_cell.get("row", 0)
                col = active_cell.get("column_id", "")
                if col != "ticker" or row_idx >= len(view):
                    raise PreventUpdate
                ticker = view[row_idx].get("ticker", "")
                if ticker:
                    return ticker
                raise PreventUpdate
        except Exception:
            pass  # Some tables may not exist on first load
