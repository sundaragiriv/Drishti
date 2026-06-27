"""Standalone dashboard launcher — no IBKR required.

Usage:
    python run_dashboard.py [--port 8050] [--debug]

Starts the Dash dashboard with all callbacks registered.
Scanner and IBKR features show as disconnected but all
intelligence, sniper board, performance, and idea surfaces work.
"""

import argparse
import sys

from loguru import logger


def main():
    parser = argparse.ArgumentParser(description="Standalone Dashboard")
    parser.add_argument("--port", type=int, default=8050)
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    # Initialize database
    from signal_scanner.database.db_manager import DatabaseManager
    db = DatabaseManager()
    db.init_db()

    # Create a minimal scanner stub for callbacks that need it
    from signal_scanner.scanner.multi_symbol_scanner import MultiSymbolScanner
    from signal_scanner.core.ibkr_connector import DataConnector
    connector = DataConnector()
    scanner = MultiSymbolScanner(connector, db)

    # Build dashboard
    from signal_scanner.dashboard.app import app
    from signal_scanner.dashboard.callbacks import register_callbacks
    from signal_scanner.dashboard.layouts.main_view import build_main_layout

    app.layout = build_main_layout()
    register_callbacks(app, db, scanner)

    # Register all callback modules
    from signal_scanner.dashboard.reports_callbacks import register_reports_callbacks
    register_reports_callbacks(app, db, scanner, live_scanners={})

    from signal_scanner.dashboard.intelligence_callbacks_v2 import register_intelligence_callbacks
    register_intelligence_callbacks(app)

    from signal_scanner.dashboard.kubera_callbacks import register_kubera_callbacks
    register_kubera_callbacks(app)

    from signal_scanner.dashboard.stock_report_callbacks import register_stock_report_callbacks
    register_stock_report_callbacks(app)

    from signal_scanner.dashboard.my_trades_callbacks import register_my_trades_callbacks
    register_my_trades_callbacks(app, db)

    from signal_scanner.dashboard.tradegpt_callbacks import register_tradegpt_callbacks
    register_tradegpt_callbacks(app)

    from signal_scanner.dashboard.sniper_callbacks import register_sniper_callbacks
    register_sniper_callbacks(app, db, scanner=scanner)

    from signal_scanner.dashboard.forecast_callbacks import register_forecast_callbacks
    register_forecast_callbacks(app)

    from signal_scanner.dashboard.drishti_callbacks import register_drishti_callbacks
    register_drishti_callbacks(app)

    logger.info(f"Dashboard at http://127.0.0.1:{args.port}")
    logger.info("IBKR not connected — scanner features disabled, intelligence/sniper/performance active")

    app.run(
        host="0.0.0.0",
        port=args.port,
        debug=args.debug,
        use_reloader=False,
        dev_tools_hot_reload=False,
    )


if __name__ == "__main__":
    main()
