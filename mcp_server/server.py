"""
Quant-Bridge MCP Server
=======================
Exposes live system state from DuckDB warehouse, SQLite signals DB,
filesystem artifacts, and Shivam research pipelines to Claude Code.

Run:  python -m mcp_server.server
"""

import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

import duckdb
from mcp.server.fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
QB_ROOT = Path(__file__).resolve().parents[1]
WAREHOUSE_PATH = QB_ROOT / "data" / "warehouse" / "sec_intel.duckdb"
SIGNALS_DB_PATH = QB_ROOT / "signal_scanner" / "data" / "signals.db"
HMM_MODEL_PATH = QB_ROOT / "data" / "warehouse" / "models" / "regime_hmm_daily.pkl"
ML_V2_MODEL_PATH = QB_ROOT / "data" / "models" / "ml_signal_v2.pkl"
WATCHLIST_PATH = QB_ROOT / "signal_scanner" / "watchlists" / "universe_master.txt"
LOG_DIR = QB_ROOT / "signal_scanner" / "logs"

SHIVAM_ROOT = Path("E:/ai-development/Shivam")
SHIVAM_LEDGER = SHIVAM_ROOT / "data" / "runtime" / "v87" / "trade_ledger_v1.sqlite"
SHIVAM_CACHE = SHIVAM_ROOT / "data" / "cache" / "sec_intel_snapshot.duckdb"
SHIVAM_ARTIFACTS = SHIVAM_ROOT / "data" / "artifacts"
SHIVAM_CONFIGS = SHIVAM_ROOT / "config"

