"""Pre-market preparation script.

Run BEFORE starting the scanner each morning (~8:30 AM ET).
Updates prices, refits HMM regime, refreshes stale data, then emits
a machine-readable readiness verdict: READY | DEGRADED | BLOCKED.

The verdict is persisted to data/warehouse/readiness.json so the
scanner startup in main.py can consume it and refuse to start if BLOCKED.

Usage:
    python run_premarket.py              # Full pre-market prep
    python run_premarket.py --prices-only  # Just update prices + HMM
"""

import argparse
import subprocess
import sys
from datetime import date, datetime, timedelta

import duckdb
from loguru import logger

from signal_scanner.core.readiness import (
    ReadinessState,
    ReadinessStatus,
    latest_complete_trading_day,
    compute_price_freshness,
)

WAREHOUSE = "data/warehouse/sec_intel.duckdb"


_session_ref = None  # set in main() for _run() to update

def _run(step: str, args: list[str]) -> bool:
    if _session_ref:
        _session_ref.set_active_job(step)
    logger.info("--- {} ---", step)
    result = subprocess.run([sys.executable, "-m"] + args, capture_output=False)
    if result.returncode != 0:
        logger.error("FAILED: {} (exit {})", step, result.returncode)
        if _session_ref:
            _session_ref.record_blocked_job(step, f"exit code {result.returncode}")
        return False
    logger.info("OK: {}", step)
    if _session_ref:
        _session_ref.heartbeat()
    return True


def check_price_freshness() -> tuple[str, str]:
    """Return (last_price_date, target_date) for price backfill."""
    conn = duckdb.connect(WAREHOUSE, read_only=True)
    last = conn.execute("SELECT MAX(trade_date) FROM fact_daily_prices").fetchone()[0]
    conn.close()
    target = latest_complete_trading_day()
    return str(last), str(target)


def update_prices(from_date: date, to_date: date) -> bool:
    """Update daily prices via massive_loader (requires date objects)."""
    logger.info("Updating prices: {} -> {}", from_date, to_date)
    try:
        from signal_scanner.institutional_intel.jobs.massive_loader import (
            load_grouped_daily,
        )
        stats = load_grouped_daily(from_date, to_date, rps=5)
        logger.info("Price update: {}", stats)
        return True
    except Exception as e:
        logger.error("Price update failed: {}", e)
        return False


def _check_hmm_model() -> bool:
    """Return True if HMM model file exists and is loadable."""
    from pathlib import Path
    return Path("data/warehouse/models/regime_hmm_daily.pkl").exists()


def _check_ml_models() -> list[str]:
    """Return list of available intraday ML model names."""
    from pathlib import Path
    models_dir = Path("data/warehouse/models")
    available = []
    for name in ["intraday_ml_vwap_mr.pkl", "intraday_ml_fpb.pkl", "intraday_ml_orb_v2.pkl"]:
        if (models_dir / name).exists():
            available.append(name.replace("intraday_ml_", "").replace(".pkl", "").upper())
    return available


def main():
    parser = argparse.ArgumentParser(description="Pre-market preparation")
    parser.add_argument("--prices-only", action="store_true",
                        help="Only update prices and HMM regime")
    args = parser.parse_args()

    # ---- Session protection ----
    from signal_scanner.core.session import SessionRegistry, SessionMode, SessionPhase
    session = SessionRegistry()
    if not session.acquire(SessionMode.PREMARKET_PREP, owner="run_premarket"):
        logger.error("=" * 50)
        logger.error(session.refusal_message())
        logger.error("=" * 50)
        sys.exit(1)
    session.set_phase(SessionPhase.RUNNING)
    session.start_background_heartbeat(interval_seconds=30)
    global _session_ref
    _session_ref = session

    start = datetime.now()
    logger.info("=" * 50)
    logger.info("PRE-MARKET PREP: {}", start.strftime("%Y-%m-%d %H:%M"))
    logger.info("=" * 50)

    state = ReadinessState()
    failures = []

    # 1. Check price freshness and update
    last_price, target = check_price_freshness()
    logger.info("Last price date: {}, target: {}", last_price, target)
    if last_price < target:
        from_dt = datetime.strptime(last_price, "%Y-%m-%d").date() + timedelta(days=1)
        target_dt = datetime.strptime(target, "%Y-%m-%d").date()
        if not update_prices(from_dt, target_dt):
            failures.append("prices")

    # Re-check freshness after any update attempt
    price_ok, age_days, latest_str = compute_price_freshness()
    state.prices_age_days = age_days
    state.latest_price_date = latest_str
    if not price_ok:
        if age_days >= 3:
            state.add_blocked(f"DATA_STALE: prices {age_days} trading days old (max 2)")
        else:
            state.add_degraded(f"DATA_STALE: prices {age_days} trading days old")

    # 2. Verify HMM model exists (EOD refits daily — just check it's there)
    if not _check_hmm_model():
        state.add_blocked("MODEL_UNAVAILABLE: HMM regime model missing — run EOD pipeline")
    else:
        logger.info("HMM regime model: OK (fitted by EOD pipeline)")

    # Steps 3-7 moved to EOD pipeline. Premarket only verifies freshness.
    # EOD runs: Form 4, Short Data, 8-K, ML v2, Squeeze, CTB, Dark Pool
    if not args.prices_only:
        logger.info("Data refresh handled by EOD pipeline — verifying freshness only")

    # 8. Health check (informational, does not affect readiness)
    _run("Health Check", ["signal_scanner.daily_health_check"])

    # 9. Check ML model availability for enabled_scanners
    ml_models = _check_ml_models()
    state.enabled_scanners = ml_models if ml_models else []
    if not ml_models:
        state.add_degraded("MODEL_UNAVAILABLE: no intraday ML models found")

    # ---- Emit final readiness verdict ---- #
    state.resolve_status()
    state.save()

    elapsed = (datetime.now() - start).total_seconds() / 60
    logger.info("=" * 50)

    status = state.readiness_status
    if status == ReadinessStatus.BLOCKED.value:
        logger.error("READINESS: BLOCKED in {:.1f}m", elapsed)
        for r in state.blocked_reasons:
            logger.error("  BLOCK: {}", r)
        for r in state.degraded_reasons:
            logger.warning("  DEGRADED: {}", r)
        logger.error("Scanner WILL NOT START until blockers are resolved.")
    elif status == ReadinessStatus.DEGRADED.value:
        logger.warning("READINESS: DEGRADED in {:.1f}m", elapsed)
        for r in state.degraded_reasons:
            logger.warning("  DEGRADED: {}", r)
        logger.warning("Scanner will start with reduced capability.")
    else:
        logger.info("READINESS: READY in {:.1f}m", elapsed)

    logger.info("=" * 50)
    logger.info("")
    if not state.is_blocked:
        logger.info("Now start scanner:")
        logger.info("  python -m signal_scanner --watchlist universe_master --ibkr-port 7497")
    else:
        logger.info("Fix blockers first, then re-run: python run_premarket.py")
    logger.info("")

    # Release session
    session.release()

    # Exit code reflects readiness
    if state.is_blocked:
        sys.exit(2)
    elif state.is_degraded:
        sys.exit(0)  # degraded is a valid start
    else:
        sys.exit(0)


if __name__ == "__main__":
    main()
