"""Premarket Universe Builder — determines tracked intraday symbols.

Runs before market open. Builds the session universe from:
  1. Intelligence snapshot (conviction + phase filtering)
  2. Open positions (always tracked)
  3. Tier assignment based on strategy eligibility

Tiers:
  Tier 1: Open positions + highest conviction (conv >= 65, VWAP_MR eligible)
  Tier 2: Moderate conviction (conv >= 50, FPB/ORB eligible)
  Tier 3: Controlled intraday additions (explicit rules only)
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List

from loguru import logger

from signal_scanner.core.live_bar_store import LiveBarStore


def build_session_universe(
    store: LiveBarStore,
    session_date: str = None,
    intelligence_snapshot: Dict[str, Dict] = None,
    open_positions: List[str] = None,
) -> int:
    """Build and persist the tracked universe for today's session.

    Args:
        store: LiveBarStore instance
        session_date: YYYY-MM-DD (default: today)
        intelligence_snapshot: {ticker: {conviction, phase, ...}} from scanner
        open_positions: list of symbols with open trades

    Returns: number of symbols in universe.
    """
    if session_date is None:
        session_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    if intelligence_snapshot is None:
        intelligence_snapshot = {}

    if open_positions is None:
        open_positions = []

    symbols = []
    open_set = set(open_positions)

    # Always include SPY + sector ETFs (needed for relative strength + intraday RS gate)
    BENCHMARKS = ["SPY", "XLK", "XLF", "XLE", "XLV", "XLI", "XLY", "XLP", "XLU", "XLB", "XLRE", "XLC"]
    for etf in BENCHMARKS:
        symbols.append({
            "symbol": etf,
            "tier": 1,
            "source_eligibility": "BENCHMARK",
            "conviction": 100,
            "accum_phase": "BENCHMARK",
            "open_position": False,
            "added_reason": "benchmark_etf",
        })

    # Open positions = always Tier 1
    for sym in open_positions:
        snap = intelligence_snapshot.get(sym, {})
        symbols.append({
            "symbol": sym,
            "tier": 1,
            "source_eligibility": "VWAP_MR,FPB,ORB_V2",
            "conviction": snap.get("inst_conviction", 0),
            "accum_phase": snap.get("inst_phase", ""),
            "open_position": True,
            "added_reason": "open_position",
        })

    seen = {"SPY"} | open_set

    # Intelligence-based universe
    ACCUM_PHASES = {"ACTIVE_ACCUM", "LATE_ACCUM", "EARLY_ACCUM", "EXPANSION"}

    for ticker, snap in intelligence_snapshot.items():
        if ticker in seen:
            continue

        conv = snap.get("inst_conviction", 0) or 0
        phase = snap.get("inst_phase", "")

        if phase not in ACCUM_PHASES:
            continue

        if conv >= 65:
            # Tier 1: VWAP_MR eligible (high conviction)
            symbols.append({
                "symbol": ticker,
                "tier": 1,
                "source_eligibility": "VWAP_MR,FPB,ORB_V2",
                "conviction": conv,
                "accum_phase": phase,
                "open_position": False,
                "added_reason": "premarket_t1",
            })
            seen.add(ticker)
        elif conv >= 50:
            # Tier 2: FPB/ORB eligible (moderate conviction)
            symbols.append({
                "symbol": ticker,
                "tier": 2,
                "source_eligibility": "FPB,ORB_V2",
                "conviction": conv,
                "accum_phase": phase,
                "open_position": False,
                "added_reason": "premarket_t2",
            })
            seen.add(ticker)

    # Sort: Tier 1 first (by conviction desc), then Tier 2
    symbols.sort(key=lambda s: (s["tier"], -s.get("conviction", 0)))

    # Persist
    store.clear_session()
    count = store.set_universe(session_date, symbols)

    t1 = sum(1 for s in symbols if s["tier"] == 1)
    t2 = sum(1 for s in symbols if s["tier"] == 2)
    logger.info(
        "Universe built: {} symbols (T1={}, T2={}, open_pos={}) for {}",
        count, t1, t2, len(open_positions), session_date,
    )
    return count
