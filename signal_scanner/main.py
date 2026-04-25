"""Signal Command Center - entry point.

Usage:
    python -m signal_scanner.main                              # Full scanner + dashboard (uses universe_master)
    python -m signal_scanner.main --watchlist universe_master  # Explicit universe master (default)
    python -m signal_scanner.main --watchlist sp500            # UI filter: S&P 500 only
    python -m signal_scanner.main --scan-once            # Single scan, no dashboard
    python -m signal_scanner.main --no-dashboard         # Scanner only, no web UI

    # Price backfill (runs inside dashboard process to avoid DuckDB lock on Windows)
    python -m signal_scanner.main --backfill-prices --price-from 2020-12-29 --price-to 2024-01-01
"""

import argparse
import threading
import time
from datetime import date, datetime, timezone

from loguru import logger

from signal_scanner.analytics.eod_analyzer import EODAnalyzer
from signal_scanner.config import DashboardConfig, IBKRConfig, ScannerConfig
from signal_scanner.core.ibkr_connector import DataConnector
from signal_scanner.database.db_manager import DatabaseManager
from signal_scanner.core.readiness import (
    ReadinessState,
    ReadinessStatus,
    business_day_lag,
    compute_price_freshness,
)
from signal_scanner.core.telemetry import (
    record_skip, SkipReason, Subsystem,
    reset_session_counters, flush_funnel, reset_funnel,
)
from signal_scanner.scanner.multi_symbol_scanner import MultiSymbolScanner
from signal_scanner.utils.logger import setup_logger


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Signal Command Center - Multi-symbol quantitative signal scanner (IBKR-only)",
    )
    parser.add_argument(
        "--watchlist",
        default="universe_master",
        help="Watchlist name without .txt extension (default: universe_master)",
    )
    parser.add_argument(
        "--no-dashboard",
        action="store_true",
        help="Run scanner only without the web dashboard",
    )
    parser.add_argument(
        "--scan-once",
        action="store_true",
        help="Run a single scan and exit (useful for testing)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="Dashboard port (default: 8050)",
    )
    parser.add_argument(
        "--alerts",
        action="store_true",
        help="Enable desktop toast notifications for high-conviction signals",
    )
    parser.add_argument(
        "--ibkr-port",
        type=int,
        default=None,
        help="Override IBKR port (e.g. 7497 paper, 7496 live)",
    )
    parser.add_argument(
        "--ibkr-client-id",
        type=int,
        default=None,
        help="Override IBKR client ID",
    )
    parser.add_argument(
        "--ibkr-reconnect-seconds",
        type=int,
        default=30,
        help="How often to retry IBKR connection when disconnected (default: 30s)",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Run with debug-friendly logging/UI behavior",
    )
    parser.add_argument(
        "--ibkr-live",
        default="",
        help=(
            "Comma-separated strategies for IBKR bracket order execution "
            "(e.g., VWAP_MR or VWAP_MR,SCANNER_MTF). Default: empty = all SIM."
        ),
    )

    # Price loader integration (runs within same process to avoid DuckDB lock)
    parser.add_argument(
        "--backfill-prices",
        action="store_true",
        help="Run price data backfill in background (avoids DuckDB lock conflict)",
    )
    parser.add_argument(
        "--price-from",
        default="2020-01-01",
        help="Price backfill start date (default: 2020-01-01)",
    )
    parser.add_argument(
        "--price-to",
        default="",
        help="Price backfill end date (default: yesterday)",
    )
    parser.add_argument(
        "--price-rps",
        type=float,
        default=0.12,
        help="Price API requests per second (default: 0.12 for free tier)",
    )
    return parser.parse_args()


def _check_data_freshness() -> dict:
    """Return freshness status for critical data sources.

    Returns dict with keys: ok (bool), prices_age_days (int|None),
    latest_price_date (str|None), warnings (list[str]).
    """
    result: dict = {"ok": True, "prices_age_days": None, "latest_price_date": None, "warnings": []}
    try:
        from signal_scanner.institutional_intel.config import safe_duckdb_connect
        conn = safe_duckdb_connect(read_only=True)
        if conn is None:
            result["warnings"].append("DuckDB locked — cannot verify freshness")
            return result
        try:
            row = conn.execute(
                "SELECT MAX(trade_date) FROM fact_daily_prices"
            ).fetchone()
            if row and row[0]:
                latest = row[0] if isinstance(row[0], date) else date.fromisoformat(str(row[0]))
                lag = business_day_lag(latest)
                result["latest_price_date"] = str(latest)
                result["prices_age_days"] = lag
                if lag > 0:
                    result["ok"] = False
                    result["warnings"].append(
                        f"Daily prices are {lag} trading days stale (latest: {latest})"
                    )
        finally:
            conn.close()
    except Exception as e:
        result["warnings"].append(f"Freshness check error: {e}")
    return result


