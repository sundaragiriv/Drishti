"""7 AM Daily Health Check for Quant-Bridge.

Checks all system components and prints an actionable status report.
Run each morning before market open to ensure the system is ready.

Usage:
    python -m signal_scanner.daily_health_check
    python -m signal_scanner.daily_health_check --json      # machine-readable output
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from signal_scanner.core.readiness import business_day_lag, latest_complete_trading_day

# ---------------------------------------------------------------------------
# Status constants
# ---------------------------------------------------------------------------
OK = "OK"
WARN = "WARN"
FAIL = "FAIL"
INFO = "INFO"

STATUS_ICON = {OK: "[OK]", WARN: "[WARN]", FAIL: "[FAIL]", INFO: "[INFO]"}
STATUS_ORDER = {OK: 0, WARN: 1, FAIL: 2}


class HealthResult:
    def __init__(self, component: str, status: str, message: str, detail: str = ""):
        self.component = component
        self.status = status
        self.message = message
        self.detail = detail

    def __repr__(self):
        icon = STATUS_ICON.get(self.status, "[ ? ]")
        base = f"{icon} {self.component:35s} {self.message}"
        if self.detail:
            base += f"\n       {' '*35}  -> {self.detail}"
        return base


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------

def check_duckdb() -> List[HealthResult]:
    results = []
    try:
        import duckdb
        db_path = Path(__file__).resolve().parents[1] / "data" / "warehouse" / "sec_intel.duckdb"
        conn = duckdb.connect(str(db_path), read_only=True)
        tables = conn.execute("SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='main'").fetchone()[0]
        conn.close()
        results.append(HealthResult("DuckDB", OK, f"Connected | {tables} tables", str(db_path)))
    except Exception as exc:
        results.append(HealthResult("DuckDB", FAIL, f"Cannot connect: {exc}"))
    return results


def check_data_freshness() -> List[HealthResult]:
    results = []
    today = date.today()
    expected_trade_day = latest_complete_trading_day(today)

    try:
        import duckdb
        db_path = Path(__file__).resolve().parents[1] / "data" / "warehouse" / "sec_intel.duckdb"
        conn = duckdb.connect(str(db_path), read_only=True)

        # Daily prices
        r = conn.execute("SELECT MAX(trade_date), COUNT(DISTINCT ticker) FROM fact_daily_prices").fetchone()
        max_price_date, price_tickers = r
        lag = business_day_lag(max_price_date, today) if max_price_date else 999
        if lag == 0:
            results.append(HealthResult("Daily Prices", OK,
                f"Latest {max_price_date} | {price_tickers:,} tickers"))
        elif lag <= 2:
            results.append(HealthResult("Daily Prices", WARN,
                f"Latest {max_price_date} ({lag} trading-day lag vs {expected_trade_day}) | {price_tickers:,} tickers",
                "Run: python run_premarket.py"))
        else:
            results.append(HealthResult("Daily Prices", FAIL,
                f"STALE: Latest {max_price_date} ({lag} trading-day lag vs {expected_trade_day}) | {price_tickers:,} tickers",
                "Run: python run_premarket.py"))

        # Intelligence scores — show the best quality quarter (quality >= 75)
        r = conn.execute("""
            SELECT report_quarter, COUNT(*), AVG(conviction_score)::INT,
                   SUM(CASE WHEN accum_phase='ACTIVE_ACCUM' THEN 1 ELSE 0 END)
            FROM intelligence_scores
            WHERE COALESCE(data_quality_score, 100.0) >= 75
            GROUP BY report_quarter ORDER BY report_quarter DESC LIMIT 1
        """).fetchone()
        if r:
            iq, cnt, avg_conv, active = r
            if avg_conv >= 30 and cnt >= 2000:
                results.append(HealthResult("Intelligence Scores", OK,
                    f"{iq} | {cnt:,} tickers | avg_conv={avg_conv} | active_accum={active}"))
            else:
                results.append(HealthResult("Intelligence Scores", WARN,
                    f"{iq} | {cnt:,} tickers | avg_conv={avg_conv} | ACTIVE_ACCUM={active}",
                    "Run: run_pipeline --stage intelligence after 13F ingest"))
        else:
            results.append(HealthResult("Intelligence Scores", FAIL, "No scores found"))

        # Active quarter
        try:
            from signal_scanner.institutional_intel.config import get_active_quarter
            aq = get_active_quarter(conn)
            results.append(HealthResult("Active Quarter", INFO if aq else WARN,
                str(aq) if aq else "None — all quarters fail quality threshold"))
        except Exception as exc:
            results.append(HealthResult("Active Quarter", WARN, f"Error: {exc}"))

        # Short volume freshness
        r = conn.execute("SELECT MAX(trade_date), COUNT(DISTINCT ticker) FROM fact_short_volume").fetchone()
        if r[0]:
            lag = business_day_lag(r[0], today)
            st = OK if lag <= 1 else WARN
            results.append(HealthResult("Short Volume (FINRA)", st,
                f"Latest {r[0]} ({lag} trading-day lag) | {r[1]:,} tickers"))
        else:
            results.append(HealthResult("Short Volume (FINRA)", WARN, "No data"))

        # Dark pool freshness
        r = conn.execute("SELECT MAX(trade_date), COUNT(DISTINCT ticker) FROM fact_dark_pool_daily").fetchone()
        if r[0]:
            lag = business_day_lag(r[0], today)
            st = OK if lag <= 1 else WARN
            results.append(HealthResult("Dark Pool (FINRA-derived)", st,
                f"Latest {r[0]} ({lag} trading-day lag) | {r[1]:,} tickers"))
        else:
            results.append(HealthResult("Dark Pool (FINRA-derived)", WARN,
                "No data — run: short_data_loader --mode dark-pool"))

        # Cost-to-borrow
        r = conn.execute("SELECT MAX(report_date), COUNT(DISTINCT ticker) FROM fact_cost_to_borrow").fetchone()
        if r[0]:
            lag = business_day_lag(r[0], today)
            st = OK if lag == 0 else WARN
            results.append(HealthResult("Cost-to-Borrow (yfinance)", st,
                f"Latest {r[0]} ({lag} trading-day lag) | {r[1]:,} tickers"))
        else:
            results.append(HealthResult("Cost-to-Borrow (yfinance)", WARN,
                "No data — run: short_data_loader --mode ctb"))

        # Options flow
        r = conn.execute("SELECT MAX(snapshot_date), COUNT(DISTINCT ticker) FROM fact_options_flow").fetchone()
        if r[0]:
            lag = business_day_lag(r[0], today)
            st = OK if lag <= 1 else WARN
            results.append(HealthResult("Options Flow (Polygon)", st,
                f"Latest {r[0]} ({lag} trading-day lag) | {r[1]:,} tickers"))
        else:
            results.append(HealthResult("Options Flow (Polygon)", WARN,
                "No data — run: options_flow_loader"))

        # News sentiment
        r = conn.execute("""
            SELECT DATE(MAX(published_at)), COUNT(DISTINCT ticker)
            FROM fact_news_sentiment
        """).fetchone()
        if r[0]:
            lag = business_day_lag(r[0], today)
            st = OK if lag <= 1 else WARN
            results.append(HealthResult("News Sentiment (Polygon)", st,
                f"Latest {r[0]} ({lag} trading-day lag) | {r[1]:,} tickers"))
        else:
            results.append(HealthResult("News Sentiment (Polygon)", WARN,
                "No data — run: news_sentiment_loader"))

        # 13F positions freshness
        r = conn.execute("""
            SELECT MAX(report_period), COUNT(*), COUNT(DISTINCT manager_cik)
            FROM fact_13f_positions
        """).fetchone()
        if r[0]:
            q_lag_days = (today - r[0]).days
            st = OK if q_lag_days <= 95 else WARN  # 13F filed 45d after Q-end
            results.append(HealthResult("13F Positions", st,
                f"Latest period {r[0]} | {r[1]:,} rows | {r[2]:,} funds",
                "Expected: ~3M rows for a full quarter"))
        else:
            results.append(HealthResult("13F Positions", FAIL, "No data"))

        # 8-K material events
        try:
            r = conn.execute("""
                SELECT MAX(filed_date), COUNT(*), COUNT(DISTINCT ticker)
                FROM fact_form8k_events
            """).fetchone()
            if r[0]:
                lag = business_day_lag(r[0], today)
                st = OK if lag <= 3 else WARN
                results.append(HealthResult("8-K Material Events", st,
                    f"Latest {r[0]} ({lag} trading-day lag) | {r[1]:,} filings | {r[2]:,} tickers"))
            else:
                results.append(HealthResult("8-K Material Events", WARN,
                    "No data — run: daily_8k_refresh --days 30"))
        except Exception:
            results.append(HealthResult("8-K Material Events", INFO, "Table not yet created"))

        conn.close()

    except Exception as exc:
        results.append(HealthResult("Data Freshness", FAIL, f"Error: {exc}"))

    return results


def check_ml_models() -> List[HealthResult]:
    results = []
    model_dir = Path(__file__).resolve().parents[1] / "data" / "warehouse" / "models"
    models = {
        "VWAP_MR (val AUC=0.823)": model_dir / "intraday_ml_vwap_mr.pkl",
        "FPB (val AUC=0.856)": model_dir / "intraday_ml_fpb.pkl",
        "ORB_V2 (val AUC=0.731)": model_dir / "intraday_ml_orb_v2.pkl",
        "Swing ML v2 (val AUC=0.560)": Path(__file__).resolve().parents[1] / "data" / "models" / "ml_signal_v2.pkl",
    }
    for name, path in models.items():
        fname = path.name
        if path.exists():
            size_kb = path.stat().st_size / 1024
            results.append(HealthResult(f"ML Model: {name}", OK, f"{fname} ({size_kb:.0f} KB)"))
        else:
            results.append(HealthResult(f"ML Model: {name}", FAIL,
                f"NOT FOUND: {path}", "Retrain required"))
    return results


def check_watchlist() -> List[HealthResult]:
    results = []
    wl_dir = Path(__file__).resolve().parent / "watchlists"
    universe_path = wl_dir / "universe_master.txt"
    if universe_path.exists():
        lines = [l.strip() for l in universe_path.read_text().splitlines()
                 if l.strip() and not l.startswith("#")]
        results.append(HealthResult("Universe Master", OK,
            f"{len(lines):,} tickers | {universe_path.name}"))
    else:
        results.append(HealthResult("Universe Master", FAIL,
            "universe_master.txt not found",
            "Run: python -m signal_scanner.scanner.watchlist_manager rebuild"))
    return results


def check_scan_cache() -> List[HealthResult]:
    results = []
    cache_path = Path(__file__).resolve().parent / "data" / "scan_cache.json"
    if not cache_path.exists():
        results.append(HealthResult("Scan Cache", WARN, "No cache file — scanner hasn't run yet"))
        return results
    try:
        import json
        cache = json.loads(cache_path.read_text())
        cached_at = cache.get("cached_at", "")
        count = cache.get("count", 0)
        if cached_at:
            dt = datetime.fromisoformat(cached_at.replace("Z", "+00:00"))
            age_min = (datetime.now(dt.tzinfo) - dt).total_seconds() / 60
            if age_min < 240:  # 4h TTL
                results.append(HealthResult("Scan Cache", OK,
                    f"{count} results | {age_min:.0f} min ago"))
            else:
                results.append(HealthResult("Scan Cache", WARN,
                    f"{count} results | {age_min:.0f} min ago (STALE > 4h)"))
        else:
            results.append(HealthResult("Scan Cache", INFO, f"{count} results | no timestamp"))
    except Exception as exc:
        results.append(HealthResult("Scan Cache", WARN, f"Cannot read cache: {exc}"))
    return results


def check_paper_trades() -> List[HealthResult]:
    results = []
    try:
        import sqlite3
        db_path = Path(__file__).resolve().parent / "data" / "signals.db"
        if not db_path.exists():
            results.append(HealthResult("Paper Trades", WARN, "signals.db not found"))
            return results

        conn = sqlite3.connect(str(db_path))
        cursor = conn.cursor()

        # Open trades
        cursor.execute("""
            SELECT COUNT(*)
            FROM paper_trades
            WHERE status = 'OPEN'
        """)
        row = cursor.fetchone()
        open_count = row[0] or 0

        # Today's entries (created_ts column)
        today_str = date.today().isoformat()
        cursor.execute("""
            SELECT COUNT(*) FROM paper_trades
            WHERE DATE(created_ts) = ?
        """, (today_str,))
        today_entries = (cursor.fetchone() or (0,))[0]

        # Win rate last 30 days
        cursor.execute("""
            SELECT COUNT(*),
                   SUM(CASE WHEN realized_pnl_pct > 0 THEN 1 ELSE 0 END)
            FROM paper_trades
            WHERE status = 'CLOSED'
              AND DATE(closed_at) >= DATE('now', '-30 days')
              AND recommendation_source NOT LIKE 'MANUAL%'
        """)
        r = cursor.fetchone()
        closed_30d = r[0] or 0
        wins_30d = r[1] or 0
        win_rate = (wins_30d / closed_30d * 100) if closed_30d > 0 else 0
        conn.close()

        results.append(HealthResult("Paper Trades", OK,
            f"Open: {open_count} | Today entries: {today_entries} | "
            f"30d WR: {win_rate:.0f}% ({closed_30d} closed)"))

    except Exception as exc:
        results.append(HealthResult("Paper Trades", WARN, f"Error: {exc}"))
    return results


def check_ibkr() -> List[HealthResult]:
    results = []
    try:
        from signal_scanner.core.ibkr_connector import DataConnector
        connector = DataConnector()
        connected = connector.connect_ibkr()
        if connected:
            results.append(HealthResult("IBKR", OK, "Connected (TWS/Gateway)"))
            connector.disconnect()
        else:
            results.append(HealthResult("IBKR", WARN,
                "Not connected — live data/execution unavailable",
                "Start TWS/Gateway before market open"))
    except Exception as exc:
        results.append(HealthResult("IBKR", WARN, f"Not connected: {str(exc)[:60]}"))
    return results


def check_api_keys() -> List[HealthResult]:
    results = []
    keys = {
        "MASSIVE_API_KEY (Polygon)": os.environ.get("MASSIVE_API_KEY", ""),
        "ANTHROPIC_API_KEY (TradeGPT)": os.environ.get("ANTHROPIC_API_KEY", ""),
    }
    for name, val in keys.items():
        if val:
            results.append(HealthResult(f"API Key: {name}", OK,
                f"Set ({val[:6]}...{val[-4:]})"))
        else:
            results.append(HealthResult(f"API Key: {name}", WARN, "NOT SET"))
    return results


# ---------------------------------------------------------------------------
# Trading Readiness Checks  (catches issues BEFORE market open)
# ---------------------------------------------------------------------------

def check_trading_readiness() -> List[HealthResult]:
    """Validate all trading paths are armed. Run before 9:00 AM.

    Catches the most common pre-market failures:
    - Intelligence snapshot has no qualifying tickers  → VWAP_MR blind
    - Sniper Board has zero ideas                     → regime/toggle issue
    - AI Signals engine is broken                     → import/DB error
    - Regime blocks all entries                       → no trades today
    - IdeaBridge would find nothing                   → conviction/phase mismatch
    """
    results = []
    try:
        from signal_scanner.institutional_intel.config import safe_duckdb_connect, get_active_quarter
        conn = safe_duckdb_connect(read_only=True)
        if conn is None:
            results.append(HealthResult("Trading Readiness", FAIL,
                "DuckDB locked — cannot validate trading paths",
                "Kill any hanging python.exe processes (tasklist | grep python)"))
            return results

        quarter = get_active_quarter(conn)
        if not quarter:
            results.append(HealthResult("Active Quarter", FAIL,
                "No active quarter found",
                "Run: python -m signal_scanner.institutional_intel.jobs.run_pipeline --stage intelligence"))
            conn.close()
            return results

        # 1. Intelligence Snapshot — how many tickers will VWAP_MR see at startup?
        n_tier1 = conn.execute("""
            SELECT COUNT(*) FROM intelligence_scores
            WHERE report_quarter = ?
              AND accum_phase IN ('EARLY_ACCUM','ACTIVE_ACCUM','LATE_ACCUM')
              AND conviction_score >= 65
        """, [quarter]).fetchone()[0]

        if n_tier1 >= 20:
            results.append(HealthResult("Intelligence Snapshot (VWAP_MR)", OK,
                f"{n_tier1} Tier 1 tickers will load at scanner startup (ACCUM + conv>=65)"))
        elif n_tier1 > 0:
            results.append(HealthResult("Intelligence Snapshot (VWAP_MR)", WARN,
                f"Only {n_tier1} Tier 1 tickers — VWAP_MR will have limited setups"))
        else:
            results.append(HealthResult("Intelligence Snapshot (VWAP_MR)", FAIL,
                "ZERO qualifying tickers — VWAP_MR will be blind at open",
                f"Run: python -m signal_scanner.institutional_intel.jobs.run_pipeline --stage intelligence"))

        # 2. Sniper LONG ideas
        n_long = conn.execute("""
            SELECT COUNT(*) FROM intelligence_scores
            WHERE report_quarter = ? AND swing_signal = 'BUY' AND conviction_score >= 65
        """, [quarter]).fetchone()[0]

        if n_long >= 5:
            results.append(HealthResult("Sniper LONG Ideas", OK,
                f"{n_long} actionable LONG ideas (conv>=65, swing=BUY)"))
        elif n_long > 0:
            results.append(HealthResult("Sniper LONG Ideas", WARN,
                f"Only {n_long} LONG ideas — may be too few for today"))
        else:
            results.append(HealthResult("Sniper LONG Ideas", FAIL,
                "ZERO Sniper LONG ideas in DB",
                "Check: Sniper Board regime toggle OFF? Run intelligence pipeline?"))

        # 3. Sniper SHORT ideas
        try:
            n_short = conn.execute("""
                SELECT COUNT(*) FROM intelligence_scores
                WHERE report_quarter = ? AND short_swing_signal = 'SHORT'
            """, [quarter]).fetchone()[0]
            st = OK if n_short >= 3 else (WARN if n_short > 0 else INFO)
            results.append(HealthResult("Sniper SHORT Ideas", st,
                f"{n_short} SHORT ideas (short_conv>=45, DISTRIBUTION/DECLINE phase)"))
        except Exception:
            results.append(HealthResult("Sniper SHORT Ideas", WARN,
                "short_swing_signal column missing — run pipeline Step 6j"))

        # 4. HMM Regime — does it block LONG entries today?
        try:
            from signal_scanner.institutional_intel.intelligence.regime_hmm import DailyRegimeHMM
            from pathlib import Path as _Path
            _model_path = _Path(__file__).resolve().parents[1] / "data" / "warehouse" / "models" / "regime_hmm_daily.pkl"
            hmm = DailyRegimeHMM()
            if _model_path.exists():
                hmm.load(_model_path)
            state, _, name = hmm.current_regime()
            if state == 0:
                results.append(HealthResult("HMM Regime", FAIL,
                    f"CRASH (state=0) — ALL trades blocked today",
                    "No entries until regime shifts. Monitor for state change."))
            elif state == 1:
                results.append(HealthResult("HMM Regime", WARN,
                    f"DISTRIBUTING (state=1) — LONG entries BLOCKED, SHORT-only today"))
            else:
                results.append(HealthResult("HMM Regime", OK,
                    f"{name} (state={state}) — LONG + SHORT entries allowed"))
        except Exception as exc:
            results.append(HealthResult("HMM Regime", WARN, f"Cannot read regime: {exc}"))

        # 5. AI Signals engine smoke test
        try:
            from signal_scanner.institutional_intel.reports.ai_signals import AISignalEngine
            engine = AISignalEngine()
            sigs = engine.detect_signals()
            n_high = sum(1 for s in sigs if s.get("strength") == "HIGH")
            n_med = sum(1 for s in sigs if s.get("strength") == "MEDIUM")
            n_ti = sum(1 for s in sigs if s.get("trade_intelligence"))
            if len(sigs) >= 10:
                results.append(HealthResult("AI Signals Engine", OK,
                    f"{len(sigs)} signals: {n_high} HIGH, {n_med} MEDIUM | {n_ti} with trade plan"))
            else:
                results.append(HealthResult("AI Signals Engine", WARN,
                    f"Only {len(sigs)} signals — check DB data quality"))
        except Exception as exc:
            results.append(HealthResult("AI Signals Engine", FAIL,
                f"Engine crashed: {exc}",
                "Fix: check ai_signals.py imports and DB tables"))

        # 6. IdeaBridge dry-run — what would qualify right now?
        try:
            n_swing_buy = conn.execute("""
                SELECT COUNT(*) FROM intelligence_scores
                WHERE report_quarter = ? AND swing_signal = 'BUY'
                  AND conviction_score >= 75
                  AND accum_phase IN ('EARLY_ACCUM','ACTIVE_ACCUM','LATE_ACCUM')
            """, [quarter]).fetchone()[0]

            n_swing_short = conn.execute("""
                SELECT COUNT(*) FROM intelligence_scores
                WHERE report_quarter = ?
                  AND short_swing_signal = 'SHORT'
                  AND accum_phase IN ('DISTRIBUTION','DECLINE')
            """, [quarter]).fetchone()[0]

            n_triple = conn.execute("""
                SELECT COUNT(*) FROM intelligence_scores
                WHERE report_quarter = ? AND triple_lock = 1
                  AND conviction_score >= 70 AND ml_score_v2 >= 70
            """, [quarter]).fetchone()[0]

            results.append(HealthResult("IdeaBridge Dry-Run", OK,
                f"Pool: {n_swing_buy} LONG swings | {n_swing_short} SHORT swings | {n_triple} Triple Lock",
                "Actual entries gated by regime + open positions (max 3)"))
        except Exception as exc:
            results.append(HealthResult("IdeaBridge Dry-Run", WARN, f"Dry-run query failed: {exc}"))

        conn.close()

    except Exception as exc:
        results.append(HealthResult("Trading Readiness", FAIL, f"Check crashed: {exc}"))

    return results


def check_timing_windows() -> List[HealthResult]:
    """Show which trading windows are OPEN or CLOSED right now (ET).

    Intraday ML:  9:30 AM – 11:30 AM ET (entry)  /  all day (exit)
    Swing/Sniper: All market hours (9:30 AM – 4:00 PM ET)
    AI Signals:   Always (static DB query, not time-gated)
    """
    results = []
    try:
        import zoneinfo
        from datetime import time as dtime
        et = zoneinfo.ZoneInfo("America/New_York")
        now_et = datetime.now(et)
        t = now_et.time()
        now_str = now_et.strftime("%I:%M %p ET")
        today_str = now_et.strftime("%A")

        market_open  = dtime(9, 30)
        market_close = dtime(16, 0)
        intraday_entry_close = dtime(11, 30)
        intraday_exit_close  = dtime(15, 45)

        is_weekend   = now_et.weekday() >= 5
        is_mkt_hours = market_open <= t <= market_close and not is_weekend

        # Swing + Sniper + AI Signals
        if is_weekend:
            results.append(HealthResult("Swing + Sniper + AI Signals", INFO,
                f"Weekend — market closed ({today_str} {now_str})"))
        elif is_mkt_hours:
            results.append(HealthResult("Swing + Sniper + AI Signals", OK,
                f"ACTIVE — all market hours | Now: {now_str}"))
        elif t < market_open:
            results.append(HealthResult("Swing + Sniper + AI Signals", INFO,
                f"Pre-market — opens 9:30 AM ET ({now_str})"))
        else:
            results.append(HealthResult("Swing + Sniper + AI Signals", INFO,
                f"After-hours — closed for entries ({now_str})"))

        # Intraday ML entry window
        if is_weekend or is_mkt_hours is False:
            pass  # covered above
        elif t < market_open:
            results.append(HealthResult("Intraday ML Entry (VWAP/FPB/ORB)", INFO,
                f"Window opens 9:30 AM ET ({now_str})"))
        elif market_open <= t <= intraday_entry_close:
            remaining_min = int((datetime.combine(now_et.date(), intraday_entry_close) -
                                 now_et.replace(tzinfo=None)).total_seconds() / 60)
            results.append(HealthResult("Intraday ML Entry (VWAP/FPB/ORB)", OK,
                f"WINDOW OPEN — {remaining_min} min remaining (closes 11:30 AM ET)"))
        elif intraday_entry_close < t <= market_close:
            results.append(HealthResult("Intraday ML Entry (VWAP/FPB/ORB)", WARN,
                f"ENTRY WINDOW CLOSED for today (closed 11:30 AM ET) | Now: {now_str}",
                "No new intraday entries until tomorrow 9:30 AM ET"))

        # Intraday exit window
        if is_mkt_hours:
            if t <= intraday_exit_close:
                results.append(HealthResult("Intraday ML Exit Window", OK,
                    f"OPEN — exits allowed until 3:45 PM ET ({now_str})"))
            else:
                results.append(HealthResult("Intraday ML Exit Window", INFO,
                    f"CLOSED — all intraday positions should be flat ({now_str})"))

    except Exception as exc:
        results.append(HealthResult("Timing Windows", WARN, f"Cannot compute: {exc}"))

    return results


# ---------------------------------------------------------------------------
# Main runner
# ---------------------------------------------------------------------------

def run_health_check(output_json: bool = False) -> Dict:
    """Run all health checks and return results dict."""
    all_results: List[HealthResult] = []

    # Load .env
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    checks = [
        ("DuckDB Connectivity", check_duckdb),
        ("Data Freshness", check_data_freshness),
        ("ML Models", check_ml_models),
        ("Watchlist", check_watchlist),
        ("Scan Cache", check_scan_cache),
        ("Paper Trades", check_paper_trades),
        ("API Keys", check_api_keys),
        ("IBKR Connection", check_ibkr),
        # --- Trading readiness (new) ---
        ("Timing Windows", check_timing_windows),
        ("Trading Readiness", check_trading_readiness),
    ]

    for section_name, fn in checks:
        try:
            section_results = fn()
            all_results.extend(section_results)
        except Exception as exc:
            all_results.append(HealthResult(section_name, FAIL, f"Check crashed: {exc}"))

    # Summary
    counts = {OK: 0, WARN: 0, FAIL: 0}
    for r in all_results:
        if r.status in counts:
            counts[r.status] += 1

    overall = FAIL if counts[FAIL] > 0 else (WARN if counts[WARN] > 0 else OK)

    if not output_json:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"\n{'='*70}")
        print(f"  QUANT-BRIDGE DAILY HEALTH CHECK — {now}")
        print(f"{'='*70}")
        for r in all_results:
            print(r)
        print(f"\n{'='*70}")
        print(f"  SUMMARY: {counts[OK]} OK | {counts[WARN]} WARN | {counts[FAIL]} FAIL "
              f"| Overall: {overall}")
        print(f"{'='*70}\n")

        # Actionable alerts
        alerts = [r for r in all_results if r.status in (WARN, FAIL)]
        if alerts:
            print("ITEMS NEEDING ATTENTION:")
            for r in alerts:
                icon = STATUS_ICON[r.status]
                print(f"  {icon} {r.component}: {r.message}")
                if r.detail:
                    print(f"       Fix: {r.detail}")
        else:
            print("All systems nominal.")
        print()

    return {
        "timestamp": datetime.now().isoformat(),
        "overall": overall,
        "counts": counts,
        "results": [
            {"component": r.component, "status": r.status,
             "message": r.message, "detail": r.detail}
            for r in all_results
        ],
    }


def main():
    p = argparse.ArgumentParser(description="Quant-Bridge daily health check")
    p.add_argument("--json", action="store_true", help="Output JSON instead of formatted text")
    args = p.parse_args()

    result = run_health_check(output_json=args.json)

    if args.json:
        print(json.dumps(result, indent=2))

    # Exit with non-zero if FAIL
    sys.exit(1 if result["overall"] == FAIL else 0)


if __name__ == "__main__":
    main()
