"""Multi-Strategy Backtester — runs multiple intraday strategies against
pre-computed features from ``fact_intraday_features``.

Strategies:
    VWAP_MR      — VWAP Mean Reversion (buy dip below VWAP on accum stocks)
    FPB          — First Pullback after OR breakout
    MOMENTUM_IGN — Momentum Ignition (tight consolidation → volume breakout)

Usage:
    # Run one strategy
    python -m signal_scanner.institutional_intel.intelligence.strategy_backtester \
        --run --strategy VWAP_MR --quarters 2024-Q1,2024-Q2,2024-Q3

    # Run all strategies
    python -m ... --run --strategy ALL --quarters 2024-Q1,2024-Q2,2024-Q3

    # Compare strategies
    python -m ... --compare --quarters 2024-Q1,2024-Q2,2024-Q3
"""

from __future__ import annotations

import argparse
import calendar
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import duckdb
import numpy as np
import pandas as pd
from loguru import logger

from signal_scanner.institutional_intel.config import WAREHOUSE_PATH

ALL_STRATEGIES = ["VWAP_MR", "FPB", "MOMENTUM_IGN", "ORB_V2"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _quarter_end_date(quarter: str) -> date:
    year = int(quarter.split("-Q")[0])
    qnum = int(quarter.split("-Q")[1])
    end_month = {1: 3, 2: 6, 3: 9, 4: 12}[qnum]
    last_day = calendar.monthrange(year, end_month)[1]
    return date(year, end_month, last_day)


def _filing_date(quarter: str) -> date:
    return _quarter_end_date(quarter) + timedelta(days=45)


def _next_quarter(quarter: str) -> str:
    year = int(quarter.split("-Q")[0])
    qnum = int(quarter.split("-Q")[1])
    qnum += 1
    if qnum > 4:
        qnum = 1
        year += 1
    return f"{year}-Q{qnum}"


# ---------------------------------------------------------------------------
# DuckDB table
# ---------------------------------------------------------------------------

def _ensure_tables(conn: duckdb.DuckDBPyConnection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS strategy_backtest_results (
            ticker              TEXT NOT NULL,
            trade_date          DATE NOT NULL,
            strategy            TEXT NOT NULL,
            report_quarter      TEXT,
            conviction_score    DOUBLE,
            accum_phase         TEXT,
            squeeze_score       DOUBLE,
            sector              TEXT,

            -- Setup & Entry
            setup_detected      BOOLEAN DEFAULT FALSE,
            setup_time          TIMESTAMP,
            entry_triggered     BOOLEAN DEFAULT FALSE,
            entry_time          TIMESTAMP,
            entry_price         DOUBLE,
            stop_price          DOUBLE,
            stop_distance_pct   DOUBLE,

            -- R targets
            hit_1r              BOOLEAN DEFAULT FALSE,
            hit_2r              BOOLEAN DEFAULT FALSE,
            hit_3r              BOOLEAN DEFAULT FALSE,
            hit_4r              BOOLEAN DEFAULT FALSE,
            hit_stop            BOOLEAN DEFAULT FALSE,
            time_to_1r_min      INTEGER,
            time_to_2r_min      INTEGER,
            time_to_3r_min      INTEGER,
            time_to_4r_min      INTEGER,
            time_to_stop_min    INTEGER,
            max_favorable_r     DOUBLE,
            max_adverse_r       DOUBLE,
            trail_exit_price    DOUBLE,
            trail_exit_r        DOUBLE,
            eod_price           DOUBLE,
            eod_r               DOUBLE,

            computed_at         TIMESTAMP,
            PRIMARY KEY (ticker, trade_date, strategy)
        )
    """)


# ---------------------------------------------------------------------------
# Shared R-target tracker
# ---------------------------------------------------------------------------

def _track_r_targets(
    bars: pd.DataFrame,
    entry_price: float,
    stop_price: float,
    entry_time: pd.Timestamp,
) -> Dict[str, Any]:
    """Track R-target hits, MFE/MAE, and trailing stop from entry forward.

    Args:
        bars: 1-min bars with columns [bar_time, open, high, low, close, volume].
        entry_price: Entry price.
        stop_price: Stop price.
        entry_time: Entry timestamp.

    Returns dict with hit_1r..hit_4r, hit_stop, times, MFE/MAE, trail, EOD.
    """
    r_unit = abs(entry_price - stop_price)
    is_long = entry_price > stop_price

    result: Dict[str, Any] = {
        "hit_1r": False, "hit_2r": False, "hit_3r": False, "hit_4r": False,
        "hit_stop": False,
        "time_to_1r_min": None, "time_to_2r_min": None,
        "time_to_3r_min": None, "time_to_4r_min": None,
        "time_to_stop_min": None,
        "max_favorable_r": 0.0, "max_adverse_r": 0.0,
        "trail_exit_price": None, "trail_exit_r": None,
        "eod_price": None, "eod_r": None,
    }

    if r_unit <= 0 or bars.empty:
        return result

    bar_times = pd.to_datetime(bars["bar_time"])
    post_entry = bars[bar_times >= entry_time]

    if post_entry.empty:
        return result

    # R-targets
    if is_long:
        targets = {n: entry_price + n * r_unit for n in [1, 2, 3, 4]}
    else:
        targets = {n: entry_price - n * r_unit for n in [1, 2, 3, 4]}

    max_fav = 0.0
    max_adv = 0.0
    trail_active = False
    trail_exited = False

    for _, bar in post_entry.iterrows():
        bar_high = float(bar["high"])
        bar_low = float(bar["low"])
        bar_time_val = bar["bar_time"]
        mins = int((pd.Timestamp(bar_time_val) - entry_time).total_seconds() / 60)

        if is_long:
            r_fav = (bar_high - entry_price) / r_unit
            r_adv = (bar_low - entry_price) / r_unit
        else:
            r_fav = (entry_price - bar_low) / r_unit
            r_adv = (entry_price - bar_high) / r_unit

        max_fav = max(max_fav, r_fav)
        max_adv = min(max_adv, r_adv)

        # Stop check first (first-one-wins)
        if is_long and bar_low <= stop_price:
            result["hit_stop"] = True
            result["time_to_stop_min"] = mins
            break
        elif not is_long and bar_high >= stop_price:
            result["hit_stop"] = True
            result["time_to_stop_min"] = mins
            break

        # Target hits
        for n in [1, 2, 3, 4]:
            key = f"hit_{n}r"
            if not result[key]:
                if is_long and bar_high >= targets[n]:
                    result[key] = True
                    result[f"time_to_{n}r_min"] = mins
                elif not is_long and bar_low <= targets[n]:
                    result[key] = True
                    result[f"time_to_{n}r_min"] = mins

        # Trailing stop: after 1R, trail to breakeven
        if result["hit_1r"] and not trail_active:
            trail_active = True

        if trail_active and not trail_exited:
            if is_long and bar_low <= entry_price:
                trail_exited = True
                result["trail_exit_price"] = entry_price
                result["trail_exit_r"] = 0.0
            elif not is_long and bar_high >= entry_price:
                trail_exited = True
                result["trail_exit_price"] = entry_price
                result["trail_exit_r"] = 0.0

    result["max_favorable_r"] = round(max_fav, 2)
    result["max_adverse_r"] = round(max_adv, 2)

    # If trailing stop active but not exited, use last bar close
    if trail_active and not trail_exited and not result["hit_stop"]:
        last_close = float(post_entry.iloc[-1]["close"])
        result["trail_exit_price"] = last_close
        if is_long:
            result["trail_exit_r"] = round((last_close - entry_price) / r_unit, 2)
        else:
            result["trail_exit_r"] = round((entry_price - last_close) / r_unit, 2)

    # EOD
    if not bars.empty:
        eod_close = float(bars.iloc[-1]["close"])
        result["eod_price"] = eod_close
        if is_long:
            result["eod_r"] = round((eod_close - entry_price) / r_unit, 2)
        else:
            result["eod_r"] = round((entry_price - eod_close) / r_unit, 2)

    return result


# ---------------------------------------------------------------------------
# Running VWAP helper (duplicated from feature engine for self-containment)
# ---------------------------------------------------------------------------

def _compute_running_vwap(highs: np.ndarray, lows: np.ndarray,
                          closes: np.ndarray, volumes: np.ndarray) -> np.ndarray:
    tp = (highs + lows + closes) / 3.0
    vol = volumes.astype(np.float64)
    cum_tp_vol = np.cumsum(tp * vol)
    cum_vol = np.cumsum(vol)
    cum_vol = np.where(cum_vol == 0, 1.0, cum_vol)
    return cum_tp_vol / cum_vol


# ---------------------------------------------------------------------------
# Strategy 1: VWAP Mean Reversion
# ---------------------------------------------------------------------------

def _simulate_vwap_mr(
    bars: pd.DataFrame,
    feat: Dict[str, Any],
) -> Dict[str, Any]:
    """VWAP Mean Reversion: buy dip below VWAP on accumulation stocks.

    Filter: accum_phase IN (ACTIVE_ACCUM, LATE_ACCUM), conviction >= 65
    Entry window: 9:45-11:00
    Setup: Price dips >0.3% below running VWAP
    Entry: First bar closing above VWAP after dip, with vol > 1.2x avg bar vol
    Stop: Day-low at entry time OR VWAP - ATR (whichever tighter)
    """
    result: Dict[str, Any] = {
        "setup_detected": False, "setup_time": None,
        "entry_triggered": False, "entry_time": None,
        "entry_price": None, "stop_price": None, "stop_distance_pct": None,
    }

    # Filter
    phase = feat.get("accum_phase", "")
    conviction = feat.get("conviction_score", 0) or 0
    if phase not in ("ACTIVE_ACCUM", "LATE_ACCUM"):
        return result
    if conviction < 65:
        return result

    if bars.empty or len(bars) < 30:
        return result

    # RTH filter
    bar_times = pd.to_datetime(bars["bar_time"])
    rth_mask = (
        ((bar_times.dt.hour == 9) & (bar_times.dt.minute >= 30)) |
        ((bar_times.dt.hour >= 10) & (bar_times.dt.hour < 16))
    )
    bars = bars[rth_mask.values].reset_index(drop=True)
    if len(bars) < 30:
        return result

    bar_times = pd.to_datetime(bars["bar_time"])
    hours = bar_times.dt.hour.values
    minutes = bar_times.dt.minute.values

    highs = bars["high"].values.astype(np.float64)
    lows = bars["low"].values.astype(np.float64)
    closes = bars["close"].values.astype(np.float64)
    volumes = bars["volume"].values.astype(np.float64)

    running_vwap = _compute_running_vwap(highs, lows, closes, volumes)
    avg_bar_vol = float(volumes.mean()) if len(volumes) > 0 else 1.0

    atr = feat.get("atr_20d") or 0

    # Entry window: 9:45-11:00
    dip_detected = False
    dip_time = None
    running_low = float("inf")

    for i in range(len(bars)):
        h, m = int(hours[i]), int(minutes[i])

        # Only scan 9:45-11:00
        if h == 9 and m < 45:
            running_low = min(running_low, lows[i])
            continue
        if h > 11 or (h == 11 and m > 0):
            break

        running_low = min(running_low, lows[i])
        vwap_here = running_vwap[i]
        if vwap_here <= 0:
            continue

        price_dev = (closes[i] - vwap_here) / vwap_here * 100

        # Setup: price dips > 0.3% below VWAP
        if not dip_detected and price_dev < -0.3:
            dip_detected = True
            dip_time = bars.iloc[i]["bar_time"]
            result["setup_detected"] = True
            result["setup_time"] = dip_time
            continue

        # Entry: after dip, first bar closing above VWAP with volume
        if dip_detected and not result["entry_triggered"]:
            if closes[i] > vwap_here and volumes[i] > 1.2 * avg_bar_vol:
                entry_price = float(closes[i])
                # Stop: day-low at entry time OR VWAP - ATR, whichever tighter
                stop_vwap = vwap_here - atr if atr > 0 else running_low
                stop_price = max(running_low, stop_vwap)  # tighter = higher

                if entry_price <= stop_price:
                    continue

                result["entry_triggered"] = True
                result["entry_time"] = bars.iloc[i]["bar_time"]
                result["entry_price"] = entry_price
                result["stop_price"] = float(stop_price)
                result["stop_distance_pct"] = (entry_price - stop_price) / entry_price * 100

                # Track R targets
                r_result = _track_r_targets(
                    bars, entry_price, float(stop_price),
                    pd.Timestamp(bars.iloc[i]["bar_time"]),
                )
                result.update(r_result)
                break

    return result


# ---------------------------------------------------------------------------
# Strategy 2: First Pullback After Breakout
# ---------------------------------------------------------------------------

def _simulate_fpb(
    bars: pd.DataFrame,
    feat: Dict[str, Any],
) -> Dict[str, Any]:
    """First Pullback after OR Breakout.

    Filter: conviction >= 75, accum phase, volume_ratio >= 1.5
    Setup: OR breakout (price > or_high after 9:45)
    Entry: After breakout, first pullback within 0.2% of or_high,
           then next bar closing above or_high
    Stop: or_low
    Time filter: Pullback must occur within 60 min of breakout
    """
    result: Dict[str, Any] = {
        "setup_detected": False, "setup_time": None,
        "entry_triggered": False, "entry_time": None,
        "entry_price": None, "stop_price": None, "stop_distance_pct": None,
    }

    # Filter
    phase = feat.get("accum_phase", "")
    conviction = feat.get("conviction_score", 0) or 0
    vol_ratio = feat.get("volume_ratio", 0) or 0
    if phase not in ("ACTIVE_ACCUM", "LATE_ACCUM", "EARLY_ACCUM"):
        return result
    if conviction < 75:
        return result
    if vol_ratio < 1.5:
        return result

    or_high = feat.get("or_high")
    or_low = feat.get("or_low")
    if not or_high or not or_low or or_high <= or_low:
        return result

    if bars.empty or len(bars) < 30:
        return result

    # RTH filter
    bar_times = pd.to_datetime(bars["bar_time"])
    rth_mask = (
        ((bar_times.dt.hour == 9) & (bar_times.dt.minute >= 30)) |
        ((bar_times.dt.hour >= 10) & (bar_times.dt.hour < 16))
    )
    bars = bars[rth_mask.values].reset_index(drop=True)
    if len(bars) < 30:
        return result

    bar_times = pd.to_datetime(bars["bar_time"])
    hours = bar_times.dt.hour.values
    minutes = bar_times.dt.minute.values
    highs = bars["high"].values.astype(np.float64)
    lows = bars["low"].values.astype(np.float64)
    closes = bars["close"].values.astype(np.float64)

    # Phase 1: Detect breakout (after 9:45)
    breakout_time = None
    breakout_idx = None

    for i in range(len(bars)):
        h, m = int(hours[i]), int(minutes[i])
        if h == 9 and m < 45:
            continue
        if highs[i] > or_high:
            breakout_time = pd.Timestamp(bars.iloc[i]["bar_time"])
            breakout_idx = i
            result["setup_detected"] = True
            result["setup_time"] = bars.iloc[i]["bar_time"]
            break

    if breakout_time is None:
        return result

    # Phase 2: Look for pullback within 60 min of breakout
    max_pullback_time = breakout_time + pd.Timedelta(minutes=60)
    pullback_detected = False
    threshold = or_high * 1.002  # within 0.2% of or_high

    for i in range(breakout_idx + 1, len(bars)):
        bar_ts = pd.Timestamp(bars.iloc[i]["bar_time"])
        if bar_ts > max_pullback_time:
            break

        # Pullback: low comes within 0.2% of OR high
        if not pullback_detected and lows[i] <= threshold:
            pullback_detected = True
            continue

        # Entry: after pullback, bar closing above or_high
        if pullback_detected and not result["entry_triggered"]:
            if closes[i] > or_high:
                entry_price = float(closes[i])
                stop_price = float(or_low)

                if entry_price <= stop_price:
                    continue

                result["entry_triggered"] = True
                result["entry_time"] = bars.iloc[i]["bar_time"]
                result["entry_price"] = entry_price
                result["stop_price"] = stop_price
                result["stop_distance_pct"] = (entry_price - stop_price) / entry_price * 100

                r_result = _track_r_targets(
                    bars, entry_price, stop_price,
                    pd.Timestamp(bars.iloc[i]["bar_time"]),
                )
                result.update(r_result)
                break

    return result


# ---------------------------------------------------------------------------
# Strategy 3: Momentum Ignition
# ---------------------------------------------------------------------------

def _simulate_momentum_ign(
    bars: pd.DataFrame,
    feat: Dict[str, Any],
) -> Dict[str, Any]:
    """Momentum Ignition: tight consolidation → volume breakout.

    Filter: squeeze_score > 0 OR conviction >= 65
    Setup: After 10:00, find 15 consecutive bars with range < 0.3% of price
    Entry: Break above consolidation high + volume > 2x avg bar vol
    Stop: Bottom of consolidation range
    """
    result: Dict[str, Any] = {
        "setup_detected": False, "setup_time": None,
        "entry_triggered": False, "entry_time": None,
        "entry_price": None, "stop_price": None, "stop_distance_pct": None,
    }

    # Filter
    squeeze = feat.get("squeeze_score", 0) or 0
    conviction = feat.get("conviction_score", 0) or 0
    if squeeze <= 0 and conviction < 65:
        return result

    if bars.empty or len(bars) < 40:
        return result

    # RTH filter
    bar_times = pd.to_datetime(bars["bar_time"])
    rth_mask = (
        ((bar_times.dt.hour == 9) & (bar_times.dt.minute >= 30)) |
        ((bar_times.dt.hour >= 10) & (bar_times.dt.hour < 16))
    )
    bars = bars[rth_mask.values].reset_index(drop=True)
    if len(bars) < 40:
        return result

    bar_times = pd.to_datetime(bars["bar_time"])
    hours = bar_times.dt.hour.values
    minutes = bar_times.dt.minute.values
    highs = bars["high"].values.astype(np.float64)
    lows = bars["low"].values.astype(np.float64)
    closes = bars["close"].values.astype(np.float64)
    volumes = bars["volume"].values.astype(np.float64)

    avg_bar_vol = float(volumes.mean()) if len(volumes) > 0 else 1.0
    consol_len = 15
    range_threshold = 0.003  # 0.3% of price

    # Scan for consolidation starting at 10:00+
    for start_i in range(len(bars) - consol_len):
        h = int(hours[start_i])
        if h < 10:
            continue
        if h >= 15:
            break

        # Check if next 15 bars form tight consolidation
        window_highs = highs[start_i:start_i + consol_len]
        window_lows = lows[start_i:start_i + consol_len]
        consol_high = float(window_highs.max())
        consol_low = float(window_lows.min())
        consol_range = consol_high - consol_low
        mid_price = (consol_high + consol_low) / 2

        if mid_price <= 0:
            continue

        if consol_range / mid_price > range_threshold:
            continue

        # Consolidation found!
        result["setup_detected"] = True
        result["setup_time"] = bars.iloc[start_i]["bar_time"]

        # Look for breakout in subsequent bars
        for j in range(start_i + consol_len, min(len(bars), start_i + consol_len + 30)):
            if highs[j] > consol_high and volumes[j] > 2.0 * avg_bar_vol:
                entry_price = float(closes[j])
                stop_price = float(consol_low)

                if entry_price <= stop_price:
                    continue

                result["entry_triggered"] = True
                result["entry_time"] = bars.iloc[j]["bar_time"]
                result["entry_price"] = entry_price
                result["stop_price"] = stop_price
                result["stop_distance_pct"] = (entry_price - stop_price) / entry_price * 100

                r_result = _track_r_targets(
                    bars, entry_price, stop_price,
                    pd.Timestamp(bars.iloc[j]["bar_time"]),
                )
                result.update(r_result)
                return result

        # Setup found but no entry — keep looking for another consolidation
        # (reset setup to allow finding a later one)
        if not result["entry_triggered"]:
            continue

    return result


# ---------------------------------------------------------------------------
# Strategy 4: ORB V2 — Structural Confirmation Opening Range Breakout
# ---------------------------------------------------------------------------

def _simulate_orb_v2(
    bars: pd.DataFrame,
    feat: Dict[str, Any],
) -> Dict[str, Any]:
    """ORB V2: Structural confirmation breakout (Peachy/modern ORB model).

    Key differences from naive ORB:
    1. CLOSE above OR high (not just wick touch) — filters liquidity grabs
    2. Displacement: breakout candle body > 50% of candle range (full-bodied)
    3. Fakeout filter: top wick < 30% of candle range
    4. Volume confirmation: breakout bar volume > 1.5x avg OR bar volume
    5. Daily bias: price > VWAP at breakout (multi-timeframe confirmation)
    6. Tighter stop: OR midpoint instead of OR low

    Filter: conviction >= 55, accum phase, OR volume ratio >= 1.2
    Entry window: 9:45-10:30
    Stop: OR midpoint (half the risk of naive ORB)
    """
    result: Dict[str, Any] = {
        "setup_detected": False, "setup_time": None,
        "entry_triggered": False, "entry_time": None,
        "entry_price": None, "stop_price": None, "stop_distance_pct": None,
    }

    # --- Intelligence filter ---
    phase = feat.get("accum_phase", "")
    conviction = feat.get("conviction_score", 0) or 0
    if phase not in ("ACTIVE_ACCUM", "LATE_ACCUM", "EARLY_ACCUM"):
        return result
    if conviction < 55:
        return result

    # --- OR data from pre-computed features ---
    or_high = feat.get("or_high")
    or_low = feat.get("or_low")
    or_range = feat.get("or_range")
    or_volume = feat.get("or_volume", 0) or 0
    avg_or_vol = feat.get("avg_or_volume_20d", 0) or 0
    gap_pct = feat.get("gap_pct", 0) or 0
    prev_close = feat.get("prev_close")

    if not or_high or not or_low or not or_range or or_range <= 0:
        return result

    # Gap filter: skip extreme gaps
    if abs(gap_pct) > 5.0:
        return result

    # OR volume filter (relaxed to 1.2x since we add breakout bar volume check)
    vol_ratio = or_volume / avg_or_vol if avg_or_vol > 0 else 1.0
    if vol_ratio < 1.2:
        return result

    # Range too tight — noise
    if prev_close and prev_close > 0 and (or_range / prev_close) < 0.001:
        return result

    or_mid = (or_high + or_low) / 2.0  # Tighter stop: midpoint of range

    if bars.empty or len(bars) < 30:
        return result

    # --- RTH filter ---
    bar_times = pd.to_datetime(bars["bar_time"])
    rth_mask = (
        ((bar_times.dt.hour == 9) & (bar_times.dt.minute >= 30)) |
        ((bar_times.dt.hour >= 10) & (bar_times.dt.hour < 16))
    )
    bars = bars[rth_mask.values].reset_index(drop=True)
    if len(bars) < 30:
        return result

    bar_times = pd.to_datetime(bars["bar_time"])
    hours = bar_times.dt.hour.values
    minutes = bar_times.dt.minute.values
    opens = bars["open"].values.astype(np.float64)
    highs = bars["high"].values.astype(np.float64)
    lows = bars["low"].values.astype(np.float64)
    closes = bars["close"].values.astype(np.float64)
    volumes = bars["volume"].values.astype(np.float64)

    # Running VWAP for daily bias check
    running_vwap = _compute_running_vwap(highs, lows, closes, volumes)

    # Average bar volume in OR (for breakout bar volume confirmation)
    or_mask = (hours == 9) & (minutes >= 30) & (minutes < 45)
    or_bar_vols = volumes[or_mask]
    avg_or_bar_vol = float(or_bar_vols.mean()) if len(or_bar_vols) > 0 else 1.0

    # Previous day high for daily bias (injected by orchestrator or from features)
    prev_day_high = feat.get("_prev_day_high")

    # Setup: OR is defined
    result["setup_detected"] = True
    result["setup_time"] = bars.iloc[0]["bar_time"]

    # --- Breakout scan: 9:45-10:30 ---
    for i in range(len(bars)):
        h, m = int(hours[i]), int(minutes[i])

        # Only scan 9:45-10:30
        if h == 9 and m < 45:
            continue
        if h > 10 or (h == 10 and m > 30):
            break

        # RULE 1: Bar must CLOSE above OR high (not just wick touch)
        if closes[i] <= or_high:
            continue

        # Breakout candle metrics
        candle_range = highs[i] - lows[i]
        if candle_range <= 0:
            continue

        body = abs(closes[i] - opens[i])
        upper_wick = highs[i] - max(opens[i], closes[i])

        body_ratio = body / candle_range
        wick_ratio = upper_wick / candle_range

        # RULE 2: Displacement — full-bodied candle (body > 50% of range)
        if body_ratio < 0.50:
            continue

        # RULE 3: Fakeout filter — top wick < 30% of range
        if wick_ratio > 0.30:
            continue

        # RULE 4: Volume confirmation — breakout bar vol > 1.5x avg OR bar vol
        if avg_or_bar_vol > 0 and volumes[i] < 1.5 * avg_or_bar_vol:
            continue

        # RULE 5: Daily bias — price must be above VWAP
        vwap_here = running_vwap[i] if i < len(running_vwap) else 0
        if vwap_here > 0 and closes[i] < vwap_here:
            continue

        # All structural confirmations passed — ENTER
        entry_price = float(closes[i])
        stop_price = float(or_mid)  # RULE 6: Tighter stop at OR midpoint

        if entry_price <= stop_price:
            continue

        result["entry_triggered"] = True
        result["entry_time"] = bars.iloc[i]["bar_time"]
        result["entry_price"] = entry_price
        result["stop_price"] = stop_price
        result["stop_distance_pct"] = (entry_price - stop_price) / entry_price * 100

        # Track R targets
        r_result = _track_r_targets(
            bars, entry_price, stop_price,
            pd.Timestamp(bars.iloc[i]["bar_time"]),
        )
        result.update(r_result)

        # Store quality metrics in result for analysis
        result["_body_ratio"] = round(body_ratio, 3)
        result["_wick_ratio"] = round(wick_ratio, 3)
        result["_bo_vol_ratio"] = round(volumes[i] / avg_or_bar_vol, 2) if avg_or_bar_vol > 0 else 0
        result["_above_vwap"] = bool(vwap_here > 0 and closes[i] > vwap_here)
        result["_above_prev_day_high"] = bool(prev_day_high and closes[i] > prev_day_high)

        # Quality score (0-4, from the video)
        q_score = 0
        if body_ratio >= 0.5:
            q_score += 1  # Displacement confirmed
        if volumes[i] > 2.0 * avg_or_bar_vol:
            q_score += 1  # Strong volume (2x, not just 1.5x)
        if vwap_here > 0 and closes[i] > vwap_here:
            q_score += 1  # Daily bias via VWAP
        if prev_day_high and closes[i] > prev_day_high:
            q_score += 1  # Above prev day high
        result["_quality_score"] = q_score

        break

    return result


def _load_prev_day_highs(
    conn: duckdb.DuckDBPyConnection,
    tickers: List[str],
    from_date: date,
    to_date: date,
) -> Dict[Tuple[str, date], float]:
    """Load previous trading day's high for each ticker x date."""
    if not tickers:
        return {}
    placeholders = ",".join(["?" for _ in tickers])
    df = conn.execute(f"""
        SELECT ticker, trade_date, high,
               LAG(high) OVER (PARTITION BY ticker ORDER BY trade_date) AS prev_high
        FROM fact_daily_prices
        WHERE ticker IN ({placeholders})
          AND trade_date >= CAST(? AS DATE) - INTERVAL '30 DAY'
          AND trade_date <= CAST(? AS DATE)
        ORDER BY ticker, trade_date
    """, [*tickers, from_date.isoformat(), to_date.isoformat()]).fetchdf()

    result = {}
    for _, row in df.iterrows():
        if row["prev_high"] is not None and pd.notna(row["prev_high"]):
            td = pd.Timestamp(row["trade_date"]).date()
            result[(str(row["ticker"]), td)] = float(row["prev_high"])
    return result


# ---------------------------------------------------------------------------
# Strategy dispatcher
# ---------------------------------------------------------------------------

STRATEGY_MAP = {
    "VWAP_MR": _simulate_vwap_mr,
    "FPB": _simulate_fpb,
    "MOMENTUM_IGN": _simulate_momentum_ign,
    "ORB_V2": _simulate_orb_v2,
}


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def run_strategy_backtest(
    conn: duckdb.DuckDBPyConnection,
    strategies: List[str],
    quarters: List[str],
) -> int:
    """Run strategy backtests using pre-computed features + raw bars.

    Returns total result rows created.
    """
    _ensure_tables(conn)
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    total_rows = 0

    for quarter in quarters:
        logger.info("=== Strategy backtest: quarter={} ===", quarter)

        # 1. Load features for this quarter
        feat_df = conn.execute("""
            SELECT * FROM fact_intraday_features
            WHERE report_quarter = ?
        """, [quarter]).fetchdf()

        if feat_df.empty:
            logger.warning("No features for {} — run feature engine first", quarter)
            continue

        logger.info("{} feature rows for {}", len(feat_df), quarter)

        # 2. Trading window
        start_date = _filing_date(quarter)
        end_date = _filing_date(_next_quarter(quarter))

        # 3. Group features by ticker
        feat_by_ticker: Dict[str, List[Dict]] = defaultdict(list)
        for _, row in feat_df.iterrows():
            td = pd.Timestamp(row["trade_date"]).date()
            d = row.to_dict()
            d["_td"] = td
            feat_by_ticker[str(row["ticker"])].append(d)

        tickers = list(feat_by_ticker.keys())
        n_tickers = len(tickers)
        logger.info("{} tickers with features", n_tickers)

        # 3b. Load prev_day_highs for ORB_V2 daily bias check
        prev_day_high_map: Dict[Tuple[str, date], float] = {}
        if "ORB_V2" in strategies:
            logger.info("Loading prev_day_highs for ORB_V2 daily bias...")
            prev_day_high_map = _load_prev_day_highs(conn, tickers, start_date, end_date)
            # Inject into feature dicts
            for ticker, feat_list in feat_by_ticker.items():
                for f in feat_list:
                    td = f["_td"]
                    f["_prev_day_high"] = prev_day_high_map.get((ticker, td))

        # 4. Simulate per strategy
        for strategy_name in strategies:
            sim_fn = STRATEGY_MAP.get(strategy_name)
            if sim_fn is None:
                logger.error("Unknown strategy: {}", strategy_name)
                continue

            logger.info("--- Running {} ---", strategy_name)
            results_batch = []

            for t_idx, ticker in enumerate(tickers):
                feat_list = feat_by_ticker[ticker]

                if (t_idx + 1) % 100 == 0 or t_idx == 0:
                    logger.info(
                        "  {} [{}/{}] {} ({} days)...",
                        strategy_name, t_idx + 1, n_tickers, ticker, len(feat_list),
                    )

                # Batch-load all bars for this ticker
                all_bars = conn.execute("""
                    SELECT bar_time, open, high, low, close, volume
                    FROM fact_intraday_bars
                    WHERE ticker = ?
                      AND CAST(bar_time AS DATE) >= ?
                      AND CAST(bar_time AS DATE) <= ?
                    ORDER BY bar_time
                """, [ticker, start_date, end_date]).fetchdf()

                if all_bars.empty:
                    continue

                all_bars["_date"] = pd.to_datetime(all_bars["bar_time"]).dt.date

                for feat in feat_list:
                    td = feat["_td"]
                    bars_df = all_bars[all_bars["_date"] == td].drop(columns=["_date"])

                    if bars_df.empty or len(bars_df) < 20:
                        continue

                    sim = sim_fn(bars_df, feat)

                    results_batch.append({
                        "ticker": ticker,
                        "trade_date": td,
                        "strategy": strategy_name,
                        "report_quarter": quarter,
                        "conviction_score": feat.get("conviction_score"),
                        "accum_phase": feat.get("accum_phase"),
                        "squeeze_score": feat.get("squeeze_score"),
                        "sector": feat.get("sector"),
                        "setup_detected": sim.get("setup_detected", False),
                        "setup_time": sim.get("setup_time"),
                        "entry_triggered": sim.get("entry_triggered", False),
                        "entry_time": sim.get("entry_time"),
                        "entry_price": sim.get("entry_price"),
                        "stop_price": sim.get("stop_price"),
                        "stop_distance_pct": sim.get("stop_distance_pct"),
                        "hit_1r": sim.get("hit_1r", False),
                        "hit_2r": sim.get("hit_2r", False),
                        "hit_3r": sim.get("hit_3r", False),
                        "hit_4r": sim.get("hit_4r", False),
                        "hit_stop": sim.get("hit_stop", False),
                        "time_to_1r_min": sim.get("time_to_1r_min"),
                        "time_to_2r_min": sim.get("time_to_2r_min"),
                        "time_to_3r_min": sim.get("time_to_3r_min"),
                        "time_to_4r_min": sim.get("time_to_4r_min"),
                        "time_to_stop_min": sim.get("time_to_stop_min"),
                        "max_favorable_r": sim.get("max_favorable_r", 0.0),
                        "max_adverse_r": sim.get("max_adverse_r", 0.0),
                        "trail_exit_price": sim.get("trail_exit_price"),
                        "trail_exit_r": sim.get("trail_exit_r"),
                        "eod_price": sim.get("eod_price"),
                        "eod_r": sim.get("eod_r"),
                        "computed_at": now_iso,
                    })

            if not results_batch:
                logger.info("No results for {} in {}", strategy_name, quarter)
                continue

            # Bulk insert
            df_res = pd.DataFrame(results_batch)
            conn.execute(
                "DELETE FROM strategy_backtest_results WHERE strategy = ? AND report_quarter = ?",
                [strategy_name, quarter],
            )
            conn.register("_strat_temp", df_res)
            conn.execute("INSERT OR REPLACE INTO strategy_backtest_results SELECT * FROM _strat_temp")
            conn.unregister("_strat_temp")

            setups = df_res["setup_detected"].sum()
            entries = df_res["entry_triggered"].sum()
            logger.info(
                "{} {}: {} ticker-days, {} setups ({:.1f}%), {} entries ({:.1f}%)",
                quarter, strategy_name, len(df_res), setups,
                100 * setups / max(1, len(df_res)),
                entries, 100 * entries / max(1, len(df_res)),
            )
            total_rows += len(df_res)

    logger.info("Strategy backtest complete: {} total rows", total_rows)
    return total_rows


# ---------------------------------------------------------------------------
# Comparative analysis
# ---------------------------------------------------------------------------

def print_comparative_summary(
    conn: duckdb.DuckDBPyConnection,
    quarters: Optional[List[str]] = None,
) -> None:
    """Print side-by-side strategy comparison."""
    _ensure_tables(conn)

    where = ""
    params: list = []
    if quarters:
        placeholders = ",".join(["?" for _ in quarters])
        where = f"WHERE report_quarter IN ({placeholders})"
        params = list(quarters)

    # 1. Strategy overview
    overview = conn.execute(f"""
        SELECT
            strategy,
            COUNT(*) AS total,
            SUM(CASE WHEN setup_detected THEN 1 ELSE 0 END) AS setups,
            SUM(CASE WHEN entry_triggered THEN 1 ELSE 0 END) AS entries,
            SUM(CASE WHEN entry_triggered AND hit_1r THEN 1 ELSE 0 END) AS h1,
            SUM(CASE WHEN entry_triggered AND hit_2r THEN 1 ELSE 0 END) AS h2,
            SUM(CASE WHEN entry_triggered AND hit_3r THEN 1 ELSE 0 END) AS h3,
            SUM(CASE WHEN entry_triggered AND hit_4r THEN 1 ELSE 0 END) AS h4,
            SUM(CASE WHEN entry_triggered AND hit_stop THEN 1 ELSE 0 END) AS stopped,
            AVG(CASE WHEN entry_triggered THEN max_favorable_r END) AS avg_mfe,
            AVG(CASE WHEN entry_triggered THEN trail_exit_r END) AS avg_trail,
            AVG(CASE WHEN entry_triggered THEN eod_r END) AS avg_eod_r
        FROM strategy_backtest_results
        {where}
        GROUP BY strategy
        ORDER BY strategy
    """, params).fetchall()

    if not overview:
        print("No strategy results found.")
        return

    print("\n" + "=" * 100)
    print("STRATEGY COMPARISON")
    print("=" * 100)

    header = (f"  {'Strategy':<15} {'Total':>7} {'Setups':>7} {'Entries':>7} "
              f"{'1R WR':>7} {'2R WR':>7} {'3R WR':>7} {'StopR':>7} "
              f"{'AvgMFE':>7} {'Trail':>7} {'EOD R':>7}")
    print(header)
    print("  " + "-" * 96)

    for row in overview:
        strat, total, setups, entries, h1, h2, h3, h4, stopped, mfe, trail, eod_r = row

        # Win rates based on resolved trades (hit target OR stop)
        def _wr(hits, stops):
            resolved = hits + stops
            return 100 * hits / resolved if resolved > 0 else 0.0

        wr1 = _wr(h1, stopped)
        wr2 = _wr(h2, stopped)
        wr3 = _wr(h3, stopped)
        stop_rate = 100 * stopped / max(1, entries)

        print(f"  {strat:<15} {total:>7} {setups:>7} {entries:>7} "
              f"{wr1:>6.1f}% {wr2:>6.1f}% {wr3:>6.1f}% {stop_rate:>6.1f}% "
              f"{(mfe or 0):>+6.1f}R {(trail or 0):>+6.1f}R {(eod_r or 0):>+6.1f}R")

    # 2. Expected value comparison
    print(f"\n  EXPECTED VALUE (resolved trades only)")
    print(f"  {'Strategy':<15} {'1:1 EV':>8} {'1:2 EV':>8} {'1:3 EV':>8} {'1:4 EV':>8}")
    print("  " + "-" * 50)

    for row in overview:
        strat = row[0]
        entries = row[3]
        h1, h2, h3, h4, stopped = row[4], row[5], row[6], row[7], row[8]

        evs = []
        for n, hits in [(1, h1), (2, h2), (3, h3), (4, h4)]:
            resolved = hits + stopped
            if resolved > 0:
                ev = (hits / resolved) * n - (stopped / resolved) * 1.0
                evs.append(f"{ev:>+7.2f}R")
            else:
                evs.append(f"{'N/A':>8}")

        print(f"  {strat:<15} {evs[0]} {evs[1]} {evs[2]} {evs[3]}")

    # 3. Per-strategy detail sections
    for row in overview:
        strat = row[0]
        entries = row[3]
        if entries == 0:
            continue

        strat_where = f"strategy = ? AND entry_triggered = TRUE"
        strat_params = [strat]
        if quarters:
            placeholders = ",".join(["?" for _ in quarters])
            strat_where += f" AND report_quarter IN ({placeholders})"
            strat_params.extend(quarters)

        print(f"\n{'='*70}")
        print(f"  {strat} — DETAILED ANALYSIS (n={entries} entries)")
        print(f"{'='*70}")

        # By conviction
        conv_data = conn.execute(f"""
            SELECT
                CASE
                    WHEN conviction_score >= 85 THEN '85-100'
                    WHEN conviction_score >= 75 THEN '75-85'
                    WHEN conviction_score >= 65 THEN '65-75'
                    ELSE '55-65'
                END AS bucket,
                COUNT(*) AS n,
                SUM(CASE WHEN hit_1r THEN 1 ELSE 0 END) AS h1,
                SUM(CASE WHEN hit_3r THEN 1 ELSE 0 END) AS h3,
                SUM(CASE WHEN hit_stop THEN 1 ELSE 0 END) AS stopped,
                AVG(max_favorable_r) AS avg_mfe
            FROM strategy_backtest_results
            WHERE {strat_where}
            GROUP BY bucket
            ORDER BY bucket
        """, strat_params).fetchall()

        if conv_data:
            print(f"\n  By Conviction:")
            print(f"  {'Bucket':<12} {'N':>5} {'1R WR':>8} {'3R WR':>8} {'StopR':>8} {'MFE':>7}")
            print(f"  {'-'*52}")
            for cr in conv_data:
                n = cr[1]
                resolved1 = cr[2] + cr[4]
                resolved3 = cr[3] + cr[4]
                wr1 = 100 * cr[2] / resolved1 if resolved1 > 0 else 0
                wr3 = 100 * cr[3] / resolved3 if resolved3 > 0 else 0
                sr = 100 * cr[4] / max(1, n)
                print(f"  {cr[0]:<12} {n:>5} {wr1:>7.1f}% {wr3:>7.1f}% {sr:>7.1f}% {cr[5]:>+6.1f}R")

        # By sector
        sector_data = conn.execute(f"""
            SELECT
                sector,
                COUNT(*) AS n,
                SUM(CASE WHEN hit_1r THEN 1 ELSE 0 END) AS h1,
                SUM(CASE WHEN hit_3r THEN 1 ELSE 0 END) AS h3,
                SUM(CASE WHEN hit_stop THEN 1 ELSE 0 END) AS stopped,
                AVG(max_favorable_r) AS avg_mfe
            FROM strategy_backtest_results
            WHERE {strat_where}
            GROUP BY sector
            HAVING COUNT(*) >= 5
            ORDER BY SUM(CASE WHEN hit_1r THEN 1 ELSE 0 END) * 1.0 / NULLIF(COUNT(*), 0) DESC
        """, strat_params).fetchall()

        if sector_data:
            print(f"\n  By Sector (min 5 trades):")
            print(f"  {'Sector':<25} {'N':>5} {'1R WR':>8} {'3R WR':>8} {'MFE':>7}")
            print(f"  {'-'*57}")
            for sr in sector_data:
                n = sr[1]
                r1 = sr[2] + sr[4]
                r3 = sr[3] + sr[4]
                wr1 = 100 * sr[2] / r1 if r1 > 0 else 0
                wr3 = 100 * sr[3] / r3 if r3 > 0 else 0
                print(f"  {(sr[0] or '?'):<25} {n:>5} {wr1:>7.1f}% {wr3:>7.1f}% {sr[5]:>+6.1f}R")

        # By phase
        phase_data = conn.execute(f"""
            SELECT
                accum_phase,
                COUNT(*) AS n,
                SUM(CASE WHEN hit_1r THEN 1 ELSE 0 END) AS h1,
                SUM(CASE WHEN hit_3r THEN 1 ELSE 0 END) AS h3,
                SUM(CASE WHEN hit_stop THEN 1 ELSE 0 END) AS stopped,
                AVG(max_favorable_r) AS avg_mfe
            FROM strategy_backtest_results
            WHERE {strat_where}
            GROUP BY accum_phase
            HAVING COUNT(*) >= 3
            ORDER BY SUM(CASE WHEN hit_1r THEN 1 ELSE 0 END) * 1.0 / NULLIF(COUNT(*), 0) DESC
        """, strat_params).fetchall()

        if phase_data:
            print(f"\n  By Phase:")
            print(f"  {'Phase':<16} {'N':>5} {'1R WR':>8} {'3R WR':>8} {'MFE':>7}")
            print(f"  {'-'*48}")
            for pr in phase_data:
                n = pr[1]
                r1 = pr[2] + pr[4]
                r3 = pr[3] + pr[4]
                wr1 = 100 * pr[2] / r1 if r1 > 0 else 0
                wr3 = 100 * pr[3] / r3 if r3 > 0 else 0
                print(f"  {pr[0]:<16} {n:>5} {wr1:>7.1f}% {wr3:>7.1f}% {pr[5]:>+6.1f}R")

        # MFE distribution
        mfe_data = conn.execute(f"""
            SELECT
                PERCENTILE_CONT(0.25) WITHIN GROUP (ORDER BY max_favorable_r) AS p25,
                PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY max_favorable_r) AS p50,
                PERCENTILE_CONT(0.75) WITHIN GROUP (ORDER BY max_favorable_r) AS p75,
                PERCENTILE_CONT(0.90) WITHIN GROUP (ORDER BY max_favorable_r) AS p90
            FROM strategy_backtest_results
            WHERE {strat_where}
        """, strat_params).fetchone()

        if mfe_data:
            print(f"\n  MFE Distribution:  P25={mfe_data[0]:+.1f}R  P50={mfe_data[1]:+.1f}R  "
                  f"P75={mfe_data[2]:+.1f}R  P90={mfe_data[3]:+.1f}R")

        # Timing
        timing = conn.execute(f"""
            SELECT
                AVG(time_to_1r_min) AS avg_1r,
                AVG(time_to_stop_min) AS avg_stop,
                AVG(stop_distance_pct) AS avg_stop_dist
            FROM strategy_backtest_results
            WHERE {strat_where}
        """, strat_params).fetchone()

        if timing:
            print(f"  Avg time to 1R: {timing[0]:.0f} min" if timing[0] else "  Avg time to 1R: N/A")
            print(f"  Avg time to stop: {timing[1]:.0f} min" if timing[1] else "  Avg time to stop: N/A")
            print(f"  Avg stop distance: {timing[2]:.2f}%" if timing[2] else "  Avg stop distance: N/A")

    print("\n" + "=" * 100)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Multi-Strategy Backtester — VWAP_MR, FPB, MOMENTUM_IGN"
    )
    parser.add_argument("--run", action="store_true", help="Run strategy backtest(s)")
    parser.add_argument(
        "--strategy", type=str, default="ALL",
        help="Strategy name or ALL (default: ALL)",
    )
    parser.add_argument(
        "--quarters", type=str, default=None,
        help="Comma-separated quarters (e.g. 2024-Q1,2024-Q2,2024-Q3)",
    )
    parser.add_argument("--compare", action="store_true", help="Print comparative summary")
    args = parser.parse_args()

    conn = duckdb.connect(str(WAREHOUSE_PATH))

    try:
        quarters = None
        if args.quarters:
            quarters = [q.strip() for q in args.quarters.split(",")]

        if args.run:
            if not quarters:
                parser.error("--run requires --quarters")
            if args.strategy.upper() == "ALL":
                strats = ALL_STRATEGIES
            else:
                strats = [s.strip().upper() for s in args.strategy.split(",")]
            run_strategy_backtest(conn, strats, quarters)

        if args.compare:
            print_comparative_summary(conn, quarters)

        if not args.run and not args.compare:
            parser.print_help()
    finally:
        conn.close()


if __name__ == "__main__":
    from signal_scanner.utils.logger import setup_logger
    setup_logger()
    main()
