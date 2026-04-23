"""Main scanning loop — orchestrates per-symbol analysis across timeframes.

V2: Adds market regime awareness, signal persistence tracking,
session time tagging, relative strength vs SPY, and MTF aggregation.
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from loguru import logger

from signal_scanner.config import GEXConfig, ScannerConfig
from signal_scanner.core.confluence_engine import ConfluenceEngine
from signal_scanner.core.gex_calculator import GEXCalculator
from signal_scanner.core.ibkr_connector import DataConnector
from signal_scanner.core.market_regime import MarketRegime, get_market_regime
from signal_scanner.core.technical_analyzer import TechnicalAnalyzer
from signal_scanner.core.watchlist_manager import WatchlistManager
from signal_scanner.database.db_manager import DatabaseManager
from signal_scanner.paper.paper_trader import PaperTrader
from signal_scanner.paper.idea_bridge import IdeaBridge
from signal_scanner.options.option_setup_engine import OptionSetupEngine
from signal_scanner.scanner.signal_ranker import SignalRanker
from signal_scanner.utils import notifications
from signal_scanner.utils.earnings_calendar import EarningsCalendar
from signal_scanner.utils.notifications import send_notification

try:
    from zoneinfo import ZoneInfo
    NY_TZ = ZoneInfo("America/New_York")
except ImportError:
    NY_TZ = None


class MultiSymbolScanner:
    """Scans multiple symbols across multiple timeframes.

    For each symbol:
        1. Fetch GEX data (once per symbol, not per timeframe)
        2. For each timeframe: fetch OHLCV, run technicals, score confluence
        3. Enrich with regime, persistence, relative strength, session time
        4. Store results in database
        5. MTF-aggregate for dashboard display
    """

    def __init__(
        self,
        connector: DataConnector,
        db_manager: DatabaseManager,
        scanner_config: Optional[ScannerConfig] = None,
        gex_config: Optional[GEXConfig] = None,
    ) -> None:
        self._connector = connector
        self._db = db_manager
        self._config = scanner_config or ScannerConfig()
        self._gex_calc = GEXCalculator(connector, gex_config or GEXConfig())
        self._tech = TechnicalAnalyzer()
        self._confluence = ConfluenceEngine()
        self._ranker = SignalRanker()
        self._watchlist_mgr = WatchlistManager()
        self._paper_trader = PaperTrader(db_manager, self._config)
        self._idea_bridge = IdeaBridge(self._paper_trader, db_manager)
        self._option_engine = OptionSetupEngine(db_manager)
        self._earnings = EarningsCalendar(buffer_days=3)

        # Scanner state (read by dashboard)
        self.is_scanning: bool = False
        self.last_scan_time: Optional[datetime] = None
        self.last_scan_results: List[Dict] = []
        self.last_mtf_results: List[Dict] = self._load_cached_results()
        self.scan_errors: int = 0
        self.current_watchlist: str = ""      # configured watchlist name (e.g. "universe_master")
        self.active_scan_source: str = ""      # scan source label (e.g. "universe_master:live")
        self.symbols_count: int = 0
        self.market_regime: Optional[MarketRegime] = None

        # Signal persistence: "symbol:timeframe" -> {signal, count}
        self._persistence: Dict[str, Dict] = {}

        # SPY benchmark returns for relative strength
        self._spy_returns: Optional[float] = None

        # Intelligence snapshot: {ticker: {accum_phase, conviction_score, ...}}
        # Pre-loaded from DuckDB at startup so intraday strategies are armed before
        # the first scan pass completes. Refreshed at the start of each scan cycle.
        self._intelligence_snapshot: Dict[str, Dict] = self._load_intelligence_snapshot()

        # Data freshness state — set by main.py at startup
        self.data_degraded: bool = False
        self.data_freshness: Dict = {}

        # Canonical readiness state — set by main.py after loading from readiness.json
        self.readiness: Optional["ReadinessState"] = None

    # Per-symbol scan cost estimate (seconds).  Used by get_live_universe()
    # to cap the list so a full scan completes within the runtime budget.
    # Empirical: GEX(~0.1s) + 3 TFs × fetch+score(~0.2s each) ≈ 0.7s
    _ESTIMATED_SECONDS_PER_SYMBOL: float = 0.7

    def get_priority_symbols(self, watchlist_name: str, max_symbols: int = 250) -> List[str]:
        """Return the most actionable symbols for an intraday-priority scan.

        During live market hours, scanning the full universe can monopolize the
        single IBKR connection long enough to starve intraday ML scanners. This
        method narrows the main scan to the highest-conviction names first.
        """
        symbols = self._watchlist_mgr.load_watchlist(watchlist_name)
        if not symbols:
            return []

        if not self._intelligence_snapshot:
            return symbols[:max_symbols]

        ranked: List[tuple] = []
        for symbol in symbols:
            intel = self._intelligence_snapshot.get(symbol, {})
            phase = str(intel.get("inst_phase", ""))
            conviction = float(intel.get("inst_conviction", 0))
            triple_lock = 1 if intel.get("inst_triple_lock") else 0
            ml_v2 = float(intel.get("inst_ml_score_v2", 0))
            phase_priority = 1 if phase in ("EARLY_ACCUM", "ACTIVE_ACCUM", "LATE_ACCUM", "EXPANSION") else 0
            ranked.append((symbol, phase_priority, triple_lock, conviction, ml_v2))

        ranked.sort(key=lambda row: (-row[1], -row[2], -row[3], -row[4], row[0]))
        return [row[0] for row in ranked[:max_symbols]]

    def get_live_universe(
        self,
        watchlist_name: str,
        runtime_budget_seconds: float = 120.0,
        min_conviction: float = 40.0,
        min_ml_v2: float = 25.0,
        min_tier1_managers: int = 2,
    ) -> List[str]:
        """Build a runtime-budgeted live trading universe.

        Unlike get_priority_symbols (which caps at a fixed count), this method
        sizes the universe by *runtime budget* so the scan finishes within the
        given time envelope.  Only tickers that pass quality filters are included:

        1. Must be in an accumulation or expansion phase.
        2. Must have conviction >= min_conviction.
        3. Must have ML v2 score >= min_ml_v2 (liquidity/quality proxy).
        4. Must have >= min_tier1_managers (institutional liquidity gate).
        5. Sorted by: triple_lock > conviction > ML v2.
        6. Capped so estimated scan time <= runtime_budget_seconds.

        Returns the list of tickers (may be empty if snapshot is unavailable).
        """
        symbols_set = set(self._watchlist_mgr.load_watchlist(watchlist_name))
        if not symbols_set:
            return []

        if not self._intelligence_snapshot:
            # Fallback: time-cap the raw watchlist alphabetically
            max_n = int(runtime_budget_seconds / self._ESTIMATED_SECONDS_PER_SYMBOL)
            return sorted(symbols_set)[:max_n]

        _ACTIONABLE_PHASES = {
            "EARLY_ACCUM", "ACTIVE_ACCUM", "LATE_ACCUM", "EXPANSION",
        }

        candidates: List[tuple] = []
        _skipped_phase = 0
        _skipped_conv = 0
        _skipped_ml = 0
        _skipped_liq = 0
        for symbol, intel in self._intelligence_snapshot.items():
            if symbol not in symbols_set:
                continue
            phase = str(intel.get("inst_phase", ""))
            conviction = float(intel.get("inst_conviction", 0))
            ml_v2 = float(intel.get("inst_ml_score_v2", 0))
            tier1 = int(intel.get("inst_tier1", 0))

            if phase not in _ACTIONABLE_PHASES:
                _skipped_phase += 1
                continue
            if conviction < min_conviction:
                _skipped_conv += 1
                continue
            if ml_v2 < min_ml_v2:
                _skipped_ml += 1
                continue
            if tier1 < min_tier1_managers:
                _skipped_liq += 1
                continue

            triple_lock = 1 if intel.get("inst_triple_lock") else 0
            candidates.append((symbol, triple_lock, conviction, ml_v2))

        # Sort: triple_lock first, then conviction desc, then ML desc
        candidates.sort(key=lambda r: (-r[1], -r[2], -r[3], r[0]))

        max_symbols = int(runtime_budget_seconds / self._ESTIMATED_SECONDS_PER_SYMBOL)
        result = [r[0] for r in candidates[:max_symbols]]
        logger.info(
            "Live universe: {} tickers from {} candidates (cap={}) | "
            "Filtered out: phase={}, conv={}, ml={}, liquidity={} | "
            "Budget={:.0f}s, min_conv={:.0f}, min_ml={:.0f}, min_tier1={}",
            len(result), len(candidates), max_symbols,
            _skipped_phase, _skipped_conv, _skipped_ml, _skipped_liq,
            runtime_budget_seconds, min_conviction, min_ml_v2, min_tier1_managers,
        )
        return result

    # ------------------------------------------------------------------
    # Scan results cache — survive restarts
    # ------------------------------------------------------------------
    _CACHE_PATH = Path(__file__).resolve().parents[1] / "data" / "scan_cache.json"

    @classmethod
    def _load_cached_results(cls) -> List[Dict]:
        """Load last scan results from disk cache (if recent enough)."""
        try:
            if not cls._CACHE_PATH.exists():
                return []
            data = json.loads(cls._CACHE_PATH.read_text(encoding="utf-8"))
            cached_at = data.get("cached_at", "")
            results = data.get("results", [])
            if not results:
                return []
            # Only use cache if less than 4 hours old
            try:
                ts = datetime.fromisoformat(cached_at)
                age_hours = (datetime.now(timezone.utc) - ts).total_seconds() / 3600
                if age_hours > 4:
                    logger.debug("Scan cache stale ({}h) — ignoring", round(age_hours, 1))
                    return []
            except (ValueError, TypeError):
                return []
            logger.info("Loaded {} cached scan results from disk (age: {:.1f}h)",
                        len(results), age_hours)
            return results
        except Exception as exc:
            logger.debug("Could not load scan cache: {}", exc)
            return []

    @classmethod
    def _save_cached_results(cls, results: List[Dict]) -> None:
        """Persist scan results to disk for restart recovery."""
        try:
            cls._CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            # Sanitize: only keep JSON-serializable fields
            clean = []
            for r in results:
                row = {}
                for k, v in r.items():
                    if isinstance(v, (str, int, float, bool, type(None))):
                        row[k] = v
                    else:
                        row[k] = str(v)
                clean.append(row)
            payload = {
                "cached_at": datetime.now(timezone.utc).isoformat(),
                "count": len(clean),
                "results": clean,
            }
            cls._CACHE_PATH.write_text(
                json.dumps(payload, indent=1), encoding="utf-8"
            )
        except Exception as exc:
            logger.debug("Could not save scan cache: {}", exc)

    def _load_intelligence_snapshot(self) -> Dict[str, Dict]:
        """Load institutional intelligence for all tickers — ONCE per scan cycle.

        Returns a dict keyed by ticker with the latest clean-quarter intelligence row.
        Uses read-only DuckDB connection so the warehouse is never write-locked.
        Degrades gracefully to {} if warehouse is unavailable.
        """
        try:
            from signal_scanner.institutional_intel.config import safe_duckdb_connect, get_active_quarter

            conn = safe_duckdb_connect(read_only=True)
            if conn is None:
                logger.warning("Intelligence snapshot unavailable: DuckDB locked")
                return {}
            try:
                # Use canonical quarter selection
                quarter = get_active_quarter(conn)
                if not quarter:
                    return {}
                rows = conn.execute("""
                    SELECT ticker, accum_phase, conviction_score,
                           tier1_manager_count, insider_cluster_detected, swing_signal,
                           COALESCE(ml_score, 0),
                           COALESCE(ml_score_v2, 0),
                           COALESCE(triple_lock, FALSE),
                           COALESCE(inst_f4_distinct_60d, 0),
                           COALESCE(price_momentum_90d, 0),
                           COALESCE(price_above_200sma, -1),
                           COALESCE(insider_effect_score, 0),
                           COALESCE(trend_score, 0),
                           COALESCE(institutional_pressure, 0),
                           insider_hist_win_rate,
                           insider_hist_alpha,
                           COALESCE(squeeze_score, 0),
                           COALESCE(short_squeeze_score, 0),
                           days_to_cover
                    FROM intelligence_scores
                    WHERE report_quarter = ? AND data_quality_score >= 75
                """, [quarter]).fetchall()
                snapshot = {
                    r[0]: {
                        "inst_phase":             str(r[1] or "UNKNOWN"),
                        "inst_conviction":        float(r[2] or 0),
                        "inst_tier1":             int(r[3] or 0),
                        "inst_insider":           bool(r[4]),
                        "inst_swing":             str(r[5] or "N/A"),
                        "inst_ml_score":          float(r[6] or 0),
                        "inst_ml_score_v2":       float(r[7] or 0),
                        "inst_triple_lock":       bool(r[8]),
                        "inst_f4_distinct_60d":   float(r[9] or 0),
                        "inst_price_momentum_90d": float(r[10] or 0),
                        "inst_price_above_200sma": int(r[11]) if r[11] is not None else -1,
                        "inst_insider_effect":    float(r[12] or 0),
                        "inst_trend_score":       float(r[13] or 0),
                        "inst_pressure":          float(r[14] or 0),
                        "inst_insider_wr90":      float(r[15]) if r[15] is not None else None,
                        "inst_insider_alpha90":   float(r[16]) if r[16] is not None else None,
                        "inst_squeeze":           float(r[17] or 0),
                        "inst_short_squeeze":     float(r[18] or 0),
                        "inst_dtc":               float(r[19]) if r[19] is not None else None,
                    }
                    for r in rows if r[0]
                }
                logger.info(
                    f"Intelligence snapshot loaded: {len(snapshot)} tickers from {quarter}"
                )
                return snapshot
            finally:
                conn.close()
        except Exception as e:
            logger.warning(f"Intelligence snapshot unavailable (non-fatal): {e}")
            return {}

    def scan_watchlist(self, watchlist_name: str) -> List[Dict]:
        """Execute a full scan of all symbols in a watchlist."""
        symbols = self._watchlist_mgr.load_watchlist(watchlist_name)
        return self.scan_symbols(symbols, watchlist_name)

    def scan_symbols(self, symbols: List[str], source_label: str = "custom") -> List[Dict]:
        """Execute a full scan of an explicit symbol list."""
        import time as _time
        _wall_start = _time.monotonic()

        self.is_scanning = True
        self.active_scan_source = source_label
        scan_start = datetime.now(timezone.utc).isoformat()
        self.symbols_count = len(symbols)

        if not symbols:
            logger.warning(f"No symbols loaded for scan source '{source_label}'")
            self.is_scanning = False
            return []

        all_results: List[Dict] = []
        error_count = 0
        try:
            # Intelligence snapshot — refreshed at start of each scan cycle (read-only, non-blocking)
            self._intelligence_snapshot = self._load_intelligence_snapshot()

            # Tiered scan order: ACCUM tickers first so intraday strategies get enriched
            # live data before the broad universe is processed. Rest follows alphabetically.
            _accum_set = {
                t for t, d in self._intelligence_snapshot.items()
                if d.get("inst_phase", "") in ("EARLY_ACCUM", "ACTIVE_ACCUM", "LATE_ACCUM")
            }
            symbols = sorted(symbols, key=lambda s: (0 if s in _accum_set else 1, s))
            logger.info(
                f"Scan order: {len(_accum_set & set(symbols))} ACCUM tickers first, "
                f"then {len(symbols) - len(_accum_set & set(symbols))} remaining"
            )

            # Market regime — fetched ONCE per scan cycle (via IBKR)
            try:
                self.market_regime = get_market_regime(self._config, connector=self._connector)
            except Exception as e:
                logger.warning(f"Market regime fetch failed: {e}")
                self.market_regime = MarketRegime()

            # SPY benchmark returns for relative strength (fetch once per cycle)
            spy_df = self._connector.get_price_data(self._config.regime_benchmark, "1h")
            self._spy_returns = self._calc_benchmark_returns(spy_df)

            # Session time tag
            session_time = self._get_session_time()

            notifications.reset()
            n_tier1 = len(_accum_set & set(symbols))
            _tier1_bridge_fired = False
            logger.info(
                f"Scan starting: {len(symbols)} symbols | "
                f"Source: {source_label} | Regime: {self.market_regime.regime} | Session: {session_time}"
            )

            for i, symbol in enumerate(symbols, 1):
                if not self._connector.is_connected():
                    logger.warning("IBKR disconnected — pausing scan")
                    error_count += 1
                    break
                try:
                    results, errors = self._scan_symbol(symbol, session_time)
                    all_results.extend(results)
                    error_count += errors

                    if i % 10 == 0:
                        logger.info(f"Progress: {i}/{len(symbols)} symbols scanned")

                except Exception as e:
                    logger.error(f"Unexpected error scanning {symbol}: {e}")
                    error_count += 1

                # Tier 1 checkpoint: fire IdeaBridge after all ACCUM tickers scanned
                # so trades can enter without waiting 2+ hours for full 2929-ticker pass
                if not _tier1_bridge_fired and n_tier1 > 0 and i >= n_tier1:
                    _tier1_bridge_fired = True
                    logger.info(
                        f"Tier 1 checkpoint: {n_tier1} ACCUM tickers scanned — "
                        f"firing IdeaBridge early (regime: {self.market_regime.regime})"
                    )
                    try:
                        self._idea_bridge.process_ideas()
                    except Exception as e:
                        logger.error(f"Idea bridge Tier 1 checkpoint failed: {e}")

            # Record scan metadata with latency
            scan_end = datetime.now(timezone.utc).isoformat()
            _wall_elapsed = _time.monotonic() - _wall_start
            _scan_type = "live" if ":live" in source_label else (
                "priority" if ":priority" in source_label else "research"
            )
            self._db.record_scan(
                start=scan_start,
                end=scan_end,
                count=len(symbols),
                found=len(all_results),
                errors=error_count,
                source=self._connector.get_data_source(),
                duration_seconds=round(_wall_elapsed, 1),
                scan_type=_scan_type,
            )

            # MTF aggregate for dashboard display
            self.last_mtf_results = self._ranker.aggregate_mtf(all_results)
            self._save_cached_results(self.last_mtf_results)
            try:
                self._paper_trader.process_scan_rows(self.last_mtf_results)
            except Exception as e:
                logger.error(f"Paper trader failed after scan: {e}")

            try:
                self._option_engine.process_rows(self.last_mtf_results)
            except Exception as e:
                logger.error(f"Option engine failed after scan: {e}")

            try:
                self._idea_bridge.process_ideas()
            except Exception as e:
                logger.error(f"Idea bridge failed after scan: {e}")

        finally:
            # Always reset scanner state — even on unexpected errors
            self.is_scanning = False
            self.last_scan_time = datetime.now(timezone.utc)
            self.last_scan_results = all_results
            self.scan_errors = error_count

        logger.info(
            f"Scan complete: {len(all_results)} signals from {len(symbols)} symbols, "
            f"{len(self.last_mtf_results)} MTF rows, {error_count} errors, "
            f"{_wall_elapsed:.1f}s ({_scan_type})"
        )
        return all_results

    def _scan_symbol(self, symbol: str, session_time: str) -> tuple:
        """Scan a single symbol across all timeframes."""
        results: List[Dict] = []
        symbol_had_error = False

        # GEX: calculate once per symbol
        gex_result = self._gex_calc.calculate_gex(symbol)

        # Earnings proximity: check once per symbol
        near_earnings, days_to_earnings = self._earnings.check_earnings_proximity(
            symbol, connector=self._connector
        )

        # Pre-fetch 1h bars once to avoid duplicate fetches in RS + 1h scan.
        symbol_1h_df = self._connector.get_price_data(symbol, "1h")
        rel_strength = self._calc_relative_strength(symbol, symbol_1h_df)

        for timeframe in self._config.timeframes:
            try:
                if timeframe == "1h" and symbol_1h_df is not None:
                    df = symbol_1h_df
                else:
                    df = self._connector.get_price_data(symbol, timeframe)
                if df is None or df.empty:
                    logger.debug(f"No data for {symbol} @ {timeframe}")
                    symbol_had_error = True
                    continue

                tech_result = self._tech.analyze(df, self._config)
                regime_str = self.market_regime.regime if self.market_regime else "NEUTRAL"
                confluence = self._confluence.score(tech_result, gex_result, self._config, market_regime=regime_str)
                recommendation, policy_note = self._apply_policy_overrides(
                    signal=confluence.signal,
                    recommendation=confluence.recommendation,
                    gex_status=gex_result.gex_status,
                )
                trade_conditions = confluence.trade_conditions
                if policy_note:
                    trade_conditions = f"{trade_conditions} | {policy_note}"

                # Earnings proximity demotion
                if near_earnings:
                    days_str = f"{days_to_earnings}d" if days_to_earnings is not None else "imminent"
                    trade_conditions = f"{trade_conditions} | EARNINGS in {days_str} — binary event risk"
                    if recommendation in ("BUY", "SELL"):
                        recommendation = "HOLD"

                # Signal persistence and decay tracking
                signal_age, score_delta, signal_momentum = self._update_persistence(
                    symbol, timeframe, confluence.signal, confluence.score
                )

                now = datetime.now(timezone.utc)
                local_now = datetime.now(NY_TZ) if NY_TZ else datetime.now()

                result = {
                    "symbol": symbol,
                    "timestamp": now.isoformat(),
                    "timeframe": timeframe,
                    "score": confluence.score,
                    "signal": confluence.signal,
                    "price": tech_result.current_price,
                    "sma_200": tech_result.sma_200,
                    "sma_50": tech_result.sma_50,
                    "price_vs_sma": tech_result.price_vs_sma,
                    "price_vs_sma_pct": tech_result.price_vs_sma_pct,
                    "zero_gamma_level": gex_result.zero_gamma_level,
                    "gamma_wall_up": gex_result.gamma_wall_up,
                    "gamma_wall_down": gex_result.gamma_wall_down,
                    "gex_status": gex_result.gex_status,
                    "rsi": tech_result.rsi,
                    "rsi_slope": tech_result.rsi_slope,
                    "adx": tech_result.adx,
                    "adx_slope": tech_result.adx_slope,
                    "atr": tech_result.atr,
                    "volume_ratio": tech_result.volume_ratio,
                    "vwap": tech_result.vwap,
                    "vwap_status": tech_result.vwap_status,
                    "trend_direction": confluence.trend_direction,
                    "recommendation": recommendation,
                    "stop_loss": confluence.stop_loss,
                    "target_1": confluence.target_1,
                    "target_2": confluence.target_2,
                    "rr_ratio": confluence.rr_ratio,
                    "trade_conditions": trade_conditions,
                    "distance_to_resistance_pct": confluence.distance_to_resistance_pct,
                    "distance_to_support_pct": confluence.distance_to_support_pct,
                    "prior_day_high": tech_result.prior_day_high,
                    "prior_day_low": tech_result.prior_day_low,
                    "prior_day_close": tech_result.prior_day_close,
                    "vwap_zscore": tech_result.vwap_zscore,
                    "vwap_std": tech_result.vwap_std,
                    "vwap_reversion_signal": tech_result.vwap_reversion_signal,
                    "rsi_bull_divergence": tech_result.rsi_bull_divergence,
                    "rsi_bear_divergence": tech_result.rsi_bear_divergence,
                    "sweep_reclaim_signal": tech_result.sweep_reclaim_signal,
                    "sweep_level": tech_result.sweep_level,
                    "fvg_signal": tech_result.fvg_signal,
                    "fvg_bullish_low": tech_result.fvg_bullish_low,
                    "fvg_bullish_high": tech_result.fvg_bullish_high,
                    "fvg_bearish_low": tech_result.fvg_bearish_low,
                    "fvg_bearish_high": tech_result.fvg_bearish_high,
                    "relative_strength": rel_strength,
                    "market_regime": self.market_regime.regime if self.market_regime else "",
                    "signal_age": signal_age,
                    "score_delta": score_delta,
                    "signal_momentum": signal_momentum,
                    "session_time": session_time,
                    "sector": self._watchlist_mgr.get_sector(symbol),
                    "near_earnings": near_earnings,
                    "days_to_earnings": days_to_earnings,
                    "last_updated": local_now.strftime("%H:%M:%S"),
                }

                # Merge institutional intelligence fields (safe default if no snapshot)
                intel = self._intelligence_snapshot.get(symbol, {})
                result["inst_phase"]              = intel.get("inst_phase",              "UNKNOWN")
                result["inst_conviction"]         = intel.get("inst_conviction",          0)
                result["inst_tier1"]              = intel.get("inst_tier1",               0)
                result["inst_insider"]            = intel.get("inst_insider",             False)
                result["inst_swing"]              = intel.get("inst_swing",               "N/A")
                result["inst_ml_score"]           = intel.get("inst_ml_score",            0)
                result["inst_ml_score_v2"]        = intel.get("inst_ml_score_v2",         0)
                result["inst_triple_lock"]        = intel.get("inst_triple_lock",         False)
                result["inst_f4_distinct_60d"]    = intel.get("inst_f4_distinct_60d",     0)
                result["inst_price_momentum_90d"] = intel.get("inst_price_momentum_90d",  0)
                result["inst_price_above_200sma"] = intel.get("inst_price_above_200sma", -1)
                result["inst_insider_effect"]     = intel.get("inst_insider_effect",      0)
                result["inst_trend_score"]        = intel.get("inst_trend_score",         0)
                result["inst_pressure"]           = intel.get("inst_pressure",            0)
                result["inst_insider_wr90"]       = intel.get("inst_insider_wr90")
                result["inst_insider_alpha90"]    = intel.get("inst_insider_alpha90")
                result["inst_squeeze"]            = intel.get("inst_squeeze",          0)
                result["inst_short_squeeze"]      = intel.get("inst_short_squeeze",    0)
                result["inst_dtc"]                = intel.get("inst_dtc")

                self._db.upsert_signal(result)
                results.append(result)

                # Notification for high-conviction signals
                if (
                    confluence.score >= self._config.notification_score_threshold
                    and recommendation in ("BUY", "SELL")
                ):
                    age_tag = f" [Age: {signal_age}]" if signal_age > 1 else ""
                    send_notification(
                        title=f"HIGH CONVICTION: {symbol}",
                        message=(
                            f"{confluence.signal} @ ${tech_result.current_price:.2f} | "
                            f"Score: {confluence.score}/100 | {timeframe}{age_tag}"
                        ),
                        symbol=symbol,
                        signal=confluence.signal,
                    )

            except Exception as e:
                logger.error(f"Error scanning {symbol} @ {timeframe}: {e}")
                symbol_had_error = True

        if not results:
            symbol_had_error = True
        return results, int(symbol_had_error)

    def _update_persistence(self, symbol: str, timeframe: str, signal: str, score: int) -> tuple:
        """Track signal persistence and score decay/momentum.

        Returns:
            (signal_age, score_delta, momentum_label)
            momentum_label: STRENGTHENING | WEAKENING | STABLE | NEW
        """
        key = f"{symbol}:{timeframe}"
        prev = self._persistence.get(key)

        if prev and prev["signal"] == signal:
            prev["count"] += 1
            delta = score - prev["score"]
            prev["score"] = score
            if delta > 5:
                momentum = "STRENGTHENING"
            elif delta < -5:
                momentum = "WEAKENING"
            else:
                momentum = "STABLE"
            return prev["count"], delta, momentum
        else:
            self._persistence[key] = {"signal": signal, "count": 1, "score": score}
            return 1, 0, "NEW"

    def _calc_benchmark_returns(self, spy_df=None) -> Optional[float]:
        """Calculate SPY returns over the relative strength period."""
        try:
            if spy_df is None:
                spy_df = self._connector.get_price_data(
                    self._config.regime_benchmark, "1h"
                )
            if spy_df is not None and len(spy_df) >= self._config.relative_strength_period:
                period = self._config.relative_strength_period
                spy_ret = (
                    (spy_df["Close"].iloc[-1] - spy_df["Close"].iloc[-period])
                    / spy_df["Close"].iloc[-period]
                ) * 100
                return round(float(spy_ret), 2)
        except Exception:
            pass
        return None

    def _calc_relative_strength(self, symbol: str, df=None) -> Optional[float]:
        """Calculate relative strength: stock return minus SPY return."""
        if self._spy_returns is None:
            return None
        try:
            if df is None:
                df = self._connector.get_price_data(symbol, "1h")
            if df is not None and len(df) >= self._config.relative_strength_period:
                period = self._config.relative_strength_period
                stock_ret = (
                    (df["Close"].iloc[-1] - df["Close"].iloc[-period])
                    / df["Close"].iloc[-period]
                ) * 100
                return round(float(stock_ret) - self._spy_returns, 2)
        except Exception:
            pass
        return None

    def _get_session_time(self) -> str:
        """Return current market session from IBKR — authoritative for trading day and hours.

        Falls back to local clock if IBKR is not connected.
        """
        return self._connector.get_market_status().get("session", "CLOSED")

    def _apply_policy_overrides(
        self,
        signal: str,
        recommendation: str,
        gex_status: str,
    ) -> tuple[str, str]:
        """Apply product-level policy gates so recommendations stay regime/GEX-aware.

        GEX UNKNOWN = no data available (IBKR outside hours or no subscription).
        That is neutral — we only block when GEX actively contradicts direction.
        """
        if recommendation not in ("BUY", "SELL"):
            return recommendation, ""

        # Block only on active GEX contradiction, not on missing data.
        if gex_status != "UNKNOWN":
            if signal == "LONG" and gex_status == "BELOW_ZERO_GAMMA":
                return "HOLD", "Policy: HOLD — GEX below zero gamma contradicts LONG"
            if signal == "SHORT" and gex_status == "ABOVE_ZERO_GAMMA":
                return "HOLD", "Policy: HOLD — GEX above zero gamma contradicts SHORT"

        regime = self.market_regime.regime if self.market_regime else "NEUTRAL"
        if regime == "RISK_OFF" and signal == "LONG":
            return "HOLD", "Policy: HOLD — LONGs paused in RISK_OFF regime"
        if regime == "RISK_ON" and signal == "SHORT":
            return "HOLD", "Policy: HOLD — SHORTs paused in RISK_ON regime"

        return recommendation, ""

    def get_status(self) -> Dict:
        """Return current scanner status for the dashboard."""
        last_scan = self._db.get_last_scan()
        return {
            "is_scanning": self.is_scanning,
            "last_scan_time": self.last_scan_time.isoformat() if self.last_scan_time else None,
            "current_watchlist": self.current_watchlist,
            "active_scan_source": self.active_scan_source,
            "symbols_count": self.symbols_count,
            "signals_count": len(self.last_mtf_results),
            "errors": self.scan_errors,
            "data_source": self._connector.get_data_source(),
            "ibkr_connected": self._connector.is_connected(),
            "ibkr_diagnostics": self._connector.get_ibkr_diagnostics(),
            "paper_policy": self._paper_trader.get_policy_status(),
            "last_scan_meta": last_scan,
            "data_degraded": self.data_degraded,
            "data_freshness": self.data_freshness,
            "readiness": self.readiness.to_dict() if self.readiness else None,
            "market_regime": {
                "regime": self.market_regime.regime,
                "description": self.market_regime.description,
                "spy_price": self.market_regime.spy_price,
                "vix_level": self.market_regime.vix_level,
            } if self.market_regime else None,
        }
