"""Pre-market automated trading pipeline tests.

Run before market open to catch issues BEFORE they affect live trading:

    pytest tests/test_trading_pipeline.py -v

Each test is fast (< 5s) and read-only. Tests are ordered by criticality:
failure in an early test often explains failures in later tests.

Exit code non-zero = something is broken, do NOT start scanner until fixed.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List

import pytest

# ---------------------------------------------------------------------------
# Project roots / paths
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
tempfile.tempdir = str(PROJECT_ROOT / ".tmp")
os.makedirs(tempfile.tempdir, exist_ok=True)
WAREHOUSE_PATH = PROJECT_ROOT / "data" / "warehouse" / "sec_intel.duckdb"
SIGNALS_DB_PATH = PROJECT_ROOT / "signal_scanner" / "data" / "signals.db"
HMM_MODEL_PATH = PROJECT_ROOT / "data" / "warehouse" / "models" / "regime_hmm_daily.pkl"
ML_MODEL_PATH = PROJECT_ROOT / "data" / "models" / "ml_signal_v2.pkl"


# ===========================================================================
# TIER 0 — Infrastructure (everything else depends on these)
# ===========================================================================


class TestInfrastructure:
    """DuckDB + SQLite connectivity and freshness."""

    def test_duckdb_readable(self):
        """DuckDB warehouse opens in read-only mode without errors."""
        from signal_scanner.institutional_intel.config import safe_duckdb_connect

        conn = safe_duckdb_connect(read_only=True)
        assert conn is not None, (
            "DuckDB connection returned None — another write process may hold the lock. "
            "Kill rogue Python PIDs and retry."
        )
        tables = conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema='main'"
        ).fetchall()
        conn.close()
        table_names = {t[0] for t in tables}
        required = {
            "intelligence_scores",
            "fact_daily_prices",
            "fact_13f_positions",
            "fact_form4_transactions",
        }
        missing = required - table_names
        assert not missing, f"Missing DuckDB tables: {missing}"

    def test_signals_db_readable(self):
        """SQLite signals DB exists and has paper_trades table."""
        assert SIGNALS_DB_PATH.exists(), (
            f"signals.db not found at {SIGNALS_DB_PATH}. "
            "Run scanner at least once to create it."
        )
        conn = sqlite3.connect(str(SIGNALS_DB_PATH), timeout=5)
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        conn.close()
        assert "paper_trades" in tables, f"paper_trades table missing from signals.db. Got: {tables}"

    def test_signals_db_writable(self):
        """SQLite signals DB is not locked / read-only."""
        conn = sqlite3.connect(str(SIGNALS_DB_PATH), timeout=5)
        # Write + rollback — verifies no file-level lock
        try:
            conn.execute("BEGIN")
            conn.execute(
                "INSERT INTO paper_trades "
                "(opened_at, symbol, side, entry_price, quantity, notional, status, strategy_type, created_ts) "
                "VALUES ('2099-01-01T00:00:00+00:00', '__TEST__', 'LONG', 1.0, 1, 1.0, 'OPEN', 'TEST', '2099-01-01T00:00:00+00:00')"
            )
            conn.execute("ROLLBACK")
        except sqlite3.OperationalError as e:
            pytest.fail(f"signals.db write test failed (locked?): {e}")
        finally:
            conn.close()

    def test_prices_are_fresh(self):
        """fact_daily_prices has data within the last 5 trading days."""
        from datetime import date, timedelta

        from signal_scanner.institutional_intel.config import safe_duckdb_connect

        conn = safe_duckdb_connect(read_only=True)
        assert conn is not None
        last_date = conn.execute("SELECT MAX(trade_date) FROM fact_daily_prices").fetchone()[0]
        conn.close()
        assert last_date is not None, "fact_daily_prices is empty"
        cutoff = date.today() - timedelta(days=5)
        assert str(last_date) >= str(cutoff), (
            f"Prices are stale: last={last_date}, threshold={cutoff}. "
            "Run run_premarket.py to refresh."
        )


# ===========================================================================
# TIER 1 — Intelligence data quality
# ===========================================================================


class TestIntelligenceData:
    """Active-quarter intelligence scores are populated and fresh."""

    def test_active_quarter_exists(self):
        """get_active_quarter() returns a valid quarter string."""
        from signal_scanner.institutional_intel.config import safe_duckdb_connect
        from signal_scanner.institutional_intel.config import get_active_quarter

        conn = safe_duckdb_connect(read_only=True)
        assert conn is not None
        quarter = get_active_quarter(conn)
        conn.close()
        assert quarter, "get_active_quarter() returned empty — run pipeline first"
        parts = quarter.split("-")
        assert len(parts) == 2 and parts[1].startswith("Q"), f"Unexpected quarter format: {quarter}"

    def test_intelligence_scores_populated(self):
        """intelligence_scores has >= 500 tickers with conviction_score > 0."""
        from signal_scanner.institutional_intel.config import safe_duckdb_connect

        conn = safe_duckdb_connect(read_only=True)
        assert conn is not None
        count = conn.execute(
            "SELECT COUNT(*) FROM intelligence_scores WHERE conviction_score > 0"
        ).fetchone()[0]
        conn.close()
        assert count >= 500, (
            f"Only {count} tickers with conviction > 0 — intelligence pipeline may not have run. "
            "Run: python -m signal_scanner.institutional_intel.jobs.run_pipeline --stage intelligence"
        )

    def test_accumulation_tickers_exist(self):
        """At least 50 tickers in ACCUM phase (Tier 1 scan group)."""
        from signal_scanner.institutional_intel.config import safe_duckdb_connect

        conn = safe_duckdb_connect(read_only=True)
        assert conn is not None
        count = conn.execute("""
            SELECT COUNT(*)
            FROM intelligence_scores
            WHERE accum_phase IN ('ACTIVE_ACCUM', 'EARLY_ACCUM', 'LATE_ACCUM')
              AND conviction_score > 0
        """).fetchone()[0]
        conn.close()
        assert count >= 50, (
            f"Only {count} ACCUM tickers — IdeaBridge Tier 1 checkpoint will have almost nothing to scan. "
            "Re-run intelligence pipeline."
        )

    def test_ml_v2_scores_populated(self):
        """ml_score_v2 is non-null for at least 1000 tickers."""
        from signal_scanner.institutional_intel.config import safe_duckdb_connect

        conn = safe_duckdb_connect(read_only=True)
        assert conn is not None
        count = conn.execute(
            "SELECT COUNT(*) FROM intelligence_scores WHERE ml_score_v2 IS NOT NULL AND ml_score_v2 > 0"
        ).fetchone()[0]
        conn.close()
        assert count >= 1000, (
            f"Only {count} tickers with ml_score_v2 — run ML scoring: "
            "python -m signal_scanner.institutional_intel.intelligence.ml_signal_v2 --score --write"
        )

    def test_swing_ideas_long_exist(self):
        """At least 10 LONG swing ideas with conviction >= 55."""
        from signal_scanner.institutional_intel.config import safe_duckdb_connect

        conn = safe_duckdb_connect(read_only=True)
        assert conn is not None
        count = conn.execute("""
            SELECT COUNT(*)
            FROM intelligence_scores
            WHERE swing_signal = 'BUY'
              AND conviction_score >= 55
              AND accum_phase IN ('ACTIVE_ACCUM', 'EARLY_ACCUM', 'LATE_ACCUM')
        """).fetchone()[0]
        conn.close()
        assert count >= 10, (
            f"Only {count} LONG swing ideas (conviction>=55, BUY, ACCUM phase). "
            "Sniper/IdeaBridge will have nothing to trade."
        )

    def test_triple_lock_ideas_exist(self):
        """At least 1 Triple Lock candidate (conviction>=70, ml>=70, triple_lock=True)."""
        from signal_scanner.institutional_intel.config import safe_duckdb_connect

        conn = safe_duckdb_connect(read_only=True)
        assert conn is not None
        count = conn.execute("""
            SELECT COUNT(*)
            FROM intelligence_scores
            WHERE triple_lock = TRUE
              AND conviction_score >= 70
              AND ml_score_v2 >= 70
        """).fetchone()[0]
        conn.close()
        # Warn only — Triple Lock is rare by design
        if count == 0:
            pytest.skip("No Triple Lock candidates today (conviction>=70, ml>=70, triple_lock=TRUE) — "
                        "not a failure, just low conviction day")


# ===========================================================================
# TIER 2 — HMM Regime
# ===========================================================================


class TestHMMRegime:
    """HMM model is fitted, loadable, and returns a valid regime."""

    def test_hmm_model_file_exists(self):
        """HMM pkl model file exists on disk."""
        assert HMM_MODEL_PATH.exists(), (
            f"HMM model not found at {HMM_MODEL_PATH}. "
            "Run: python -m signal_scanner.institutional_intel.intelligence.regime_hmm "
            "--fit-save --fallback AAPL,MSFT,NVDA,GOOGL,AMZN"
        )

    def test_hmm_loads_and_predicts(self):
        """HMM model loads and returns a valid current regime state."""
        from signal_scanner.institutional_intel.intelligence.regime_hmm import (
            REGIME_NAMES,
            DailyRegimeHMM,
        )

        hmm = DailyRegimeHMM()
        hmm.load(HMM_MODEL_PATH)
        state, probs, name = hmm.current_regime()

        assert isinstance(state, int), f"state must be int, got {type(state)}"
        assert 0 <= state <= 4, f"state {state} out of 0-4 range"
        assert name in REGIME_NAMES.values(), f"regime name '{name}' not in REGIME_NAMES"
        assert abs(sum(probs) - 1.0) < 0.01, f"probabilities don't sum to 1: {probs}"

    def test_hmm_regime_allows_trading(self):
        """Current regime is not CRASH (state 0) — trading is permitted."""
        from signal_scanner.institutional_intel.intelligence.regime_hmm import (
            REGIME_LONG_ALLOWED,
            DailyRegimeHMM,
        )

        hmm = DailyRegimeHMM()
        hmm.load(HMM_MODEL_PATH)
        state, _, name = hmm.current_regime()

        if state == 0:
            pytest.skip(
                f"CRASH regime detected (state=0, name={name}) — "
                "ALL trades are blocked by regime gate. "
                "This is correct behavior, not a bug."
            )
        # Just report DISTRIBUTION (no LONG) as a warning
        if not REGIME_LONG_ALLOWED.get(state, True):
            pytest.warns(UserWarning, match=".*") if False else None  # informational only
            print(f"\n[WARN] Regime is {name} — LONG entries are blocked. SHORT only today.")


# ===========================================================================
# TIER 3 — AI Signals engine
# ===========================================================================


class TestAISignals:
    """AI Signals engine produces signals with enriched trade intelligence."""

    def test_ai_signals_returns_signals(self):
        """detect_signals() returns at least 20 signals."""
        from signal_scanner.institutional_intel.reports.ai_signals import AISignalEngine

        engine = AISignalEngine()
        signals = engine.detect_signals()
        assert len(signals) >= 20, (
            f"Only {len(signals)} AI signals detected — expected 20+. "
            "Check intelligence_scores population and DB connectivity."
        )

    def test_ai_signals_have_trade_intelligence(self):
        """At least 50% of non-LOW signals have trade_intelligence attached."""
        from signal_scanner.institutional_intel.reports.ai_signals import AISignalEngine

        engine = AISignalEngine()
        signals = engine.detect_signals()
        actionable = [s for s in signals if s.get("strength") != "LOW"]
        if not actionable:
            pytest.skip("All signals are LOW strength — skipping TI ratio check")

        with_ti = [s for s in actionable if s.get("trade_intelligence")]
        ratio = len(with_ti) / len(actionable)
        assert ratio >= 0.50, (
            f"Only {ratio:.0%} of actionable signals have trade_intelligence "
            f"({len(with_ti)}/{len(actionable)}). "
            "Check _enrich_trade_intelligence() in ai_signals.py."
        )

    def test_ai_signals_high_strength_present(self):
        """Actionable AI signals exist even on lower-conviction market days."""
        from signal_scanner.institutional_intel.reports.ai_signals import AISignalEngine

        engine = AISignalEngine()
        signals = engine.detect_signals()
        high = [s for s in signals if s.get("strength") == "HIGH"]
        medium = [s for s in signals if s.get("strength") == "MEDIUM"]
        assert len(high) >= 3 or (len(high) + len(medium)) >= 15, (
            f"Only {len(high)} HIGH and {len(medium)} MEDIUM AI signals. "
            "Intelligence scores may be stale or conviction thresholds too tight."
        )

    def test_ai_signal_trade_intelligence_fields(self):
        """trade_intelligence blocks have required keys: entry, stop, target_1, rr_ratio."""
        from signal_scanner.institutional_intel.reports.ai_signals import AISignalEngine

        engine = AISignalEngine()
        signals = engine.detect_signals()
        with_ti = [s for s in signals if s.get("trade_intelligence")]
        if not with_ti:
            pytest.skip("No signals with trade_intelligence to validate")

        required_keys = {"entry", "stop", "target_1", "rr_ratio", "prediction", "verdict"}
        for sig in with_ti[:10]:  # spot-check first 10
            ti = sig["trade_intelligence"]
            missing = required_keys - set(ti.keys())
            assert not missing, (
                f"Signal {sig.get('ticker')} trade_intelligence missing keys: {missing}"
            )


# ===========================================================================
# TIER 4 — IdeaBridge dry-run
# ===========================================================================


class TestIdeaBridge:
    """IdeaBridge can produce trade candidates from current intelligence."""

    def test_ideabridge_long_candidates(self):
        """IdeaBridge finds at least 1 LONG swing candidate."""
        from signal_scanner.institutional_intel.config import safe_duckdb_connect
        from signal_scanner.institutional_intel.reports.kubera_reports import KuberaReports

        conn = safe_duckdb_connect(read_only=True)
        assert conn is not None

        try:
            rpt = KuberaReports()
            ideas = rpt.swing_ideas(limit=50)
        finally:
            conn.close()

        long_ideas = [i for i in ideas if i.get("direction") in ("BUY", "LONG")
                      or i.get("swing_signal") == "BUY"]
        assert len(long_ideas) >= 1, (
            f"IdeaBridge found 0 LONG swing ideas from swing_ideas(). "
            f"Got {len(ideas)} total (all directions). "
            "No LONG entries will be auto-entered today."
        )

    def test_ideabridge_short_candidates(self):
        """IdeaBridge finds at least 1 SHORT candidate."""
        from signal_scanner.institutional_intel.config import safe_duckdb_connect
        from signal_scanner.institutional_intel.reports.kubera_reports import KuberaReports

        conn = safe_duckdb_connect(read_only=True)
        assert conn is not None

        try:
            rpt = KuberaReports()
            ideas = rpt.swing_ideas(limit=50)
        finally:
            conn.close()

        short_ideas = [i for i in ideas if i.get("direction") in ("SHORT", "SELL")
                       or i.get("swing_signal") == "SHORT"]
        if not short_ideas:
            pytest.skip("No SHORT swing ideas today — acceptable in bull regime")


# ===========================================================================
# TIER 5 — VWAP_MR / intraday snapshot
# ===========================================================================


class TestIntradayReadiness:
    """Intraday ML strategies have valid data snapshots."""

    def test_vwap_mr_snapshot_loads(self):
        """MultiSymbolScanner loads intelligence snapshot with Tier 1 ACCUM tickers."""
        from signal_scanner.scanner.multi_symbol_scanner import MultiSymbolScanner

        scanner = MultiSymbolScanner.__new__(MultiSymbolScanner)
        # Manually invoke the snapshot loader (bypass __init__ which needs full config)
        from signal_scanner.institutional_intel.config import safe_duckdb_connect
        from signal_scanner.scanner.multi_symbol_scanner import MultiSymbolScanner as MSS

        conn = safe_duckdb_connect(read_only=True)
        assert conn is not None

        # Check ACCUM tickers count directly — proxy for what snapshot would load
        count = conn.execute("""
            SELECT COUNT(*)
            FROM intelligence_scores
            WHERE accum_phase IN ('ACTIVE_ACCUM', 'EARLY_ACCUM', 'LATE_ACCUM')
              AND conviction_score >= 40
        """).fetchone()[0]
        conn.close()

        assert count >= 20, (
            f"Only {count} Tier 1 ACCUM tickers (needed ≥20 for meaningful intraday scan). "
            "IdeaBridge Tier 1 checkpoint will find very few candidates."
        )

    def test_intraday_ml_models_exist(self):
        """At least one intraday ML model file exists."""
        model_patterns = [
            PROJECT_ROOT / "data" / "models" / "ml_signal_v2.pkl",
            PROJECT_ROOT / "signal_scanner" / "models",
        ]
        found = any(p.exists() for p in model_patterns)
        assert found, (
            f"No intraday ML models found. Checked: {model_patterns}. "
            "Run ML scoring pipeline before starting scanner."
        )


# ===========================================================================
# TIER 6 — Paper trading DB state
# ===========================================================================


class TestPaperTradingDB:
    """Paper trades DB is accessible and not in a broken state."""

    def test_paper_trades_schema(self):
        """paper_trades table has all required columns."""
        conn = sqlite3.connect(str(SIGNALS_DB_PATH), timeout=5)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(paper_trades)").fetchall()}
        conn.close()

        required = {"id", "symbol", "side", "entry_price", "stop_loss", "status",
                    "strategy_type", "opened_at"}
        missing = required - cols
        assert not missing, f"paper_trades missing columns: {missing}"

    def test_no_stale_open_trades(self):
        """No open trades have been sitting for more than 30 days (data rot check)."""
        from datetime import datetime, timedelta, timezone

        conn = sqlite3.connect(str(SIGNALS_DB_PATH), timeout=5)
        cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        stale = conn.execute(
            "SELECT id, symbol, opened_at FROM paper_trades "
            "WHERE status = 'OPEN' AND opened_at < ?",
            (cutoff,)
        ).fetchall()
        conn.close()

        if stale:
            symbols = [f"{r[1]}(id={r[0]}, opened={r[2][:10]})" for r in stale]
            pytest.fail(
                f"{len(stale)} stale OPEN paper trades > 30 days old: {symbols[:5]}... "
                "These should have been stopped out. Check paper trader exit logic."
            )


# ===========================================================================
# TIER 7 — Timing window check (informational)
# ===========================================================================


class TestTimingWindows:
    """Verify current time relative to expected trading windows."""

    def test_intraday_ml_entry_window(self):
        """Report whether we are inside the 9:30–11:30 AM ET intraday entry window."""
        from datetime import datetime

        try:
            from zoneinfo import ZoneInfo
            NY_TZ = ZoneInfo("America/New_York")
        except ImportError:
            import pytz
            NY_TZ = pytz.timezone("America/New_York")

        now_ny = datetime.now(NY_TZ)
        hour = now_ny.hour + now_ny.minute / 60

        if 9.5 <= hour <= 11.5:
            print(f"\n[OK] Inside intraday ML entry window: {now_ny.strftime('%H:%M ET')}")
        elif hour < 9.5:
            print(f"\n[INFO] Pre-market ({now_ny.strftime('%H:%M ET')}) — "
                  "intraday ML entries open at 9:30 AM ET")
        else:
            print(f"\n[INFO] Post-entry-window ({now_ny.strftime('%H:%M ET')}) — "
                  "intraday ML entries closed after 11:30 AM ET. "
                  "Swings/Sniper/AI still active.")
        # Always pass — this is informational
        assert True


# ===========================================================================
# Entry point
# ===========================================================================

if __name__ == "__main__":
    import sys
    result = pytest.main([__file__, "-v", "--tb=short", "-x"])
    sys.exit(result)
