"""VWAP Standard Deviation Bands — exhaustion detection.

Computes running VWAP + standard deviation bands from 1-min bars.
A setup at +3σ is likely a trap (exhaustion), not a continuation.

Used as:
  - Exhaustion feature for VWAP_MR, FPB, ORB_V2
  - Context for ISR mean reversion block
  - Why-No-Trade diagnostic

Usage:
    sigma = compute_vwap_sigma(bars)
"""

from __future__ import annotations

from typing import Optional, Tuple

import numpy as np
import pandas as pd


def compute_vwap_sigma(bars: pd.DataFrame) -> Optional[dict]:
    """Compute current price position relative to VWAP standard deviation bands.

    Args:
        bars: DataFrame with columns [Open, High, Low, Close, Volume]

    Returns:
        dict with vwap, sigma, bands, and current position, or None if insufficient data.
    """
    if bars is None or len(bars) < 10:
        return None

    try:
        typical = (bars["High"] + bars["Low"] + bars["Close"]) / 3
        volume = bars["Volume"].astype(float)

        # Cumulative VWAP
        cum_tp_vol = (typical * volume).cumsum()
        cum_vol = volume.cumsum()
        vwap = cum_tp_vol / cum_vol.replace(0, np.nan)

        # Running standard deviation of price around VWAP
        # Using volume-weighted variance
        cum_tp2_vol = (typical ** 2 * volume).cumsum()
        variance = (cum_tp2_vol / cum_vol) - (vwap ** 2)
        variance = variance.clip(lower=0)
        sigma = np.sqrt(variance)

        current_close = float(bars.iloc[-1]["Close"])
        current_vwap = float(vwap.iloc[-1])
        current_sigma = float(sigma.iloc[-1]) if sigma.iloc[-1] > 0 else 0.01

        # How many sigmas from VWAP
        sigma_distance = (current_close - current_vwap) / current_sigma if current_sigma > 0 else 0

        # Bands
        band_1_upper = current_vwap + current_sigma
        band_1_lower = current_vwap - current_sigma
        band_2_upper = current_vwap + 2 * current_sigma
        band_2_lower = current_vwap - 2 * current_sigma
        band_3_upper = current_vwap + 3 * current_sigma
        band_3_lower = current_vwap - 3 * current_sigma

        # Exhaustion verdict
        if abs(sigma_distance) >= 3.0:
            verdict = "EXHAUSTED"
        elif abs(sigma_distance) >= 2.5:
            verdict = "STRETCHED"
        elif abs(sigma_distance) >= 2.0:
            verdict = "EXTENDED"
        elif abs(sigma_distance) <= 0.5:
            verdict = "AT_VWAP"
        else:
            verdict = "NORMAL"

        return {
            "vwap": round(current_vwap, 4),
            "sigma": round(current_sigma, 4),
            "sigma_distance": round(sigma_distance, 2),
            "verdict": verdict,
            "close": current_close,
            "band_1_upper": round(band_1_upper, 4),
            "band_1_lower": round(band_1_lower, 4),
            "band_2_upper": round(band_2_upper, 4),
            "band_2_lower": round(band_2_lower, 4),
            "band_3_upper": round(band_3_upper, 4),
            "band_3_lower": round(band_3_lower, 4),
        }
    except Exception:
        return None
