"""Daily operational evidence report — strategy-grade evaluation.

Deterministic command that fully evaluates a trading day:
  - Readiness state (live-enriched)
  - Trade funnel per subsystem/source
  - P&L by source/strategy with win rate, avg hold, avg W/L
  - Unrealized P&L for open positions
  - Zero-output explainability
  - Skip telemetry

Also used as the EOD evaluation command via --eod flag.

Usage:
    python -m signal_scanner.daily_evidence_report
    python -m signal_scanner.daily_evidence_report --date 2026-03-15
    python -m signal_scanner.daily_evidence_report --json
    python -m signal_scanner.daily_evidence_report --eod   # EOD evaluation mode
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path

from signal_scanner.core.readiness import ReadinessState, compute_price_freshness
from signal_scanner.core.telemetry import get_daily_summary, get_daily_funnel, flush_funnel

SIGNALS_DB = Path("signal_scanner/data/signals.db")
WAREHOUSE_DB = Path("data/warehouse/sec_intel.duckdb")


def _section(title: str) -> str:
    return f"\n{'=' * 70}\n  {title}\n{'=' * 70}"


def _sqlite_query(sql: str, params: tuple = ()) -> list[dict]:
    if not SIGNALS_DB.exists():
        return []
    try:
        conn = sqlite3.connect(f"file:{SIGNALS_DB}?mode=ro", uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(sql, params).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception:
        return []


def _duck_query(sql: str) -> list[dict]:
    try:
        import duckdb
        conn = duckdb.connect(str(WAREHOUSE_DB), read_only=True)
        rows = conn.execute(sql).fetchall()
        cols = [d[0] for d in conn.description]
        conn.close()
        return [dict(zip(cols, r)) for r in rows]
    except Exception:
        return []


def _live_readiness() -> dict:
    state = ReadinessState.load()
    price_ok, age_days, latest_str = compute_price_freshness()
    state.prices_age_days = age_days
    state.latest_price_date = latest_str
    scanner_list = []
    for name, path in [
        ("VWAP_MR", Path("data/warehouse/models/intraday_ml_vwap_mr.pkl")),
        ("FPB", Path("data/warehouse/models/intraday_ml_fpb.pkl")),
        ("ORB_V2", Path("data/warehouse/models/intraday_ml_orb_v2.pkl")),
    ]:
        if path.exists():
            scanner_list.append(name)
    state.enabled_scanners = scanner_list
    recent_scans = _sqlite_query(
        "SELECT data_source FROM scan_history ORDER BY id DESC LIMIT 1"
    )
    if recent_scans and recent_scans[0].get("data_source") == "IBKR":
        state.ibkr_connected = True
    state.resolve_status()
    return state.to_dict()


def _get_pnl_by_source(trade_date: str) -> list[dict]:
    """P&L breakdown by recommendation_source for closed trades up to trade_date."""
    return _sqlite_query("""
        SELECT recommendation_source AS source,
               COUNT(*) AS trades,
               SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) AS wins,
               SUM(CASE WHEN realized_pnl <= 0 THEN 1 ELSE 0 END) AS losses,
               ROUND(SUM(realized_pnl), 2) AS total_pnl,
               ROUND(AVG(realized_pnl), 2) AS avg_pnl,
               ROUND(AVG(CASE WHEN realized_pnl > 0 THEN realized_pnl END), 2) AS avg_winner,
               ROUND(AVG(CASE WHEN realized_pnl <= 0 THEN realized_pnl END), 2) AS avg_loser,
               ROUND(AVG(
                   CASE WHEN closed_at IS NOT NULL AND opened_at IS NOT NULL
                   THEN (julianday(closed_at) - julianday(opened_at)) * 24
                   END
               ), 1) AS avg_hold_hours
        FROM paper_trades
        WHERE status = 'CLOSED'
          AND closed_at >= datetime(?, '-30 days')
        GROUP BY recommendation_source
        ORDER BY total_pnl DESC
    """, (trade_date + " 23:59:59",))


def _get_unrealized_pnl(open_positions: list[dict]) -> list[dict]:
    """Compute unrealized P&L using latest available prices from DuckDB."""
    if not open_positions:
        return []
    symbols = [p.get("symbol") for p in open_positions if p.get("symbol")]
    if not symbols:
        return []
    placeholders = ",".join(f"'{s}'" for s in symbols)
    prices = _duck_query(f"""
        SELECT ticker, close, trade_date
        FROM fact_daily_prices
        WHERE ticker IN ({placeholders})
        AND trade_date = (SELECT MAX(trade_date) FROM fact_daily_prices)
    """)
    price_map = {r["ticker"]: r["close"] for r in prices}

    result = []
    for pos in open_positions:
        sym = pos.get("symbol", "")
        entry = pos.get("entry_price") or 0
        qty = pos.get("quantity") or 0
        side = pos.get("side", "LONG")
        current = price_map.get(sym)
        if current and entry > 0:
            if side == "LONG":
                unrealized = round((current - entry) * qty, 2)
                unrealized_pct = round((current - entry) / entry * 100, 2)
            else:
                unrealized = round((entry - current) * qty, 2)
                unrealized_pct = round((entry - current) / entry * 100, 2)
        else:
            unrealized = None
            unrealized_pct = None
        result.append({
            **pos,
            "current_price": current,
            "unrealized_pnl": unrealized,
            "unrealized_pnl_pct": unrealized_pct,
        })
    return result


def _explain_zero_output(report: dict) -> list[str]:
    """Generate plain-language explanations for low/zero trade activity."""
    explanations = []
    rd = report.get("readiness", {})
    pt = report.get("paper_trades", {})
    funnel = report.get("trade_funnel", {})
    skips = report.get("skip_telemetry", [])

    entered_today = pt.get("entered_today", 0)
    if entered_today > 0:
        return []  # not zero output

    # Check blocked reasons
    for r in rd.get("blocked_reasons", []):
        explanations.append(f"Startup BLOCKED: {r}")
    for r in rd.get("degraded_reasons", []):
        explanations.append(f"Running DEGRADED: {r}")

    # Check skip telemetry for common blockers
    skip_map = {(s["subsystem"], s["reason"]): s["count"] for s in skips}
    if skip_map.get(("execution_loop", "DATA_STALE")):
        explanations.append("Stale prices — intelligence scores may be outdated")
    if skip_map.get(("execution_loop", "IBKR_DISCONNECTED")):
        explanations.append("IBKR was disconnected — no live data for scans")
    if skip_map.get(("execution_loop", "NO_LIVE_UNIVERSE")):
        explanations.append("No qualifying tickers in live universe (all below conviction threshold)")

    for sub in ["VWAP_MR", "FPB", "ORB_V2"]:
        if skip_map.get((sub, "MODEL_UNAVAILABLE")):
            explanations.append(f"{sub}: ML model not available")
        if skip_map.get((sub, "NO_SETUP_QUALIFIED")):
            explanations.append(f"{sub}: no tickers qualified from intelligence snapshot")
        if skip_map.get((sub, "IBKR_DISCONNECTED")):
            explanations.append(f"{sub}: IBKR disconnected, scans skipped")
        if skip_map.get((sub, "LOCK_TIMEOUT")):
            explanations.append(f"{sub}: IBKR lock contention (main scan too slow)")

    if skip_map.get(("IdeaBridge", "REGIME_BLOCKED")):
        explanations.append("IdeaBridge: regime blocked all entries (CRASH state)")
    if skip_map.get(("PaperTrader", "POSITION_LIMIT")):
        explanations.append("PaperTrader: max open positions reached")
    if skip_map.get(("PaperTrader", "LATE_ENTRY_CUTOFF")):
        explanations.append("PaperTrader: past late-entry cutoff time")
    if skip_map.get(("OrderExecutor", "ORPHAN_GATE")):
        explanations.append("OrderExecutor: orphan gate blocked IBKR entries")

    # Check funnel for zero throughput
    for sub, stages in funnel.items():
        candidates = stages.get("candidates", 0)
        entered = stages.get("entered", 0)
        skipped = stages.get("skipped", 0)
        if candidates > 0 and entered == 0:
            explanations.append(
                f"{sub}: {candidates} candidates considered, 0 entered, {skipped} skipped"
            )

    if not explanations:
        explanations.append("No scan cycles ran during market hours (scanner may not have been started)")

    return explanations


def build_report(trade_date: str | None = None) -> dict:
    if trade_date is None:
        trade_date = date.today().isoformat()

    # Flush any in-memory funnel data before building report
    flush_funnel(trade_date)

    report: dict = {
        "report_date": trade_date,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "readiness": _live_readiness(),
        "data_freshness": {},
        "models": {},
        "ideas": {},
        "trade_funnel": {},
        "paper_trades": {},
        "open_positions": [],
        "closed_trades": [],
        "pnl_summary": {},
        "pnl_by_source": [],
        "zero_output_explanation": [],
        "skip_telemetry": [],
        "subsystem_summary": {},
    }

    # Data freshness
    price_ok, age_days, latest_str = compute_price_freshness()
    report["data_freshness"] = {
        "prices_ok": price_ok, "prices_age_days": age_days,
        "latest_price_date": latest_str,
    }

    # Models
    model_files = {
        "intraday_ml_vwap_mr.pkl": Path("data/warehouse/models/intraday_ml_vwap_mr.pkl"),
        "intraday_ml_fpb.pkl": Path("data/warehouse/models/intraday_ml_fpb.pkl"),
        "intraday_ml_orb_v2.pkl": Path("data/warehouse/models/intraday_ml_orb_v2.pkl"),
        "ml_signal_v2.pkl": Path("data/models/ml_signal_v2.pkl"),
        "regime_hmm_daily.pkl": Path("data/warehouse/models/regime_hmm_daily.pkl"),
    }
    for name, p in model_files.items():
        report["models"][name] = {
            "exists": p.exists(),
            "size_kb": round(p.stat().st_size / 1024) if p.exists() else 0,
        }

    # Ideas from intelligence
    for label, sql_filter in [
        ("swing_buy", "swing_signal = 'BUY' AND conviction_score >= 55"),
        ("swing_short", "swing_signal = 'SHORT'"),
        ("triple_lock", "triple_lock = true"),
    ]:
        rows = _duck_query(f"""
            SELECT COUNT(*) as cnt FROM intelligence_scores
            WHERE report_quarter = (
                SELECT MAX(report_quarter) FROM intelligence_scores
                WHERE data_quality_score >= 75
                GROUP BY report_quarter HAVING COUNT(*) >= 1000
                ORDER BY report_quarter DESC LIMIT 1
            ) AND {sql_filter}
        """)
        report["ideas"][label] = rows[0]["cnt"] if rows else 0

    # Trade funnel
    report["trade_funnel"] = get_daily_funnel(trade_date)

    # Paper trades entered today
    entered_today = _sqlite_query(
        "SELECT recommendation_source, side, symbol, entry_price, stop_loss, "
        "target_1, entry_rr_ratio, opened_at "
        "FROM paper_trades WHERE substr(opened_at, 1, 10) = ? ORDER BY opened_at",
        (trade_date,),
    )
    by_source: dict[str, int] = {}
    for t in entered_today:
        src = t.get("recommendation_source") or "UNKNOWN"
        by_source[src] = by_source.get(src, 0) + 1
    report["paper_trades"] = {
        "entered_today": len(entered_today),
        "by_source": by_source,
        "entries": entered_today,
    }

    # Open positions with unrealized P&L
    open_pos_raw = _sqlite_query(
        "SELECT id, symbol, side, entry_price, stop_loss, target_1, target_2, "
        "recommendation_source, opened_at, notional, quantity "
        "FROM paper_trades WHERE status='OPEN' ORDER BY opened_at"
    )
    report["open_positions"] = _get_unrealized_pnl(open_pos_raw)

    # Closed trades today
    closed = _sqlite_query(
        "SELECT id, symbol, side, entry_price, exit_price, realized_pnl, "
        "realized_pnl_pct, exit_reason, recommendation_source, opened_at, closed_at "
        "FROM paper_trades WHERE status='CLOSED' AND substr(closed_at, 1, 10) = ? "
        "ORDER BY closed_at",
        (trade_date,),
    )
    report["closed_trades"] = closed

    # P&L summary
    realized_pnl = sum(t.get("realized_pnl") or 0 for t in closed)
    wins = sum(1 for t in closed if (t.get("realized_pnl") or 0) > 0)
    losses = sum(1 for t in closed if (t.get("realized_pnl") or 0) <= 0)
    unrealized_total = sum(
        p.get("unrealized_pnl") or 0 for p in report["open_positions"]
        if p.get("unrealized_pnl") is not None
    )

    agg_30d = _sqlite_query(
        "SELECT COUNT(*) as total, "
        "SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) as wins, "
        "SUM(CASE WHEN realized_pnl <= 0 THEN 1 ELSE 0 END) as losses, "
        "ROUND(SUM(realized_pnl), 2) as total_pnl, "
        "ROUND(AVG(realized_pnl), 2) as avg_pnl, "
        "ROUND(AVG(CASE WHEN realized_pnl > 0 THEN realized_pnl END), 2) as avg_winner, "
        "ROUND(AVG(CASE WHEN realized_pnl <= 0 THEN realized_pnl END), 2) as avg_loser, "
        "ROUND(AVG((julianday(closed_at) - julianday(opened_at)) * 24), 1) as avg_hold_hours "
        "FROM paper_trades WHERE status='CLOSED' AND closed_at >= datetime('now', '-30 days')"
    )
    report["pnl_summary"] = {
        "today_closed": len(closed),
        "today_wins": wins, "today_losses": losses,
        "today_realized_pnl": round(realized_pnl, 2),
        "open_count": len(report["open_positions"]),
        "open_notional": round(sum(p.get("notional") or 0 for p in open_pos_raw), 2),
        "unrealized_pnl": round(unrealized_total, 2),
        "30d": agg_30d[0] if agg_30d else {},
    }

    # P&L by source/strategy
    report["pnl_by_source"] = _get_pnl_by_source(trade_date)

    # Skip telemetry
    skips = get_daily_summary(trade_date)
    report["skip_telemetry"] = skips
    sub_totals: dict[str, dict[str, int]] = {}
    for s in skips:
        sub = s["subsystem"]
        if sub not in sub_totals:
            sub_totals[sub] = {}
        sub_totals[sub][s["reason"]] = s["count"]
    report["subsystem_summary"] = sub_totals

    # Zero-output explainability
    report["zero_output_explanation"] = _explain_zero_output(report)

    # Real-session fields (actual live activity, not controlled tests)
    try:
        from signal_scanner.session_monitor import get_real_session_fields
        report["real_session"] = get_real_session_fields(trade_date)
    except Exception:
        report["real_session"] = {}

    return report


# ===================================================================
# PRINT FUNCTIONS
# ===================================================================

def print_report(report: dict, eod_mode: bool = False) -> None:
    rd = report["readiness"]
    title = "END-OF-DAY EVALUATION" if eod_mode else "DAILY OPERATIONAL EVIDENCE REPORT"
    print(_section(title))
    print(f"  Date:      {report['report_date']}")
    print(f"  Generated: {report['generated_at'][:19]}Z")

    # Readiness
    print(_section("READINESS"))
    print(f"  Status: {rd.get('readiness_status', 'UNKNOWN')}  |  "
          f"IBKR: {'connected' if rd.get('ibkr_connected') else 'disconnected'}  |  "
          f"Orphan gate: {'ACTIVE' if rd.get('orphan_gate_active') else 'clear'}")
    print(f"  Watchlist: {rd.get('configured_watchlist', 'N/A')}  |  "
          f"Scanners: {', '.join(rd.get('enabled_scanners', [])) or 'none'}")
    for r in rd.get("blocked_reasons", []):
        print(f"  [BLOCK]   {r}")
    for r in rd.get("degraded_reasons", []):
        print(f"  [DEGRADE] {r}")

    # Data freshness
    df = report["data_freshness"]
    ok = "FRESH" if df["prices_ok"] else "STALE"
    print(f"  Prices: {ok} (latest: {df['latest_price_date']}, {df['prices_age_days']}d lag)")

    # Real session activity
    rs = report.get("real_session", {})
    if rs:
        print(_section("REAL SESSION ACTIVITY"))
        print(f"  Scan cycles today:           {rs.get('total_scan_cycles_today', 0)}")
        print(f"  Execution loop ran:          {'YES' if rs.get('execution_loop_ran') else 'NO'}")
        print(f"  Intraday scans (candidates): {rs.get('actual_intraday_scans_today', 0)}")
        print(f"  Intraday setups detected:    {rs.get('actual_intraday_setups_today', 0)}")
        print(f"  Intraday entries:            {rs.get('actual_intraday_entries_today', 0)}")
        print(f"  Scanner MTF entries:         {rs.get('actual_scanner_mtf_entries_today', 0)}")
        print(f"  IdeaBridge entries:          {rs.get('actual_idea_entries_today', 0)}")
        print(f"  Total entries today:         {rs.get('total_entries_today', 0)}")

    # Ideas
    print(_section("IDEAS GENERATED"))
    ideas = report.get("ideas", {})
    print(f"  Swing BUY:     {ideas.get('swing_buy', 0):>5d}   (conv>=55, accum phase)")
    print(f"  Swing SHORT:   {ideas.get('swing_short', 0):>5d}")
    print(f"  Triple Lock:   {ideas.get('triple_lock', 0):>5d}   (conv>=70 + ML>=70 + insiders)")

    # Trade funnel
    print(_section("TRADE FUNNEL"))
    funnel = report.get("trade_funnel", {})
    if not funnel:
        print("  No funnel data recorded (scanner may not have run).")
    else:
        print(f"  {'Source':<22s} {'Cand':>6s} {'Setup':>6s} {'Try':>6s} {'Enter':>6s} {'Skip':>6s}  Conv%")
        print(f"  {'-'*22} {'-'*6} {'-'*6} {'-'*6} {'-'*6} {'-'*6}  {'-'*5}")
        for sub in sorted(funnel.keys()):
            s = funnel[sub]
            cand = s.get("candidates", 0)
            setup = s.get("setups", 0)
            att = s.get("attempted", 0)
            ent = s.get("entered", 0)
            skip = s.get("skipped", 0)
            conv = f"{100*ent/cand:.0f}%" if cand > 0 else "—"
            print(f"  {sub:<22s} {cand:>6d} {setup:>6d} {att:>6d} {ent:>6d} {skip:>6d}  {conv:>5s}")

    # Paper trades entered today
    print(_section("TRADES ENTERED TODAY"))
    pt = report.get("paper_trades", {})
    entries = pt.get("entries", [])
    if not entries:
        print("  No trades entered today.")
    else:
        print(f"  Total: {pt.get('entered_today', 0)} trades")
        for src, cnt in sorted(pt.get("by_source", {}).items(), key=lambda x: -x[1]):
            print(f"    {src:<35s} {cnt}")
        print(f"\n  {'Symbol':<8s} {'Side':<6s} {'Entry':>8s} {'Stop':>8s} {'R:R':>5s} Source")
        print(f"  {'-'*8} {'-'*6} {'-'*8} {'-'*8} {'-'*5} {'-'*25}")
        for t in entries:
            print(f"  {(t.get('symbol') or '?'):<8s} {(t.get('side') or '?'):<6s} "
                  f"{(t.get('entry_price') or 0):>8.2f} {(t.get('stop_loss') or 0):>8.2f} "
                  f"{(t.get('entry_rr_ratio') or 0):>5.1f} "
                  f"{(t.get('recommendation_source') or '?')}")

    # Open positions with unrealized P&L
    print(_section("OPEN POSITIONS"))
    open_pos = report.get("open_positions", [])
    if not open_pos:
        print("  No open positions.")
    else:
        print(f"  {'Symbol':<8s} {'Side':<6s} {'Entry':>8s} {'Current':>8s} {'Unreal':>8s} {'%':>6s} Source")
        print(f"  {'-'*8} {'-'*6} {'-'*8} {'-'*8} {'-'*8} {'-'*6} {'-'*20}")
        for p in open_pos:
            cur = p.get("current_price")
            unr = p.get("unrealized_pnl")
            unr_pct = p.get("unrealized_pnl_pct")
            print(f"  {(p.get('symbol') or '?'):<8s} {(p.get('side') or '?'):<6s} "
                  f"{(p.get('entry_price') or 0):>8.2f} "
                  f"{cur if cur else 'N/A':>8} "
                  f"{'$'+f'{unr:.2f}' if unr is not None else 'N/A':>8s} "
                  f"{f'{unr_pct:+.1f}%' if unr_pct is not None else 'N/A':>6s} "
                  f"{(p.get('recommendation_source') or '?')}")

    # Closed trades today
    print(_section("CLOSED TRADES TODAY"))
    closed = report.get("closed_trades", [])
    if not closed:
        print("  No trades closed today.")
    else:
        print(f"  {'Symbol':<8s} {'Side':<6s} {'Entry':>8s} {'Exit':>8s} {'P&L':>8s} {'%':>6s} Reason")
        print(f"  {'-'*8} {'-'*6} {'-'*8} {'-'*8} {'-'*8} {'-'*6} {'-'*15}")
        for t in closed:
            print(f"  {(t.get('symbol') or '?'):<8s} {(t.get('side') or '?'):<6s} "
                  f"{(t.get('entry_price') or 0):>8.2f} {(t.get('exit_price') or 0):>8.2f} "
                  f"${(t.get('realized_pnl') or 0):>7.2f} "
                  f"{(t.get('realized_pnl_pct') or 0):>+5.1f}% "
                  f"{(t.get('exit_reason') or '?')}")

    # P&L summary
    print(_section("P&L SUMMARY"))
    pnl = report.get("pnl_summary", {})
    print(f"  Today:       {pnl.get('today_closed', 0)} closed "
          f"({pnl.get('today_wins', 0)}W / {pnl.get('today_losses', 0)}L)  "
          f"realized: ${pnl.get('today_realized_pnl', 0):.2f}")
    print(f"  Open:        {pnl.get('open_count', 0)} positions  "
          f"notional: ${pnl.get('open_notional', 0):,.2f}  "
          f"unrealized: ${pnl.get('unrealized_pnl', 0):,.2f}")
    agg = pnl.get("30d", {})
    if agg:
        total_30 = agg.get("total", 0) or 0
        wins_30 = agg.get("wins", 0) or 0
        wr = round(100 * wins_30 / total_30, 1) if total_30 > 0 else 0
        avg_w = agg.get("avg_winner") or 0
        avg_l = agg.get("avg_loser") or 0
        avg_h = agg.get("avg_hold_hours") or 0
        print(f"  30-day:      {total_30} trades  WR={wr}%  "
              f"P&L: ${agg.get('total_pnl', 0) or 0:.2f}  "
              f"avg: ${agg.get('avg_pnl', 0) or 0:.2f}")
        print(f"               avg winner: ${avg_w:.2f}  avg loser: ${avg_l:.2f}  "
              f"avg hold: {avg_h:.1f}h")

    # P&L by source
    print(_section("P&L BY SOURCE/STRATEGY (30d)"))
    pnl_src = report.get("pnl_by_source", [])
    if not pnl_src:
        print("  No closed trades in last 30 days.")
    else:
        print(f"  {'Source':<35s} {'Trades':>6s} {'WR':>5s} {'P&L':>9s} {'AvgW':>8s} {'AvgL':>8s} {'Hold':>5s}")
        print(f"  {'-'*35} {'-'*6} {'-'*5} {'-'*9} {'-'*8} {'-'*8} {'-'*5}")
        for s in pnl_src:
            trades = s.get("trades", 0) or 0
            w = s.get("wins", 0) or 0
            wr = f"{100*w/trades:.0f}%" if trades > 0 else "—"
            print(f"  {(s.get('source') or '?'):<35s} "
                  f"{trades:>6d} {wr:>5s} "
                  f"${(s.get('total_pnl') or 0):>8.2f} "
                  f"${(s.get('avg_winner') or 0):>7.2f} "
                  f"${(s.get('avg_loser') or 0):>7.2f} "
                  f"{(s.get('avg_hold_hours') or 0):>4.0f}h")

    # Skip telemetry
    print(_section("SKIP TELEMETRY"))
    skips = report.get("skip_telemetry", [])
    if not skips:
        print("  No skip events recorded.")
    else:
        print(f"  {'Subsystem':<20s} {'Reason':<25s} {'Count':>6s}  Detail")
        print(f"  {'-'*20} {'-'*25} {'-'*6}  {'-'*30}")
        for s in skips:
            detail = (s.get("detail") or "")[:40]
            print(f"  {s['subsystem']:<20s} {s['reason']:<25s} {s['count']:>6d}  {detail}")

    # Zero-output explainability
    explanations = report.get("zero_output_explanation", [])
    if explanations:
        print(_section("WHY NO/LOW TRADE ACTIVITY"))
        for i, exp in enumerate(explanations, 1):
            print(f"  {i}. {exp}")

    print()


# ===================================================================
# EOD EVALUATION — answers the 6 key questions
# ===================================================================

def print_eod_summary(report: dict) -> None:
    """Concise EOD evaluation answering the 6 key questions."""
    print(_section("END-OF-DAY EVALUATION SUMMARY"))
    print(f"  Date: {report['report_date']}")
    print()

    ideas = report.get("ideas", {})
    pt = report.get("paper_trades", {})
    pnl = report.get("pnl_summary", {})
    funnel = report.get("trade_funnel", {})
    skips = report.get("skip_telemetry", [])

    # Q1: Did the system produce ideas?
    total_ideas = sum(ideas.values())
    print(f"  1. IDEAS PRODUCED:  {total_ideas} total "
          f"(BUY={ideas.get('swing_buy',0)}, SHORT={ideas.get('swing_short',0)}, "
          f"TL={ideas.get('triple_lock',0)})")

    # Q2: Did it place paper trades?
    entered = pt.get("entered_today", 0)
    print(f"  2. TRADES PLACED:   {entered} today")
    for src, cnt in sorted(pt.get("by_source", {}).items(), key=lambda x: -x[1]):
        print(f"     - {src}: {cnt}")

    # Q3: Which strategies fired?
    fired = [sub for sub, stages in funnel.items() if stages.get("entered", 0) > 0]
    print(f"  3. STRATEGIES FIRED: {', '.join(fired) if fired else 'none'}")

    # Q4: Which strategies were blocked?
    blocked_subs = set()
    for s in skips:
        if s["reason"] in ("MODEL_UNAVAILABLE", "REGIME_BLOCKED", "IBKR_DISCONNECTED", "LOCK_TIMEOUT"):
            blocked_subs.add(s["subsystem"])
    blocked_no_fire = [sub for sub, stages in funnel.items()
                       if stages.get("candidates", 0) > 0 and stages.get("entered", 0) == 0]
    all_blocked = sorted(set(blocked_subs) | set(blocked_no_fire))
    print(f"  4. STRATEGIES BLOCKED: {', '.join(all_blocked) if all_blocked else 'none'}")

    # Q5: What made money today?
    closed = report.get("closed_trades", [])
    winners = [t for t in closed if (t.get("realized_pnl") or 0) > 0]
    losers = [t for t in closed if (t.get("realized_pnl") or 0) <= 0]
    print(f"  5. TODAY'S P&L:     ${pnl.get('today_realized_pnl', 0):.2f} realized  "
          f"({len(winners)}W / {len(losers)}L)")
    print(f"     Unrealized:      ${pnl.get('unrealized_pnl', 0):.2f}")
    for w in winners:
        print(f"     + {w['symbol']:<6s} ${w.get('realized_pnl',0):>+8.2f}  ({w.get('recommendation_source','')})")
    for l in losers:
        print(f"     - {l['symbol']:<6s} ${l.get('realized_pnl',0):>+8.2f}  ({l.get('recommendation_source','')})")

    # Q6: What failed operationally?
    explanations = report.get("zero_output_explanation", [])
    print(f"  6. OPERATIONAL ISSUES:")
    if not explanations and not all_blocked:
        print("     None — all systems operational")
    for exp in explanations:
        print(f"     - {exp}")

    print()


def main():
    parser = argparse.ArgumentParser(description="Daily evidence / EOD evaluation report")
    parser.add_argument("--date", default=None, help="Trade date (YYYY-MM-DD)")
    parser.add_argument("--json", action="store_true", help="JSON output")
    parser.add_argument("--eod", action="store_true", help="EOD evaluation summary mode")
    args = parser.parse_args()

    report = build_report(args.date)

    if args.json:
        print(json.dumps(report, indent=2, default=str))
    elif args.eod:
        print_eod_summary(report)
        print_report(report, eod_mode=True)
    else:
        print_report(report)


if __name__ == "__main__":
    main()
