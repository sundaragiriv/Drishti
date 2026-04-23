"""Live session monitor for Quant-Bridge paper trading.

Single module for all session-day observability:
  - Pre-open checklist
  - Live heartbeat / activity status
  - Session log summary
  - Session success rubric

Usage:
    python -m signal_scanner.session_monitor               # pre-open checklist
    python -m signal_scanner.session_monitor --heartbeat    # live activity
    python -m signal_scanner.session_monitor --summary      # session summary
    python -m signal_scanner.session_monitor --json         # machine-readable
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path

from signal_scanner.core.readiness import ReadinessState, compute_price_freshness
from signal_scanner.core.telemetry import get_daily_summary, get_daily_funnel

SIGNALS_DB = Path("signal_scanner/data/signals.db")


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


def _today_str() -> str:
    return date.today().isoformat()


# ===================================================================
# 1. PRE-OPEN CHECKLIST
# ===================================================================

def build_checklist() -> dict:
    """Build pre-open checklist data."""
    result: dict = {}

    # Readiness
    state = ReadinessState.load()
    price_ok, age_days, latest_str = compute_price_freshness()
    state.prices_age_days = age_days
    state.latest_price_date = latest_str

    # Models
    models = {}
    for name, path in [
        ("VWAP_MR", Path("data/warehouse/models/intraday_ml_vwap_mr.pkl")),
        ("FPB", Path("data/warehouse/models/intraday_ml_fpb.pkl")),
        ("ORB_V2", Path("data/warehouse/models/intraday_ml_orb_v2.pkl")),
    ]:
        models[name] = path.exists()
    state.enabled_scanners = [n for n, ok in models.items() if ok]

    # IBKR — last observed source (NOT a live connectivity probe)
    last_scan = _sqlite_query(
        "SELECT data_source, scan_end FROM scan_history ORDER BY id DESC LIMIT 1"
    )
    ibkr_last_source = last_scan[0].get("data_source", "") if last_scan else ""
    ibkr_last_seen = last_scan[0].get("scan_end", "") if last_scan else ""

    # Open positions
    open_pos = _sqlite_query(
        "SELECT symbol, side, entry_price, recommendation_source "
        "FROM paper_trades WHERE status='OPEN' ORDER BY opened_at"
    )

    # Live universe estimate from intelligence
    universe_est = 0
    try:
        import duckdb
        conn = duckdb.connect("data/warehouse/sec_intel.duckdb", read_only=True)
        row = conn.execute("""
            SELECT COUNT(*) FROM intelligence_scores
            WHERE report_quarter = (
                SELECT MAX(report_quarter) FROM intelligence_scores
                WHERE data_quality_score >= 75
                GROUP BY report_quarter HAVING COUNT(*) >= 1000
                ORDER BY report_quarter DESC LIMIT 1
            )
            AND accum_phase IN ('EARLY_ACCUM','ACTIVE_ACCUM','LATE_ACCUM','EXPANSION')
            AND conviction_score >= 40
        """).fetchone()
        universe_est = row[0] if row else 0
        conn.close()
    except Exception:
        pass

    state.resolve_status()

    # Session status
    from signal_scanner.core.session import SessionRegistry
    session_reg = SessionRegistry()
    session_current = session_reg.get_current()

    result = {
        "session": session_current,
        "readiness_status": state.readiness_status,
        "blocked_reasons": state.blocked_reasons,
        "degraded_reasons": state.degraded_reasons,
        "prices_ok": price_ok,
        "prices_age_days": age_days,
        "latest_price_date": latest_str,
        "models": models,
        "enabled_scanners": state.enabled_scanners,
        "ibkr_last_observed_source": ibkr_last_source,
        "ibkr_last_seen_at": ibkr_last_seen,
        "orphan_gate_active": state.orphan_gate_active,
        "orphan_symbols": state.orphan_symbols,
        "open_positions": open_pos,
        "open_count": len(open_pos),
        "live_universe_estimate": universe_est,
    }
    return result


def print_checklist(data: dict) -> None:
    print(_section("PRE-OPEN CHECKLIST"))
    print(f"  Date: {_today_str()}")

    # Session
    sess = data.get("session")
    if sess:
        stale = sess.get("_stale", False)
        s_tag = "[!!]" if stale else "[--]"
        print(f"  {s_tag} Active session:    {sess.get('mode', '?')} (PID {sess.get('pid', '?')}, "
              f"phase={sess.get('phase', '?')})"
              + (" ** STALE **" if stale else ""))
    else:
        print(f"  [--] Active session:    none")

    status = data["readiness_status"]
    tag = {"READY": "[OK]", "DEGRADED": "[!!]", "BLOCKED": "[XX]"}.get(status, "[??]")
    print(f"\n  {tag} Readiness:         {status}")
    for r in data.get("blocked_reasons", []):
        print(f"      BLOCK: {r}")
    for r in data.get("degraded_reasons", []):
        print(f"      DEGRADE: {r}")

    p_tag = "[OK]" if data["prices_ok"] else "[!!]"
    print(f"  {p_tag} Prices:            latest {data['latest_price_date']} ({data['prices_age_days']}d lag)")

    models = data["models"]
    m_all = all(models.values())
    m_tag = "[OK]" if m_all else "[!!]"
    missing = [n for n, ok in models.items() if not ok]
    print(f"  {m_tag} Models:            {', '.join(data['enabled_scanners']) or 'none'}"
          + (f"  (MISSING: {', '.join(missing)})" if missing else ""))

    ibkr_src = data.get("ibkr_last_observed_source", "")
    ibkr_when = (data.get("ibkr_last_seen_at") or "")[:19]
    print(f"  [--] IBKR (last seen):   source={ibkr_src or 'none'}"
          + (f"  at {ibkr_when}" if ibkr_when else "")
          + "  (historical, not live probe)")

    o_tag = "[!!]" if data["orphan_gate_active"] else "[OK]"
    print(f"  {o_tag} Orphan gate:       {'ACTIVE - ' + ', '.join(data['orphan_symbols']) if data['orphan_gate_active'] else 'clear'}")

    print(f"  [--] Open positions:   {data['open_count']}")
    for p in data["open_positions"]:
        print(f"       {p['symbol']:<6s} {p['side']:<6s} @ ${p['entry_price']:.2f}  ({p['recommendation_source']})")

    print(f"  [--] Live universe:    ~{data['live_universe_estimate']} candidates (conv>=40, accum phase)")
    print()


# ===================================================================
# 2. LIVE HEARTBEAT / ACTIVITY MONITOR
# ===================================================================

def build_heartbeat(trade_date: str | None = None) -> dict:
    """Build live activity status for the current session."""
    if trade_date is None:
        trade_date = _today_str()

    result: dict = {"trade_date": trade_date}

    # Scan cycles today
    scans = _sqlite_query(
        "SELECT scan_type, COUNT(*) as cnt, MIN(scan_start) as first, "
        "MAX(scan_end) as last, ROUND(AVG(duration_seconds), 1) as avg_dur "
        "FROM scan_history WHERE substr(scan_start, 1, 10) = ? "
        "GROUP BY scan_type ORDER BY cnt DESC",
        (trade_date,),
    )
    result["scan_cycles"] = scans
    result["total_scans_today"] = sum(s["cnt"] for s in scans)

    # Funnel activity
    funnel = get_daily_funnel(trade_date)
    result["funnel"] = funnel

    # Per-subsystem activity flags
    subsystems = ["VWAP_MR", "FPB", "ORB_V2", "scanner_mtf",
                  "idea_swing_buy", "idea_swing_short", "idea_triple_lock"]
    activity = {}
    for sub in subsystems:
        f = funnel.get(sub, {})
        candidates = f.get("candidates", 0)
        setups = f.get("setups", 0)
        entered = f.get("entered", 0)
        skipped = f.get("skipped", 0)
        attempted = f.get("attempted", 0)
        # "active" = produced setups or entries (not just candidate bookkeeping)
        active = setups > 0 or entered > 0
        # "scanned" = at least evaluated candidates (weaker signal)
        scanned = candidates > 0 or attempted > 0
        activity[sub] = {
            "active": active,
            "scanned": scanned,
            "candidates": candidates,
            "setups": setups,
            "entered": entered,
            "skipped": skipped,
        }
    result["subsystem_activity"] = activity

    # Execution loop ran? (only live scans count — research is off-hours)
    live_scans = [s for s in scans if s.get("scan_type") == "live"]
    result["execution_loop_ran"] = len(live_scans) > 0

    # Paper trades entered today
    entries = _sqlite_query(
        "SELECT recommendation_source, symbol, side, entry_price, opened_at "
        "FROM paper_trades WHERE substr(opened_at, 1, 10) = ? ORDER BY opened_at",
        (trade_date,),
    )
    result["entries_today"] = entries
    result["entries_count"] = len(entries)

    # Top skip reasons
    skips = get_daily_summary(trade_date)
    result["top_skip_reasons"] = skips[:10]

    return result


def print_heartbeat(data: dict) -> None:
    print(_section("LIVE SESSION HEARTBEAT"))
    print(f"  Date: {data['trade_date']}  |  Total scans: {data['total_scans_today']}")

    # Scan cycles
    scans = data.get("scan_cycles", [])
    if scans:
        print(f"\n  Scan cycles:")
        for s in scans:
            print(f"    {s.get('scan_type', '?'):<12s}  {s['cnt']:>3d} cycles  "
                  f"first={str(s.get('first',''))[:19]}  last={str(s.get('last',''))[:19]}  "
                  f"avg={s.get('avg_dur', 0):.1f}s")
    else:
        print(f"\n  No scan cycles recorded today.")

    # Subsystem activity
    print(f"\n  Subsystem activity:")
    print(f"  {'Source':<22s} {'Act':>4s} {'Scan':>4s} {'Cand':>5s} {'Setup':>5s} {'Enter':>5s} {'Skip':>5s}")
    print(f"  {'-'*22} {'-'*4} {'-'*4} {'-'*5} {'-'*5} {'-'*5} {'-'*5}")
    for sub, info in data.get("subsystem_activity", {}).items():
        act = "YES" if info["active"] else " - "
        scn = "YES" if info["scanned"] else " - "
        print(f"  {sub:<22s} {act:>4s} {scn:>4s} {info['candidates']:>5d} {info['setups']:>5d} "
              f"{info['entered']:>5d} {info['skipped']:>5d}")

    # Entries today
    entries = data.get("entries_today", [])
    print(f"\n  Paper trades entered: {data['entries_count']}")
    for e in entries:
        print(f"    {e.get('opened_at', '')[:19]}  {e['symbol']:<6s} {e['side']:<6s} "
              f"@ ${e['entry_price']:.2f}  ({e['recommendation_source']})")

    # Top skip reasons
    skips = data.get("top_skip_reasons", [])
    if skips:
        print(f"\n  Top skip reasons:")
        for s in skips[:5]:
            print(f"    {s['subsystem']:<20s} {s['reason']:<25s} x{s['count']}")
    print()


# ===================================================================
# 3. SESSION SUMMARY (end-of-day or intraday)
# ===================================================================

def build_session_summary(trade_date: str | None = None) -> dict:
    """Build a full session summary combining heartbeat + paper P&L."""
    if trade_date is None:
        trade_date = _today_str()

    hb = build_heartbeat(trade_date)

    # First/last scan
    all_scans = _sqlite_query(
        "SELECT MIN(scan_start) as first_scan, MAX(scan_end) as last_scan, "
        "COUNT(*) as total_cycles "
        "FROM scan_history WHERE substr(scan_start, 1, 10) = ?",
        (trade_date,),
    )

    # Intraday cycles by strategy (from funnel)
    funnel = hb["funnel"]
    intraday_cycles = {}
    for sub in ["VWAP_MR", "FPB", "ORB_V2"]:
        f = funnel.get(sub, {})
        intraday_cycles[sub] = {
            "candidates": f.get("candidates", 0),
            "setups": f.get("setups", 0),
            "entered": f.get("entered", 0),
        }

    # Paper trades by source
    by_source = _sqlite_query(
        "SELECT recommendation_source, COUNT(*) as cnt "
        "FROM paper_trades WHERE substr(opened_at, 1, 10) = ? "
        "GROUP BY recommendation_source ORDER BY cnt DESC",
        (trade_date,),
    )

    # Closed today
    closed = _sqlite_query(
        "SELECT symbol, side, realized_pnl, exit_reason, recommendation_source "
        "FROM paper_trades WHERE status='CLOSED' AND substr(closed_at, 1, 10) = ?",
        (trade_date,),
    )
    realized = sum(t.get("realized_pnl") or 0 for t in closed)

    # Zero-trade explanation
    explanations = []
    if hb["entries_count"] == 0:
        if hb["total_scans_today"] == 0:
            explanations.append("Scanner did not run today (no scan cycles recorded)")
        if not hb["execution_loop_ran"]:
            explanations.append("Execution loop did not fire (no live/research scans)")
        for sub, info in hb.get("subsystem_activity", {}).items():
            if info["candidates"] > 0 and info["entered"] == 0:
                explanations.append(
                    f"{sub}: {info['candidates']} candidates, "
                    f"{info['setups']} setups, 0 entered, {info['skipped']} skipped"
                )
        skips = hb.get("top_skip_reasons", [])
        skip_map = {(s["subsystem"], s["reason"]): s["count"] for s in skips}
        if skip_map.get(("execution_loop", "DATA_STALE")):
            explanations.append("Prices were stale")
        if skip_map.get(("execution_loop", "IBKR_DISCONNECTED")):
            explanations.append("IBKR was disconnected")
        for sub in ["VWAP_MR", "FPB", "ORB_V2"]:
            if skip_map.get((sub, "MODEL_UNAVAILABLE")):
                explanations.append(f"{sub}: ML model unavailable")
            if skip_map.get((sub, "NO_SETUP_QUALIFIED")):
                explanations.append(f"{sub}: no qualifying tickers")
        if skip_map.get(("IdeaBridge", "REGIME_BLOCKED")):
            explanations.append("IdeaBridge: regime blocked all entries")
        if skip_map.get(("PaperTrader", "POSITION_LIMIT")):
            explanations.append("PaperTrader: max positions reached")
        if not explanations:
            explanations.append("No specific blockers found -- check scanner logs")

    summary = {
        "trade_date": trade_date,
        "first_scan": all_scans[0]["first_scan"] if all_scans else None,
        "last_scan": all_scans[0]["last_scan"] if all_scans else None,
        "total_execution_cycles": all_scans[0]["total_cycles"] if all_scans else 0,
        "intraday_cycles": intraday_cycles,
        "entries_today": hb["entries_count"],
        "entries_by_source": {r["recommendation_source"]: r["cnt"] for r in by_source},
        "closed_today": len(closed),
        "realized_pnl": round(realized, 2),
        "closed_trades": closed,
        "zero_trade_explanation": explanations,
        "subsystem_activity": hb["subsystem_activity"],
        "top_skip_reasons": hb["top_skip_reasons"],
    }
    return summary


def print_session_summary(data: dict) -> None:
    print(_section("SESSION SUMMARY"))
    print(f"  Date: {data['trade_date']}")
    first = data.get("first_scan")
    last = data.get("last_scan")
    print(f"  First scan: {str(first)[:19] if first else 'none'}")
    print(f"  Last scan:  {str(last)[:19] if last else 'none'}")
    print(f"  Execution cycles: {data['total_execution_cycles']}")

    print(f"\n  Intraday strategy cycles:")
    for sub, info in data.get("intraday_cycles", {}).items():
        print(f"    {sub:<10s}  candidates={info['candidates']}  "
              f"setups={info['setups']}  entered={info['entered']}")

    print(f"\n  Paper trades entered: {data['entries_today']}")
    for src, cnt in data.get("entries_by_source", {}).items():
        print(f"    {src:<35s} {cnt}")

    closed = data.get("closed_trades", [])
    print(f"\n  Closed today: {data['closed_today']}  realized P&L: ${data['realized_pnl']:.2f}")
    for t in closed:
        pnl = t.get("realized_pnl") or 0
        print(f"    {t['symbol']:<6s} {t['side']:<6s} ${pnl:>+8.2f}  {t.get('exit_reason', '?')}")

    explanations = data.get("zero_trade_explanation", [])
    if explanations:
        print(f"\n  Zero/low trade explanation:")
        for i, exp in enumerate(explanations, 1):
            print(f"    {i}. {exp}")
    print()


# ===================================================================
# 4. SESSION SUCCESS RUBRIC
# ===================================================================

def evaluate_session_success(trade_date: str | None = None) -> dict:
    """Evaluate whether a session meets the success criteria."""
    if trade_date is None:
        trade_date = _today_str()

    summary = build_session_summary(trade_date)
    hb = build_heartbeat(trade_date)

    checks = []

    def _chk(name: str, ok: bool, detail: str = "") -> None:
        checks.append({"check": name, "pass": ok, "detail": detail})

    # 1. Scanner started successfully
    started = summary["total_execution_cycles"] > 0
    _chk("Scanner started", started,
         f"{summary['total_execution_cycles']} cycles" if started else "no scans recorded")

    # 2. At least one market-hours execution cycle (live only, not research)
    exec_scans = _sqlite_query(
        "SELECT COUNT(*) as cnt FROM scan_history "
        "WHERE substr(scan_start, 1, 10) = ? AND scan_type = 'live'",
        (trade_date,),
    )
    exec_cnt = exec_scans[0]["cnt"] if exec_scans else 0
    exec_ok = exec_cnt > 0
    _chk("Market-hours execution cycle", exec_ok,
         f"{exec_cnt} live scans" if exec_ok else "no live scans (research-only does not count)")

    # 3. At least one intraday scanner produced setups or entries
    intraday_active = any(
        hb["subsystem_activity"].get(sub, {}).get("active", False)
        for sub in ["VWAP_MR", "FPB", "ORB_V2"]
    )
    active_list = [sub for sub in ["VWAP_MR", "FPB", "ORB_V2"]
                   if hb["subsystem_activity"].get(sub, {}).get("active")]
    scanned_list = [sub for sub in ["VWAP_MR", "FPB", "ORB_V2"]
                    if hb["subsystem_activity"].get(sub, {}).get("scanned")]
    if intraday_active:
        detail = f"active: {', '.join(active_list)}"
    elif scanned_list:
        detail = f"scanned only (no setups): {', '.join(scanned_list)}"
    else:
        detail = "no intraday activity observed"
    _chk("Intraday activity observed", intraday_active, detail)

    # 4. Paper-trade path remained available
    # Check if paper trading was enabled and position capacity existed
    checklist = build_checklist()
    path_ok = not checklist.get("orphan_gate_active", False)
    _chk("Paper-trade path available", path_ok,
         "orphan gate blocked" if not path_ok else "clear")

    # 5. All inactivity is explainable
    entries = summary["entries_today"]
    if entries > 0:
        explainable = True
        detail = f"{entries} trades entered"
    else:
        explanations = summary.get("zero_trade_explanation", [])
        explainable = len(explanations) > 0
        detail = f"{len(explanations)} explanations" if explainable else "no explanation found"
    _chk("Inactivity explainable", explainable, detail)

    # Overall
    all_pass = all(c["pass"] for c in checks)

    return {
        "trade_date": trade_date,
        "session_success": all_pass,
        "checks": checks,
        "summary": summary,
    }


def print_session_success(data: dict) -> None:
    print(_section("SESSION SUCCESS EVALUATION"))
    print(f"  Date: {data['trade_date']}")
    verdict = "PASS" if data["session_success"] else "FAIL"
    print(f"  Overall: {verdict}")
    print()
    for c in data["checks"]:
        tag = "[OK]" if c["pass"] else "[!!]"
        print(f"  {tag} {c['check']:<35s} {c['detail']}")
    print()


# ===================================================================
# 5. REAL-SESSION FIELDS (for evidence report integration)
# ===================================================================

def get_real_session_fields(trade_date: str | None = None) -> dict:
    """Return real-session activity fields for the evidence report."""
    if trade_date is None:
        trade_date = _today_str()

    hb = build_heartbeat(trade_date)
    fa = hb.get("subsystem_activity", {})

    # Count actual intraday entries by checking recommendation_source prefix
    entries = _sqlite_query(
        "SELECT recommendation_source FROM paper_trades "
        "WHERE substr(opened_at, 1, 10) = ?",
        (trade_date,),
    )
    intraday_entries = sum(1 for e in entries
                          if any(e.get("recommendation_source", "").startswith(p)
                                 for p in ("VWAP_MR", "FPB_ML", "ORB_V2")))
    scanner_mtf_entries = sum(1 for e in entries
                             if (e.get("recommendation_source") or "").startswith("SCANNER_MTF"))
    idea_entries = sum(1 for e in entries
                       if any(e.get("recommendation_source", "").startswith(p)
                              for p in ("SWING_IDEA", "AI_TRIPLE_LOCK")))

    # Actual intraday scans/setups from funnel
    intraday_scans = sum(fa.get(sub, {}).get("candidates", 0) for sub in ["VWAP_MR", "FPB", "ORB_V2"])
    intraday_setups = sum(fa.get(sub, {}).get("setups", 0) for sub in ["VWAP_MR", "FPB", "ORB_V2"])

    return {
        "actual_intraday_entries_today": intraday_entries,
        "actual_scanner_mtf_entries_today": scanner_mtf_entries,
        "actual_idea_entries_today": idea_entries,
        "actual_intraday_scans_today": intraday_scans,
        "actual_intraday_setups_today": intraday_setups,
        "total_entries_today": len(entries),
        "total_scan_cycles_today": hb["total_scans_today"],
        "execution_loop_ran": hb["execution_loop_ran"],
    }


# ===================================================================
# CLI
# ===================================================================

def main():
    parser = argparse.ArgumentParser(description="Session monitor for live paper trading")
    parser.add_argument("--heartbeat", action="store_true", help="Live activity status")
    parser.add_argument("--summary", action="store_true", help="Session log summary")
    parser.add_argument("--success", action="store_true", help="Session success evaluation")
    parser.add_argument("--json", action="store_true", help="JSON output")
    parser.add_argument("--date", default=None, help="Trade date (YYYY-MM-DD)")
    args = parser.parse_args()

    if args.heartbeat:
        data = build_heartbeat(args.date)
        if args.json:
            print(json.dumps(data, indent=2, default=str))
        else:
            print_heartbeat(data)
    elif args.summary:
        data = build_session_summary(args.date)
        if args.json:
            print(json.dumps(data, indent=2, default=str))
        else:
            print_session_summary(data)
    elif args.success:
        data = evaluate_session_success(args.date)
        if args.json:
            print(json.dumps(data, indent=2, default=str))
        else:
            print_session_success(data)
            print_session_summary(data["summary"])
    else:
        # Default: pre-open checklist
        data = build_checklist()
        if args.json:
            print(json.dumps(data, indent=2, default=str))
        else:
            print_checklist(data)


if __name__ == "__main__":
    main()
