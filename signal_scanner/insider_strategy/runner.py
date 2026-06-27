"""Daily orchestrator for the insider Director-cluster strategy.

Flow each day (typically after EOD pipeline, ~7 PM):
   1. Pull regime state (SPY 200SMA proxy). If long is blocked AND we have
      open positions, the exiter will force-flatten them in step 4.
   2. Run the detector to find clusters that crystallised today / yesterday.
   3. Open new positions for fresh clusters that pass all gates
      (dedupe, price/ADV, regime), sized at 5% of current paper equity,
      capped at 10 concurrent and 50% deployed.
   4. Walk every open position through the exiter: time, stop, target, ML, regime.
   5. Log everything to the strategy ledger and print a daily summary.

SIM mode (default) just writes to the ledger — no IBKR orders. Live mode
(--live) is reserved for later wiring; the executor module is stubbed.

Usage:
   python -m signal_scanner.insider_strategy.runner --daily
   python -m signal_scanner.insider_strategy.runner --daily --paper-equity 100000
   python -m signal_scanner.insider_strategy.runner --status
"""

from __future__ import annotations

import argparse
from datetime import date, datetime
from typing import List, Dict

from loguru import logger

from signal_scanner.insider_strategy.detector import (
    detect_new_clusters, regime_allows_long,
    PRICE_FLOOR, ADV_FLOOR_DOLLARS, DEDUPE_DAYS,
)
from signal_scanner.insider_strategy.exiter import (
    evaluate_position, ML_HOLD, ML_STOP_X, ML_TGT_R,
)
from signal_scanner.insider_strategy.ledger import StrategyLedger

POSITION_PCT = 0.05
MAX_CONCURRENT = 10
MAX_DEPLOYED_PCT = 0.50
COMMISSION = 1.0
SLIPPAGE_BPS = 10


def _print_header(title: str) -> None:
    print()
    print("=" * 72)
    print(f"  {title}")
    print("=" * 72)


def _current_equity(ledger: StrategyLedger, starting_capital: float) -> float:
    """Compute paper equity = starting + realized P&L (closed) +
    cost_basis of open positions (lower-bound — no live MTM)."""
    open_positions = ledger.get_open_positions()
    open_cost = sum(float(p.get("cost_basis") or 0) for p in open_positions)
    with ledger._conn() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(realized_pnl), 0) AS pnl "
            "FROM insider_strategy_positions WHERE status='CLOSED'"
        ).fetchone()
    realized = float(row["pnl"] or 0)
    cash = starting_capital + realized - open_cost
    return cash + open_cost  # == starting + realized; we surface cash separately


def _cash_available(ledger: StrategyLedger, starting_capital: float) -> float:
    open_cost = sum(float(p.get("cost_basis") or 0) for p in ledger.get_open_positions())
    with ledger._conn() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(realized_pnl), 0) AS pnl "
            "FROM insider_strategy_positions WHERE status='CLOSED'"
        ).fetchone()
    realized = float(row["pnl"] or 0)
    return starting_capital + realized - open_cost


