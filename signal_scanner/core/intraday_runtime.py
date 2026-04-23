"""Intraday runtime: shared bar cache + fair strategy scheduling.

Eliminates strategy starvation by:
  1. Fetching bars once per ticker, caching for all strategies
  2. Running strategies sequentially with per-strategy time budgets
  3. Never holding the IBKR lock for more than one ticker at a time

Architecture:
  - IntradayBarCache: shared 1-min bar storage, refreshed per cycle
  - IntradayScheduler: orchestrates fetch + strategy runs with fairness

Usage (in main.py):
    cache = IntradayBarCache(connector)
    scheduler = IntradayScheduler(cache, [vwap_mr, fpb, orb_v2])
    # Called every 5 min by APScheduler:
    scheduler.run_cycle()
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import pandas as pd
from loguru import logger

from signal_scanner.core.telemetry import (
    record_skip, record_funnel, SkipReason, Subsystem,
)


class IntradayBarCache:
    """Shared 1-min bar cache for intraday strategies.

    Fetches each ticker's bars from IBKR once per cycle, stores in memory.
    All three strategies read from the cache instead of making their own
    IBKR calls. SPY bars are fetched once and shared.

    Thread-safe: the cache is populated by one thread (the scheduler),
    then read by strategies in the same thread. No concurrent writes.
    """

    def __init__(self, connector: Any):
        self._connector = connector
        self._bars: Dict[str, pd.DataFrame] = {}  # ticker -> 1-min bars
        self._fetch_times: Dict[str, float] = {}   # ticker -> epoch of last fetch
        self._cycle_id: int = 0
        self._cycle_start: float = 0.0
        self._spy_bars: Optional[pd.DataFrame] = None

        # Telemetry
        self.tickers_fetched: int = 0
        self.fetch_errors: int = 0
        self.cache_hits: int = 0
        self.fetch_duration_seconds: float = 0.0

    def start_cycle(self) -> None:
        """Mark the start of a new fetch cycle. Clears stale data."""
        self._cycle_id += 1
        self._cycle_start = time.monotonic()
        self._bars.clear()
        self._fetch_times.clear()
        self._spy_bars = None
        self.tickers_fetched = 0
        self.fetch_errors = 0
        self.cache_hits = 0
        self.fetch_duration_seconds = 0.0

    def fetch_tickers(
        self,
        tickers: List[str],
        ibkr_lock: threading.Lock,
        budget_seconds: float = 60.0,
    ) -> int:
        """Fetch 1-min bars for a list of tickers, one at a time.

        Acquires and releases the IBKR lock per-ticker (not for the whole batch).
        Stops early if budget is exceeded.

        Returns the number of tickers successfully fetched.
        """
        t0 = time.monotonic()
        fetched = 0

        # SPY first (all strategies need it)
        if "SPY" not in self._bars:
            if ibkr_lock.acquire(timeout=5):
                try:
                    spy = self._connector.get_price_data("SPY", "1m")
                    if spy is not None and len(spy) > 0:
                        self._spy_bars = spy
                        self._bars["SPY"] = spy
                finally:
                    ibkr_lock.release()

        for ticker in tickers:
            if time.monotonic() - t0 > budget_seconds:
                logger.info(
                    "BarCache: budget {:.0f}s exhausted after {}/{} tickers",
                    budget_seconds, fetched, len(tickers),
                )
                break

            if ticker in self._bars:
                self.cache_hits += 1
                continue

            # Per-ticker lock: acquire, fetch, release immediately
            if not ibkr_lock.acquire(timeout=2):
                self.fetch_errors += 1
                continue

            try:
                bars = self._connector.get_price_data(ticker, "1m")
                if bars is not None and len(bars) > 0:
                    self._bars[ticker] = bars
                    self._fetch_times[ticker] = time.monotonic()
                    fetched += 1
                else:
                    self.fetch_errors += 1
            except Exception:
                self.fetch_errors += 1
            finally:
                ibkr_lock.release()

        self.tickers_fetched = fetched
        self.fetch_duration_seconds = time.monotonic() - t0
        return fetched

    def get_bars(self, ticker: str) -> Optional[pd.DataFrame]:
        """Get cached 1-min bars for a ticker. Returns None if not cached."""
        return self._bars.get(ticker)

    def get_spy_bars(self) -> Optional[pd.DataFrame]:
        """Get cached SPY 1-min bars."""
        return self._spy_bars

    def get_current_price(
        self, ticker: str, ibkr_lock: threading.Lock
    ) -> Optional[float]:
        """Get current price for a ticker (for exit management).

        Uses cached bars if available (last close), otherwise fetches fresh.
        """
        cached = self._bars.get(ticker)
        if cached is not None and len(cached) > 0:
            return float(cached.iloc[-1]["Close"])

        # Fetch fresh if not cached
        if ibkr_lock.acquire(timeout=2):
            try:
                bars = self._connector.get_price_data(ticker, "1m")
                if bars is not None and len(bars) > 0:
                    self._bars[ticker] = bars
                    return float(bars.iloc[-1]["Close"])
            finally:
                ibkr_lock.release()
        return None

    @property
    def cached_tickers(self) -> List[str]:
        return list(self._bars.keys())

    @property
    def size(self) -> int:
        return len(self._bars)

    @property
    def newest_bar_time(self) -> Optional[str]:
        """Timestamp of the newest bar across all cached tickers."""
        newest = None
        for bars in self._bars.values():
            if bars is not None and len(bars) > 0:
                last_idx = bars.index[-1]
                ts = str(last_idx)
                if newest is None or ts > newest:
                    newest = ts
        return newest

    def get_telemetry(self) -> dict:
        return {
            "cycle_id": self._cycle_id,
            "tickers_cached": self.size,
            "tickers_fetched": self.tickers_fetched,
            "cache_hits": self.cache_hits,
            "fetch_errors": self.fetch_errors,
            "fetch_duration_s": round(self.fetch_duration_seconds, 1),
            "newest_bar_time": self.newest_bar_time,
        }


class IntradayScheduler:
    """Fair scheduler for intraday strategies with timing controls.

    Architecture:
    1. One shared fetch pass (populates IntradayBarCache)
    2. Sequential strategy evaluation (no IBKR calls during evaluation)
    3. Per-strategy time budgets
    4. Priority-tiered universe (Tier 1 always, Tier 2/3 budget-permitting)
    5. Cycle SLA enforcement with graceful degradation

    Priority tiers:
      Tier 1 (always scanned): open positions + highest conviction (conv >= 70)
      Tier 2 (scanned if budget permits): moderate conviction (conv >= 55)
      Tier 3 (scanned if surplus budget): remaining qualifying tickers
    """

    # Cycle timing SLA
    CYCLE_SLA_SECONDS = 200.0      # Hard cap: never exceed this
    CYCLE_TARGET_SECONDS = 150.0   # Soft target: ideally finish here

    # Per-strategy time budget for evaluation (after bars are cached)
    STRATEGY_BUDGET_SECONDS = 30.0

    # Fetch budget per tier
    FETCH_BUDGET_TIER1 = 90.0     # Tier 1 gets dedicated budget
    FETCH_BUDGET_TIER2 = 60.0     # Tier 2 gets remainder
    FETCH_BUDGET_TIER3 = 30.0     # Tier 3 only if surplus time

    # Universe cap: max tickers to fetch per cycle regardless
    MAX_FETCH_TICKERS = 30        # Conservative cap — first cycle is cold (contract qual ~2-5s each)

    def __init__(
        self,
        cache: IntradayBarCache,
        connector: Any,
        ibkr_lock: threading.Lock,
        session: Any = None,
    ):
        self._cache = cache
        self._connector = connector
        self._ibkr_lock = ibkr_lock
        self._session = session
        self._strategies: List[dict] = []
        self._cycle_count = 0

        # Telemetry
        self.last_cycle_duration: float = 0.0
        self.last_cycle_strategies: Dict[str, dict] = {}
        self.last_cycle_freshness: Dict[str, Any] = {}
        self.last_cycle_tiers: Dict[str, int] = {}
        self.sla_breaches: int = 0

    def register_strategy(
        self,
        name: str,
        scanner: Any,
        get_tickers_fn: callable,
    ) -> None:
        """Register an intraday strategy for fair scheduling."""
        self._strategies.append({
            "name": name,
            "scanner": scanner,
            "get_tickers_fn": get_tickers_fn,
        })

    def run_cycle(self) -> dict:
        """Execute one fair intraday scan cycle with timing controls.

        1. Collect qualifying tickers, assign priority tiers
        2. Fetch Tier 1 first (open positions + high conviction)
        3. Fetch Tier 2/3 if budget permits
        4. Run each strategy with cached bars
        5. Record freshness telemetry

        Returns telemetry dict.
        """
        if not self._connector.is_connected():
            for s in self._strategies:
                record_skip(s["name"], SkipReason.IBKR_DISCONNECTED)
            return {"error": "IBKR_DISCONNECTED"}

        self._cycle_count += 1
        cycle_start = time.monotonic()
        cycle_started_at = datetime.now(timezone.utc).isoformat()

        if self._session:
            self._session.set_active_job("intraday_fetch")

        # 1. Collect per-strategy tickers
        all_tickers = set()
        per_strategy_tickers: Dict[str, List[str]] = {}
        for s in self._strategies:
            try:
                tickers = s["get_tickers_fn"]()
                per_strategy_tickers[s["name"]] = tickers
                all_tickers.update(tickers)
            except Exception as e:
                logger.warning(f"IntradayScheduler: {s['name']} get_tickers error: {e}")
                per_strategy_tickers[s["name"]] = []

        if not all_tickers:
            for s in self._strategies:
                record_skip(s["name"], SkipReason.NO_SETUP_QUALIFIED)
            if self._session:
                self._session.clear_active_job()
            return {"tickers": 0, "fetched": 0, "strategies_run": 0}

        # 2. Assign priority tiers
        tier1, tier2, tier3 = self._assign_tiers(
            all_tickers, per_strategy_tickers
        )

        logger.info(
            "IntradayScheduler: cycle {} | {} unique tickers | "
            "T1={} T2={} T3={} (cap={})",
            self._cycle_count, len(all_tickers),
            len(tier1), len(tier2), len(tier3), self.MAX_FETCH_TICKERS,
        )

        # 3. Tiered fetch with per-tier budgets
        self._cache.start_cycle()
        total_fetched = 0
        skipped_by_cap = 0

        # Tier 1: always fetch (open positions + highest priority)
        t1_fetched = self._cache.fetch_tickers(
            tier1, self._ibkr_lock, budget_seconds=self.FETCH_BUDGET_TIER1,
        )
        total_fetched += t1_fetched
        elapsed = time.monotonic() - cycle_start

        # Tier 2: fetch if under SLA target
        t2_fetched = 0
        if elapsed < self.CYCLE_TARGET_SECONDS and total_fetched < self.MAX_FETCH_TICKERS:
            remaining_cap = self.MAX_FETCH_TICKERS - total_fetched
            t2_batch = tier2[:remaining_cap]
            t2_fetched = self._cache.fetch_tickers(
                t2_batch, self._ibkr_lock, budget_seconds=self.FETCH_BUDGET_TIER2,
            )
            total_fetched += t2_fetched
            skipped_by_cap += max(0, len(tier2) - remaining_cap)

        elapsed = time.monotonic() - cycle_start

        # Tier 3: fetch only if surplus budget
        t3_fetched = 0
        if elapsed < self.CYCLE_TARGET_SECONDS * 0.7 and total_fetched < self.MAX_FETCH_TICKERS:
            remaining_cap = self.MAX_FETCH_TICKERS - total_fetched
            t3_batch = tier3[:remaining_cap]
            t3_fetched = self._cache.fetch_tickers(
                t3_batch, self._ibkr_lock, budget_seconds=self.FETCH_BUDGET_TIER3,
            )
            total_fetched += t3_fetched
            skipped_by_cap += max(0, len(tier3) - remaining_cap)

        fetch_elapsed = time.monotonic() - cycle_start
        self.last_cycle_tiers = {
            "tier1_requested": len(tier1), "tier1_fetched": t1_fetched,
            "tier2_requested": len(tier2), "tier2_fetched": t2_fetched,
            "tier3_requested": len(tier3), "tier3_fetched": t3_fetched,
            "skipped_by_cap": skipped_by_cap,
        }

        logger.info(
            "IntradayScheduler: fetch complete in {:.1f}s | "
            "T1={}/{} T2={}/{} T3={}/{} | total={}/{} | skipped_cap={}",
            fetch_elapsed,
            t1_fetched, len(tier1), t2_fetched, len(tier2),
            t3_fetched, len(tier3), total_fetched, len(all_tickers),
            skipped_by_cap,
        )

        # 4. Run each strategy with cached bars (NO IBKR lock held)
        # Check SLA before starting evaluation
        if time.monotonic() - cycle_start > self.CYCLE_SLA_SECONDS:
            self.sla_breaches += 1
            logger.warning(
                "IntradayScheduler: CYCLE SLA BREACH — fetch took {:.1f}s "
                "(SLA={:.0f}s), skipping strategy evaluation",
                fetch_elapsed, self.CYCLE_SLA_SECONDS,
            )
            if self._session:
                self._session.clear_active_job()
            return self._build_result(
                cycle_start, cycle_started_at, all_tickers,
                total_fetched, {}, "SLA_BREACH_FETCH",
            )

        telemetry: Dict[str, dict] = {}
        for s in self._strategies:
            # Check remaining SLA budget before each strategy
            remaining = self.CYCLE_SLA_SECONDS - (time.monotonic() - cycle_start)
            if remaining < 5:
                self.sla_breaches += 1
                logger.warning(
                    "IntradayScheduler: SLA exhausted, skipping {} "
                    "({:.1f}s remaining)",
                    s["name"], remaining,
                )
                record_skip(s["name"], SkipReason.LOCK_TIMEOUT,
                             f"cycle SLA exhausted ({remaining:.0f}s left)")
                telemetry[s["name"]] = {
                    "tickers": len(per_strategy_tickers.get(s["name"], [])),
                    "scanned": 0, "entries": 0, "duration_s": 0,
                    "skipped": "SLA_EXHAUSTED",
                }
                continue

            name = s["name"]
            scanner = s["scanner"]
            strat_tickers = per_strategy_tickers.get(name, [])

            if self._session:
                self._session.set_active_job(name)

            budget = min(self.STRATEGY_BUDGET_SECONDS, remaining - 2)
            t0 = time.monotonic()
            entries = 0
            scanned = 0
            try:
                entries = self._run_strategy_with_cache(
                    scanner, name, strat_tickers, budget,
                )
                scanned = len(strat_tickers)
            except Exception as e:
                logger.warning(f"IntradayScheduler: {name} error: {e}")

            duration = time.monotonic() - t0
            telemetry[name] = {
                "tickers": len(strat_tickers),
                "scanned": scanned,
                "entries": entries,
                "duration_s": round(duration, 1),
            }
            logger.info(
                "IntradayScheduler: {} | {} tickers | {} entries | {:.1f}s",
                name, scanned, entries, duration,
            )

        if self._session:
            self._session.clear_active_job()

        return self._build_result(
            cycle_start, cycle_started_at, all_tickers, total_fetched, telemetry,
        )

    def _assign_tiers(
        self,
        all_tickers: set,
        per_strategy: Dict[str, List[str]],
    ) -> tuple:
        """Assign priority tiers to the ticker universe.

        Tier 1: Tickers in VWAP_MR (highest conviction gate, conv>=65)
                + any open position symbols
        Tier 2: Tickers qualifying for any strategy but not in Tier 1
        Tier 3: Remaining tickers (lowest priority)

        Returns (tier1_list, tier2_list, tier3_list).
        """
        # VWAP_MR has the highest conviction bar — those are the best signals
        tier1_set = set(per_strategy.get("VWAP_MR", []))

        # Add open position symbols (always need fresh data for exits)
        for s in self._strategies:
            scanner = s["scanner"]
            if hasattr(scanner, "_get_open_vwap_mr_positions"):
                try:
                    for p in scanner._get_open_vwap_mr_positions():
                        tier1_set.add(p.get("symbol", ""))
                except Exception:
                    pass
            if hasattr(scanner, "_get_open_fpb_positions"):
                try:
                    for p in scanner._get_open_fpb_positions():
                        tier1_set.add(p.get("symbol", ""))
                except Exception:
                    pass
            if hasattr(scanner, "_get_open_orb_positions"):
                try:
                    for p in scanner._get_open_orb_positions():
                        tier1_set.add(p.get("symbol", ""))
                except Exception:
                    pass

        tier1_set.discard("")

        # Tier 2: in at least 2 strategies but not Tier 1
        ticker_strategy_count: Dict[str, int] = {}
        for strat_tickers in per_strategy.values():
            for t in strat_tickers:
                ticker_strategy_count[t] = ticker_strategy_count.get(t, 0) + 1

        tier2_set = set()
        tier3_set = set()
        for t in all_tickers:
            if t in tier1_set:
                continue
            if ticker_strategy_count.get(t, 0) >= 2:
                tier2_set.add(t)
            else:
                tier3_set.add(t)

        return list(tier1_set), list(tier2_set), list(tier3_set)

    def _build_result(
        self, cycle_start: float, cycle_started_at: str,
        all_tickers: set, total_fetched: int,
        telemetry: Dict[str, dict], degradation: str = None,
    ) -> dict:
        """Build cycle result with freshness telemetry."""
        self.last_cycle_duration = time.monotonic() - cycle_start
        self.last_cycle_strategies = telemetry
        cycle_ended_at = datetime.now(timezone.utc).isoformat()

        cache_telem = self._cache.get_telemetry()
        self.last_cycle_freshness = {
            "cycle_started_at": cycle_started_at,
            "cycle_ended_at": cycle_ended_at,
            "newest_bar_time": cache_telem.get("newest_bar_time"),
            "decision_age_s": round(self.last_cycle_duration, 1),
        }

        result = {
            "cycle": self._cycle_count,
            "unique_tickers": len(all_tickers),
            "fetched": total_fetched,
            "cache": cache_telem,
            "tiers": self.last_cycle_tiers,
            "strategies": telemetry,
            "total_duration_s": round(self.last_cycle_duration, 1),
            "freshness": self.last_cycle_freshness,
            "sla_breaches_total": self.sla_breaches,
        }
        if degradation:
            result["degradation"] = degradation

        sla_status = "OK"
        if self.last_cycle_duration > self.CYCLE_SLA_SECONDS:
            sla_status = "BREACH"
        elif self.last_cycle_duration > self.CYCLE_TARGET_SECONDS:
            sla_status = "WARN"
        result["sla_status"] = sla_status

        logger.info(
            "IntradayScheduler: cycle {} complete | {:.1f}s | SLA={} | "
            "fetched={}/{} | bar_age={:.1f}s{}",
            self._cycle_count, self.last_cycle_duration, sla_status,
            total_fetched, len(all_tickers),
            self.last_cycle_duration,
            f" | DEGRADED={degradation}" if degradation else "",
        )
        return result

    def _run_strategy_with_cache(
        self,
        scanner: Any,
        name: str,
        tickers: List[str],
        budget: float = None,
    ) -> int:
        """Run a strategy's scan loop using cached bars.

        Passes explicit ticker list and budget so the strategy:
        - only evaluates tickers present in the shared cache
        - respects a wall-clock time budget
        - never falls back to direct IBKR fetches
        """
        if budget is None:
            budget = self.STRATEGY_BUDGET_SECONDS
        if hasattr(scanner, "run_with_cache"):
            return scanner.run_with_cache(
                self._cache,
                self._ibkr_lock,
                tickers=tickers,
                budget_seconds=budget,
            )
        else:
            logger.warning(
                f"IntradayScheduler: {name} has no run_with_cache() — skipped"
            )
            record_skip(name, SkipReason.MODEL_UNAVAILABLE,
                         "no run_with_cache method")
            return 0
