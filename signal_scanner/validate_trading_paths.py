"""Deterministic validation of all trading paths.

Answers four questions for each intraday scanner and idea source:
  1. Can it currently produce candidates?
  2. Can it produce setups?
  3. Can it create paper trades?
  4. If not, what exact gate is blocking it?

Usage:
    python -m signal_scanner.validate_trading_paths
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

from loguru import logger


def _section(title: str) -> str:
    return f"\n{'=' * 70}\n  {title}\n{'=' * 70}"


def _check(label: str, ok: bool, detail: str = "") -> dict:
    status = "PASS" if ok else "FAIL"
    tag = f"  [{status}] {label}"
    if detail:
        tag += f"  ({detail})"
    print(tag)
    return {"check": label, "status": status, "detail": detail}


def validate_models() -> list[dict]:
    """Check all ML models exist and are loadable."""
    print(_section("MODEL AVAILABILITY"))
    results = []
    models = {
        "VWAP_MR": Path("data/warehouse/models/intraday_ml_vwap_mr.pkl"),
        "FPB": Path("data/warehouse/models/intraday_ml_fpb.pkl"),
        "ORB_V2": Path("data/warehouse/models/intraday_ml_orb_v2.pkl"),
        "ML_v2": Path("data/models/ml_signal_v2.pkl"),
        "HMM": Path("data/warehouse/models/regime_hmm_daily.pkl"),
    }
    for name, path in models.items():
        exists = path.exists()
        size = f"{path.stat().st_size / 1024:.0f}KB" if exists else "missing"
        results.append(_check(f"Model {name}", exists, size))
    return results


def validate_intelligence_snapshot() -> list[dict]:
    """Check intelligence snapshot can produce candidates for intraday scanners."""
    print(_section("INTELLIGENCE SNAPSHOT (candidates)"))
    results = []
    try:
        from signal_scanner.institutional_intel.config import safe_duckdb_connect
        conn = safe_duckdb_connect(read_only=True)
        if not conn:
            results.append(_check("DuckDB connection", False, "locked or unavailable"))
            return results
        results.append(_check("DuckDB connection", True))

        # Count ACCUM-phase tickers with conviction >= 55 (VWAP_MR minimum)
        row = conn.execute("""
            SELECT COUNT(*) FROM intelligence_scores
            WHERE report_quarter = (
                SELECT MAX(report_quarter) FROM intelligence_scores
                WHERE data_quality_score >= 75
                GROUP BY report_quarter HAVING COUNT(*) >= 1000
                ORDER BY report_quarter DESC LIMIT 1
            )
            AND accum_phase IN ('EARLY_ACCUM', 'ACTIVE_ACCUM', 'LATE_ACCUM', 'EXPANSION')
            AND conviction_score >= 55
        """).fetchone()
        n_accum = row[0] if row else 0
        results.append(_check("ACCUM tickers (conv>=55)", n_accum > 0, f"{n_accum} tickers"))

        # VWAP_MR requires conv>=65
        row65 = conn.execute("""
            SELECT COUNT(*) FROM intelligence_scores
            WHERE report_quarter = (
                SELECT MAX(report_quarter) FROM intelligence_scores
                WHERE data_quality_score >= 75
                GROUP BY report_quarter HAVING COUNT(*) >= 1000
                ORDER BY report_quarter DESC LIMIT 1
            )
            AND accum_phase IN ('EARLY_ACCUM', 'ACTIVE_ACCUM', 'LATE_ACCUM', 'EXPANSION')
            AND conviction_score >= 65
        """).fetchone()
        n_vwap = row65[0] if row65 else 0
        results.append(_check("VWAP_MR candidates (conv>=65)", n_vwap > 0, f"{n_vwap} tickers"))

        # Triple Lock
        tl = conn.execute("""
            SELECT COUNT(*) FROM intelligence_scores
            WHERE report_quarter = (
                SELECT MAX(report_quarter) FROM intelligence_scores
                WHERE data_quality_score >= 75
                GROUP BY report_quarter HAVING COUNT(*) >= 1000
                ORDER BY report_quarter DESC LIMIT 1
            )
            AND triple_lock = true
        """).fetchone()
        n_tl = tl[0] if tl else 0
        results.append(_check("Triple Lock candidates", n_tl > 0, f"{n_tl} tickers"))

        # Swing BUY ideas
        buy = conn.execute("""
            SELECT COUNT(*) FROM intelligence_scores
            WHERE report_quarter = (
                SELECT MAX(report_quarter) FROM intelligence_scores
                WHERE data_quality_score >= 75
                GROUP BY report_quarter HAVING COUNT(*) >= 1000
                ORDER BY report_quarter DESC LIMIT 1
            )
            AND swing_signal = 'BUY' AND conviction_score >= 55
        """).fetchone()
        n_buy = buy[0] if buy else 0
        results.append(_check("Swing BUY ideas (conv>=55)", n_buy > 0, f"{n_buy} ideas"))

        conn.close()
    except Exception as e:
        results.append(_check("Intelligence query", False, str(e)))
    return results


def validate_price_data() -> list[dict]:
    """Check daily price data freshness."""
    print(_section("PRICE DATA"))
    results = []
    from signal_scanner.core.readiness import compute_price_freshness
    ok, age, latest = compute_price_freshness()
    results.append(_check("Price freshness", ok, f"latest={latest}, {age}d lag"))
    return results


def validate_paper_trading() -> list[dict]:
    """Check paper trading can create trades."""
    print(_section("PAPER TRADE CREATION"))
    results = []
    try:
        from signal_scanner.database.db_manager import DatabaseManager
        from signal_scanner.config import ScannerConfig
        from signal_scanner.paper.paper_trader import PaperTrader

        db = DatabaseManager()
        db.init_db()
        cfg = ScannerConfig()

        results.append(_check("Paper trading enabled", cfg.paper_trading_enabled))
        results.append(_check("Max open positions", True, str(cfg.paper_max_open_positions)))

        # Check current open positions
        open_trades = db.get_open_paper_trades()
        at_limit = len(open_trades) >= cfg.paper_max_open_positions
        results.append(_check(
            "Position capacity",
            not at_limit,
            f"{len(open_trades)}/{cfg.paper_max_open_positions} open"
            + (" — AT LIMIT, new entries blocked" if at_limit else ""),
        ))

    except Exception as e:
        results.append(_check("Paper trade system", False, str(e)))
    return results


def validate_regime() -> list[dict]:
    """Check HMM regime allows trading."""
    print(_section("HMM REGIME"))
    results = []
    try:
        from signal_scanner.institutional_intel.intelligence.regime_hmm import DailyRegimeHMM
        hmm = DailyRegimeHMM()
        hmm.load()
        raw = hmm.current_regime()
        # current_regime may return int, numpy scalar, tuple, or array
        if isinstance(raw, (tuple, list)):
            state = int(raw[0])
        elif raw is not None:
            state = int(raw)
        else:
            state = None
        state_name = {0: "CRASH", 1: "DISTRIBUTION", 2: "ACCUMULATION",
                      3: "MEAN_REVERSION", 4: "BULL_TREND"}.get(state, f"UNKNOWN({state})")
        long_ok = hmm.is_long_allowed(state)
        short_ok = hmm.is_short_allowed(state)
        results.append(_check("HMM model loaded", True, f"state={state} ({state_name})"))
        results.append(_check("LONG entries allowed", long_ok,
                               "" if long_ok else "GATE: REGIME_BLOCKED for LONG"))
        # SHORT blocked in ACCUMULATION is normal — report but don't fail
        results.append(_check("SHORT entries allowed", True,
                               "YES" if short_ok else f"blocked by {state_name} (informational)"))
    except Exception as e:
        results.append(_check("HMM regime", False, str(e)))
    return results


def validate_intraday_scanners() -> list[dict]:
    """Check each intraday scanner can load its model."""
    print(_section("INTRADAY SCANNER READINESS"))
    results = []
    scanners = {
        "VWAP_MR": "intraday_ml_vwap_mr.pkl",
        "FPB": "intraday_ml_fpb.pkl",
        "ORB_V2": "intraday_ml_orb_v2.pkl",
    }
    for name, model_file in scanners.items():
        path = Path("data/warehouse/models") / model_file
        if not path.exists():
            results.append(_check(f"{name} model", False,
                                   f"GATE: MODEL_UNAVAILABLE — {path}"))
            continue

        # Try loading
        try:
            import pickle
            with open(path, "rb") as f:
                data = pickle.load(f)
            model = data.get("model")
            metrics = data.get("metrics", {})
            auc = metrics.get("val_auc", 0)
            n_features = len(metrics.get("feature_cols", []))
            results.append(_check(f"{name} model loadable", model is not None,
                                   f"AUC={auc:.3f}, {n_features} features"))
        except Exception as e:
            results.append(_check(f"{name} model", False, str(e)))

    return results


def validate_ibkr() -> list[dict]:
    """Check IBKR connectivity (non-blocking — just reports status)."""
    print(_section("IBKR CONNECTIVITY"))
    results = []
    # Check via signals.db scan history
    try:
        import sqlite3
        db_path = Path("signal_scanner/data/signals.db")
        if db_path.exists():
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT data_source FROM scan_history ORDER BY id DESC LIMIT 1"
            ).fetchone()
            conn.close()
            if row:
                src = dict(row).get("data_source", "UNKNOWN")
                try:
                    from zoneinfo import ZoneInfo
                    weekend = datetime.now(ZoneInfo("America/New_York")).weekday() >= 5
                except Exception:
                    weekend = False
                ok = src == "IBKR" or weekend
                detail = f"source={src}"
                if weekend and src != "IBKR":
                    detail += " (weekend - IBKR not expected)"
                results.append(_check("Last scan source", ok, detail))
            else:
                results.append(_check("Scan history", False, "no scans recorded"))
        else:
            results.append(_check("Signals DB", False, "not found"))
    except Exception as e:
        results.append(_check("IBKR check", False, str(e)))
    return results


def validate_path_execution() -> list[dict]:
    """Drive each scanner through a controlled setup -> entry path.

    Uses a temporary DB and mocked IBKR data to prove each scanner CAN create
    a paper trade when all gates pass. This is the definitive proof.
    """
    print(_section("PATH EXECUTION (controlled end-to-end)"))
    results = []

    import tempfile
    import numpy as np
    import pandas as pd
    from unittest.mock import MagicMock, patch

    tmp_dir = Path(tempfile.mkdtemp(prefix="qb_validate_"))
    try:
        from signal_scanner.database.db_manager import DatabaseManager
        test_db = DatabaseManager(str(tmp_dir / "validate.db"))
        test_db.init_db()

        def _make_scanner(cls, ticker="AAPL"):
            connector = MagicMock()
            connector.is_connected.return_value = True
            scanner_mock = MagicMock()
            scanner_mock._intelligence_snapshot = {
                ticker: {"inst_phase": "ACTIVE_ACCUM", "inst_conviction": 80},
            }
            scanner_mock.market_regime = None
            s = cls(connector, test_db, scanner_mock)
            s._daily_context = {ticker: {"prev_close": 180.0, "atr_20d": 3.0}}
            return s

        def _et_now():
            try:
                from zoneinfo import ZoneInfo
                return datetime(2026, 3, 16, 10, 15, tzinfo=ZoneInfo("America/New_York"))
            except ImportError:
                return datetime(2026, 3, 16, 10, 15)

        # --- VWAP_MR ---
        try:
            from signal_scanner.paper.vwap_mr_live import VWAPMRLiveScanner
            vwap = _make_scanner(VWAPMRLiveScanner)
            vwap._model = MagicMock()
            vwap._feature_cols = ["f1"]
            with patch.object(vwap, '_compute_features', return_value={
                "vwap_cross_count": 4, "price_vs_vwap_1000": 0.1,
                "_vwap_array": np.full(60, 180.0), "_bars_n": 60, "_or_count": 15,
            }), patch.object(vwap, '_check_vwap_setup', return_value={"dip_detected": True}), \
                 patch.object(vwap, '_score_ml', return_value=0.78), \
                 patch.object(vwap, '_compute_ml_percentile', return_value=97), \
                 patch.object(vwap, '_get_intel', return_value={"inst_conviction": 80, "inst_phase": "ACTIVE_ACCUM"}):
                n = 60
                bars = pd.DataFrame({
                    "Open": np.full(n, 179.95), "High": np.full(n, 180.1),
                    "Low": np.full(n, 179.85), "Close": np.full(n, 180.0),
                    "Volume": np.full(n, 50000),
                }, index=pd.date_range("2026-03-16 09:30", periods=n, freq="1min"))
                vwap._connector.get_price_data.return_value = bars
                ok = vwap._scan_ticker("AAPL", _et_now())
            trades = test_db.get_open_paper_trades()
            vwap_ok = ok and len(trades) >= 1 and any("VWAP_MR" in (t.get("recommendation_source") or "") for t in trades)
            results.append(_check("VWAP_MR: setup -> trade", vwap_ok,
                                   f"trade created" if vwap_ok else "no trade"))
        except Exception as e:
            results.append(_check("VWAP_MR path", False, str(e)))

        # --- FPB ---
        try:
            from signal_scanner.paper.fpb_live import FPBLiveScanner
            fpb = _make_scanner(FPBLiveScanner, "MSFT")
            fpb._model = MagicMock()
            fpb._feature_cols = ["f1"]
            with patch.object(fpb, '_compute_features', return_value={
                "or_high": 181.0, "or_low": 179.0,
            }), patch.object(fpb, '_check_fpb_setup', return_value={
                "entry_price": 181.5, "stop_price": 179.0,
            }), patch.object(fpb, '_score_ml', return_value=0.85), \
                 patch.object(fpb, '_compute_ml_percentile', return_value=99), \
                 patch.object(fpb, '_detect_sniper_candles', return_value=True), \
                 patch.object(fpb, '_get_intel', return_value={"inst_conviction": 80, "inst_phase": "ACTIVE_ACCUM"}):
                n = 60
                bars = pd.DataFrame({
                    "Open": np.full(n, 179.95), "High": np.full(n, 181.6),
                    "Low": np.full(n, 179.0), "Close": np.full(n, 181.5),
                    "Volume": np.full(n, 50000),
                }, index=pd.date_range("2026-03-16 09:30", periods=n, freq="1min"))
                fpb._connector.get_price_data.return_value = bars
                ok = fpb._scan_ticker("MSFT", _et_now())
            trades = test_db.get_open_paper_trades()
            fpb_ok = ok and any("FPB" in (t.get("recommendation_source") or "") for t in trades)
            results.append(_check("FPB: setup -> trade", fpb_ok,
                                   "trade created" if fpb_ok else "no trade"))
        except Exception as e:
            results.append(_check("FPB path", False, str(e)))

        # --- ORB_V2 ---
        try:
            from signal_scanner.paper.orb_v2_live import ORBV2LiveScanner
            orb = _make_scanner(ORBV2LiveScanner, "NVDA")
            orb._model = MagicMock()
            orb._feature_cols = ["f1"]
            with patch.object(orb, '_compute_features', return_value={
                "or_high": 181.0, "or_low": 179.0,
            }), patch.object(orb, '_check_orb_v2_setup', return_value={
                "entry_price": 181.2, "stop_price": 180.0, "quality_score": 5,
                "or_range_pct": 0.011, "gap_pct": 0.5, "body_ratio": 0.65, "wick_ratio": 0.15,
            }), patch.object(orb, '_score_ml', return_value=0.72), \
                 patch.object(orb, '_get_intel', return_value={"inst_conviction": 70, "inst_phase": "ACTIVE_ACCUM"}):
                n = 60
                bars = pd.DataFrame({
                    "Open": np.full(n, 179.95), "High": np.full(n, 181.3),
                    "Low": np.full(n, 179.0), "Close": np.full(n, 181.2),
                    "Volume": np.full(n, 50000),
                }, index=pd.date_range("2026-03-16 09:30", periods=n, freq="1min"))
                orb._connector.get_price_data.return_value = bars
                ok = orb._scan_ticker("NVDA", _et_now())
            trades = test_db.get_open_paper_trades()
            orb_ok = ok and any("ORB_V2" in (t.get("recommendation_source") or "") for t in trades)
            results.append(_check("ORB_V2: setup -> trade", orb_ok,
                                   "trade created" if orb_ok else "no trade"))
        except Exception as e:
            results.append(_check("ORB_V2 path", False, str(e)))

        # --- IdeaBridge -> enter_idea_trade ---
        try:
            from signal_scanner.config import ScannerConfig
            from signal_scanner.paper.paper_trader import PaperTrader
            pt = PaperTrader(test_db, ScannerConfig())
            tid = pt.enter_idea_trade({
                "symbol": "GOOG", "side": "LONG", "entry_price": 175.0,
                "stop_loss": 170.0, "target_1": 180.0, "target_2": 185.0,
                "source": "SWING_IDEA_BUY", "market_regime": "ACCUMULATION",
            })
            results.append(_check("IdeaBridge: idea -> trade", tid is not None,
                                   f"trade_id={tid}" if tid else "blocked"))
        except Exception as e:
            results.append(_check("IdeaBridge path", False, str(e)))

        # --- Scanner MTF -> process_scan_rows ---
        try:
            row = {
                "symbol": "META", "recommendation": "BUY", "stock_state": "NEW",
                "recommendation_confirms": 1, "price": 500.0, "stop_loss": 490.0,
                "target_1": 510.0, "target_2": 520.0, "score": 85.0,
                "rr_ratio": 2.0, "mtf_score": 0.85, "signal": "CONFLUENCE_BUY",
                "market_regime": "ACCUMULATION", "gex_status": "POSITIVE",
                "session_time": "REGULAR", "trade_conditions": "validate",
                "inst_phase": "ACTIVE_ACCUM", "inst_conviction": 70,
                "inst_triple_lock": False, "inst_ml_score_v2": 60,
                "inst_price_above_200sma": 1,
            }
            with patch.object(pt, '_past_late_entry_cutoff', return_value=False), \
                 patch.object(pt, '_entry_policy_violation', return_value=""), \
                 patch.object(pt, '_check_eod_evaluation'):
                pt.process_scan_rows([row])
            trades = test_db.get_open_paper_trades()
            mtf_ok = any(t["symbol"] == "META" for t in trades)
            results.append(_check("Scanner MTF: row -> trade", mtf_ok,
                                   "trade created" if mtf_ok else "blocked"))
        except Exception as e:
            results.append(_check("Scanner MTF path", False, str(e)))

    except Exception as e:
        results.append(_check("Path execution setup", False, str(e)))
    finally:
        import shutil
        shutil.rmtree(tmp_dir, ignore_errors=True)

    return results


def main():
    print(_section("QUANT-BRIDGE TRADING PATH VALIDATION"))
    print(f"  Time: {datetime.now(timezone.utc).isoformat()[:19]}Z")

    all_results = []
    all_results.extend(validate_models())
    all_results.extend(validate_price_data())
    all_results.extend(validate_intelligence_snapshot())
    all_results.extend(validate_regime())
    all_results.extend(validate_intraday_scanners())
    all_results.extend(validate_paper_trading())
    all_results.extend(validate_ibkr())
    all_results.extend(validate_path_execution())

    # Summary
    passes = sum(1 for r in all_results if r["status"] == "PASS")
    fails = sum(1 for r in all_results if r["status"] == "FAIL")

    print(_section("VALIDATION SUMMARY"))
    print(f"  {passes} PASS  |  {fails} FAIL  |  {len(all_results)} total")

    if fails > 0:
        print(f"\n  BLOCKING GATES:")
        for r in all_results:
            if r["status"] == "FAIL":
                print(f"    - {r['check']}: {r['detail']}")

    print()
    sys.exit(1 if fails > 0 else 0)


if __name__ == "__main__":
    main()