def run_daily(starting_capital: float = 100_000.0, live: bool = False,
              dry_run: bool = False) -> Dict:
    """Single daily run. Returns a summary dict."""
    today = date.today()
    ledger = StrategyLedger()

    _print_header(f"INSIDER STRATEGY — daily run for {today}")

    # 1) Regime
    allowed, current, sma200 = regime_allows_long(today)
    cur_s = f"{current:.2f}" if current else "N/A"
    sma_s = f"{sma200:.2f}" if sma200 else "N/A"
    print(f"  regime check: long {'ALLOWED' if allowed else 'BLOCKED'} "
          f"(market {cur_s} vs 200SMA {sma_s})")

    open_positions_before = ledger.get_open_positions()
    print(f"  open positions at start: {len(open_positions_before)}")

    # 2) Detect new clusters (last 3 days, so we don't miss weekends)
    print("\n  [DETECT] scanning for new Director clusters ...")
    fresh = detect_new_clusters(as_of=today, lookback_days=3)
    print(f"  found {len(fresh)} clusters in the last 3 days passing gates "
          f"(price>=${PRICE_FLOOR}, ADV>=${ADV_FLOOR_DOLLARS:,})")

    # 3) Filter dedupe + open new positions
    new_entries = 0
    skipped_reasons = []
    for c in fresh:
        if ledger.already_entered_recently(c["ticker"], DEDUPE_DAYS):
            skipped_reasons.append(f"{c['ticker']}: dedupe")
            continue
        if not allowed:
            skipped_reasons.append(f"{c['ticker']}: regime")
            continue

        equity = _current_equity(ledger, starting_capital)
        cash = _cash_available(ledger, starting_capital)
        open_now = ledger.get_open_positions()
        if len(open_now) >= MAX_CONCURRENT:
            skipped_reasons.append(f"{c['ticker']}: max-concurrent")
            continue
        deployed = sum(float(p.get("cost_basis") or 0) for p in open_now)
        if deployed >= MAX_DEPLOYED_PCT * equity:
            skipped_reasons.append(f"{c['ticker']}: max-deployed")
            continue

        notional = POSITION_PCT * equity
        if cash < notional + COMMISSION:
            skipped_reasons.append(f"{c['ticker']}: insufficient-cash")
            continue

        fill_px = c["current_price"] * (1 + SLIPPAGE_BPS / 10_000.0)
        shares = round(notional / fill_px, 4)
        cost_basis = shares * fill_px + COMMISSION
        stop_dist = ML_STOP_X * c["atr14"]
        stop_px = fill_px - stop_dist
        target_px = fill_px + ML_TGT_R * stop_dist

        payload = {
            "ticker": c["ticker"],
            "cluster_date": c["cluster_date"],
            "known_date": c["known_date"],
            "n_insiders": c["n_insiders"],
            "n_directors": c["n_directors"],
            "n_officers": c["n_officers"],
            "total_value": c["total_value"],
            "avg_buy_price": c["avg_buy_price"],
            "entry_date": str(today),
            "entry_price": round(fill_px, 4),
            "shares": shares,
            "cost_basis": round(cost_basis, 4),
            "atr14": c["atr14"],
            "stop_price": round(stop_px, 4),
            "target_price": round(target_px, 4),
            "target_r_mult": ML_TGT_R,
            "stop_atr_mult": ML_STOP_X,
            "time_stop_days": ML_HOLD,
            "status": "OPEN",
            "execution_mode": "IBKR_PAPER" if live else "SIM",
        }
        if dry_run:
            print(f"  [DRY] would open: {c['ticker']} @ ${fill_px:.2f} "
                  f"x {shares} (stop ${stop_px:.2f}, target ${target_px:.2f})")
        else:
            pos_id = ledger.open_position(payload)
            new_entries += 1
            print(f"  ENTERED #{pos_id}: {c['ticker']:6s} @ ${fill_px:.2f} "
                  f"x {shares:.0f}  (stop ${stop_px:.2f}, target ${target_px:.2f}, "
                  f"{c['n_directors']} director(s))")

    if skipped_reasons:
        print(f"  skipped {len(skipped_reasons)}: {', '.join(skipped_reasons[:8])}"
              f"{' ...' if len(skipped_reasons) > 8 else ''}")

    # 4) Walk open positions through the exiter
    print("\n  [EXIT] checking open positions ...")
    try:
        from signal_scanner.insider_strategy.exiter import _load_model
        model = _load_model()
    except FileNotFoundError as e:
        logger.warning(str(e))
        model = None

    exit_counts = {"STOP": 0, "TARGET": 0, "TIME": 0, "ML": 0, "REGIME": 0}
    open_positions = ledger.get_open_positions()
    for pos in open_positions:
        should_exit, reason, px = evaluate_position(pos, today, model=model,
                                                     regime_ok=allowed)
        if should_exit:
            # Apply slippage on exit close
            exit_fill = (px * (1 - SLIPPAGE_BPS / 10_000.0)) if px else px
            if not dry_run:
                ledger.close_position(pos["id"], exit_fill, reason, today)
            print(f"  EXIT #{pos['id']}: {pos['ticker']:6s} @ ${exit_fill:.2f} "
                  f"reason={reason}")
            exit_counts[reason] = exit_counts.get(reason, 0) + 1

    # 5) Log run
    open_positions_after = ledger.get_open_positions()
    summary = {
        "run_date": str(today),
        "new_clusters_found": len(fresh),
        "new_entries": new_entries,
        "open_positions_before": len(open_positions_before),
        "ml_exits": exit_counts.get("ML", 0),
        "regime_exits": exit_counts.get("REGIME", 0),
        "stop_exits": exit_counts.get("STOP", 0),
        "target_exits": exit_counts.get("TARGET", 0),
        "time_exits": exit_counts.get("TIME", 0),
        "open_positions_after": len(open_positions_after),
        "regime_allows_long": 1 if allowed else 0,
    }
    if not dry_run:
        ledger.log_run(summary)

    print("\n  ---- SUMMARY ----")
    for k, v in summary.items():
        print(f"  {k:25s} : {v}")

    equity = _current_equity(ledger, starting_capital)
    print(f"\n  paper equity: ${equity:,.2f}  "
          f"(starting ${starting_capital:,.0f}, "
          f"P&L {(equity / starting_capital - 1) * 100:+.2f}%)")
    return summary


def print_status() -> None:
    ledger = StrategyLedger()
    open_positions = ledger.get_open_positions()
    _print_header(f"INSIDER STRATEGY — current status")
    print(f"  open positions: {len(open_positions)}")
    for p in open_positions:
        print(f"    #{p['id']:>3d}  {p['ticker']:6s}  "
              f"entry {p['entry_date']} @ ${p['entry_price']:.2f}  "
              f"stop ${p['stop_price']:.2f}  target ${p['target_price']:.2f}  "
              f"({p['n_directors']} director, {p['n_insiders']} insiders)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--daily", action="store_true",
                    help="Run the daily detect/enter/monitor/exit cycle")
    ap.add_argument("--status", action="store_true",
                    help="Show current open positions")
    ap.add_argument("--paper-equity", type=float, default=100_000.0,
                    help="Starting paper capital for sizing decisions")
    ap.add_argument("--live", action="store_true",
                    help="(reserved) wire to IBKR live brackets — not yet implemented")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print decisions but do NOT write to ledger")
    args = ap.parse_args()

    if args.live:
        print("--live IBKR bracket wiring is not yet implemented in this version.")
        print("Running in SIM mode for now. Live wiring is the next phase.")

    if args.daily:
        run_daily(starting_capital=args.paper_equity, live=False,
                  dry_run=args.dry_run)
    elif args.status:
        print_status()
    else:
        ap.print_help()