def main() -> None:
    """Entry point for Signal Command Center."""
    args = parse_args()
    setup_logger()
    if args.debug:
        logger.info("Debug mode enabled")

    logger.info("=" * 60)
    logger.info("Signal Command Center starting (IBKR-only mode)")
    logger.info("=" * 60)

    # ---- Session protection ----
    from signal_scanner.core.session import SessionRegistry, SessionMode, SessionPhase
    session = SessionRegistry()
    if not session.acquire(
        SessionMode.LIVE_EXECUTION,
        owner="scanner",
        dashboard_port=args.port or 8050,
        ibkr_client_id=args.ibkr_client_id or 20,
    ):
        logger.error("=" * 60)
        logger.error(session.refusal_message())
        logger.error("=" * 60)
        import sys
        sys.exit(1)
    session.set_phase(SessionPhase.RUNNING)

    # ---- Readiness enforcement ----
    # Load readiness state from last run_premarket.py execution
    readiness = ReadinessState.load()
    if readiness.is_blocked:
        logger.error("=" * 60)
        logger.error("STARTUP BLOCKED by pre-market readiness gate")
        for r in readiness.blocked_reasons:
            logger.error("  BLOCK: {}", r)
        logger.error("Run 'python run_premarket.py' to resolve, then retry.")
        logger.error("=" * 60)
        import sys
        sys.exit(2)

    # ---- Data freshness gate ----
    # Re-check live (premarket result may be stale if scanner restarts mid-day)
    freshness = _check_data_freshness()
    price_ok, age_days, latest_str = compute_price_freshness()
    readiness.prices_age_days = age_days
    readiness.latest_price_date = latest_str
    if freshness["warnings"]:
        for w in freshness["warnings"]:
            logger.warning("FRESHNESS: {}", w)
    if not freshness["ok"]:
        readiness.add_degraded(f"DATA_STALE: prices {age_days} trading days old")
        record_skip(Subsystem.EXECUTION_LOOP, SkipReason.DATA_STALE,
                     f"prices {age_days}d stale at startup")
        logger.error("=" * 60)
        logger.error(
            "DATA FRESHNESS GATE FAILED — prices are {} trading days stale (latest: {}). "
            "Live scanning will run in DEGRADED mode. "
            "Run EOD pipeline to refresh: python -m signal_scanner.institutional_intel.jobs.run_eod_pipeline",
            freshness["prices_age_days"], freshness["latest_price_date"],
        )
        logger.error("=" * 60)
    _data_degraded = not freshness["ok"]
    reset_session_counters()  # fresh session
    reset_funnel()

    # ---- Initialize database ----
    db = DatabaseManager()
    db.init_db()
    db.cleanup_old_signals(days=7)

    # ---- Initialize data connector ----
    ib_cfg = IBKRConfig()
    if args.ibkr_port is not None:
        ib_cfg.port = args.ibkr_port
    if args.ibkr_client_id is not None:
        ib_cfg.client_id = args.ibkr_client_id

    connector = DataConnector(ib_cfg)

    # ---- Enable desktop alerts if requested ----
    if args.alerts:
        from signal_scanner.utils import notifications

        notifications.enable()
        logger.info("Desktop notifications enabled")

    # ---- Initialize scanner ----
    scanner = MultiSymbolScanner(connector, db)
    scanner.current_watchlist = args.watchlist
    scanner.data_degraded = _data_degraded
    scanner.data_freshness = freshness
    readiness.configured_watchlist = args.watchlist
    readiness.resolve_status()
    scanner.readiness = readiness
    eod = EODAnalyzer(db)

    # ---- Single scan mode ----
    if args.scan_once:
        connected = connector.connect_ibkr()
        if not connected:
            logger.error(
                "Cannot connect to IBKR. Ensure TWS/Gateway is running with API enabled. "
                "Scanner requires IBKR to operate."
            )
            return
        logger.info(f"Running single scan: watchlist='{args.watchlist}'")
        results = scanner.scan_watchlist(args.watchlist)
        logger.info(f"Scan complete: {len(results)} signals")

        from signal_scanner.scanner.signal_ranker import SignalRanker

        top = SignalRanker.rank_signals(results, min_score=40)
        if top:
            logger.info("Top signals:")
            for s in top[:15]:
                logger.info(
                    f"  {s['symbol']:6s} | {s['signal']:7s} | "
                    f"Score: {s['score']:3d} | {s['timeframe']} | "
                    f"RSI: {s.get('rsi', 'N/A')}"
                )
        else:
            logger.info("No signals above threshold")
        return

    # ---- PREFLIGHT: Connect to IBKR NOW (fail loud, not silent) ----
    logger.info("PREFLIGHT: Connecting to IBKR...")
    _preflight_ok = connector.connect_ibkr()
    if _preflight_ok:
        logger.info(
            "PREFLIGHT PASSED: IBKR connected on port {} (clientId={})",
            connector._connected_port, connector._connected_client_id,
        )
    else:
        logger.error("=" * 60)
        logger.error("PREFLIGHT FAILED: Cannot connect to IBKR!")
        logger.error(
            "Intraday ML (VWAP_MR/FPB/ORB_V2) WILL NOT WORK without IBKR."
        )
        logger.error(
            "Check: TWS/Gateway running? API enabled? Port {}? "
            "Last error: {}",
            ib_cfg.port, connector._last_ibkr_error,
        )
        logger.error("=" * 60)
        # Don't silently continue — give user 10s to see the error
        import sys
        print(
            "\n*** IBKR CONNECTION FAILED ***\n"
            "The scanner will start but Intraday ML strategies WILL NOT FIRE.\n"
            "Fix: Start TWS/Gateway, enable API, check port.\n",
            file=sys.stderr,
        )

    # ---- Start scheduled scanning ----
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.cron import CronTrigger

    scheduler = BackgroundScheduler()
    scan_interval = ScannerConfig().scan_interval_seconds
    ibkr_reconnect_seconds = max(5, int(args.ibkr_reconnect_seconds))

    # Shared lock: IBKR (ib_insync) is NOT thread-safe.
    # Intraday scanners and the main scan must not use IBKR concurrently.
    ibkr_lock = threading.Lock()

    def _et_now():
        """Current time in US/Eastern."""
        try:
            from zoneinfo import ZoneInfo
            return datetime.now(ZoneInfo("America/New_York"))
        except ImportError:
            return datetime.now()

    def _et_hm() -> int:
        """Current ET as HHMM int (e.g. 940 = 9:40 AM)."""
        n = _et_now()
        return n.hour * 100 + n.minute

    def _is_market_hours() -> bool:
        """True during normal market hours 9:30-15:55 ET."""
        return 930 <= _et_hm() <= 1555

    # ------------------------------------------------------------------ #
    # EXECUTION LOOP — market hours only, live universe, fast             #
    # Runs the intelligence-filtered universe (<2 min scan budget).       #
    # Does NOT run during the intraday ML entry window (9:40-11:35).      #
    # ------------------------------------------------------------------ #
    def run_execution_scan() -> None:
        """Market-hours scan: small live universe, fast, respects intraday ML."""
        _sub = Subsystem.EXECUTION_LOOP
        if not _is_market_hours():
            return  # Off-hours: handled by research loop
        session.set_active_job("execution_scan")
        if scanner.data_degraded:
            record_skip(_sub, SkipReason.DATA_STALE, "prices stale, running degraded")
        if not connector.is_connected():
            connected = connector.connect_ibkr()
            if not connected:
                record_skip(_sub, SkipReason.IBKR_DISCONNECTED)
                session.record_blocked_job("execution_scan", "IBKR_DISCONNECTED")
                logger.warning("IBKR unavailable — execution scan skipped")
                return

        # Unconditional yield during intraday ML entry window
        hm = _et_hm()
        if 940 <= hm <= 1135:
            logger.info(
                "Execution scan deferred — intraday ML entry window (9:40-11:35 ET). "
                "IBKR reserved for VWAP_MR/FPB/ORB_V2."
            )
            return

        if not ibkr_lock.acquire(timeout=5):
            record_skip(_sub, SkipReason.LOCK_TIMEOUT, "IBKR lock held by intraday scanner")
            logger.debug("Execution scan skipped — IBKR lock held by intraday scanner")
            return
        try:
            wl = args.watchlist
            live_symbols = scanner.get_live_universe(
                wl,
                runtime_budget_seconds=120.0,  # 2-min budget
                min_conviction=40.0,
            )
            if live_symbols:
                logger.info(
                    "EXECUTION scan: {} symbols (budget=120s) from {}",
                    len(live_symbols), wl,
                )
                readiness.live_universe_size = len(live_symbols)
                scanner.scan_symbols(live_symbols, source_label=f"{wl}:live")
            else:
                record_skip(_sub, SkipReason.NO_LIVE_UNIVERSE)
                logger.info("Execution scan: no qualifying symbols in live universe")

            # Persist runtime readiness after each execution cycle
            readiness.ibkr_connected = connector.is_connected()
            readiness.active_scan_source = scanner.active_scan_source
            readiness.resolve_status()
            readiness.save()
        finally:
            ibkr_lock.release()
            session.clear_active_job()

    # ------------------------------------------------------------------ #
    # RESEARCH LOOP — pre-market and post-market only, full universe      #
    # Runs 6:00-9:29 ET and 16:00-20:00 ET to refresh the full           #
    # intelligence picture. Does NOT run overnight.                       #
    # ------------------------------------------------------------------ #
    def _is_research_window() -> bool:
        """True during pre-market (6:00-9:29 ET) or post-market (16:00-20:00 ET)."""
        hm = _et_hm()
        return (600 <= hm <= 929) or (1600 <= hm <= 2000)

    def run_research_scan() -> None:
        """Pre/post-market scan: full universe, no time budget, no lock contention."""
        if not _is_research_window():
            return  # Market hours or overnight — skip
        if not connector.is_connected():
            connected = connector.connect_ibkr()
            if not connected:
                logger.warning("IBKR unavailable — research scan skipped")
                return

        if not ibkr_lock.acquire(timeout=5):
            logger.debug("Research scan skipped — IBKR lock held")
            return
        try:
            wl = args.watchlist
            logger.info("RESEARCH scan: full universe from {}", wl)
            scanner.scan_watchlist(wl)
        finally:
            ibkr_lock.release()

    def run_ibkr_heartbeat() -> None:
        """Retry IBKR connection independently from scan schedule."""
        if connector.is_connected():
            return
        connected = connector.connect_ibkr()
        if connected:
            logger.info("IBKR heartbeat reconnected successfully")
        else:
            logger.warning("IBKR heartbeat reconnect failed; will retry")

    scheduler.add_job(
        run_ibkr_heartbeat,
        trigger="interval",
        seconds=ibkr_reconnect_seconds,
        id="ibkr_heartbeat",
        name="IBKR Heartbeat",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=15,
    )

    # Session heartbeat — updates last_heartbeat so stale detection works
    def _session_heartbeat() -> None:
        session.heartbeat()

    scheduler.add_job(
        _session_heartbeat,
        trigger="interval",
        seconds=60,
        id="session_heartbeat",
        name="Session Heartbeat",
        max_instances=1,
        coalesce=True,
    )

    # Daily DB retention cleanup
    scheduler.add_job(
        db.cleanup_old_signals,
        trigger="interval",
        hours=24,
        kwargs={"days": 7},
        id="db_cleanup",
        name="DB Cleanup",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=300,
    )

    # Smart EOD evaluation — close weak positions, promote strong ones to swing.
    def run_eod_close_job() -> None:
        try:
            paper_trader = scanner._paper_trader
            rows = scanner.last_mtf_results or []
            paper_trader.run_eod_evaluation(rows)
            flush_funnel()  # persist trade funnel to SQLite at EOD
            logger.info("EOD evaluation job completed")
        except Exception as ex:
            logger.warning(f"EOD evaluation job error: {ex}")

    cfg = ScannerConfig()
    scheduler.add_job(
        run_eod_close_job,
        trigger=CronTrigger(
            day_of_week="mon-fri",
            hour=cfg.eod_evaluation_hour,
            minute=cfg.eod_evaluation_minute,
            timezone="America/New_York",
        ),
        id="eod_evaluation",
        name="EOD Evaluation",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=120,
    )

    # End-of-day analysis refresh (plus quick backfill on startup).
    scheduler.add_job(
        eod.run_recent,
        trigger=CronTrigger(day_of_week="mon-fri", hour=16, minute=10, timezone="America/New_York"),
        kwargs={"days": 5},
        id="eod_analysis",
        name="EOD Analysis",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=600,
    )

    # -- Daily price refresh (within same process, no DuckDB lock conflict) --
    def run_price_refresh_job() -> None:
        """Fetch recent daily prices for warehouse tickers."""
        try:
            from signal_scanner.institutional_intel.jobs.massive_loader import (
                refresh_warehouse_prices,
            )
            stats = refresh_warehouse_prices(days_back=7, rps=4.0)
            logger.info("Daily price refresh done: {}", stats)
        except Exception as e:
            logger.warning(f"Price refresh job error: {e}")

    scheduler.add_job(
        run_price_refresh_job,
        trigger=CronTrigger(
            day_of_week="mon-fri",
            hour=17,
            minute=30,
            timezone="America/New_York",
        ),
        id="price_refresh",
        name="Daily Price Refresh",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=600,
    )

    # -- Options ideas bridge: sync institutional contract ideas to option_setups --
    def run_options_bridge_job() -> None:
        """Sync institutional contract ideas to option_setups table."""
        try:
            from signal_scanner.institutional_intel.reports.options_bridge import run_bridge_job
            run_bridge_job()
        except Exception as e:
            logger.warning(f"Options bridge job error: {e}")

    scheduler.add_job(
        run_options_bridge_job,
        trigger=CronTrigger(day_of_week="mon-fri", hour=7, minute=0, timezone="America/New_York"),
        id="options_bridge_am",
        name="Options Ideas Bridge (7AM)",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=600,
    )
    scheduler.add_job(
        run_options_bridge_job,
        trigger=CronTrigger(day_of_week="mon-fri", hour=12, minute=0, timezone="America/New_York"),
        id="options_bridge_noon",
        name="Options Ideas Bridge (12PM)",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=600,
    )

    # -- Daily short data refresh (short interest + short volume) --
    def run_short_data_refresh() -> None:
        """Refresh short interest and short volume data daily after market close."""
        try:
            from signal_scanner.institutional_intel.jobs.short_data_loader import (
                load_short_interest,
                load_short_volume,
            )
            from datetime import timedelta

            # Short interest: bi-monthly, fetch last 45 days to catch new settlements
            si_stats = load_short_interest(
                from_date=date.today() - timedelta(days=45), rps=4.0,
            )
            logger.info("Short interest refresh: {}", si_stats)

            # Short volume: daily, fetch last 5 days (catches any missed days)
            sv_stats = load_short_volume(
                from_date=date.today() - timedelta(days=5), rps=4.0,
            )
            logger.info("Short volume refresh: {}", sv_stats)
        except Exception as e:
            logger.warning(f"Short data refresh error: {e}")

    scheduler.add_job(
        run_short_data_refresh,
        trigger=CronTrigger(
            day_of_week="mon-fri",
            hour=18,
            minute=0,
            timezone="America/New_York",
        ),
        id="short_data_refresh",
        name="Short Data Refresh",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=600,
    )

    # -- Squeeze score refresh (after short data and price refresh) --
    def run_squeeze_refresh() -> None:
        """Recompute squeeze scores from latest short data."""
        try:
            from signal_scanner.institutional_intel.intelligence.squeeze_detector import (
                update_squeeze_in_intelligence,
            )
            import duckdb
            from signal_scanner.institutional_intel.config import WAREHOUSE_PATH

            from signal_scanner.institutional_intel.config import safe_duckdb_connect
            conn = safe_duckdb_connect(read_only=False)
            if conn is None:
                logger.warning("DuckDB locked — skipping scheduled intelligence refresh")
                return
            try:
                # Get best quarter
                row = conn.execute("""
                    SELECT report_quarter FROM intelligence_scores
                    WHERE data_quality_score >= 75
                    GROUP BY report_quarter
                    HAVING COUNT(*) >= 500
                    ORDER BY report_quarter DESC LIMIT 1
                """).fetchone()
                if row:
                    updated = update_squeeze_in_intelligence(conn, row[0])
                    logger.info("Squeeze scores refreshed: {} tickers for {}", updated, row[0])
            finally:
                conn.close()
        except Exception as e:
            logger.warning(f"Squeeze refresh error: {e}")

    scheduler.add_job(
        run_squeeze_refresh,
        trigger=CronTrigger(
            day_of_week="mon-fri",
            hour=18,
            minute=30,
            timezone="America/New_York",
        ),
        id="squeeze_refresh",
        name="Squeeze Score Refresh",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=600,
    )

    # -- Daily Form 4 insider refresh (before squeeze, after price) --
    def run_daily_form4_refresh() -> None:
        """Fetch recent Form 4 filings from SEC EDGAR daily index."""
        try:
            from signal_scanner.institutional_intel.jobs.daily_form4_refresh import (
                refresh_daily_form4,
            )
            stats = refresh_daily_form4(lookback_days=5)
            logger.info("Daily Form 4 refresh: {}", stats)
        except Exception as e:
            logger.warning(f"Daily Form 4 refresh error: {e}")

    scheduler.add_job(
        run_daily_form4_refresh,
        trigger=CronTrigger(
            day_of_week="mon-fri",
            hour=18,
            minute=15,
            timezone="America/New_York",
        ),
        id="daily_form4_refresh",
        name="Daily Form 4 Insider Refresh",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=600,
    )

    # -- Dark pool (FINRA-derived) daily refresh — 6:45 PM after short volume --
    def run_dark_pool_refresh() -> None:
        """Derive dark pool metrics from FINRA short volume data."""
        try:
            from signal_scanner.institutional_intel.jobs.short_data_loader import load_dark_pool_daily
            from datetime import timedelta
            stats = load_dark_pool_daily(
                tickers=[],  # empty = all tickers in fact_short_volume
                from_date=date.today() - timedelta(days=5),
                to_date=date.today() - timedelta(days=1),
            )
            logger.info("Dark pool refresh: {}", stats)
        except Exception as e:
            logger.warning(f"Dark pool refresh error: {e}")

    scheduler.add_job(
        run_dark_pool_refresh,
        trigger=CronTrigger(
            day_of_week="mon-fri",
            hour=18,
            minute=45,
            timezone="America/New_York",
        ),
        id="dark_pool_refresh",
        name="Dark Pool Refresh (FINRA-derived)",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=600,
    )

    # -- Options flow snapshot — 4:30 PM after market close --
    def run_options_flow_snapshot() -> None:
        """Fetch daily options flow snapshot from Polygon."""
        try:
            from signal_scanner.institutional_intel.jobs.options_flow_loader import (
                load_options_flow,
                get_options_tickers,
            )
            tickers = get_options_tickers(min_conviction=40)[:300]
            if tickers:
                stats = load_options_flow(tickers, exp_weeks_ahead=8, rps=3.0)
                logger.info("Options flow snapshot: {}", stats)
        except Exception as e:
            logger.warning(f"Options flow error: {e}")

    scheduler.add_job(
        run_options_flow_snapshot,
        trigger=CronTrigger(
            day_of_week="mon-fri",
            hour=16,
            minute=30,
            timezone="America/New_York",
        ),
        id="options_flow_snapshot",
        name="Options Flow Snapshot",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=600,
    )

    # -- News sentiment refresh — every 4 hours during market hours --
    def run_news_sentiment_refresh() -> None:
        """Fetch latest news with sentiment from Polygon."""
        try:
            from signal_scanner.institutional_intel.jobs.news_sentiment_loader import (
                load_news_sentiment,
                get_news_tickers,
            )
            tickers = get_news_tickers(min_conviction=30)[:300]
            if tickers:
                stats = load_news_sentiment(tickers, days_back=2, rps=4.0)
                logger.info("News sentiment refresh: {}", stats)
        except Exception as e:
            logger.warning(f"News sentiment error: {e}")

    scheduler.add_job(
        run_news_sentiment_refresh,
        trigger=CronTrigger(
            day_of_week="mon-fri",
            hour="8,12,16",
            minute=0,
            timezone="America/New_York",
        ),
        id="news_sentiment_refresh",
        name="News Sentiment Refresh",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=600,
    )

    # -- Cost-to-borrow (yfinance) — 5:00 PM --
    def run_ctb_refresh() -> None:
        """Refresh cost-to-borrow / short metrics from yfinance."""
        try:
            from signal_scanner.institutional_intel.jobs.short_data_loader import (
                load_cost_to_borrow,
                get_intelligence_tickers,
            )
            tickers = set(get_intelligence_tickers(min_conviction=40)[:500])
            stats = load_cost_to_borrow(from_date=date.today(), ticker_filter=tickers)
            logger.info("CTB refresh: {}", stats)
        except Exception as e:
            logger.warning(f"CTB refresh error: {e}")

    scheduler.add_job(
        run_ctb_refresh,
        trigger=CronTrigger(
            day_of_week="mon-fri",
            hour=17,
            minute=0,
            timezone="America/New_York",
        ),
        id="ctb_refresh",
        name="Cost-to-Borrow Refresh (yfinance)",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=600,
    )

    # -- Related stocks & correlations — weekly Sunday 8 PM --
    def run_related_stocks_refresh() -> None:
        """Refresh related stock pairs and rolling correlations."""
        try:
            from signal_scanner.institutional_intel.jobs.related_stocks_loader import (
                load_related_companies,
                compute_correlations,
            )
            from signal_scanner.institutional_intel.config import safe_duckdb_connect
            conn = safe_duckdb_connect(read_only=True)
            if conn:
                rows = conn.execute("""
                    SELECT ticker FROM intelligence_scores
                    WHERE conviction_score >= 40
                      AND ticker NOT IN ('N/A','NONE','NULL','')
                      AND LENGTH(ticker) <= 5
                    ORDER BY conviction_score DESC LIMIT 300
                """).fetchall()
                conn.close()
                tickers = [r[0] for r in rows]
                if tickers:
                    load_related_companies(tickers, rps=4.0)
            compute_correlations(lookback_days=60, min_correlation=0.5)
        except Exception as e:
            logger.warning(f"Related stocks error: {e}")

    scheduler.add_job(
        run_related_stocks_refresh,
        trigger=CronTrigger(
            day_of_week="sun",
            hour=20,
            minute=0,
            timezone="America/New_York",
        ),
        id="related_stocks_refresh",
        name="Related Stocks & Correlations",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,
    )

    # -- Daily 13F incremental refresh — 6:20 PM (after Form 4, before dark pool) --
    def run_daily_13f_refresh() -> None:
        """Fetch new 13F-HR / 13F-HR/A filings from SEC EDGAR daily index."""
        try:
            from signal_scanner.institutional_intel.jobs.daily_13f_refresh import (
                refresh_daily_13f,
            )
            stats = refresh_daily_13f(lookback_days=5)
            logger.info("Daily 13F refresh: {}", stats)
        except Exception as e:
            logger.warning(f"Daily 13F refresh error: {e}")

    scheduler.add_job(
        run_daily_13f_refresh,
        trigger=CronTrigger(
            day_of_week="mon-fri",
            hour=18,
            minute=20,
            timezone="America/New_York",
        ),
        id="daily_13f_refresh",
        name="Daily 13F Incremental Refresh",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,
    )

    # -- Daily 8-K material events refresh — 6:50 PM (after short data) --
    def run_daily_8k_refresh() -> None:
        """Fetch and classify new Form 8-K material event filings."""
        try:
            from signal_scanner.institutional_intel.jobs.daily_8k_refresh import (
                refresh_daily_8k,
            )
            stats = refresh_daily_8k(lookback_days=5)
            logger.info("Daily 8-K refresh: {}", stats)
        except Exception as e:
            logger.warning(f"Daily 8-K refresh error: {e}")

    scheduler.add_job(
        run_daily_8k_refresh,
        trigger=CronTrigger(
            day_of_week="mon-fri",
            hour=18,
            minute=50,
            timezone="America/New_York",
        ),
        id="daily_8k_refresh",
        name="Daily 8-K Material Events Refresh",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,
    )

    # -- Price backfill (one-shot, if requested) --
    if args.backfill_prices:
        from signal_scanner.institutional_intel.jobs.massive_loader import (
            get_warehouse_tickers,
            load_grouped_daily,
        )

        _bf_from = date.fromisoformat(args.price_from)
        _bf_to = date.fromisoformat(args.price_to) if args.price_to else None
        _bf_rps = args.price_rps
        _bf_tickers = set(get_warehouse_tickers())
        logger.info(
            "Price backfill queued: {} to {}, {} tickers, {} rps",
            _bf_from, _bf_to or "yesterday", len(_bf_tickers), _bf_rps,
        )

        def run_price_backfill() -> None:
            try:
                stats = load_grouped_daily(
                    from_date=_bf_from,
                    to_date=_bf_to,
                    ticker_filter=_bf_tickers,
                    rps=_bf_rps,
                )
                logger.info("Price backfill complete: {}", stats)
            except Exception as e:
                logger.error(f"Price backfill error: {e}")

        scheduler.add_job(
            run_price_backfill,
            trigger="date",
            run_date=datetime.now(timezone.utc),
            id="price_backfill",
            name="Price Backfill",
            max_instances=1,
        )

    # -- LOCAL INTRADAY DATA PLANE --
    # Architecture: Bar Printer (IBKR→SQLite) → Strategy Engine (SQLite→evaluate)
    # Strategies NEVER touch IBKR for market data.
    from signal_scanner.core.live_bar_store import LiveBarStore
    from signal_scanner.core.bar_printer import BarPrinter
    from signal_scanner.core.strategy_engine import StrategyEngine
    from signal_scanner.core.universe_builder import build_session_universe

    bar_store = LiveBarStore()
    # Bar printer gets its own IBKRConfig with offset clientId
    from signal_scanner.config import IBKRConfig as _IBKRConfig
    _bar_ibkr_cfg = _IBKRConfig()
    _bar_ibkr_cfg.port = ib_cfg.port  # same port as main scanner
    _bar_ibkr_cfg.client_id = ib_cfg.client_id + 5  # different clientId
    bar_printer = BarPrinter(_bar_ibkr_cfg, bar_store)
    strategy_engine = StrategyEngine(bar_store)

    # Initialize scanners
    vwap_mr_scanner = None
    fpb_scanner = None
    orb_v2_scanner = None

    try:
        from signal_scanner.paper.vwap_mr_live import VWAPMRLiveScanner
        vwap_mr_scanner = VWAPMRLiveScanner(connector, db, scanner)
        vwap_mr_scanner._load_daily_context()
        strategy_engine.register(vwap_mr_scanner, "VWAP_MR",
                                 vwap_mr_scanner._get_qualifying_tickers)
        logger.info("VWAP_MR registered (%d daily context)", len(vwap_mr_scanner._daily_context))
    except Exception as e:
        logger.warning(f"VWAP_MR init failed: {e}")

    try:
        from signal_scanner.paper.fpb_live import FPBLiveScanner
        fpb_scanner = FPBLiveScanner(connector, db, scanner)
        fpb_scanner._load_daily_context()
        strategy_engine.register(fpb_scanner, "FPB",
                                 fpb_scanner._get_qualifying_tickers)
        logger.info("FPB registered (%d daily context)", len(fpb_scanner._daily_context))
    except Exception as e:
        logger.warning(f"FPB init failed: {e}")

    try:
        from signal_scanner.paper.orb_v2_live import ORBV2LiveScanner
        orb_v2_scanner = ORBV2LiveScanner(connector, db, scanner)
        orb_v2_scanner._load_daily_context()
        strategy_engine.register(orb_v2_scanner, "ORB_V2",
                                 orb_v2_scanner._get_qualifying_tickers)
        logger.info("ORB_V2 registered (%d daily context)", len(orb_v2_scanner._daily_context))
    except Exception as e:
        logger.warning(f"ORB_V2 init failed: {e}")

    # Build session universe (Tier 1 + Tier 2 from intelligence snapshot)
    try:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        snap = getattr(scanner, "_intelligence_snapshot", {})
        open_syms = [t.get("symbol", "") for t in db.get_open_paper_trades()]
        n_universe = build_session_universe(bar_store, today, snap, open_syms)
        logger.info("Session universe: %d symbols", n_universe)
    except Exception as e:
        logger.warning(f"Universe build failed: {e}")

    # --- BAR PRINTER (dedicated thread, own IBKR connection) ---
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    bar_symbols = bar_store.get_tracked_symbols(today)
    bar_printer.start(bar_symbols)  # spawns background thread with own IB()

    # --- STRATEGY ENGINE (scheduler, reads SQLite only, no IBKR) ---
    # --- CONTEXT MOMENTUM SCANNER (non-pattern entry) ---
    context_scanner = None
    try:
        from signal_scanner.paper.context_momentum_live import ContextMomentumScanner
        snap = getattr(scanner, "_intelligence_snapshot", {})
        context_scanner = ContextMomentumScanner(bar_store, db, snap)
        logger.info("Context Momentum scanner registered")
    except Exception as e:
        logger.warning(f"Context Momentum init failed: {e}")

    # --- EXECUTION CONSUMER (processes pending signals → creates trades) ---
    from signal_scanner.core.execution_consumer import ExecutionConsumer
    exec_consumer = ExecutionConsumer(db, bar_store)

    def run_strategy_eval() -> None:
        """Evaluate strategies from stored bars, then execute pending signals.

        Strategy evaluation: pure, no IBKR, no side effects.
        Context Momentum: separate entry family, tracked independently.
        Execution: consumes PENDING_EXECUTION signals, creates trades.
        """
        session.set_active_job("strategy_eval")
        try:
            strategy_engine.evaluate_all()
        except Exception as e:
            logger.warning("Strategy evaluation error: {}", e)

        # Context Momentum — separate entry family
        if context_scanner:
            try:
                from datetime import datetime
                try:
                    from zoneinfo import ZoneInfo
                    now_et = datetime.now(ZoneInfo("America/New_York"))
                except ImportError:
                    now_et = datetime.utcnow()

                today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                ctx_tickers = bar_store.get_tracked_symbols(today_str)
                # Filter to non-benchmark tickers
                ctx_tickers = [t for t in ctx_tickers if t not in (
                    "SPY", "XLK", "XLF", "XLE", "XLV", "XLI", "XLY", "XLP", "XLU", "XLB", "XLRE", "XLC"
                )]
                signals = context_scanner.scan_universe(ctx_tickers, now_et)
                for sig in signals:
                    import json as _json

                    # Create idea in ledger (persists across sessions)
                    try:
                        idea_id = db.idea_ledger.upsert_idea({
                            "symbol": sig["symbol"],
                            "side": sig["side"],
                            "source": "CONTEXT_MOMENTUM",
                            "entry_price": sig["entry_price"],
                            "stop_loss": sig["stop_price"],
                            "target_1": sig["target_1"],
                            "target_2": sig["target_2"],
                            "conviction": sig.get("conviction"),
                            "ev_score": sig.get("vol_pressure"),
                            "accum_phase": sig.get("phase"),
                            "market_regime": getattr(scanner, "market_regime", None),
                        })
                        sig["idea_id"] = idea_id
                    except Exception:
                        pass

                    bar_store.record_signal({
                        "strategy": "CONTEXT_MOMENTUM",
                        "symbol": sig["symbol"],
                        "bar_ts_used": sig.get("bar_ts"),
                        "signal_type": "ENTRY",
                        "freshness_state": "FRESH",
                        "score": sig.get("vol_pressure"),
                        "percentile": sig.get("sector_rs"),
                        "rationale": _json.dumps(sig, default=str),
                        "recommendation_source": "CONTEXT_MOMENTUM",
                        "status": "PENDING_EXECUTION",
                    })
            except Exception as e:
                logger.warning("Context Momentum error: {}", e)

        session.clear_active_job()

        # Execute pending signals (separate from evaluation)
        session.set_active_job("execution")
        try:
            exec_consumer.process_pending()
        except Exception as e:
            logger.warning("Execution consumer error: {}", e)

        # EOD exit sweep — close intraday positions at 3:50 PM ET
        try:
            from signal_scanner.paper.eod_exit import should_run_eod_exit, run_eod_exit
            from datetime import datetime as _dt
            try:
                from zoneinfo import ZoneInfo
                _now_et = _dt.now(ZoneInfo("America/New_York"))
            except ImportError:
                _now_et = _dt.utcnow()
            if should_run_eod_exit(_now_et):
                run_eod_exit(db, bar_store)
        except Exception as e:
            logger.warning("EOD exit error: {}", e)
        finally:
            session.clear_active_job()

    scheduler.add_job(
        run_strategy_eval,
        trigger="interval",
        seconds=60,
        id="strategy_eval",
        name="Strategy Engine + Execution Consumer",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=30,
    )

    logger.info(
        "Data plane active: bar_printer (own thread/IBKR) → strategy_engine (scheduler, SQLite only)"
    )

    # -- IBKR Order Executor (bracket orders for enabled strategies) --
    order_executor = None
    ibkr_live_strategies = {
        s.strip().upper()
        for s in (args.ibkr_live or "").split(",")
        if s.strip()
    }

    # Auto-enable all strategies for IBKR paper accounts (port 7497)
    if not ibkr_live_strategies and ib_cfg.port == 7497:
        ibkr_live_strategies = {
            "VWAP_MR", "FPB", "ORB_V2", "SCANNER_MTF", "IDEA_BRIDGE",
        }
        logger.info(
            "Paper port 7497 detected — auto-enabling IBKR execution for: "
            f"{', '.join(sorted(ibkr_live_strategies))}"
        )

    if ibkr_live_strategies:
        try:
            from signal_scanner.core.order_executor import OrderExecutor

            order_executor = OrderExecutor(connector, db, ibkr_live_strategies)

            # Inject into traders + execution consumer
            scanner._paper_trader._order_executor = order_executor
            if vwap_mr_scanner is not None:
                vwap_mr_scanner._order_executor = order_executor
            if fpb_scanner is not None:
                fpb_scanner._order_executor = order_executor
            if orb_v2_scanner is not None:
                orb_v2_scanner._order_executor = order_executor
            exec_consumer._executor = order_executor

            # Reconcile IBKR positions on startup (one-shot, after IBKR connects)
            def run_ibkr_reconciliation() -> None:
                if not connector.is_connected():
                    logger.info("IBKR not connected yet — skipping reconciliation")
                    return
                try:
                    stats = order_executor.reconcile_on_startup()
                    logger.info(f"IBKR startup reconciliation: {stats}")
                    # Propagate orphan gate to readiness
                    readiness.orphan_gate_active = order_executor._orphan_gate_active
                    readiness.orphan_symbols = list(order_executor._orphan_symbols)
                    if readiness.orphan_gate_active:
                        readiness.add_degraded(
                            f"ORPHAN_GATE: {len(readiness.orphan_symbols)} unresolved positions"
                        )
                    readiness.ibkr_connected = True
                    readiness.resolve_status()
                    readiness.save()
                except Exception as e:
                    logger.warning(f"IBKR reconciliation error: {e}")

            scheduler.add_job(
                run_ibkr_reconciliation,
                trigger="date",
                run_date=datetime.now(timezone.utc),
                id="ibkr_reconcile",
                name="IBKR Startup Reconciliation",
            )

            # Periodic position sync (catch missed fills)
            scheduler.add_job(
                order_executor.check_open_orders,
                trigger="interval",
                seconds=60,
                id="ibkr_order_check",
                name="IBKR Order Monitor",
                max_instances=1,
                coalesce=True,
                misfire_grace_time=30,
            )

            logger.info(
                f"IBKR live execution enabled for: {', '.join(sorted(ibkr_live_strategies))}"
            )
        except ImportError as e:
            logger.warning(f"OrderExecutor not available: {e}")
        except Exception as e:
            logger.warning(f"OrderExecutor init failed: {e}")

    scheduler.start()
    logger.info(f"Scheduler started: scanning every {scan_interval}s")
    logger.info(f"IBKR reconnect heartbeat every {ibkr_reconnect_seconds}s")
    logger.info("Initial scan running in background...")

    # ---- Launch dashboard or keep alive ----
    if not args.no_dashboard:
        from signal_scanner.dashboard.app import app
        from signal_scanner.dashboard.callbacks import register_callbacks
        from signal_scanner.dashboard.layouts.main_view import build_main_layout

        app.layout = build_main_layout()
        register_callbacks(app, db, scanner)

        # Build live scanners dict for dashboard ML overlay
        live_scanners = {}
        if vwap_mr_scanner is not None:
            live_scanners["vwap_mr"] = vwap_mr_scanner
        if fpb_scanner is not None:
            live_scanners["fpb"] = fpb_scanner
        if orb_v2_scanner is not None:
            live_scanners["orb_v2"] = orb_v2_scanner

        # Register Kubera Reports callbacks
        from signal_scanner.dashboard.reports_callbacks import register_reports_callbacks
        register_reports_callbacks(app, db, scanner, live_scanners=live_scanners)

        # Register Intelligence dashboard callbacks
        from signal_scanner.dashboard.intelligence_callbacks_v2 import register_intelligence_callbacks
        register_intelligence_callbacks(app)

        # Register Ask Kubera callbacks
        from signal_scanner.dashboard.kubera_callbacks import register_kubera_callbacks
        register_kubera_callbacks(app)

        from signal_scanner.dashboard.stock_report_callbacks import register_stock_report_callbacks
        register_stock_report_callbacks(app)

        # Register My Trades callbacks
        from signal_scanner.dashboard.my_trades_callbacks import register_my_trades_callbacks
        register_my_trades_callbacks(app, db)

        # Register TradeGPT floating chat callbacks
        from signal_scanner.dashboard.tradegpt_callbacks import register_tradegpt_callbacks
        register_tradegpt_callbacks(app)

        # Register Sniper Board + Performance + Global Search callbacks
        from signal_scanner.dashboard.sniper_callbacks import register_sniper_callbacks
        register_sniper_callbacks(app, db, scanner=scanner)

        dash_cfg = DashboardConfig()
        port = args.port or dash_cfg.port
        logger.info(f"Dashboard available at http://{dash_cfg.host}:{port}")

        run_debug = bool(args.debug or dash_cfg.debug)
        try:
            app.run(
                host=dash_cfg.host,
                port=port,
                debug=run_debug,
                use_reloader=False,
                dev_tools_hot_reload=False,
            )
        except KeyboardInterrupt:
            pass
        finally:
            if order_executor:
                order_executor.shutdown()
            scheduler.shutdown(wait=False)
            connector.disconnect()
            session.release()
            logger.info("Shutdown complete — session released")
    else:
        logger.info("Running in headless mode (no dashboard). Press Ctrl+C to stop.")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            logger.info("Shutting down...")
            if order_executor:
                order_executor.shutdown()
            scheduler.shutdown(wait=False)
            connector.disconnect()
            session.release()
            logger.info("Shutdown complete — session released")


if __name__ == "__main__":
    main()
