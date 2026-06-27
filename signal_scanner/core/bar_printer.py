"""Bar Printer — IBKR → SQLite ingest daemon.

One job, one purpose: fetch 1-min bars from IBKR, write to LiveBarStore.
No strategy logic. No order logic. Just data ingestion.

Runs on its own dedicated thread with its own IBKR connection + event loop.
This avoids ib_insync threading issues with APScheduler.

Usage:
    printer = BarPrinter(ibkr_config, bar_store)
    printer.start(symbols)   # spawns background thread
    printer.stop()           # clean shutdown
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from loguru import logger

from signal_scanner.core.live_bar_store import LiveBarStore


class BarPrinter:
    """Fetches bars from IBKR and writes to LiveBarStore.

    Runs on its own thread with its own IB() connection.
    Adaptive: measures throughput, prioritizes, rotates.
    """

    CYCLE_INTERVAL = 60         # seconds between fetch cycles
    BUDGET_SECONDS = 55.0       # max fetch time per cycle

    def __init__(self, ibkr_config: Any, store: LiveBarStore,
                 client_id_offset: int = 5):
        self._ibkr_config = ibkr_config
        self._store = store
        self._client_id_offset = client_id_offset
        self._connector = None  # own IBKR connection, created on thread
        self._rotation_offset = 0
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._symbols: List[str] = []
        self._cycle_count = 0
        self._last_cycle_ms_per_ticker = 0.0
        self._total_fetched = 0
        self._total_errors = 0

    def start(self, symbols: List[str]) -> None:
        """Start the bar printer thread."""
        self._symbols = list(symbols)
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop, name="BarPrinter", daemon=True,
        )
        self._thread.start()
        logger.info("BarPrinter: started on dedicated thread ({} symbols)", len(symbols))

    def stop(self) -> None:
        """Stop the bar printer thread."""
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=10)
        if self._connector and self._connector.is_connected():
            try:
                self._connector._ib.disconnect()
            except Exception:
                pass
        logger.info("BarPrinter: stopped")

    def update_symbols(self, symbols: List[str]) -> None:
        """Update the tracked symbol list (thread-safe)."""
        self._symbols = list(symbols)

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _run_loop(self) -> None:
        """Main loop: ensure IBKR connection, then cycle fetch→sleep→fetch.

        Stays alive even if IBKR isn't reachable at startup — keeps retrying
        every 30s until it connects, then runs the bar fetch loop.  If the
        connection drops mid-session, falls back to the retry path instead of
        exiting the thread.
        """
        # Create own IBKR connection on this thread (own event loop)
        from signal_scanner.core.ibkr_connector import DataConnector
        self._connector = DataConnector(self._ibkr_config)

        connect_logged = False
        while not self._stop_event.is_set():
            if not self._connector.is_connected():
                ok = self._connector.connect_ibkr()
                if not ok:
                    if not connect_logged:
                        logger.warning(
                            "BarPrinter: IBKR not reachable — will retry every 30s"
                        )
                        connect_logged = True
                    self._store.update_health(
                        "bar_printer", errors=1, notes="IBKR_CONNECT_RETRY"
                    )
                    self._stop_event.wait(timeout=30)
                    continue
                logger.info(
                    "BarPrinter: IBKR connected (clientId={})",
                    self._connector._connected_client_id,
                )
                connect_logged = False

            cycle_start = time.monotonic()
            try:
                self.run_cycle(self._symbols, budget_seconds=self.BUDGET_SECONDS)
            except Exception as e:
                logger.warning("BarPrinter cycle error: {}", e)
                self._store.update_health(
                    "bar_printer", errors=1, notes=str(e)[:100]
                )

            # Wall-clock cadence: wait until next scheduled time, not after
            elapsed = time.monotonic() - cycle_start
            remaining = max(1, self.CYCLE_INTERVAL - elapsed)
            self._stop_event.wait(timeout=remaining)

        # Cleanup
        try:
            if self._connector and self._connector._ib is not None:
                self._connector._ib.disconnect()
        except Exception:
            pass

    def run_cycle(
        self,
        symbols: List[str],
        budget_seconds: float = 55.0,
    ) -> Dict[str, Any]:
        """Fetch bars for as many symbols as budget allows.

        Symbols should be pre-sorted by priority (Tier 1 first).
        Stops when budget_seconds is exhausted.
        Rotates offset for next cycle to cover remaining symbols.

        Returns telemetry dict.
        """
        if not self._connector.is_connected():
            logger.warning("BarPrinter: IBKR not connected")
            self._store.update_health(
                "bar_printer", errors=1, notes="IBKR_DISCONNECTED",
            )
            return {"error": "IBKR_DISCONNECTED", "fetched": 0}

        self._cycle_count += 1
        cycle_start = time.monotonic()

        # Apply rotation offset for large universes
        n = len(symbols)
        if n == 0:
            self._store.update_health("bar_printer", cycles=1, notes="empty universe")
            return {"fetched": 0, "total": 0, "cycle": self._cycle_count}

        # Rotate through large universes so the tail doesn't starve.
        # Tier 1 (front of list) always first; rotation applies to remainder.
        t1_end = min(n, 80)  # first ~80 are Tier 1 (caller sorts by priority)
        tier1 = list(symbols[:t1_end])
        tail = list(symbols[t1_end:])
        if tail and self._rotation_offset > 0:
            offset = self._rotation_offset % len(tail)
            tail = tail[offset:] + tail[:offset]
        self._rotation_offset += max(1, len(tail) // 3)  # advance by ~1/3 each cycle
        ordered = tier1 + tail

        fetched = 0
        errors = 0
        new_bars = 0

        for symbol in ordered:
            elapsed = time.monotonic() - cycle_start
            if elapsed >= budget_seconds:
                break

            try:
                bars = self._connector.get_price_data(symbol, "1m")
                if bars is not None and len(bars) > 0:
                    inserted = self._store.write_bars(symbol, bars)
                    fetched += 1
                    new_bars += inserted
                else:
                    self._store.mark_fetch_error(symbol, "empty_response")
                    errors += 1
            except Exception as e:
                self._store.mark_fetch_error(symbol, str(e)[:200])
                errors += 1

        elapsed = time.monotonic() - cycle_start

        # Update staleness for all tracked symbols
        stale_count = self._store.update_staleness()

        # Compute throughput
        if fetched > 0:
            self._last_cycle_ms_per_ticker = (elapsed / fetched) * 1000
        capacity_per_min = int(60_000 / max(self._last_cycle_ms_per_ticker, 1)) if self._last_cycle_ms_per_ticker > 0 else 0

        self._total_fetched += fetched
        self._total_errors += errors

        # Health update
        self._store.update_health(
            "bar_printer",
            cycles=1,
            errors=errors,
            lag=round(elapsed, 1),
            notes=(
                f"fetched={fetched}/{n} | new_bars={new_bars} | "
                f"stale={stale_count} | {self._last_cycle_ms_per_ticker:.0f}ms/tick | "
                f"capacity={capacity_per_min}/min"
            ),
        )

        result = {
            "cycle": self._cycle_count,
            "fetched": fetched,
            "total_universe": n,
            "errors": errors,
            "new_bars": new_bars,
            "stale": stale_count,
            "elapsed_s": round(elapsed, 1),
            "ms_per_ticker": round(self._last_cycle_ms_per_ticker, 0),
            "capacity_per_min": capacity_per_min,
            "coverage_pct": round(fetched / n * 100, 0) if n > 0 else 0,
        }

        logger.info(
            "BarPrinter cycle {}: {}/{} tickers in {:.1f}s | "
            "{:.0f}ms/tick | capacity={}/min | new_bars={} | stale={}",
            self._cycle_count, fetched, n, elapsed,
            self._last_cycle_ms_per_ticker, capacity_per_min,
            new_bars, stale_count,
        )

        return result
