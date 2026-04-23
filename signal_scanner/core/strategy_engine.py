"""Strategy Engine — reads bars from LiveBarStore, evaluates locally.

Zero IBKR dependency. Pure CPU evaluation.
Consumes the same shared bars that the bar printer wrote.
Emits signals to live_strategy_signals table.

Usage:
    engine = StrategyEngine(bar_store, db_manager)
    engine.register(vwap_mr_scanner, "VWAP_MR")
    engine.evaluate_all()  # called on schedule, after bar printer writes
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from loguru import logger

from signal_scanner.core.live_bar_store import LiveBarStore


class StrategyEngine:
    """Evaluates strategies against locally-stored bars.

    No IBKR calls. Reads from LiveBarStore only.
    """

    def __init__(self, store: LiveBarStore):
        self._store = store
        self._strategies: List[Dict] = []
        self._cycle_count = 0

    def register(self, scanner: Any, name: str,
                 get_tickers_fn: callable) -> None:
        """Register a strategy for evaluation.

        Args:
            scanner: live scanner instance (VWAPMRLiveScanner, etc.)
            name: strategy name
            get_tickers_fn: returns qualifying tickers for this strategy
        """
        self._strategies.append({
            "name": name,
            "scanner": scanner,
            "get_tickers_fn": get_tickers_fn,
        })

    def evaluate_all(self) -> Dict[str, Any]:
        """Evaluate all registered strategies against stored bars.

        Returns telemetry dict.
        """
        self._cycle_count += 1
        cycle_start = time.monotonic()
        results = {}

        try:
            from zoneinfo import ZoneInfo
            now_et = datetime.now(ZoneInfo("America/New_York"))
        except ImportError:
            now_et = datetime.utcnow()

        # Get SPY bars for relative strength (shared across strategies)
        spy_bars = self._store.get_bars("SPY")

        for s in self._strategies:
            name = s["name"]
            scanner = s["scanner"]
            t0 = time.monotonic()

            try:
                # Feed SPY bars
                if spy_bars is not None:
                    scanner._spy_bars_today = spy_bars

                # Get qualifying tickers for this strategy
                tickers = s["get_tickers_fn"]()

                evaluated = 0
                entries = 0
                stale_skipped = 0

                # Pre-compute sector ETF mapping for RS gate
                from signal_scanner.core.intraday_rs import compute_intraday_rs, get_sector_etf
                from signal_scanner.core.vwap_bands import compute_vwap_sigma
                from signal_scanner.core.volume_pressure import compute_volume_pressure

                for ticker in tickers:
                    # Read bars from store
                    bars = self._store.get_bars(ticker)
                    last_bar_ts = str(bars.index[-1]) if bars is not None and len(bars) > 0 else None

                    if bars is None:
                        self._store.record_signal({
                            "strategy": name, "symbol": ticker,
                            "bar_ts_used": None,
                            "signal_type": "NO_BARS",
                            "freshness_state": "MISSING",
                        })
                        continue

                    # Check freshness
                    sym_status = self._store.get_symbol_status(ticker)
                    if sym_status and sym_status.get("is_stale"):
                        stale_skipped += 1
                        self._store.record_signal({
                            "strategy": name, "symbol": ticker,
                            "bar_ts_used": last_bar_ts,
                            "signal_type": "STALE_SKIP",
                            "freshness_state": "STALE",
                        })
                        continue

                    evaluated += 1

                    # Compute context features (score boosts, not hard gates)
                    sector_rs = None
                    vwap_sigma = None
                    vol_pressure = None
                    context_parts = []
                    try:
                        # Sector RS: stock return vs sector ETF
                        sector_etf = get_sector_etf(
                            getattr(scanner, "_daily_context", {}).get(ticker, {}).get("sector", "")
                        )
                        sector_rs = compute_intraday_rs(self._store, ticker, sector_etf)
                        if sector_rs is not None:
                            context_parts.append(f"RS={sector_rs:+.4f}vs{sector_etf}")

                        # VWAP sigma: exhaustion detection
                        vwap_sigma = compute_vwap_sigma(bars)
                        if vwap_sigma:
                            context_parts.append(f"VWAP_sigma={vwap_sigma['sigma_distance']:+.1f}({vwap_sigma['verdict']})")

                        # Volume pressure: buying/selling proxy
                        vol_pressure = compute_volume_pressure(bars)
                        if vol_pressure:
                            context_parts.append(f"VolP={vol_pressure['pressure_score']:.0f}({vol_pressure['verdict']})")
                    except Exception:
                        pass

                    context_str = " | ".join(context_parts) if context_parts else ""

                    try:
                        # Use _evaluate_setup only for VWAP_MR (fully refactored).
                        # FPB/ORB_V2 use _scan_ticker fallback until Phase H full refactor.
                        if hasattr(scanner, "_evaluate_setup") and name == "VWAP_MR":
                            signal = scanner._evaluate_setup(ticker, now_et, cached_bars=bars)
                            if signal:
                                # Attach context features to signal
                                signal["sector_rs"] = sector_rs
                                signal["vwap_sigma"] = vwap_sigma.get("sigma_distance") if vwap_sigma else None
                                signal["vwap_exhaustion"] = vwap_sigma.get("verdict") if vwap_sigma else None
                                signal["vol_pressure"] = vol_pressure.get("pressure_score") if vol_pressure else None
                                entries += 1
                                import json as _json
                                self._store.record_signal({
                                    "strategy": name, "symbol": ticker,
                                    "bar_ts_used": last_bar_ts,
                                    "signal_type": "ENTRY",
                                    "freshness_state": "FRESH",
                                    "score": signal.get("ml_prob"),
                                    "percentile": signal.get("ml_percentile"),
                                    "rationale": _json.dumps(signal, default=str),
                                    "recommendation_source": f"{name}_ML",
                                    "status": "PENDING_EXECUTION",
                                })
                            else:
                                self._store.record_signal({
                                    "strategy": name, "symbol": ticker,
                                    "bar_ts_used": last_bar_ts,
                                    "signal_type": "EVALUATED_NO_SETUP",
                                    "freshness_state": "FRESH",
                                    "rationale": context_str,
                                })
                        else:
                            # Fallback: _scan_ticker (coupled evaluation + execution)
                            fired = scanner._scan_ticker(ticker, now_et, cached_bars=bars)
                            if fired:
                                entries += 1
                                self._store.record_signal({
                                    "strategy": name, "symbol": ticker,
                                    "bar_ts_used": last_bar_ts,
                                    "signal_type": "ENTRY",
                                    "freshness_state": "FRESH",
                                    "recommendation_source": f"{name}_ML",
                                    "rationale": context_str,
                                })
                            else:
                                self._store.record_signal({
                                    "strategy": name, "symbol": ticker,
                                    "bar_ts_used": last_bar_ts,
                                    "signal_type": "EVALUATED_NO_SETUP",
                                    "freshness_state": "FRESH",
                                    "rationale": context_str,
                                })
                    except Exception as e:
                        logger.debug(f"{name} {ticker}: {e}")
                        self._store.record_signal({
                            "strategy": name, "symbol": ticker,
                            "bar_ts_used": last_bar_ts,
                            "signal_type": "EVAL_ERROR",
                            "freshness_state": "FRESH",
                            "rationale": str(e)[:200],
                        })

                duration = time.monotonic() - t0
                results[name] = {
                    "tickers": len(tickers),
                    "evaluated": evaluated,
                    "entries": entries,
                    "stale_skipped": stale_skipped,
                    "duration_s": round(duration, 1),
                }

            except Exception as e:
                logger.warning(f"StrategyEngine {name} error: {e}")
                results[name] = {"error": str(e)}

        total = time.monotonic() - cycle_start

        # Health update
        self._store.update_health(
            "strategy_engine",
            cycles=1,
            lag=round(total, 1),
            notes=" | ".join(
                f"{k}={v.get('entries', 0)}e/{v.get('evaluated', 0)}eval"
                for k, v in results.items()
                if isinstance(v, dict) and "error" not in v
            ),
        )

        logger.info(
            "StrategyEngine cycle {}: {:.1f}s | {}",
            self._cycle_count, total,
            " | ".join(
                f"{k}={v.get('entries', 0)}e/{v.get('evaluated', 0)}eval/{v.get('duration_s', 0)}s"
                for k, v in results.items()
                if isinstance(v, dict) and "error" not in v
            ),
        )

        return {
            "cycle": self._cycle_count,
            "total_s": round(total, 1),
            "strategies": results,
        }
