"""Swing Backtester — simulate 7 swing strategies and track R-target outcomes.

Reads from ``fact_swing_features`` (computed by swing_feature_engine.py) and
forward-looking daily bars to simulate entries/exits. Results stored in
``swing_backtest_results`` for ML training.

Strategies:
    SQUEEZE            — TTM volatility squeeze fire (5-15 day hold)
    MEAN_REV           — Connors RSI(2) < 10 mean reversion (2-5 day hold)
    GAP_DRIFT          — Post-earnings gap drift proxy (10-30 day hold)
    INSIDER_BREAKOUT   — Insider cluster + 20d high breakout (15-30 day hold)
    GAP_DRIFT_FILTERED — GAP_DRIFT + intelligence gates (conv>=60, ADX>=25, phase)
    MEAN_REV_FILTERED  — MEAN_REV + intelligence gates (conv>=80, ADX>=25, vol>=1.5)
    CANDLE_REVERSAL    — Candlestick reversal + RSI/SMA/volume context (10 day hold)

Usage:
    python -m signal_scanner.institutional_intel.intelligence.swing_backtester \\
        --run --strategy ALL --quarters 2023-Q4,2024-Q1,2024-Q2,2024-Q3,2024-Q4

    python -m signal_scanner.institutional_intel.intelligence.swing_backtester --compare
"""

from __future__ import annotations

import argparse
import calendar
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import duckdb
import numpy as np
import pandas as pd
from loguru import logger

from signal_scanner.institutional_intel.config import WAREHOUSE_PATH

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _quarter_end_date(quarter: str) -> date:
    year = int(quarter.split("-Q")[0])
    qnum = int(quarter.split("-Q")[1])
    end_month = {1: 3, 2: 6, 3: 9, 4: 12}[qnum]
    last_day = calendar.monthrange(year, end_month)[1]
    return date(year, end_month, last_day)


def _quarter_start_date(quarter: str) -> date:
    year = int(quarter.split("-Q")[0])
    qnum = int(quarter.split("-Q")[1])
    start_month = {1: 1, 2: 4, 3: 7, 4: 10}[qnum]
    return date(year, start_month, 1)


# ---------------------------------------------------------------------------
# DuckDB table
# ---------------------------------------------------------------------------

