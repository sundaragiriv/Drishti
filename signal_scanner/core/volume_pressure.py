"""Volume Pressure Proxy — bar-level buying/selling pressure approximation.

Not true Order Flow Imbalance (requires tick data).
This is a proxy using 1-min bar characteristics:
  - Up-bar vs down-bar volume
  - Close location within bar range
  - Volume relative to baseline

Used as:
  - Secondary confirmation for intraday entries
  - Context for ISR drivers block

Usage:
    pressure = compute_volume_pressure(bars, window=5)
"""

from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd


def compute_volume_pressure(bars: pd.DataFrame, window: int = 5) -> Optional[dict]:
    """Compute volume-based buying/selling pressure from 1-min bars.

    Args:
        bars: DataFrame with [Open, High, Low, Close, Volume]
        window: number of recent bars to analyze

    Returns:
        dict with pressure metrics, or None if insufficient data.
    """
    if bars is None or len(bars) < window + 5:
        return None

    try:
        recent = bars.iloc[-window:]
        baseline = bars.iloc[:-window] if len(bars) > window * 2 else bars

        # Up-bar vs down-bar volume
        up_mask = recent["Close"] >= recent["Open"]
        up_volume = float(recent.loc[up_mask, "Volume"].sum())
        down_volume = float(recent.loc[~up_mask, "Volume"].sum())
        total_volume = up_volume + down_volume

        if total_volume <= 0:
            return None

        buy_ratio = up_volume / total_volume  # 0-1, >0.5 = buyer dominant

        # Close location value (CLV): where close sits in the bar range
        # CLV = (close - low) / (high - low), averaged over window
        # 1.0 = closed at high (buyers won), 0.0 = closed at low (sellers won)
        ranges = recent["High"] - recent["Low"]
        clv = ((recent["Close"] - recent["Low"]) / ranges.replace(0, np.nan)).mean()

        # Volume relative to baseline
        recent_avg_vol = float(recent["Volume"].mean())
        baseline_avg_vol = float(baseline["Volume"].mean()) if len(baseline) > 0 else recent_avg_vol
        volume_ratio = recent_avg_vol / baseline_avg_vol if baseline_avg_vol > 0 else 1.0

        # Composite pressure score (0-100)
        # >50 = buying pressure, <50 = selling pressure
        buy_component = buy_ratio * 40  # 0-40
        clv_component = (clv or 0.5) * 30  # 0-30
        vol_component = min(30, volume_ratio * 15)  # 0-30

        pressure_score = round(buy_component + clv_component + vol_component, 1)

        # Verdict
        if pressure_score >= 65 and volume_ratio > 1.2:
            verdict = "STRONG_BUY_PRESSURE"
        elif pressure_score >= 55:
            verdict = "BUY_PRESSURE"
        elif pressure_score <= 35 and volume_ratio > 1.2:
            verdict = "STRONG_SELL_PRESSURE"
        elif pressure_score <= 45:
            verdict = "SELL_PRESSURE"
        else:
            verdict = "NEUTRAL"

        return {
            "pressure_score": pressure_score,
            "buy_ratio": round(buy_ratio, 3),
            "clv": round(clv or 0.5, 3),
            "volume_ratio": round(volume_ratio, 2),
            "verdict": verdict,
        }
    except Exception:
        return None
