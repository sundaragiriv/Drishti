"""My Trades tab callbacks — enter, exit, and refresh manual trades."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Dict, List, Optional

from dash import Input, Output, State, callback_context, no_update
from dash.exceptions import PreventUpdate
from loguru import logger

from signal_scanner.database.db_manager import DatabaseManager


def _get_exit_signals(symbols: List[str]) -> Dict[str, str]:
    """Query DuckDB intelligence for exit-signal alerts on open trade symbols.

    Returns {symbol: alert_string} where alert_string is one of:
      EXIT: <reason>   — strong signal to close
      WARN: <reason>   — caution, consider closing
      OK               — no adverse signals
    """
    if not symbols:
        return {}
    try:
        from signal_scanner.institutional_intel.config import safe_duckdb_connect
        conn = safe_duckdb_connect(read_only=True)
        if conn is None:
            return {}
        try:
            # Get best quarter
            best_q = conn.execute("""
                SELECT report_quarter FROM intelligence_scores
                WHERE data_quality_score >= 75
                GROUP BY report_quarter HAVING COUNT(*) >= 500
                ORDER BY report_quarter DESC LIMIT 1
            """).fetchone()
            if not best_q:
                return {}
            quarter = best_q[0]

            placeholders = ",".join("?" * len(symbols))
            rows = conn.execute(f"""
                SELECT ticker, accum_phase, conviction_score, swing_signal,
                       price_momentum_90d, insider_effect_score, short_squeeze_score
                FROM intelligence_scores
                WHERE report_quarter = ? AND ticker IN ({placeholders})
            """, [quarter] + symbols).fetchall()
        finally:
            conn.close()

        alerts: Dict[str, str] = {}
        for r in rows:
            ticker = r[0]
            phase = str(r[1] or "")
            conviction = float(r[2] or 0)
            swing = str(r[3] or "")
            momentum = float(r[4] or 0)
            insider_eff = float(r[5] or 0)
            squeeze = float(r[6] or 0)

            flags = []
            # Strong exit signals
            if phase == "DISTRIBUTION":
                flags.append("EXIT: Distribution phase")
            elif phase == "LATE DISTRIBUTION":
                flags.append("EXIT: Late distribution")
            if swing == "AVOID":
                flags.append("EXIT: Swing AVOID")
            if conviction < 30:
                flags.append("EXIT: Low conviction ({:.0f})".format(conviction))

            # Warning signals
            if not flags:
                if momentum < -10:
                    flags.append("WARN: Neg momentum ({:.0f}%)".format(momentum))
                if conviction < 45 and conviction >= 30:
                    flags.append("WARN: Weak conviction ({:.0f})".format(conviction))
                if swing == "WATCH":
                    flags.append("WARN: Swing WATCH")

            alerts[ticker] = flags[0] if flags else "OK"
        return alerts
    except Exception as exc:
        logger.debug("Exit signal check skipped: {}", exc)
        return {}


def register_my_trades_callbacks(app, db: DatabaseManager) -> None:
    """Wire all My Trades tab interactivity."""

    # ------------------------------------------------------------------
    # 1. Refresh trades table + stat tiles on interval / after entry/exit
    # ------------------------------------------------------------------
    @app.callback(
        Output("mt-trades-table", "data"),
        Output("mt-open-count", "children"),
        Output("mt-closed-count", "children"),
        Output("mt-win-rate", "children"),
        Output("mt-realized-pnl", "children"),
        Output("mt-wins", "children"),
        Output("mt-losses", "children"),
        Input("refresh-interval", "n_intervals"),
        Input("mt-enter-btn", "n_clicks"),
        Input("mt-exit-btn", "n_clicks"),
        State("mt-status-filter", "value"),
    )
    def refresh_my_trades(n_intervals, _enter_clicks, _exit_clicks, status_filter):
        trades = db.get_manual_trades(limit=200)
        perf = db.get_manual_performance()

        # Get exit signal alerts for open trades
        open_symbols = list({
            t.get("symbol", "") for t in trades
            if t.get("status") == "OPEN" and t.get("symbol")
        })
        alerts = _get_exit_signals(open_symbols) if open_symbols else {}

        # Apply status filter
        if status_filter and status_filter != "ALL":
            trades = [t for t in trades if t.get("status") == status_filter]

        # Format opened_at / closed_at for display + attach alerts
        for t in trades:
            oa = t.get("opened_at") or ""
            if len(oa) > 16:
                t["opened_at"] = oa[:16].replace("T", " ")
            ca = t.get("closed_at") or ""
            if len(ca) > 16:
                t["closed_at"] = ca[:16].replace("T", " ")
            # Clean up recommendation_source display
            src = t.get("recommendation_source") or ""
            t["recommendation_source"] = src.replace("MANUAL_", "")
            # Attach alert state
            if t.get("status") == "OPEN":
                t["alert_state"] = alerts.get(t.get("symbol", ""), "OK")
            else:
                t["alert_state"] = ""

        pnl = perf.get("realized_pnl", 0)
        pnl_str = f"${pnl:+,.2f}" if pnl else "$0"
        wr = perf.get("win_rate", 0)
        wr_str = f"{wr:.1f}%"

        return (
            trades,
            str(perf.get("open_positions", 0)),
            str(perf.get("closed_trades", 0)),
            wr_str,
            pnl_str,
            str(perf.get("wins", 0)),
            str(perf.get("losses", 0)),
        )

    # ------------------------------------------------------------------
    # 2. Show/hide option fields based on instrument type
    # ------------------------------------------------------------------
    @app.callback(
        Output("mt-opt-expiry-col", "style"),
        Output("mt-opt-strike-col", "style"),
        Input("mt-entry-instrument", "value"),
    )
    def toggle_option_fields(instrument):
        if instrument in ("CALL", "PUT"):
            return {"display": "block"}, {"display": "block"}
        return {"display": "none"}, {"display": "none"}

    # ------------------------------------------------------------------
    # 3. Enter trade
    # ------------------------------------------------------------------
    @app.callback(
        Output("mt-entry-status", "children"),
        Output("mt-entry-symbol", "value"),
        Output("mt-entry-price", "value"),
        Output("mt-entry-stop", "value"),
        Output("mt-entry-target", "value"),
        Output("mt-entry-notes", "value"),
        Output("mt-entry-opt-expiry", "value"),
        Output("mt-entry-opt-strike", "value"),
        Input("mt-enter-btn", "n_clicks"),
        State("mt-entry-symbol", "value"),
        State("mt-entry-side", "value"),
        State("mt-entry-instrument", "value"),
        State("mt-entry-price", "value"),
        State("mt-entry-qty", "value"),
        State("mt-entry-stop", "value"),
        State("mt-entry-target", "value"),
        State("mt-entry-source", "value"),
        State("mt-entry-notes", "value"),
        State("mt-entry-opt-expiry", "value"),
        State("mt-entry-opt-strike", "value"),
        prevent_initial_call=True,
    )
    def enter_trade(
        n_clicks, symbol, side, instrument, price, qty,
        stop_loss, target, source, notes, opt_expiry, opt_strike,
    ):
        if not n_clicks:
            raise PreventUpdate

        # Validate required fields
        if not symbol or not symbol.strip():
            return "Symbol is required.", no_update, no_update, no_update, no_update, no_update, no_update, no_update
        if not price or float(price) <= 0:
            return "Entry price is required.", no_update, no_update, no_update, no_update, no_update, no_update, no_update
        if not qty or int(qty) <= 0:
            return "Quantity is required.", no_update, no_update, no_update, no_update, no_update, no_update, no_update

        symbol = symbol.strip().upper()
        now = datetime.now(timezone.utc).isoformat()

        # Determine instrument_type and option fields
        inst_type = "STOCK"
        opt_type = None
        if instrument in ("CALL", "PUT"):
            inst_type = "OPTION"
            opt_type = instrument

        data = {
            "opened_at": now,
            "symbol": symbol,
            "side": side,
            "entry_price": float(price),
            "quantity": int(qty),
            "stop_loss": float(stop_loss) if stop_loss else None,
            "target_1": float(target) if target else None,
            "target_2": None,
            "recommendation_source": source or "MANUAL",
            "instrument_type": inst_type,
            "option_type": opt_type,
            "option_expiry": opt_expiry if opt_type else None,
            "option_strike": float(opt_strike) if opt_type and opt_strike else None,
            "created_ts": now,
        }

        try:
            trade_id = db.create_manual_trade(data)
            logger.info(f"Manual trade entered: {symbol} {side} {inst_type} @ {price} x{qty} (ID={trade_id})")
            # Clear form fields on success
            return (
                f"Trade entered: {symbol} {side} @ ${float(price):.2f} x {qty} (ID: {trade_id})",
                "",      # clear symbol
                None,    # clear price
                None,    # clear stop
                None,    # clear target
                "",      # clear notes
                "",      # clear opt expiry
                None,    # clear opt strike
            )
        except Exception as e:
            logger.error(f"Failed to enter manual trade: {e}")
            return f"Error: {e}", no_update, no_update, no_update, no_update, no_update, no_update, no_update

    # ------------------------------------------------------------------
    # 4. Show exit panel when a row is selected
    # ------------------------------------------------------------------
    @app.callback(
        Output("mt-exit-panel", "hidden"),
        Output("mt-exit-info", "children"),
        Output("mt-exit-trade-id", "data"),
        Output("mt-exit-price", "value"),
        Input("mt-trades-table", "selected_rows"),
        State("mt-trades-table", "data"),
        prevent_initial_call=True,
    )
    def show_exit_panel(selected_rows, table_data):
        if not selected_rows or not table_data:
            return True, "", None, None

        row = table_data[selected_rows[0]]
        if row.get("status") != "OPEN":
            return True, "", None, None

        trade_id = row.get("id")
        symbol = row.get("symbol", "?")
        side = row.get("side", "?")
        entry = row.get("entry_price", 0)
        qty = row.get("quantity", 0)
        info = f"{symbol}  |  {side}  |  Entry: ${entry:.2f}  |  Qty: {qty}  |  ID: {trade_id}"
        return False, info, trade_id, None

    # ------------------------------------------------------------------
    # 5. Execute exit
    # ------------------------------------------------------------------
    @app.callback(
        Output("mt-exit-status", "children"),
        Output("mt-exit-panel", "hidden", allow_duplicate=True),
        Output("mt-trades-table", "selected_rows"),
        Input("mt-exit-btn", "n_clicks"),
        State("mt-exit-trade-id", "data"),
        State("mt-exit-price", "value"),
        State("mt-exit-qty", "value"),
        State("mt-exit-notes", "value"),
        prevent_initial_call=True,
    )
    def exit_trade(n_clicks, trade_id, exit_price, _exit_qty, notes):
        """Close full position. Partial exits not supported."""
        if not n_clicks or not trade_id:
            raise PreventUpdate

        if not exit_price or float(exit_price) <= 0:
            return "Exit price is required.", False, no_update

        try:
            # Use close_trade_and_update_idea to propagate to idea ledger
            result = db.close_trade_and_update_idea(
                trade_id=int(trade_id),
                exit_price=float(exit_price),
                exit_reason=f"MANUAL_EXIT: {notes}" if notes else "MANUAL_EXIT",
                fees=0,
            )
            if not result:
                # Fallback to legacy close (full position)
                now = datetime.now(timezone.utc).isoformat()
                result = db.close_manual_trade(
                    trade_id=int(trade_id),
                    closed_at=now,
                    exit_price=float(exit_price),
                    notes=notes or "",
                )
            if not result:
                return "Trade not found or already closed.", False, no_update

            pnl = result.get("realized_pnl", 0)
            symbol = result.get("symbol", "?")
            logger.info(f"Manual trade closed: {symbol} ID={trade_id} P&L=${pnl:.2f}")
            return (
                f"Closed {symbol} — P&L: ${pnl:+,.2f} ({result.get('realized_pnl_pct', 0):+.1f}%)",
                True,  # hide exit panel
                [],    # deselect row
            )
        except Exception as e:
            logger.error(f"Failed to close manual trade: {e}")
            return f"Error: {e}", False, no_update

    # ------------------------------------------------------------------
    # 6. Cancel exit panel
    # ------------------------------------------------------------------
    @app.callback(
        Output("mt-exit-panel", "hidden", allow_duplicate=True),
        Output("mt-trades-table", "selected_rows", allow_duplicate=True),
        Input("mt-exit-cancel-btn", "n_clicks"),
        prevent_initial_call=True,
    )
    def cancel_exit(_n):
        if not _n:
            raise PreventUpdate
        return True, []

    # ------------------------------------------------------------------
    # 7. Status filter updates table data
    # ------------------------------------------------------------------
    @app.callback(
        Output("mt-trades-table", "data", allow_duplicate=True),
        Input("mt-status-filter", "value"),
        prevent_initial_call=True,
    )
    def filter_trades(status_filter):
        trades = db.get_manual_trades(limit=200)
        open_symbols = list({
            t.get("symbol", "") for t in trades
            if t.get("status") == "OPEN" and t.get("symbol")
        })
        alerts = _get_exit_signals(open_symbols) if open_symbols else {}
        if status_filter and status_filter != "ALL":
            trades = [t for t in trades if t.get("status") == status_filter]
        for t in trades:
            oa = t.get("opened_at") or ""
            if len(oa) > 16:
                t["opened_at"] = oa[:16].replace("T", " ")
            ca = t.get("closed_at") or ""
            if len(ca) > 16:
                t["closed_at"] = ca[:16].replace("T", " ")
            src = t.get("recommendation_source") or ""
            t["recommendation_source"] = src.replace("MANUAL_", "")
            if t.get("status") == "OPEN":
                t["alert_state"] = alerts.get(t.get("symbol", ""), "OK")
            else:
                t["alert_state"] = ""
        return trades

    # ------------------------------------------------------------------
    # 8. Prefill entry form from stock idea tables (cross-tab navigation)
    # ------------------------------------------------------------------
    @app.callback(
        Output("mt-entry-symbol", "value", allow_duplicate=True),
        Output("mt-entry-side", "value", allow_duplicate=True),
        Output("mt-entry-price", "value", allow_duplicate=True),
        Output("mt-entry-stop", "value", allow_duplicate=True),
        Output("mt-entry-target", "value", allow_duplicate=True),
        Output("nav-my-trades", "n_clicks"),
        Input("mt-prefill-store", "data"),
        prevent_initial_call=True,
    )
    def prefill_entry_form(prefill_data):
        if not prefill_data or not isinstance(prefill_data, dict):
            raise PreventUpdate
        symbol = prefill_data.get("symbol", "")
        if not symbol:
            raise PreventUpdate
        side = prefill_data.get("side", "LONG")
        price = prefill_data.get("price")
        stop = prefill_data.get("stop")
        target = prefill_data.get("target")
        # Trigger nav click to switch to My Trades tab
        return symbol, side, price, stop, target, 1