def _ensure_tables(conn: duckdb.DuckDBPyConnection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS swing_backtest_results (
            ticker              TEXT NOT NULL,
            trade_date          DATE NOT NULL,
            strategy            TEXT NOT NULL,

            -- Context
            report_quarter      TEXT,
            conviction_score    DOUBLE,
            accum_phase         TEXT,
            squeeze_score       DOUBLE,
            sector              TEXT,

            -- Setup & Entry
            setup_detected      BOOLEAN DEFAULT FALSE,
            setup_date          DATE,
            entry_triggered     BOOLEAN DEFAULT FALSE,
            entry_date          DATE,
            entry_price         DOUBLE,
            stop_price          DOUBLE,
            stop_distance_pct   DOUBLE,
            r_unit              DOUBLE,

            -- R-target outcomes
            hit_1r              BOOLEAN DEFAULT FALSE,
            hit_2r              BOOLEAN DEFAULT FALSE,
            hit_stop            BOOLEAN DEFAULT FALSE,
            days_to_1r          INTEGER,
            days_to_2r          INTEGER,
            days_to_stop        INTEGER,
            max_favorable_r     DOUBLE,
            max_adverse_r       DOUBLE,

            -- Exit
            hold_days           INTEGER,
            exit_type           TEXT,
            exit_date           DATE,
            exit_price          DOUBLE,
            exit_r              DOUBLE,

            -- Strategy-specific
            hit_ema10           BOOLEAN DEFAULT FALSE,
            days_to_ema10       INTEGER,

            -- Feature snapshot
            rsi_14_at_setup     DOUBLE,
            rsi_2_at_setup      DOUBLE,
            squeeze_on_at_setup BOOLEAN,
            bb_width_at_setup   DOUBLE,
            volume_ratio_at_setup DOUBLE,
            atr_at_setup        DOUBLE,
            linreg_slope_at_setup DOUBLE,

            computed_at         TIMESTAMP,
            PRIMARY KEY (ticker, trade_date, strategy)
        )
    """)


# ---------------------------------------------------------------------------
# R-target tracking on daily bars
# ---------------------------------------------------------------------------

def _track_r_targets(
    forward_bars: pd.DataFrame,
    entry_price: float,
    stop_price: float,
    r_unit: float,
    max_hold: int,
    sma_10_series: Optional[pd.Series] = None,
    exit_signal_series: Optional[pd.Series] = None,
) -> Dict[str, Any]:
    """Track R-target outcomes on daily OHLCV bars after entry.

    First-one-wins: check stop before targets each day.

    Args:
        forward_bars: DataFrame with open/high/low/close starting from entry day+1
        entry_price: entry price
        stop_price: stop loss price
        r_unit: |entry - stop| (always positive)
        max_hold: maximum holding days
        sma_10_series: optional SMA(10) values for MEAN_REV exit signal
        exit_signal_series: optional boolean Series for pattern-based exit signals

    Returns dict with hit_1r, hit_2r, hit_stop, days_to_*, exit info.
    """
    result = {
        "hit_1r": False, "hit_2r": False, "hit_stop": False,
        "days_to_1r": None, "days_to_2r": None, "days_to_stop": None,
        "max_favorable_r": 0.0, "max_adverse_r": 0.0,
        "hold_days": 0, "exit_type": "TIME_STOP",
        "exit_date": None, "exit_price": None, "exit_r": 0.0,
        "hit_ema10": False, "days_to_ema10": None,
    }

    if forward_bars.empty or r_unit <= 0:
        return result

    is_long = entry_price > stop_price
    target_1r = entry_price + r_unit if is_long else entry_price - r_unit
    target_2r = entry_price + 2 * r_unit if is_long else entry_price - 2 * r_unit

    for day_idx, (_, bar) in enumerate(forward_bars.iterrows()):
        if day_idx >= max_hold:
            break

        bar_high = float(bar["high"])
        bar_low = float(bar["low"])
        bar_close = float(bar["close"])
        bar_date = pd.Timestamp(bar["trade_date"]).date()
        hold = day_idx + 1

        # Compute R excursion
        if is_long:
            fav_r = (bar_high - entry_price) / r_unit
            adv_r = (bar_low - entry_price) / r_unit
        else:
            fav_r = (entry_price - bar_low) / r_unit
            adv_r = (entry_price - bar_high) / r_unit

        result["max_favorable_r"] = max(result["max_favorable_r"], fav_r)
        result["max_adverse_r"] = min(result["max_adverse_r"], adv_r)

        # FIRST-ONE-WINS: check stop
        if is_long and bar_low <= stop_price:
            result["hit_stop"] = True
            result["days_to_stop"] = hold
            result["hold_days"] = hold
            result["exit_type"] = "STOP"
            result["exit_date"] = bar_date
            result["exit_price"] = stop_price
            result["exit_r"] = -1.0
            return result
        elif not is_long and bar_high >= stop_price:
            result["hit_stop"] = True
            result["days_to_stop"] = hold
            result["hold_days"] = hold
            result["exit_type"] = "STOP"
            result["exit_date"] = bar_date
            result["exit_price"] = stop_price
            result["exit_r"] = -1.0
            return result

        # Check targets
        if is_long:
            if not result["hit_1r"] and bar_high >= target_1r:
                result["hit_1r"] = True
                result["days_to_1r"] = hold
            if not result["hit_2r"] and bar_high >= target_2r:
                result["hit_2r"] = True
                result["days_to_2r"] = hold
                result["hold_days"] = hold
                result["exit_type"] = "TARGET_2R"
                result["exit_date"] = bar_date
                result["exit_price"] = target_2r
                result["exit_r"] = 2.0
                return result
        else:
            if not result["hit_1r"] and bar_low <= target_1r:
                result["hit_1r"] = True
                result["days_to_1r"] = hold
            if not result["hit_2r"] and bar_low <= target_2r:
                result["hit_2r"] = True
                result["days_to_2r"] = hold
                result["hold_days"] = hold
                result["exit_type"] = "TARGET_2R"
                result["exit_date"] = bar_date
                result["exit_price"] = target_2r
                result["exit_r"] = 2.0
                return result

        # MEAN_REV: EMA10 exit signal
        if sma_10_series is not None and day_idx < len(sma_10_series):
            sma10_val = sma_10_series.iloc[day_idx]
            if pd.notna(sma10_val) and bar_close >= sma10_val:
                result["hit_ema10"] = True
                result["days_to_ema10"] = hold
                result["hold_days"] = hold
                result["exit_type"] = "EXIT_SIGNAL"
                result["exit_date"] = bar_date
                result["exit_price"] = bar_close
                result["exit_r"] = round((bar_close - entry_price) / r_unit, 2) if is_long else round((entry_price - bar_close) / r_unit, 2)
                return result

        # Generic exit signal (boolean series — candlestick patterns etc.)
        if exit_signal_series is not None and day_idx < len(exit_signal_series):
            if exit_signal_series.iloc[day_idx]:
                result["hold_days"] = hold
                result["exit_type"] = "EXIT_SIGNAL"
                result["exit_date"] = bar_date
                result["exit_price"] = bar_close
                result["exit_r"] = round(
                    (bar_close - entry_price) / r_unit if is_long
                    else (entry_price - bar_close) / r_unit, 2
                )
                return result

    # Time stop — exit at last bar's close
    if not forward_bars.empty:
        last_idx = min(max_hold - 1, len(forward_bars) - 1)
        last_bar = forward_bars.iloc[last_idx]
        bar_close = float(last_bar["close"])
        result["hold_days"] = last_idx + 1
        result["exit_date"] = pd.Timestamp(last_bar["trade_date"]).date()
        result["exit_price"] = bar_close
        result["exit_r"] = round(
            (bar_close - entry_price) / r_unit if is_long
            else (entry_price - bar_close) / r_unit, 2
        )

    return result


# ---------------------------------------------------------------------------
# Strategy simulations
# ---------------------------------------------------------------------------

def _simulate_squeeze(
    features_df: pd.DataFrame,
    forward_prices: Dict[str, pd.DataFrame],
    max_hold: int = 15,
) -> List[Dict[str, Any]]:
    """SQUEEZE: TTM squeeze fires (BB exits KC after being inside).

    Setup: squeeze_on=True for >=6 of last 8 days, then squeeze_on=False today.
    Filter: linreg_slope_12d > 0 and close > ema_20 (sma_20 as proxy).
    Entry: close on squeeze fire day.
    Stop: kc_lower at entry.
    """
    results = []
    if len(features_df) < 9:
        return results

    squeeze_col = features_df["squeeze_on"].values
    dates = features_df["_date"].tolist() if "_date" in features_df.columns else [pd.Timestamp(d).date() for d in features_df["trade_date"]]

    for i in range(8, len(features_df)):
        row = features_df.iloc[i]
        td = dates[i]

        # Squeeze fires: was on, now off
        if row["squeeze_on"]:
            continue

        # Check 6/8 prior days were squeeze_on
        prior_8 = squeeze_col[i - 8: i]
        if np.sum(prior_8) < 6:
            continue

        # Filters
        if pd.isna(row.get("linreg_slope_12d")) or row["linreg_slope_12d"] <= 0:
            continue
        close = row["close"]
        sma20 = row.get("sma_20")
        if pd.isna(sma20) or close <= sma20:
            continue

        kc_lower = row.get("kc_lower")
        if pd.isna(kc_lower) or kc_lower <= 0 or kc_lower >= close:
            continue

        entry_price = close
        stop_price = kc_lower
        r_unit = entry_price - stop_price

        # Get forward bars
        ticker = str(row["ticker"])
        fwd_key = ticker
        fwd_df = forward_prices.get(fwd_key)
        if fwd_df is None or fwd_df.empty:
            continue

        fwd_after = fwd_df[fwd_df["_date"] > td].head(max_hold)

        if fwd_after.empty:
            continue

        outcome = _track_r_targets(fwd_after, entry_price, stop_price, r_unit, max_hold)

        results.append({
            "ticker": ticker,
            "trade_date": td,
            "strategy": "SQUEEZE",
            "setup_detected": True,
            "setup_date": td,
            "entry_triggered": True,
            "entry_date": td,
            "entry_price": entry_price,
            "stop_price": stop_price,
            "stop_distance_pct": round((entry_price - stop_price) / entry_price * 100, 2),
            "r_unit": round(r_unit, 4),
            # Feature snapshot
            "rsi_14_at_setup": row.get("rsi_14"),
            "rsi_2_at_setup": row.get("rsi_2"),
            "squeeze_on_at_setup": True,  # prior days were squeezed
            "bb_width_at_setup": row.get("bb_width_pct"),
            "volume_ratio_at_setup": row.get("volume_ratio_20d"),
            "atr_at_setup": row.get("atr_20"),
            "linreg_slope_at_setup": row.get("linreg_slope_12d"),
            **outcome,
        })

    return results


def _simulate_mean_rev(
    features_df: pd.DataFrame,
    forward_prices: Dict[str, pd.DataFrame],
    max_hold: int = 5,
) -> List[Dict[str, Any]]:
    """MEAN_REV: Connors RSI(2) < 10 mean reversion.

    Setup: close > sma_200 (uptrend) + rsi_2 < 10 (oversold).
    Entry: next day's open.
    Stop: entry - 1*ATR(20).
    Exit signal: close >= sma_10.
    """
    results = []
    dates = features_df["_date"].tolist() if "_date" in features_df.columns else [pd.Timestamp(d).date() for d in features_df["trade_date"]]

    for i in range(1, len(features_df)):
        row = features_df.iloc[i]
        td = dates[i]

        # Setup conditions
        rsi2 = row.get("rsi_2")
        sma200 = row.get("sma_200")
        close = row["close"]
        atr = row.get("atr_20")

        if pd.isna(rsi2) or rsi2 >= 10:
            continue
        if pd.isna(sma200) or close <= sma200:
            continue
        if pd.isna(atr) or atr <= 0:
            continue

        # Entry: next day's open
        ticker = str(row["ticker"])
        fwd_df = forward_prices.get(ticker)
        if fwd_df is None or fwd_df.empty:
            continue

        fwd_after = fwd_df[fwd_df["_date"] > td]

        if fwd_after.empty:
            continue

        entry_bar = fwd_after.iloc[0]
        entry_date = pd.Timestamp(entry_bar["trade_date"]).date()
        entry_price = float(entry_bar["open"])
        if entry_price <= 0:
            continue

        stop_price = entry_price - atr
        r_unit = atr

        # Forward bars after entry for tracking
        fwd_tracking = fwd_after.iloc[1:max_hold + 1] if len(fwd_after) > 1 else pd.DataFrame()

        # SMA10 series for exit signal — need to compute from feature data
        # Use sma_10 values from features for dates after entry
        sma10_series = None
        if not fwd_tracking.empty:
            fwd_dates = fwd_tracking["_date"].tolist() if "_date" in fwd_tracking.columns else [pd.Timestamp(d).date() for d in fwd_tracking["trade_date"]]
            sma10_vals = []
            for fd in fwd_dates:
                feat_match = features_df[features_df["_date"] == fd] if "_date" in features_df.columns else features_df[features_df["trade_date"].apply(lambda x: pd.Timestamp(x).date()) == fd]
                if not feat_match.empty:
                    sma10_vals.append(feat_match.iloc[0].get("sma_10"))
                else:
                    sma10_vals.append(None)
            sma10_series = pd.Series(sma10_vals)

        outcome = _track_r_targets(
            fwd_tracking, entry_price, stop_price, r_unit, max_hold,
            sma_10_series=sma10_series,
        )

        results.append({
            "ticker": ticker,
            "trade_date": td,
            "strategy": "MEAN_REV",
            "setup_detected": True,
            "setup_date": td,
            "entry_triggered": True,
            "entry_date": entry_date,
            "entry_price": entry_price,
            "stop_price": round(stop_price, 4),
            "stop_distance_pct": round(atr / entry_price * 100, 2),
            "r_unit": round(r_unit, 4),
            # Feature snapshot
            "rsi_14_at_setup": row.get("rsi_14"),
            "rsi_2_at_setup": rsi2,
            "squeeze_on_at_setup": row.get("squeeze_on"),
            "bb_width_at_setup": row.get("bb_width_pct"),
            "volume_ratio_at_setup": row.get("volume_ratio_20d"),
            "atr_at_setup": atr,
            "linreg_slope_at_setup": row.get("linreg_slope_12d"),
            **outcome,
        })

    return results


def _simulate_gap_drift(
    features_df: pd.DataFrame,
    forward_prices: Dict[str, pd.DataFrame],
    max_hold: int = 30,
) -> List[Dict[str, Any]]:
    """GAP_DRIFT: Post-earnings gap drift proxy.

    D0: gap_pct > 5% and volume_surge_3x.
    D1-D3: consolidation — no close below D0 low.
    D4+: breakout above consolidation high.
    Stop: consolidation low.
    """
    results = []
    dates = features_df["_date"].tolist() if "_date" in features_df.columns else [pd.Timestamp(d).date() for d in features_df["trade_date"]]

    for i in range(len(features_df)):
        row = features_df.iloc[i]
        td = dates[i]

        # D0 detection: gap > 5% + volume surge
        gap = row.get("gap_pct_from_prev")
        vol_surge = row.get("volume_surge_3x")
        if pd.isna(gap) or gap <= 5.0:
            continue
        if not vol_surge:
            continue

        ticker = str(row["ticker"])
        fwd_df = forward_prices.get(ticker)
        if fwd_df is None or fwd_df.empty:
            continue

        fwd_after = fwd_df[fwd_df["_date"] > td]

        if len(fwd_after) < 5:
            continue

        # D0 low from the feature's day bar (use forward_prices to get D0 bar)
        d0_bar_matches = fwd_df[fwd_df["_date"] == td]
        if d0_bar_matches.empty:
            # Use feature row's close as approximation
            d0_low = row["close"] * 0.97  # rough estimate
        else:
            d0_low = float(d0_bar_matches.iloc[0]["low"])

        # D1-D3: consolidation check
        consol_bars = fwd_after.iloc[:3]
        consol_valid = True
        consol_high = -np.inf
        consol_low = np.inf

        for _, cbar in consol_bars.iterrows():
            c_close = float(cbar["close"])
            c_high = float(cbar["high"])
            c_low = float(cbar["low"])
            if c_close < d0_low:
                consol_valid = False
                break
            consol_high = max(consol_high, c_high)
            consol_low = min(consol_low, c_low)

        if not consol_valid or consol_high == -np.inf:
            continue

        # D4+: look for breakout above consolidation high
        breakout_bars = fwd_after.iloc[3:10]  # search D4-D10 for breakout
        entry_triggered = False
        entry_date = None
        entry_price = None

        for _, bbar in breakout_bars.iterrows():
            if float(bbar["high"]) > consol_high:
                entry_triggered = True
                entry_date = pd.Timestamp(bbar["trade_date"]).date()
                entry_price = consol_high  # enter at breakout level
                break

        if not entry_triggered:
            continue

        stop_price = consol_low
        r_unit = entry_price - stop_price
        if r_unit <= 0:
            continue

        # Forward bars after entry
        fwd_tracking = fwd_after[fwd_after["_date"] > entry_date].head(max_hold)

        outcome = _track_r_targets(fwd_tracking, entry_price, stop_price, r_unit, max_hold)

        results.append({
            "ticker": ticker,
            "trade_date": td,
            "strategy": "GAP_DRIFT",
            "setup_detected": True,
            "setup_date": td,
            "entry_triggered": True,
            "entry_date": entry_date,
            "entry_price": round(entry_price, 4),
            "stop_price": round(stop_price, 4),
            "stop_distance_pct": round(r_unit / entry_price * 100, 2),
            "r_unit": round(r_unit, 4),
            "rsi_14_at_setup": row.get("rsi_14"),
            "rsi_2_at_setup": row.get("rsi_2"),
            "squeeze_on_at_setup": row.get("squeeze_on"),
            "bb_width_at_setup": row.get("bb_width_pct"),
            "volume_ratio_at_setup": row.get("volume_ratio_20d"),
            "atr_at_setup": row.get("atr_20"),
            "linreg_slope_at_setup": row.get("linreg_slope_12d"),
            **outcome,
        })

    return results


def _simulate_insider_breakout(
    features_df: pd.DataFrame,
    forward_prices: Dict[str, pd.DataFrame],
    max_hold: int = 30,
) -> List[Dict[str, Any]]:
    """INSIDER_BREAKOUT: Insider cluster + 20-day high breakout.

    Setup: days_since_insider_cluster <= 20.
    Entry: close breaks above 20-day high (price_vs_20d_high_pct >= 0).
    Stop: 20-day low.
    """
    results = []
    dates = features_df["_date"].tolist() if "_date" in features_df.columns else [pd.Timestamp(d).date() for d in features_df["trade_date"]]

    for i in range(20, len(features_df)):
        row = features_df.iloc[i]
        td = dates[i]

        # Setup: recent insider cluster
        days_since = row.get("days_since_insider_cluster")
        if pd.isna(days_since) or days_since > 20:
            continue

        # Entry: breaking above 20-day high
        pvs20h = row.get("price_vs_20d_high_pct")
        if pd.isna(pvs20h) or pvs20h < 0:
            continue

        close = row["close"]
        if close <= 0:
            continue

        ticker = str(row["ticker"])
        fwd_df = forward_prices.get(ticker)
        if fwd_df is None or fwd_df.empty:
            continue

        # 20-day low for stop
        lookback = features_df.iloc[max(0, i - 19): i + 1]
        if lookback.empty:
            continue

        # Get daily bars for 20d low computation
        fwd_all = fwd_df[fwd_df["trade_date"].apply(lambda x: pd.Timestamp(x).date()) <= td]
        if len(fwd_all) < 20:
            continue
        low_20d = fwd_all.tail(20)["low"].min()
        if pd.isna(low_20d) or low_20d <= 0 or low_20d >= close:
            continue

        entry_price = close
        stop_price = float(low_20d)
        r_unit = entry_price - stop_price

        if r_unit <= 0:
            continue

        # Forward bars
        fwd_tracking = fwd_df[fwd_df["_date"] > td].head(max_hold)

        if fwd_tracking.empty:
            continue

        outcome = _track_r_targets(fwd_tracking, entry_price, stop_price, r_unit, max_hold)

        results.append({
            "ticker": ticker,
            "trade_date": td,
            "strategy": "INSIDER_BREAKOUT",
            "setup_detected": True,
            "setup_date": td,
            "entry_triggered": True,
            "entry_date": td,
            "entry_price": entry_price,
            "stop_price": stop_price,
            "stop_distance_pct": round(r_unit / entry_price * 100, 2),
            "r_unit": round(r_unit, 4),
            "rsi_14_at_setup": row.get("rsi_14"),
            "rsi_2_at_setup": row.get("rsi_2"),
            "squeeze_on_at_setup": row.get("squeeze_on"),
            "bb_width_at_setup": row.get("bb_width_pct"),
            "volume_ratio_at_setup": row.get("volume_ratio_20d"),
            "atr_at_setup": row.get("atr_20"),
            "linreg_slope_at_setup": row.get("linreg_slope_12d"),
            **outcome,
        })

    return results


# ---------------------------------------------------------------------------
# Filtered / v2 strategies
# ---------------------------------------------------------------------------

_EXCLUDED_PHASES = {"DISTRIBUTION", "DECLINE", "DORMANT", "LATE_ACCUM"}


def _simulate_gap_drift_filtered(
    features_df: pd.DataFrame,
    forward_prices: Dict[str, pd.DataFrame],
    max_hold: int = 30,
) -> List[Dict[str, Any]]:
    """GAP_DRIFT_FILTERED: Gap drift + proven intelligence gates.

    Same entry logic as GAP_DRIFT but requires:
    - conviction_score >= 60
    - adx_14 >= 25
    - accum_phase NOT IN ('DISTRIBUTION', 'DECLINE', 'DORMANT', 'LATE_ACCUM')
    - quarter_month IN (1, 2)
    """
    results = []
    dates = features_df["_date"].tolist() if "_date" in features_df.columns else [pd.Timestamp(d).date() for d in features_df["trade_date"]]

    for i in range(len(features_df)):
        row = features_df.iloc[i]
        td = dates[i]

        # Intelligence gates
        conv = row.get("conviction_score")
        if pd.isna(conv) or conv < 60:
            continue
        adx = row.get("adx_14")
        if pd.isna(adx) or adx < 25:
            continue
        phase = row.get("accum_phase")
        if phase in _EXCLUDED_PHASES:
            continue
        qm = row.get("quarter_month")
        if pd.notna(qm) and qm == 3:
            continue

        # D0 detection: gap > 5% + volume surge (same as GAP_DRIFT)
        gap = row.get("gap_pct_from_prev")
        vol_surge = row.get("volume_surge_3x")
        if pd.isna(gap) or gap <= 5.0:
            continue
        if not vol_surge:
            continue

        ticker = str(row["ticker"])
        fwd_df = forward_prices.get(ticker)
        if fwd_df is None or fwd_df.empty:
            continue

        fwd_after = fwd_df[fwd_df["_date"] > td]
        if len(fwd_after) < 5:
            continue

        d0_bar_matches = fwd_df[fwd_df["_date"] == td]
        if d0_bar_matches.empty:
            d0_low = row["close"] * 0.97
        else:
            d0_low = float(d0_bar_matches.iloc[0]["low"])

        # D1-D3: consolidation check
        consol_bars = fwd_after.iloc[:3]
        consol_valid = True
        consol_high = -np.inf
        consol_low = np.inf

        for _, cbar in consol_bars.iterrows():
            c_close = float(cbar["close"])
            c_high = float(cbar["high"])
            c_low = float(cbar["low"])
            if c_close < d0_low:
                consol_valid = False
                break
            consol_high = max(consol_high, c_high)
            consol_low = min(consol_low, c_low)

        if not consol_valid or consol_high == -np.inf:
            continue

        # D4+: look for breakout above consolidation high
        breakout_bars = fwd_after.iloc[3:10]
        entry_triggered = False
        entry_date = None
        entry_price = None

        for _, bbar in breakout_bars.iterrows():
            if float(bbar["high"]) > consol_high:
                entry_triggered = True
                entry_date = pd.Timestamp(bbar["trade_date"]).date()
                entry_price = consol_high
                break

        if not entry_triggered:
            continue

        stop_price = consol_low
        r_unit = entry_price - stop_price
        if r_unit <= 0:
            continue

        fwd_tracking = fwd_after[fwd_after["_date"] > entry_date].head(max_hold)
        outcome = _track_r_targets(fwd_tracking, entry_price, stop_price, r_unit, max_hold)

        results.append({
            "ticker": ticker,
            "trade_date": td,
            "strategy": "GAP_DRIFT_FILTERED",
            "setup_detected": True,
            "setup_date": td,
            "entry_triggered": True,
            "entry_date": entry_date,
            "entry_price": round(entry_price, 4),
            "stop_price": round(stop_price, 4),
            "stop_distance_pct": round(r_unit / entry_price * 100, 2),
            "r_unit": round(r_unit, 4),
            "rsi_14_at_setup": row.get("rsi_14"),
            "rsi_2_at_setup": row.get("rsi_2"),
            "squeeze_on_at_setup": row.get("squeeze_on"),
            "bb_width_at_setup": row.get("bb_width_pct"),
            "volume_ratio_at_setup": row.get("volume_ratio_20d"),
            "atr_at_setup": row.get("atr_20"),
            "linreg_slope_at_setup": row.get("linreg_slope_12d"),
            **outcome,
        })

    return results


def _simulate_mean_rev_filtered(
    features_df: pd.DataFrame,
    forward_prices: Dict[str, pd.DataFrame],
    max_hold: int = 5,
) -> List[Dict[str, Any]]:
    """MEAN_REV_FILTERED: Mean reversion + intelligence gates.

    Same entry logic as MEAN_REV but requires:
    - conviction_score >= 80
    - adx_14 >= 25
    - volume_ratio_20d >= 1.5
    """
    results = []
    dates = features_df["_date"].tolist() if "_date" in features_df.columns else [pd.Timestamp(d).date() for d in features_df["trade_date"]]

    for i in range(1, len(features_df)):
        row = features_df.iloc[i]
        td = dates[i]

        # Intelligence gates
        conv = row.get("conviction_score")
        if pd.isna(conv) or conv < 80:
            continue
        adx = row.get("adx_14")
        if pd.isna(adx) or adx < 25:
            continue
        vol_r = row.get("volume_ratio_20d")
        if pd.isna(vol_r) or vol_r < 1.5:
            continue

        # Setup conditions (same as MEAN_REV)
        rsi2 = row.get("rsi_2")
        sma200 = row.get("sma_200")
        close = row["close"]
        atr = row.get("atr_20")

        if pd.isna(rsi2) or rsi2 >= 10:
            continue
        if pd.isna(sma200) or close <= sma200:
            continue
        if pd.isna(atr) or atr <= 0:
            continue

        ticker = str(row["ticker"])
        fwd_df = forward_prices.get(ticker)
        if fwd_df is None or fwd_df.empty:
            continue

        fwd_after = fwd_df[fwd_df["_date"] > td]
        if fwd_after.empty:
            continue

        entry_bar = fwd_after.iloc[0]
        entry_date = pd.Timestamp(entry_bar["trade_date"]).date()
        entry_price = float(entry_bar["open"])
        if entry_price <= 0:
            continue

        stop_price = entry_price - atr
        r_unit = atr

        fwd_tracking = fwd_after.iloc[1:max_hold + 1] if len(fwd_after) > 1 else pd.DataFrame()

        # SMA10 exit signal (same as MEAN_REV)
        sma10_series = None
        if not fwd_tracking.empty:
            fwd_dates = fwd_tracking["_date"].tolist() if "_date" in fwd_tracking.columns else [pd.Timestamp(d).date() for d in fwd_tracking["trade_date"]]
            sma10_vals = []
            for fd in fwd_dates:
                feat_match = features_df[features_df["_date"] == fd] if "_date" in features_df.columns else features_df[features_df["trade_date"].apply(lambda x: pd.Timestamp(x).date()) == fd]
                if not feat_match.empty:
                    sma10_vals.append(feat_match.iloc[0].get("sma_10"))
                else:
                    sma10_vals.append(None)
            sma10_series = pd.Series(sma10_vals)

        outcome = _track_r_targets(
            fwd_tracking, entry_price, stop_price, r_unit, max_hold,
            sma_10_series=sma10_series,
        )

        results.append({
            "ticker": ticker,
            "trade_date": td,
            "strategy": "MEAN_REV_FILTERED",
            "setup_detected": True,
            "setup_date": td,
            "entry_triggered": True,
            "entry_date": entry_date,
            "entry_price": entry_price,
            "stop_price": round(stop_price, 4),
            "stop_distance_pct": round(atr / entry_price * 100, 2),
            "r_unit": round(r_unit, 4),
            "rsi_14_at_setup": row.get("rsi_14"),
            "rsi_2_at_setup": rsi2,
            "squeeze_on_at_setup": row.get("squeeze_on"),
            "bb_width_at_setup": row.get("bb_width_pct"),
            "volume_ratio_at_setup": vol_r,
            "atr_at_setup": atr,
            "linreg_slope_at_setup": row.get("linreg_slope_12d"),
            **outcome,
        })

    return results


def _simulate_candle_reversal(
    features_df: pd.DataFrame,
    forward_prices: Dict[str, pd.DataFrame],
    max_hold: int = 10,
) -> List[Dict[str, Any]]:
    """CANDLE_REVERSAL: Candlestick reversal + technical context.

    Setup: (hammer OR morning_star OR piercing_line)
           AND rsi_14 < 35 AND close > sma_200 AND volume_ratio_20d >= 1.2
    Entry: next day's open
    Stop: setup day's low - 0.5 * ATR(20)
    Exit signal: engulfing_bear OR evening_star OR dark_cloud_cover
    """
    results = []
    dates = features_df["_date"].tolist() if "_date" in features_df.columns else [pd.Timestamp(d).date() for d in features_df["trade_date"]]

    for i in range(1, len(features_df)):
        row = features_df.iloc[i]
        td = dates[i]

        # Candlestick setup: at least one bullish reversal pattern
        has_hammer = row.get("hammer", False)
        has_morning = row.get("morning_star", False)
        has_piercing = row.get("piercing_line", False)
        if not (has_hammer or has_morning or has_piercing):
            continue

        # Technical context
        rsi = row.get("rsi_14")
        if pd.isna(rsi) or rsi >= 35:
            continue
        sma200 = row.get("sma_200")
        close = row["close"]
        if pd.isna(sma200) or close <= sma200:
            continue
        vol_r = row.get("volume_ratio_20d")
        if pd.isna(vol_r) or vol_r < 1.2:
            continue

        atr = row.get("atr_20")
        if pd.isna(atr) or atr <= 0:
            continue

        ticker = str(row["ticker"])
        fwd_df = forward_prices.get(ticker)
        if fwd_df is None or fwd_df.empty:
            continue

        # Get setup day's low from price data
        setup_bar = fwd_df[fwd_df["_date"] == td]
        if setup_bar.empty:
            continue
        setup_low = float(setup_bar.iloc[0]["low"])

        # Entry: next day's open
        fwd_after = fwd_df[fwd_df["_date"] > td]
        if fwd_after.empty:
            continue

        entry_bar = fwd_after.iloc[0]
        entry_date = pd.Timestamp(entry_bar["trade_date"]).date()
        entry_price = float(entry_bar["open"])
        if entry_price <= 0:
            continue

        stop_price = setup_low - 0.5 * atr
        r_unit = entry_price - stop_price
        if r_unit <= 0:
            continue

        # Forward bars for tracking
        fwd_tracking = fwd_after.iloc[1:max_hold + 1] if len(fwd_after) > 1 else pd.DataFrame()

        # Build bearish exit signal from features
        exit_signal = None
        if not fwd_tracking.empty:
            fwd_dates = fwd_tracking["_date"].tolist() if "_date" in fwd_tracking.columns else [pd.Timestamp(d).date() for d in fwd_tracking["trade_date"]]
            sig_vals = []
            for fd in fwd_dates:
                feat_match = features_df[features_df["_date"] == fd] if "_date" in features_df.columns else None
                if feat_match is not None and not feat_match.empty:
                    fm = feat_match.iloc[0]
                    bearish = (fm.get("engulfing_bear", False) or
                               fm.get("evening_star", False) or
                               fm.get("dark_cloud_cover", False))
                    sig_vals.append(bool(bearish))
                else:
                    sig_vals.append(False)
            exit_signal = pd.Series(sig_vals)

        outcome = _track_r_targets(
            fwd_tracking, entry_price, stop_price, r_unit, max_hold,
            exit_signal_series=exit_signal,
        )

        results.append({
            "ticker": ticker,
            "trade_date": td,
            "strategy": "CANDLE_REVERSAL",
            "setup_detected": True,
            "setup_date": td,
            "entry_triggered": True,
            "entry_date": entry_date,
            "entry_price": entry_price,
            "stop_price": round(stop_price, 4),
            "stop_distance_pct": round(r_unit / entry_price * 100, 2),
            "r_unit": round(r_unit, 4),
            "rsi_14_at_setup": rsi,
            "rsi_2_at_setup": row.get("rsi_2"),
            "squeeze_on_at_setup": row.get("squeeze_on"),
            "bb_width_at_setup": row.get("bb_width_pct"),
            "volume_ratio_at_setup": vol_r,
            "atr_at_setup": atr,
            "linreg_slope_at_setup": row.get("linreg_slope_12d"),
            **outcome,
        })

    return results


# ---------------------------------------------------------------------------
# Main backtest runner
# ---------------------------------------------------------------------------

_STRATEGY_MAP = {
    "SQUEEZE": _simulate_squeeze,
    "MEAN_REV": _simulate_mean_rev,
    "GAP_DRIFT": _simulate_gap_drift,
    "INSIDER_BREAKOUT": _simulate_insider_breakout,
    "GAP_DRIFT_FILTERED": _simulate_gap_drift_filtered,
    "MEAN_REV_FILTERED": _simulate_mean_rev_filtered,
    "CANDLE_REVERSAL": _simulate_candle_reversal,
}

ALL_STRATEGIES = list(_STRATEGY_MAP.keys())


def run_swing_backtest(
    conn: duckdb.DuckDBPyConnection,
    strategies: List[str],
    quarters: List[str],
) -> int:
    """Run swing backtests for given strategies across quarters.

    Returns total result rows written.
    """
    _ensure_tables(conn)
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    total_rows = 0

    for quarter in quarters:
        q_start = _quarter_start_date(quarter)
        q_end = _quarter_end_date(quarter)
        # Need forward prices beyond quarter end for holding periods
        fwd_end = q_end + timedelta(days=45)

        logger.info("=== Swing backtest: quarter={} ===", quarter)

        # Load features for this quarter
        features_df = conn.execute("""
            SELECT * FROM fact_swing_features
            WHERE report_quarter = ?
            ORDER BY ticker, trade_date
        """, [quarter]).fetchdf()

        if features_df.empty:
            logger.warning("  No features found for {}. Run feature engine first.", quarter)
            continue

        # Pre-convert dates for fast filtering
        features_df["_date"] = pd.to_datetime(features_df["trade_date"]).dt.date

        tickers = features_df["ticker"].unique().tolist()
        logger.info("  {} feature rows for {} tickers", len(features_df), len(tickers))

        # Pre-load forward daily prices for all tickers (single batch query)
        logger.info("  Loading forward daily prices...")
        all_prices_df = conn.execute("""
            SELECT ticker, trade_date, open, high, low, close, volume
            FROM fact_daily_prices
            WHERE trade_date >= ? AND trade_date <= ?
              AND close IS NOT NULL AND close > 0
            ORDER BY ticker, trade_date
        """, [q_start.isoformat(), fwd_end.isoformat()]).fetchdf()

        # Pre-convert dates to python date objects for fast filtering
        all_prices_df["_date"] = pd.to_datetime(all_prices_df["trade_date"]).dt.date

        forward_prices: Dict[str, pd.DataFrame] = {}
        for ticker, grp in all_prices_df.groupby("ticker"):
            if ticker in set(tickers):
                forward_prices[str(ticker)] = grp.reset_index(drop=True)

        logger.info("  Forward prices loaded for {} tickers", len(forward_prices))

        for strategy in strategies:
            sim_fn = _STRATEGY_MAP.get(strategy)
            if sim_fn is None:
                logger.warning("  Unknown strategy: {}", strategy)
                continue

            logger.info("  Running {} simulation...", strategy)

            # Run per-ticker
            all_results = []
            ticker_groups = features_df.groupby("ticker")

            for ticker, ticker_feats in ticker_groups:
                ticker_feats = ticker_feats.sort_values("trade_date").reset_index(drop=True)
                sim_results = sim_fn(ticker_feats, forward_prices)
                all_results.extend(sim_results)

            if not all_results:
                logger.info("    {} — 0 setups detected", strategy)
                continue

            # Add context and write
            batch = []
            for r in all_results:
                # Get intelligence context from features
                feat_match = features_df[
                    (features_df["ticker"] == r["ticker"]) &
                    (features_df["_date"] == r["trade_date"])
                ]
                if not feat_match.empty:
                    fm = feat_match.iloc[0]
                    r["report_quarter"] = quarter
                    r["conviction_score"] = fm.get("conviction_score")
                    r["accum_phase"] = fm.get("accum_phase")
                    r["squeeze_score"] = fm.get("int_squeeze_score")
                    r["sector"] = fm.get("sector")
                else:
                    r["report_quarter"] = quarter
                    r.setdefault("conviction_score", None)
                    r.setdefault("accum_phase", None)
                    r.setdefault("squeeze_score", None)
                    r.setdefault("sector", None)

                r["computed_at"] = now_iso
                batch.append(r)

            # Write to DuckDB
            _write_results(conn, batch, strategy, quarter)
            total_rows += len(batch)

            # Quick summary
            triggered = [r for r in batch if r.get("entry_triggered")]
            hit_2r = sum(1 for r in triggered if r.get("hit_2r"))
            hit_stop = sum(1 for r in triggered if r.get("hit_stop"))
            logger.info("    {} — {} setups, {} triggered, {} hit 2R ({:.1f}%), {} stopped",
                        strategy, len(batch), len(triggered),
                        hit_2r, hit_2r / len(triggered) * 100 if triggered else 0,
                        hit_stop)

    logger.info("=== Swing backtest complete: {:,} total results ===", total_rows)
    return total_rows


def _write_results(conn: duckdb.DuckDBPyConnection, batch: List[Dict],
                   strategy: str, quarter: str) -> None:
    """Write results batch to DuckDB."""
    df = pd.DataFrame(batch)

    expected_cols = [
        "ticker", "trade_date", "strategy",
        "report_quarter", "conviction_score", "accum_phase", "squeeze_score", "sector",
        "setup_detected", "setup_date", "entry_triggered", "entry_date",
        "entry_price", "stop_price", "stop_distance_pct", "r_unit",
        "hit_1r", "hit_2r", "hit_stop",
        "days_to_1r", "days_to_2r", "days_to_stop",
        "max_favorable_r", "max_adverse_r",
        "hold_days", "exit_type", "exit_date", "exit_price", "exit_r",
        "hit_ema10", "days_to_ema10",
        "rsi_14_at_setup", "rsi_2_at_setup", "squeeze_on_at_setup",
        "bb_width_at_setup", "volume_ratio_at_setup", "atr_at_setup",
        "linreg_slope_at_setup",
        "computed_at",
    ]

    for col in expected_cols:
        if col not in df.columns:
            df[col] = None
    df = df[expected_cols]

    # Round floats
    float_cols = ["entry_price", "stop_price", "stop_distance_pct", "r_unit",
                  "max_favorable_r", "max_adverse_r", "exit_price", "exit_r"]
    for col in float_cols:
        if col in df.columns:
            df[col] = df[col].apply(lambda x: round(x, 4) if pd.notna(x) else x)

    conn.register("_swing_bt_temp", df)
    conn.execute("""
        DELETE FROM swing_backtest_results
        WHERE strategy = ? AND report_quarter = ?
    """, [strategy, quarter])
    conn.execute("INSERT INTO swing_backtest_results SELECT * FROM _swing_bt_temp")
    conn.unregister("_swing_bt_temp")


# ---------------------------------------------------------------------------
# Comparative summary
# ---------------------------------------------------------------------------

def print_comparative_summary(conn: duckdb.DuckDBPyConnection,
                              quarters: Optional[List[str]] = None) -> None:
    """Print side-by-side strategy comparison."""
    where = ""
    params = []
    if quarters:
        placeholders = ",".join(["?" for _ in quarters])
        where = f"WHERE report_quarter IN ({placeholders})"
        params = quarters

    print(f"\n{'='*70}")
    print(f"  SWING BACKTEST COMPARATIVE SUMMARY")
    print(f"{'='*70}")

    # Overall by strategy
    df = conn.execute(f"""
        SELECT
            strategy,
            COUNT(*) AS total_setups,
            SUM(CASE WHEN entry_triggered THEN 1 ELSE 0 END) AS triggered,
            SUM(CASE WHEN hit_1r THEN 1 ELSE 0 END) AS hit_1r,
            SUM(CASE WHEN hit_2r THEN 1 ELSE 0 END) AS hit_2r,
            SUM(CASE WHEN hit_stop THEN 1 ELSE 0 END) AS stopped,
            AVG(CASE WHEN entry_triggered THEN max_favorable_r END) AS avg_mfe,
            AVG(CASE WHEN entry_triggered THEN max_adverse_r END) AS avg_mae,
            AVG(CASE WHEN entry_triggered THEN exit_r END) AS avg_exit_r,
            AVG(CASE WHEN entry_triggered THEN hold_days END) AS avg_hold
        FROM swing_backtest_results
        {where}
        GROUP BY strategy
        ORDER BY strategy
    """, params).fetchdf()

    if df.empty:
        print("  No results found. Run --run first.")
        return

    print(f"\n  {'Strategy':<20} {'Setups':>7} {'Trig':>6} {'1R%':>6} {'2R%':>6} "
          f"{'Stop%':>6} {'AvgMFE':>7} {'AvgMAE':>7} {'AvgR':>6} {'Hold':>5}")
    print(f"  {'-'*20} {'-'*7} {'-'*6} {'-'*6} {'-'*6} {'-'*6} {'-'*7} {'-'*7} {'-'*6} {'-'*5}")

    for _, row in df.iterrows():
        trig = int(row["triggered"]) if pd.notna(row["triggered"]) else 0
        h1r = int(row["hit_1r"]) if pd.notna(row["hit_1r"]) else 0
        h2r = int(row["hit_2r"]) if pd.notna(row["hit_2r"]) else 0
        stp = int(row["stopped"]) if pd.notna(row["stopped"]) else 0
        r1_pct = h1r / trig * 100 if trig > 0 else 0
        r2_pct = h2r / trig * 100 if trig > 0 else 0
        s_pct = stp / trig * 100 if trig > 0 else 0

        print(f"  {row['strategy']:<20} {int(row['total_setups']):>7,} {trig:>6,} "
              f"{r1_pct:>5.1f}% {r2_pct:>5.1f}% {s_pct:>5.1f}% "
              f"{row['avg_mfe']:>7.2f} {row['avg_mae']:>7.2f} "
              f"{row['avg_exit_r']:>6.2f} {row['avg_hold']:>5.1f}")

    # By quarter
    print(f"\n  By Quarter:")
    qdf = conn.execute(f"""
        SELECT strategy, report_quarter,
               COUNT(*) AS n,
               SUM(CASE WHEN entry_triggered THEN 1 ELSE 0 END) AS trig,
               SUM(CASE WHEN hit_2r THEN 1 ELSE 0 END) AS h2r,
               SUM(CASE WHEN hit_stop THEN 1 ELSE 0 END) AS stp
        FROM swing_backtest_results
        {where}
        GROUP BY strategy, report_quarter
        ORDER BY strategy, report_quarter
    """, params).fetchdf()

    for _, row in qdf.iterrows():
        trig = int(row["trig"]) if pd.notna(row["trig"]) else 0
        h2r = int(row["h2r"]) if pd.notna(row["h2r"]) else 0
        stp = int(row["stp"]) if pd.notna(row["stp"]) else 0
        r2_pct = h2r / trig * 100 if trig > 0 else 0
        print(f"    {row['strategy']:<18} {row['report_quarter']}  "
              f"n={trig:>5,}  2R={r2_pct:>5.1f}%  stop={stp:>4,}")

    # Exit type distribution
    print(f"\n  Exit Type Distribution:")
    entry_filter = "AND entry_triggered = TRUE" if where else "WHERE entry_triggered = TRUE"
    edf = conn.execute(f"""
        SELECT strategy, exit_type, COUNT(*) AS cnt
        FROM swing_backtest_results
        {where}
        {entry_filter}
        GROUP BY strategy, exit_type
        ORDER BY strategy, cnt DESC
    """, params).fetchdf()

    for strat in df["strategy"].unique():
        subset = edf[edf["strategy"] == strat]
        parts = [f"{row['exit_type']}={int(row['cnt'])}" for _, row in subset.iterrows()]
        print(f"    {strat:<18} {', '.join(parts)}")

    print(f"{'='*70}\n")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Swing Backtester — 4 strategy simulations with R-target tracking"
    )
    parser.add_argument("--run", action="store_true", help="Run backtests")
    parser.add_argument("--compare", action="store_true", help="Print comparative summary")
    parser.add_argument(
        "--strategy", type=str, default="ALL",
        help="Strategy to run: SQUEEZE|MEAN_REV|GAP_DRIFT|INSIDER_BREAKOUT|GAP_DRIFT_FILTERED|MEAN_REV_FILTERED|CANDLE_REVERSAL|ALL",
    )
    parser.add_argument(
        "--quarters", type=str, default=None,
        help="Comma-separated quarters (e.g. 2023-Q4,2024-Q1)",
    )
    args = parser.parse_args()

    conn = duckdb.connect(str(WAREHOUSE_PATH))
    try:
        quarters = None
        if args.quarters:
            quarters = [q.strip() for q in args.quarters.split(",")]

        strategies = ALL_STRATEGIES if args.strategy == "ALL" else [args.strategy.upper()]

        if args.run:
            if not quarters:
                parser.error("--run requires --quarters")
            run_swing_backtest(conn, strategies, quarters)

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