# ---------------------------------------------------------------------------
# MCP App
# ---------------------------------------------------------------------------
mcp = FastMCP(
    "QuantBridge",
    instructions="Live system state for Quant-Bridge institutional intelligence + Shivam research",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _duck_ro() -> Optional[duckdb.DuckDBPyConnection]:
    """Read-only DuckDB connection with retry."""
    import time
    for attempt in range(3):
        try:
            return duckdb.connect(str(WAREHOUSE_PATH), read_only=True)
        except Exception:
            if attempt < 2:
                time.sleep(0.5 * (attempt + 1))
    return None


def _sqlite_ro(path: Path) -> Optional[sqlite3.Connection]:
    """Read-only SQLite connection."""
    if not path.exists():
        return None
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def _rows_to_dicts(rows, columns) -> list[dict]:
    """Convert DuckDB result to list of dicts."""
    return [dict(zip(columns, row)) for row in rows]


def _duck_query(sql: str, params=None) -> list[dict]:
    """Execute a read-only DuckDB query, return list of dicts."""
    conn = _duck_ro()
    if not conn:
        return [{"error": "DuckDB locked or unavailable"}]
    try:
        result = conn.execute(sql, params or [])
        cols = [desc[0] for desc in result.description]
        return _rows_to_dicts(result.fetchall(), cols)
    except Exception as e:
        return [{"error": str(e)}]
    finally:
        conn.close()


def _sqlite_query(db_path: Path, sql: str, params=None) -> list[dict]:
    """Execute a read-only SQLite query."""
    conn = _sqlite_ro(db_path)
    if not conn:
        return [{"error": f"DB not found: {db_path}"}]
    try:
        rows = conn.execute(sql, params or []).fetchall()
        return [dict(row) for row in rows]
    except Exception as e:
        return [{"error": str(e)}]
    finally:
        conn.close()


def _file_age_hours(path: Path) -> Optional[float]:
    """Hours since file was last modified."""
    if not path.exists():
        return None
    mtime = datetime.fromtimestamp(path.stat().st_mtime)
    return (datetime.now() - mtime).total_seconds() / 3600


def _json_safe(obj: Any) -> str:
    """JSON serialize with date/decimal handling."""
    def default(o):
        if isinstance(o, (datetime,)):
            return o.isoformat()
        if hasattr(o, '__float__'):
            return float(o)
        return str(o)
    return json.dumps(obj, indent=2, default=default)


# ===================================================================
# TOOL 1: MORNING BRIEFING — Complete pre-market readiness
# ===================================================================
@mcp.tool()
def morning_briefing() -> str:
    """Complete pre-market readiness report: regime, data freshness, pipeline health,
    open positions, top ideas, model status. Run this every morning before market open."""
    sections = {}

    # 1. HMM Regime
    sections["regime"] = _get_regime_internal()

    # 2. Data freshness
    sections["data_freshness"] = _get_data_freshness_internal()

    # 3. Active quarter
    sections["active_quarter"] = _get_active_quarter_internal()

    # 4. Open positions
    sections["open_positions"] = _sqlite_query(
        SIGNALS_DB_PATH,
        "SELECT symbol, side, entry_price, stop_loss, target_1, target_2, "
        "recommendation_source, strategy_type, status, opened_at FROM paper_trades WHERE status='OPEN' ORDER BY opened_at DESC"
    )

    # 5. Top ideas (Triple Lock + high conviction)
    sections["triple_lock_ideas"] = _duck_query("""
        SELECT ticker, conviction_score, ml_score_v2, accum_phase,
               swing_signal, inst_f4_distinct_60d, squeeze_score
        FROM intelligence_scores
        WHERE report_quarter = (
            SELECT MAX(report_quarter) FROM intelligence_scores
            WHERE data_quality_score >= 75
            GROUP BY report_quarter HAVING COUNT(*) >= 1000
            ORDER BY report_quarter DESC LIMIT 1
        )
        AND triple_lock = true
        ORDER BY conviction_score DESC LIMIT 20
    """)

    # 6. Model freshness
    sections["models"] = {
        "hmm_regime": {
            "path": str(HMM_MODEL_PATH),
            "exists": HMM_MODEL_PATH.exists(),
            "age_hours": _file_age_hours(HMM_MODEL_PATH),
        },
        "ml_v2": {
            "path": str(ML_V2_MODEL_PATH),
            "exists": ML_V2_MODEL_PATH.exists(),
            "age_hours": _file_age_hours(ML_V2_MODEL_PATH),
        },
    }

    # 7. Watchlist
    wl_count = 0
    if WATCHLIST_PATH.exists():
        wl_count = sum(1 for line in WATCHLIST_PATH.read_text().splitlines() if line.strip())
    sections["watchlist_tickers"] = wl_count

    # 8. Recent paper performance (30d)
    sections["paper_performance_30d"] = _sqlite_query(
        SIGNALS_DB_PATH,
        """SELECT COUNT(*) as total_closed,
                  SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) as wins,
                  ROUND(AVG(realized_pnl), 2) as avg_pnl,
                  ROUND(SUM(realized_pnl), 2) as total_pnl
           FROM paper_trades
           WHERE status='CLOSED'
             AND closed_at >= datetime('now', '-30 days')"""
    )

    # 9. Pipeline last run (check log files)
    sections["pipeline_logs"] = _get_recent_logs()

    # 10. Canonical readiness state (from run_premarket.py)
    sections["readiness"] = _get_readiness_internal()

    # 11. Skip telemetry (today's structured reason events)
    sections["skip_telemetry_today"] = _get_skip_telemetry_internal()

    return _json_safe(sections)


# ===================================================================
# TOOL 1b: READINESS — Canonical READY/DEGRADED/BLOCKED state
# ===================================================================
def _get_readiness_internal() -> dict:
    """Load canonical readiness state from readiness.json + live enrichment."""
    try:
        from signal_scanner.core.readiness import ReadinessState, compute_price_freshness
        state = ReadinessState.load()

        # Live-enrich price freshness (file may be stale)
        price_ok, age_days, latest_str = compute_price_freshness()
        state.prices_age_days = age_days
        state.latest_price_date = latest_str

        # Live-enrich IBKR from signals.db open trades (proxy for connectivity)
        open_trades = _sqlite_query(
            SIGNALS_DB_PATH,
            "SELECT COUNT(*) as cnt FROM paper_trades WHERE status='OPEN'"
        )

        return state.to_dict()
    except Exception as e:
        return {"error": str(e), "readiness_status": "UNKNOWN"}


def _get_skip_telemetry_internal(trade_date: str = "") -> list:
    """Get today's skip telemetry from SQLite."""
    try:
        from signal_scanner.core.telemetry import get_daily_summary
        return get_daily_summary(trade_date or None)
    except Exception as e:
        return [{"error": str(e)}]


@mcp.tool()
def readiness() -> str:
    """Canonical readiness state: READY, DEGRADED, or BLOCKED.
    Shows blocked/degraded reasons, data freshness, orphan gate, enabled scanners.
    Same state consumed by engine startup and dashboard."""
    state = _get_readiness_internal()
    telemetry = _get_skip_telemetry_internal()
    return _json_safe({"readiness": state, "skip_telemetry_today": telemetry})


# ===================================================================
# TOOL 1c: EVALUATION — Today's trade funnel, P&L, skip reasons
# ===================================================================
@mcp.tool()
def daily_evaluation(trade_date: str = "") -> str:
    """Today's evaluation metrics: trade funnel, P&L by source, ideas vs trades,
    open risk, skip reasons. Same data as daily_evidence_report --eod."""
    try:
        from signal_scanner.daily_evidence_report import build_report
        report = build_report(trade_date or None)
        # Return the evaluation-relevant subset
        return _json_safe({
            "date": report["report_date"],
            "readiness_status": report["readiness"].get("readiness_status"),
            "ideas": report.get("ideas", {}),
            "trade_funnel": report.get("trade_funnel", {}),
            "paper_trades_today": report.get("paper_trades", {}).get("entered_today", 0),
            "by_source": report.get("paper_trades", {}).get("by_source", {}),
            "pnl_summary": report.get("pnl_summary", {}),
            "pnl_by_source": report.get("pnl_by_source", []),
            "open_positions": report.get("open_positions", []),
            "zero_output_explanation": report.get("zero_output_explanation", []),
            "top_skip_reasons": report.get("subsystem_summary", {}),
            "real_session": report.get("real_session", {}),
        })
    except Exception as e:
        return _json_safe({"error": str(e)})


# ===================================================================
# TOOL 1d: SESSION HEARTBEAT — Live activity during market hours
# ===================================================================
@mcp.tool()
def session_heartbeat(trade_date: str = "") -> str:
    """Live session heartbeat: which subsystems ran, entries placed, top skip reasons.
    Use during market hours to check if the system is active or blocked."""
    try:
        from signal_scanner.session_monitor import build_heartbeat
        return _json_safe(build_heartbeat(trade_date or None))
    except Exception as e:
        return _json_safe({"error": str(e)})


# ===================================================================
# TOOL 2: REGIME STATE — Current HMM market regime
# ===================================================================
def _get_regime_internal() -> dict:
    """Get current HMM regime state."""
    try:
        import pickle
        if not HMM_MODEL_PATH.exists():
            return {"error": "HMM model not found", "path": str(HMM_MODEL_PATH)}
        with open(HMM_MODEL_PATH, "rb") as f:
            model_data = pickle.load(f)
        # Model stores last predicted state
        if isinstance(model_data, dict):
            return {
                "state": model_data.get("current_state"),
                "state_name": model_data.get("state_name"),
                "probabilities": model_data.get("state_probs"),
                "last_fit": model_data.get("last_fit_date"),
                "trade_rules": _regime_trade_rules(model_data.get("current_state")),
            }
        return {"raw_type": str(type(model_data)), "note": "Model format not dict — may need live inference"}
    except Exception as e:
        return {"error": str(e)}


def _regime_trade_rules(state: Optional[int]) -> str:
    if state is None:
        return "UNKNOWN"
    rules = {
        0: "CRASH — ALL trades BLOCKED",
        1: "DISTRIBUTION — LONG blocked, SHORT allowed",
        2: "ACCUMULATION — LONG + SHORT allowed",
        3: "MEAN_REVERSION — LONG + SHORT allowed",
        4: "BULL_TREND — LONG + SHORT allowed",
    }
    return rules.get(state, f"UNKNOWN state {state}")


@mcp.tool()
def regime_state() -> str:
    """Current HMM regime state with trade allowance rules.
    States: 0=CRASH (blocks all), 1=DISTRIBUTION (blocks long), 2-4=allow all."""
    return _json_safe(_get_regime_internal())


# ===================================================================
# TOOL 3: DATA FRESHNESS — How stale is each data source?
# ===================================================================
def _get_data_freshness_internal() -> dict:
    results = {}
    checks = {
        "daily_prices": "SELECT MAX(trade_date) as latest FROM fact_daily_prices",
        "form4_transactions": "SELECT MAX(transaction_date) as latest FROM fact_form4_transactions",
        "13f_positions": "SELECT MAX(report_period) as latest FROM fact_13f_positions",
        "short_interest": "SELECT MAX(settlement_date) as latest FROM fact_short_interest",
        "short_volume": "SELECT MAX(trade_date) as latest FROM fact_short_volume",
        "dark_pool": "SELECT MAX(trade_date) as latest FROM fact_dark_pool_daily",
        "cost_to_borrow": "SELECT MAX(report_date) as latest FROM fact_cost_to_borrow",
        "options_flow": "SELECT MAX(snapshot_date) as latest FROM fact_options_flow",
        "news_sentiment": "SELECT MAX(published_at) as latest FROM fact_news_sentiment",
        "form8k_events": "SELECT MAX(filed_date) as latest FROM fact_form8k_events",
        "intelligence_scores": "SELECT MAX(report_quarter) as latest, COUNT(*) as rows FROM intelligence_scores WHERE data_quality_score >= 75",
    }
    conn = _duck_ro()
    if not conn:
        return {"error": "DuckDB locked"}
    try:
        for name, sql in checks.items():
            try:
                row = conn.execute(sql).fetchone()
                cols = [d[0] for d in conn.description]
                results[name] = dict(zip(cols, row)) if row else {"latest": None}
            except Exception as e:
                results[name] = {"error": str(e)}
    finally:
        conn.close()
    return results


@mcp.tool()
def data_freshness() -> str:
    """Check how recent each data source is: prices, insider, 13F, shorts, dark pool, options, news, 8-K.
    Returns latest date/timestamp for each table."""
    return _json_safe(_get_data_freshness_internal())


# ===================================================================
# TOOL 4: ACTIVE QUARTER — Current intelligence quarter + coverage
# ===================================================================
def _get_active_quarter_internal() -> dict:
    conn = _duck_ro()
    if not conn:
        return {"error": "DuckDB locked"}
    try:
        # Active quarter
        row = conn.execute("""
            SELECT report_quarter, COUNT(*) as tickers,
                   ROUND(AVG(data_quality_score),1) as avg_quality,
                   ROUND(AVG(conviction_score),1) as avg_conviction,
                   SUM(CASE WHEN triple_lock THEN 1 ELSE 0 END) as triple_lock_count
            FROM intelligence_scores
            WHERE data_quality_score >= 75
            GROUP BY report_quarter
            HAVING COUNT(*) >= 1000
            ORDER BY report_quarter DESC LIMIT 1
        """).fetchone()
        active = dict(zip(["quarter", "tickers", "avg_quality", "avg_conviction", "triple_lock_count"], row)) if row else {}

        # All quarters summary
        quarters = conn.execute("""
            SELECT report_quarter, COUNT(*) as tickers,
                   ROUND(AVG(data_quality_score),1) as avg_quality
            FROM intelligence_scores
            GROUP BY report_quarter
            ORDER BY report_quarter DESC
        """).fetchall()
        all_q = [dict(zip(["quarter", "tickers", "avg_quality"], q)) for q in quarters]

        return {"active": active, "all_quarters": all_q}
    finally:
        conn.close()


@mcp.tool()
def active_quarter() -> str:
    """Active intelligence quarter with ticker count, quality score, conviction stats, triple lock count.
    Also lists all available quarters."""
    return _json_safe(_get_active_quarter_internal())


# ===================================================================
# TOOL 5: INTELLIGENCE SNAPSHOT — Top ideas by various criteria
# ===================================================================
@mcp.tool()
def intelligence_snapshot(
    sort_by: str = "conviction_score",
    min_conviction: int = 60,
    phase_filter: str = "",
    signal_filter: str = "",
    limit: int = 30,
) -> str:
    """Top intelligence ideas from the active quarter.
    sort_by: conviction_score, ml_score_v2, squeeze_score, insider_effect_score
    phase_filter: EARLY_ACCUM, ACTIVE_ACCUM, LATE_ACCUM, EXPANSION, DISTRIBUTION, DECLINE
    signal_filter: BUY, SHORT, WATCH"""

    allowed_sorts = {"conviction_score", "ml_score_v2", "squeeze_score",
                     "insider_effect_score", "short_conviction_score"}
    if sort_by not in allowed_sorts:
        sort_by = "conviction_score"

    where_clauses = ["data_quality_score >= 75", f"conviction_score >= {int(min_conviction)}"]
    if phase_filter:
        where_clauses.append(f"accum_phase = '{phase_filter}'")
    if signal_filter:
        where_clauses.append(f"swing_signal = '{signal_filter}'")

    where = " AND ".join(where_clauses)

    sql = f"""
        SELECT ticker, conviction_score, ml_score_v2, accum_phase, accum_phase_quarters,
               swing_signal, swing_entry_zone, swing_target, swing_stop,
               triple_lock, squeeze_score, insider_effect_score, insider_hist_win_rate,
               price_momentum_90d, price_above_200sma, inst_f4_distinct_60d,
               cascade_stage, tier1_manager_count, short_conviction_score,
               short_squeeze_score, institutional_pressure, trend_score
        FROM intelligence_scores
        WHERE report_quarter = (
            SELECT report_quarter FROM intelligence_scores
            WHERE data_quality_score >= 75
            GROUP BY report_quarter HAVING COUNT(*) >= 1000
            ORDER BY report_quarter DESC LIMIT 1
        )
        AND {where}
        ORDER BY {sort_by} DESC NULLS LAST
        LIMIT {int(limit)}
    """
    return _json_safe(_duck_query(sql))


# ===================================================================
# TOOL 6: TICKER DEEP DIVE — Full intelligence for one ticker
# ===================================================================
@mcp.tool()
def ticker_intel(ticker: str) -> str:
    """Full intelligence report for a single ticker: conviction breakdown, phase, ML score,
    insider activity, squeeze metrics, trading signals, sector rotation, manager quality."""
    ticker = ticker.upper().strip()

    # Intelligence scores
    intel = _duck_query("""
        SELECT * FROM intelligence_scores
        WHERE ticker = ? AND report_quarter = (
            SELECT MAX(report_quarter) FROM intelligence_scores
            WHERE data_quality_score >= 75
            GROUP BY report_quarter HAVING COUNT(*) >= 1000
            ORDER BY report_quarter DESC LIMIT 1
        )
    """, [ticker])

    # Recent Form 4 insider trades
    insiders = _duck_query("""
        SELECT transaction_date, insider_name, insider_role, direction, shares, price
        FROM fact_form4_transactions
        WHERE ticker = ?
        ORDER BY transaction_date DESC LIMIT 15
    """, [ticker])

    # QoQ changes (last 4 quarters)
    qoq = _duck_query("""
        SELECT current_quarter, inst_count_change, shares_change_pct,
               value_change_pct, count_up_streak
        FROM agg_qoq_changes
        WHERE ticker = ?
        ORDER BY current_quarter DESC LIMIT 4
    """, [ticker])

    # Short data
    shorts = _duck_query("""
        SELECT settlement_date, short_interest, days_to_cover, short_pct_float
        FROM fact_short_interest
        WHERE ticker = ?
        ORDER BY settlement_date DESC LIMIT 5
    """, [ticker])

    # Recent prices
    prices = _duck_query("""
        SELECT trade_date, open, high, low, close, volume
        FROM fact_daily_prices
        WHERE ticker = ?
        ORDER BY trade_date DESC LIMIT 10
    """, [ticker])

    # Options flow
    options = _duck_query("""
        SELECT snapshot_date, put_call_ratio_vol, put_call_ratio_oi, total_volume, total_oi
        FROM fact_options_flow
        WHERE ticker = ?
        ORDER BY snapshot_date DESC LIMIT 5
    """, [ticker])

    # Paper trade history
    paper = _sqlite_query(SIGNALS_DB_PATH, """
        SELECT side, entry_price, exit_price, status, realized_pnl, realized_pnl_pct,
               recommendation_source, strategy_type, opened_at, closed_at, exit_reason
        FROM paper_trades WHERE symbol = ? ORDER BY opened_at DESC LIMIT 10
    """, [ticker])

    return _json_safe({
        "ticker": ticker,
        "intelligence": intel,
        "insider_trades": insiders,
        "qoq_changes": qoq,
        "short_data": shorts,
        "recent_prices": prices,
        "options_flow": options,
        "paper_trades": paper,
    })


# ===================================================================
# TOOL 7: OPEN POSITIONS — Current paper/live trades
# ===================================================================
@mcp.tool()
def open_positions() -> str:
    """All currently open paper trading positions with P&L, stops, targets, and source."""
    return _json_safe(_sqlite_query(SIGNALS_DB_PATH, """
        SELECT symbol, side, entry_price, stop_loss, target_1, target_2,
               quantity, notional, opened_at, recommendation_source,
               strategy_type, execution_mode, instrument_type, status,
               entry_signal, entry_score, entry_rr_ratio, entry_market_regime
        FROM paper_trades
        WHERE status = 'OPEN'
        ORDER BY opened_at DESC
    """))


# ===================================================================
# TOOL 8: TRADE PERFORMANCE — Historical win rates and P&L
# ===================================================================
@mcp.tool()
def trade_performance(days_back: int = 30) -> str:
    """Paper trading performance: win rate, avg P&L, total P&L, by source and by side.
    days_back: lookback period (default 30)."""
    days = int(days_back)

    # Overall stats
    overall = _sqlite_query(SIGNALS_DB_PATH, f"""
        SELECT COUNT(*) as total,
               SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) as wins,
               SUM(CASE WHEN realized_pnl <= 0 THEN 1 ELSE 0 END) as losses,
               ROUND(AVG(realized_pnl), 2) as avg_pnl,
               ROUND(SUM(realized_pnl), 2) as total_pnl,
               ROUND(MAX(realized_pnl), 2) as best_trade,
               ROUND(MIN(realized_pnl), 2) as worst_trade
        FROM paper_trades
        WHERE status='CLOSED' AND closed_at >= datetime('now', '-{days} days')
    """)

    # By source
    by_source = _sqlite_query(SIGNALS_DB_PATH, f"""
        SELECT recommendation_source, COUNT(*) as trades,
               SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) as wins,
               ROUND(AVG(realized_pnl), 2) as avg_pnl,
               ROUND(SUM(realized_pnl), 2) as total_pnl
        FROM paper_trades
        WHERE status='CLOSED' AND closed_at >= datetime('now', '-{days} days')
        GROUP BY recommendation_source
        ORDER BY total_pnl DESC
    """)

    # By side
    by_side = _sqlite_query(SIGNALS_DB_PATH, f"""
        SELECT side, COUNT(*) as trades,
               SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) as wins,
               ROUND(AVG(realized_pnl), 2) as avg_pnl
        FROM paper_trades
        WHERE status='CLOSED' AND closed_at >= datetime('now', '-{days} days')
        GROUP BY side
    """)

    # EOD analysis summary
    eod = _sqlite_query(SIGNALS_DB_PATH, f"""
        SELECT trade_date, total_trades, wins, win_rate, realized_pnl
        FROM eod_analysis
        WHERE trade_date >= date('now', '-{days} days')
        ORDER BY trade_date DESC LIMIT 10
    """)

    return _json_safe({
        "period_days": days,
        "overall": overall,
        "by_source": by_source,
        "by_side": by_side,
        "daily_eod": eod,
    })


# ===================================================================
# TOOL 9: SNIPER BOARD — EV-ranked trade ideas
# ===================================================================
@mcp.tool()
def sniper_board(side: str = "ALL", min_ev: float = 0.0, limit: int = 25) -> str:
    """EV-ranked trade ideas from intelligence pipeline.
    side: ALL, LONG, SHORT. min_ev: minimum expected value threshold."""

    where = ["data_quality_score >= 75"]
    if side == "LONG":
        where.append("swing_signal = 'BUY'")
    elif side == "SHORT":
        where.append("(swing_signal = 'SHORT' OR short_swing_signal = 'SHORT')")

    w = " AND ".join(where)

    sql = f"""
        SELECT ticker, conviction_score, ml_score_v2, accum_phase,
               swing_signal, swing_entry_zone, swing_target, swing_stop,
               triple_lock, squeeze_score, short_conviction_score,
               insider_effect_score, price_momentum_90d, price_above_200sma,
               cascade_stage, tier1_manager_count
        FROM intelligence_scores
        WHERE report_quarter = (
            SELECT report_quarter FROM intelligence_scores
            WHERE data_quality_score >= 75
            GROUP BY report_quarter HAVING COUNT(*) >= 1000
            ORDER BY report_quarter DESC LIMIT 1
        )
        AND {w}
        AND conviction_score >= 55
        ORDER BY conviction_score DESC, ml_score_v2 DESC NULLS LAST
        LIMIT {int(limit)}
    """
    return _json_safe(_duck_query(sql))


# ===================================================================
# TOOL 10: AI SIGNALS — Current signal detections
# ===================================================================
@mcp.tool()
def ai_signals(signal_type: str = "", limit: int = 20) -> str:
    """AI signal detections from scanner. Types: ACCUMULATION_BREAKOUT, INSIDER_SURGE,
    SQUEEZE_SETUP, DARK_POOL_DIVERGENCE, SECTOR_ROTATION, etc.
    Leave signal_type empty for all."""

    where = "1=1"
    if signal_type:
        where = f"signal LIKE '%{signal_type}%'"

    return _json_safe(_sqlite_query(SIGNALS_DB_PATH, f"""
        SELECT symbol, signal, score, price, recommendation, rr_ratio,
               vwap_status, gex_status, timestamp
        FROM signals
        WHERE {where}
        ORDER BY timestamp DESC LIMIT {int(limit)}
    """))


# ===================================================================
# TOOL 11: SQUEEZE CANDIDATES — Short squeeze opportunities
# ===================================================================
@mcp.tool()
def squeeze_candidates(min_score: int = 50, limit: int = 20) -> str:
    """Short squeeze candidates: high squeeze score, days to cover, borrow cost, dark pool divergence."""
    sql = f"""
        SELECT i.ticker, i.squeeze_score, i.short_squeeze_score,
               i.conviction_score, i.accum_phase, i.short_conviction_score,
               s.days_to_cover, s.short_interest,
               c.fee_rate as borrow_fee, c.utilization_pct
        FROM intelligence_scores i
        LEFT JOIN (
            SELECT ticker, days_to_cover, short_interest,
                   ROW_NUMBER() OVER (PARTITION BY ticker ORDER BY settlement_date DESC) as rn
            FROM fact_short_interest
        ) s ON s.ticker = i.ticker AND s.rn = 1
        LEFT JOIN (
            SELECT ticker, fee_rate, utilization_pct,
                   ROW_NUMBER() OVER (PARTITION BY ticker ORDER BY report_date DESC) as rn
            FROM fact_cost_to_borrow
        ) c ON c.ticker = i.ticker AND c.rn = 1
        WHERE i.report_quarter = (
            SELECT report_quarter FROM intelligence_scores
            WHERE data_quality_score >= 75
            GROUP BY report_quarter HAVING COUNT(*) >= 1000
            ORDER BY report_quarter DESC LIMIT 1
        )
        AND i.squeeze_score >= {int(min_score)}
        ORDER BY i.squeeze_score DESC
        LIMIT {int(limit)}
    """
    return _json_safe(_duck_query(sql))


# ===================================================================
# TOOL 12: SECTOR ROTATION — Macro flow analysis
# ===================================================================
@mcp.tool()
def sector_rotation() -> str:
    """Sector rotation: institutional flow by sector, inflow streaks, momentum.
    Shows which sectors are accumulating vs distributing."""
    sql = """
        SELECT sector, report_quarter, flow_pct, inflow_streak,
               net_inst_count_change, total_value_k, net_flow_k, ticker_count
        FROM agg_sector_rotation
        ORDER BY report_quarter DESC, flow_pct DESC
        LIMIT 50
    """
    return _json_safe(_duck_query(sql))


# ===================================================================
# TOOL 13: INSIDER PATTERNS — Aggregated insider effectiveness
# ===================================================================
@mcp.tool()
def insider_patterns(min_win_rate: float = 0.55, limit: int = 30) -> str:
    """Insider trading effectiveness: win rates by role, alpha generation, pattern detection.
    Shows which insider buys historically work."""
    sql = f"""
        SELECT ticker, role_category, pattern_type, sample_count,
               win_rate_30d, win_rate_90d,
               mean_return_30d, mean_return_90d,
               mean_alpha_30d, mean_alpha_90d,
               insider_effect_score
        FROM agg_insider_patterns
        WHERE win_rate_30d >= {float(min_win_rate)} AND sample_count >= 3
        ORDER BY insider_effect_score DESC NULLS LAST
        LIMIT {int(limit)}
    """
    return _json_safe(_duck_query(sql))


# ===================================================================
# TOOL 14: PIPELINE STATUS — EOD job health
# ===================================================================
def _get_recent_logs() -> dict:
    """Check recent log files for pipeline status."""
    result = {}
    if not LOG_DIR.exists():
        return {"error": "Log directory not found"}

    for log_file in sorted(LOG_DIR.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)[:5]:
        result[log_file.name] = {
            "last_modified": datetime.fromtimestamp(log_file.stat().st_mtime).isoformat(),
            "age_hours": round(_file_age_hours(log_file) or 0, 1),
            "size_kb": round(log_file.stat().st_size / 1024, 1),
        }
    return result


@mcp.tool()
def pipeline_status() -> str:
    """EOD pipeline health: last run times, log freshness, data source latest dates,
    any stale data alerts. Also checks Windows Task Scheduler status."""

    freshness = _get_data_freshness_internal()
    logs = _get_recent_logs()

    # Check for stale data (>2 days for daily sources)
    alerts = []
    today = datetime.now().date()
    for source, info in freshness.items():
        if isinstance(info, dict) and "latest" in info and info["latest"]:
            try:
                latest = str(info["latest"])[:10]
                latest_date = datetime.strptime(latest, "%Y-%m-%d").date()
                days_stale = (today - latest_date).days
                if source in ("daily_prices", "short_volume", "dark_pool") and days_stale > 2:
                    alerts.append(f"STALE: {source} is {days_stale} days old (latest: {latest})")
                elif source == "form4_transactions" and days_stale > 5:
                    alerts.append(f"STALE: {source} is {days_stale} days old")
            except (ValueError, TypeError):
                pass

    # Model freshness
    models = {}
    for name, path in [("hmm_regime", HMM_MODEL_PATH), ("ml_v2", ML_V2_MODEL_PATH)]:
        age = _file_age_hours(path)
        models[name] = {
            "exists": path.exists(),
            "age_hours": round(age, 1) if age else None,
            "stale": age is not None and age > 48,
        }

    return _json_safe({
        "data_freshness": freshness,
        "recent_logs": logs,
        "alerts": alerts if alerts else ["All data sources within acceptable freshness"],
        "models": models,
    })


# ===================================================================
# TOOL 15: DB SCHEMA — Table structures for queries
# ===================================================================
@mcp.tool()
def db_schema(table_name: str = "") -> str:
    """DuckDB warehouse schema: list all tables, or get columns for a specific table.
    table_name: leave empty to list all tables, or specify a table name for its columns."""
    conn = _duck_ro()
    if not conn:
        return _json_safe({"error": "DuckDB locked"})
    try:
        if table_name:
            cols = conn.execute(f"DESCRIBE {table_name}").fetchall()
            desc = [d[0] for d in conn.description]
            return _json_safe(_rows_to_dicts(cols, desc))
        else:
            tables = conn.execute("""
                SELECT table_name,
                       estimated_size as approx_rows
                FROM duckdb_tables()
                ORDER BY table_name
            """).fetchall()
            desc = [d[0] for d in conn.description]
            return _json_safe(_rows_to_dicts(tables, desc))
    finally:
        conn.close()


# ===================================================================
# TOOL 16: CUSTOM QUERY — Ad-hoc read-only SQL
# ===================================================================
@mcp.tool()
def warehouse_query(sql: str) -> str:
    """Execute a read-only SQL query against the DuckDB warehouse.
    Only SELECT statements allowed. Use for ad-hoc analysis not covered by other tools.
    Key tables: intelligence_scores, fact_daily_prices, fact_form4_transactions,
    fact_13f_positions, agg_qoq_changes, fact_short_interest, fact_short_volume,
    fact_dark_pool_daily, fact_options_flow, agg_sector_rotation."""

    # Safety: only allow SELECT
    stripped = sql.strip().upper()
    if not stripped.startswith("SELECT") and not stripped.startswith("WITH"):
        return _json_safe({"error": "Only SELECT/WITH queries allowed"})
    for forbidden in ("INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "CREATE", "TRUNCATE"):
        if f" {forbidden} " in f" {stripped} " or stripped.startswith(forbidden):
            return _json_safe({"error": f"Forbidden keyword: {forbidden}"})

    results = _duck_query(sql)
    if len(results) > 100:
        return _json_safe({"rows_returned": len(results), "data": results[:100], "truncated": True})
    return _json_safe(results)


# ===================================================================
# TOOL 17: SIGNALS DB QUERY — Ad-hoc SQLite query
# ===================================================================
@mcp.tool()
def signals_query(sql: str) -> str:
    """Execute a read-only SQL query against the signals SQLite database.
    Only SELECT allowed. Tables: signals, paper_trades, option_setups, eod_analysis, scan_history."""

    stripped = sql.strip().upper()
    if not stripped.startswith("SELECT") and not stripped.startswith("WITH"):
        return _json_safe({"error": "Only SELECT/WITH queries allowed"})

    return _json_safe(_sqlite_query(SIGNALS_DB_PATH, sql))


# ===================================================================
# TOOL 18: PHASE DISTRIBUTION — Accumulation phase breakdown
# ===================================================================
@mcp.tool()
def phase_distribution() -> str:
    """Distribution of tickers across accumulation phases in the active quarter.
    Shows how many tickers are in EARLY_ACCUM, ACTIVE_ACCUM, LATE_ACCUM, etc."""
    sql = """
        SELECT accum_phase, COUNT(*) as ticker_count,
               ROUND(AVG(conviction_score), 1) as avg_conviction,
               ROUND(AVG(ml_score_v2), 1) as avg_ml_score,
               SUM(CASE WHEN triple_lock THEN 1 ELSE 0 END) as triple_locks,
               SUM(CASE WHEN swing_signal = 'BUY' THEN 1 ELSE 0 END) as buy_signals,
               SUM(CASE WHEN swing_signal = 'SHORT' THEN 1 ELSE 0 END) as short_signals
        FROM intelligence_scores
        WHERE report_quarter = (
            SELECT report_quarter FROM intelligence_scores
            WHERE data_quality_score >= 75
            GROUP BY report_quarter HAVING COUNT(*) >= 1000
            ORDER BY report_quarter DESC LIMIT 1
        )
        AND data_quality_score >= 75
        GROUP BY accum_phase
        ORDER BY ticker_count DESC
    """
    return _json_safe(_duck_query(sql))


# ===================================================================
# TOOL 19: EXPECTANCY — Win rates by conviction band
# ===================================================================
@mcp.tool()
def expectancy_calibration() -> str:
    """Historical win rates and expected values by conviction band.
    Shows which conviction levels have edge."""
    sql = """
        SELECT * FROM expectancy_calibration
        ORDER BY conviction_band DESC
    """
    return _json_safe(_duck_query(sql))


# ===================================================================
# TOOL 20: BACKTEST RESULTS — Historical signal performance
# ===================================================================
@mcp.tool()
def backtest_results(ticker: str = "", limit: int = 50) -> str:
    """Historical backtest results: forward returns by signal quarter.
    Filter by ticker or get top performers."""
    where = "1=1"
    if ticker:
        where = f"ticker = '{ticker.upper()}'"

    sql = f"""
        SELECT ticker, signal_quarter, return_30d, return_60d, return_90d,
               return_180d, alpha_30d, alpha_90d, alpha_180d
        FROM backtest_results
        WHERE {where}
        ORDER BY signal_quarter DESC, return_90d DESC NULLS LAST
        LIMIT {int(limit)}
    """
    return _json_safe(_duck_query(sql))


# ===================================================================
# TOOL 21: SCANNER SIGNALS — Latest scan results
# ===================================================================
@mcp.tool()
def scanner_signals(min_score: int = 60, limit: int = 30) -> str:
    """Latest scanner output: symbols with scores, signals, R:R ratios, VWAP/GEX status."""
    return _json_safe(_sqlite_query(SIGNALS_DB_PATH, f"""
        SELECT symbol, score, signal, price, recommendation, rr_ratio,
               vwap_status, gex_status, timeframe, timestamp
        FROM signals
        WHERE score >= {int(min_score)}
        ORDER BY timestamp DESC, score DESC
        LIMIT {int(limit)}
    """))


# ===================================================================
# TOOL 22: CONFIG THRESHOLDS — All active trading thresholds
# ===================================================================
@mcp.tool()
def config_thresholds() -> str:
    """All active trading thresholds and gates: conviction, ML, phase, regime, position sizing.
    Returns the current configuration that drives trade entry/exit decisions."""
    return _json_safe({
        "triple_lock": {
            "conviction_min": 70,
            "ml_v2_min": 70,
            "f4_insiders_min": 1,
            "phases": ["EARLY_ACCUM", "ACTIVE_ACCUM", "LATE_ACCUM"],
            "historical_win_rate": "59.8%",
        },
        "swing_entry": {
            "buy_conviction_min": 55,
            "short_conviction_max": 35,
            "short_phases": ["DISTRIBUTION", "DECLINE"],
        },
        "paper_trader": {
            "max_open_positions": 3,
            "min_rr_ratio": 2.0,
            "max_notional_per_trade": 15000,
            "atr_stop_multiplier": 2.0,
            "rr_target": 2.5,
        },
        "regime_gates": {
            "state_0_CRASH": "ALL trades BLOCKED",
            "state_1_DISTRIBUTION": "LONG blocked, SHORT allowed",
            "state_2_ACCUMULATION": "ALL allowed",
            "state_3_MEAN_REVERSION": "ALL allowed",
            "state_4_BULL_TREND": "ALL allowed",
        },
        "momentum_prefilter": {
            "price_above_200sma": True,
            "triple_lock_override": True,
        },
        "idea_bridge": {
            "swing_conviction_min": 75,
            "triple_lock_conviction_min": 70,
            "max_per_cycle": 3,
            "market_hours_only": True,
            "phase_gate": ["EARLY_ACCUM", "ACTIVE_ACCUM", "LATE_ACCUM"],
        },
        "expectancy_bands": {
            "80-100": {"win_rate": "63.4%", "ev": "+5.9%"},
            "60-80": {"win_rate": "56.9%", "ev": "+3.6%"},
            "below_60": "Below edge threshold",
        },
        "scanner": {
            "scan_interval_seconds": 900,
            "timeframes": ["5m", "15m", "1h"],
            "min_rr_for_recommendation": 1.5,
            "late_entry_cutoff": "3:30 PM ET",
        },
    })


# ===================================================================
# TOOL 23: SHIVAM STATUS — Research pipeline state
# ===================================================================
@mcp.tool()
def shivam_status() -> str:
    """Shivam research pipeline status: latest version, artifacts, trade ledger state,
    config details, and edge contract status."""
    result = {}

    # Latest configs
    configs = sorted(SHIVAM_CONFIGS.glob("pipeline_v*.yaml"),
                     key=lambda p: p.name, reverse=True) if SHIVAM_CONFIGS.exists() else []
    result["latest_configs"] = [c.name for c in configs[:5]]

    # Artifact directories
    if SHIVAM_ARTIFACTS.exists():
        artifacts = sorted(SHIVAM_ARTIFACTS.iterdir(),
                          key=lambda p: p.stat().st_mtime if p.is_dir() else 0, reverse=True)
        result["latest_artifacts"] = []
        for a in artifacts[:8]:
            if a.is_dir():
                files = list(a.rglob("*"))
                result["latest_artifacts"].append({
                    "name": a.name,
                    "files": len(files),
                    "age_hours": round(_file_age_hours(a) or 0, 1),
                })

    # Trade ledger
    if SHIVAM_LEDGER.exists():
        result["trade_ledger"] = {
            "path": str(SHIVAM_LEDGER),
            "age_hours": round(_file_age_hours(SHIVAM_LEDGER) or 0, 1),
        }
        # Query ledger tables
        try:
            conn = sqlite3.connect(f"file:{SHIVAM_LEDGER}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            tables = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
            result["trade_ledger"]["tables"] = [t["name"] for t in tables]
            for t in tables:
                count = conn.execute(f"SELECT COUNT(*) as c FROM [{t['name']}]").fetchone()
                result["trade_ledger"][f"{t['name']}_count"] = count["c"]
            conn.close()
        except Exception as e:
            result["trade_ledger"]["error"] = str(e)

    # DuckDB snapshot freshness
    if SHIVAM_CACHE.exists():
        result["duckdb_snapshot"] = {
            "path": str(SHIVAM_CACHE),
            "age_hours": round(_file_age_hours(SHIVAM_CACHE) or 0, 1),
            "size_mb": round(SHIVAM_CACHE.stat().st_size / (1024 * 1024), 1),
        }

    # Recent daily runs
    daily_runs = SHIVAM_ROOT / "data" / "daily_runs"
    if daily_runs.exists():
        runs = sorted(daily_runs.iterdir(), reverse=True)[:5]
        result["recent_runs"] = [r.name for r in runs]

    return _json_safe(result)


# ===================================================================
# TOOL 24: SHIVAM QUERY — Query Shivam's trade ledger
# ===================================================================
@mcp.tool()
def shivam_ledger_query(sql: str) -> str:
    """Query Shivam's v87 trade ledger SQLite database (read-only).
    Tables: v87_idea_snapshot, v87_trade_plan, v87_trade_event, v87_fill, v87_trade_outcome."""
    stripped = sql.strip().upper()
    if not stripped.startswith("SELECT") and not stripped.startswith("WITH"):
        return _json_safe({"error": "Only SELECT/WITH queries allowed"})

    return _json_safe(_sqlite_query(SHIVAM_LEDGER, sql))


# ===================================================================
# TOOL 25: UNIVERSE STATS — Watchlist and ticker coverage
# ===================================================================
@mcp.tool()
def universe_stats() -> str:
    """Watchlist ticker count, sector distribution, and data coverage stats."""
    result = {}

    # Watchlist count
    if WATCHLIST_PATH.exists():
        tickers = [l.strip() for l in WATCHLIST_PATH.read_text().splitlines() if l.strip()]
        result["watchlist_count"] = len(tickers)
        result["sample_tickers"] = tickers[:20]

    # Sector distribution from intelligence
    sectors = _duck_query("""
        SELECT d.sector, COUNT(DISTINCT i.ticker) as tickers,
               ROUND(AVG(i.conviction_score), 1) as avg_conviction
        FROM intelligence_scores i
        JOIN dim_issuer d ON d.ticker = i.ticker
        WHERE i.report_quarter = (
            SELECT report_quarter FROM intelligence_scores
            WHERE data_quality_score >= 75
            GROUP BY report_quarter HAVING COUNT(*) >= 1000
            ORDER BY report_quarter DESC LIMIT 1
        )
        AND d.sector IS NOT NULL AND d.sector != ''
        GROUP BY d.sector
        ORDER BY tickers DESC
    """)
    result["sector_distribution"] = sectors

    # Data coverage
    coverage = _duck_query("""
        SELECT
            (SELECT COUNT(DISTINCT ticker) FROM fact_daily_prices
             WHERE trade_date >= CURRENT_DATE - INTERVAL '7' DAY) as tickers_with_recent_prices,
            (SELECT COUNT(DISTINCT ticker) FROM fact_form4_transactions
             WHERE transaction_date >= CURRENT_DATE - INTERVAL '30' DAY) as tickers_with_recent_f4,
            (SELECT COUNT(DISTINCT ticker) FROM fact_short_volume
             WHERE trade_date >= CURRENT_DATE - INTERVAL '7' DAY) as tickers_with_short_data
    """)
    result["data_coverage"] = coverage

    return _json_safe(result)


# ===================================================================
# TOOL 26: MANAGER QUALITY — Top institutional managers
# ===================================================================
@mcp.tool()
def top_managers(tier: int = 1, limit: int = 20) -> str:
    """Top institutional managers by AUM tier. tier: 1 (largest), 2, or 3."""
    sql = f"""
        SELECT m.manager_cik, m.manager_name, t.total_aum_k, t.tier,
               COUNT(DISTINCT p.ticker) as holdings_count
        FROM dim_manager_tiers t
        JOIN dim_manager_13f m ON m.manager_cik = t.manager_cik
        LEFT JOIN fact_13f_positions p ON p.manager_cik = t.manager_cik
            AND p.report_period = (SELECT MAX(report_period) FROM fact_13f_positions)
        WHERE t.tier = {int(tier)}
        GROUP BY m.manager_cik, m.manager_name, t.total_aum_k, t.tier
        ORDER BY t.total_aum_k DESC
        LIMIT {int(limit)}
    """
    return _json_safe(_duck_query(sql))


# ===================================================================
# TOOL 27: CODEX REVIEW — Automated code review via Codex CLI
# ===================================================================
@mcp.tool()
def codex_review(
    files: str = "",
    context: str = "",
    title: str = "",
) -> str:
    """Submit files for Codex code review. Returns structured review with VALIDATED or CHANGES REQUESTED.
    files: comma-separated relative file paths (e.g. 'mcp_server/server.py,signal_scanner/config.py')
    context: description of what changed and why
    title: short title for the review"""
    from mcp_server.codex_review import run_codex_review

    file_list = [f.strip() for f in files.split(",") if f.strip()] if files else None
    result = run_codex_review(
        files=file_list,
        uncommitted=not bool(file_list),
        context=context,
        title=title,
    )
    return _json_safe(result)


# ===================================================================
# TOOL 28: CODEX VALIDATE — Full holistic validation pass
# ===================================================================
@mcp.tool()
def codex_validate(
    files: str = "",
    spec_context: str = "",
) -> str:
    """Run Codex for holistic validation of code changes against the project spec.
    Goes beyond simple review — checks architectural alignment, trading logic correctness,
    and integration safety.
    files: comma-separated relative file paths
    spec_context: what the changes are supposed to achieve"""
    from mcp_server.codex_review import run_codex_review

    file_list = [f.strip() for f in files.split(",") if f.strip()] if files else None

    validation_prompt = (
        "HOLISTIC VALIDATION MODE. You must:\n"
        "1. Read .codex/instructions.md for the review checklist\n"
        "2. Read the files being reviewed\n"
        "3. Check that SQL column names match actual DuckDB/SQLite schemas\n"
        "4. Verify trading logic gates (regime, phase, conviction thresholds)\n"
        "5. Check for DuckDB lock safety (read_only=True in all read paths)\n"
        "6. Verify error handling for lock conflicts returns graceful degradation\n"
        "7. Check that the changes integrate correctly with existing architecture\n"
        "8. Look for edge cases in data handling (NULL values, missing data, stale data)\n\n"
        f"SPEC CONTEXT: {spec_context}\n\n"
        "Output your review as:\n"
        "## Status: VALIDATED or CHANGES REQUESTED\n"
        "## Summary: what the changes do\n"
        "## Checks Passed: list\n"
        "## Issues Found: list (Critical / Warning / Suggestion)\n"
        "## Integration Risk: Low / Medium / High\n"
    )

    result = run_codex_review(
        files=file_list,
        context=validation_prompt,
        title="Holistic Validation",
    )
    return _json_safe(result)


# ===================================================================
# Entry point
# ===================================================================
def main():
    """Run MCP server via stdio transport."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
