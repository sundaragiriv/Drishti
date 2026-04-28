"""Hurst exponent via Detrended Fluctuation Analysis (DFA).

The Hurst exponent H is a measure of long-range dependence in a time
series:

    H > 0.5  -> persistent / trending (positive autocorrelation at long lags)
    H = 0.5  -> random walk (no long-range dependence)
    H < 0.5  -> anti-persistent / mean-reverting

For our 5-day swing target, this directly answers "is this ticker
currently trending or mean-reverting?" — the structural question that
ATR/SMA/RSI try to estimate via short-term proxies.

Implementation uses Detrended Fluctuation Analysis (Peng et al. 1994),
which is more robust to non-stationarity than classical R/S analysis.

Reference: Peng, C.-K. et al. (1994). "Mosaic organization of DNA
nucleotides." Phys. Rev. E 49(2): 1685-1689.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def hurst_dfa(returns: np.ndarray,
              min_window: int = 4,
              max_window_frac: float = 0.25) -> float:
    """Compute the Hurst exponent via DFA on a 1D return series.

    Args:
        returns: 1D numpy array of log returns.
        min_window: smallest box size for the fluctuation curve (>=4).
        max_window_frac: largest box size as fraction of N (default 0.25).

    Returns:
        H in [0, 1]. NaN if N < 64 (not enough scales for a stable fit).
    """
    r = np.asarray(returns, dtype=float)
    r = r[~np.isnan(r)]
    n = len(r)
    if n < 64:
        return float("nan")

    # Step 1: integrate (cumulative deviation from mean)
    y = np.cumsum(r - r.mean())

    # Step 2: choose log-spaced box sizes
    max_window = max(min_window + 1, int(n * max_window_frac))
    if max_window <= min_window:
        return float("nan")
    n_scales = max(8, int(np.log2(max_window / min_window) * 4))
    scales = np.unique(np.logspace(
        np.log10(min_window), np.log10(max_window), num=n_scales
    ).astype(int))
    scales = scales[scales >= min_window]
    if len(scales) < 4:
        return float("nan")

    # Step 3: for each scale s, split y into non-overlapping boxes of length s,
    # detrend each box with a linear fit, compute root-mean-square of the
    # residual. F(s) is the average across boxes.
    F = np.empty(len(scales))
    for i, s in enumerate(scales):
        n_boxes = n // s
        if n_boxes < 1:
            F[i] = np.nan
            continue
        # Reshape into (n_boxes, s); detrend each row
        chunks = y[: n_boxes * s].reshape(n_boxes, s)
        x = np.arange(s)
        # Linear detrend: residual = chunk - (a*x + b)
        # Vectorized: for each row, fit (slope, intercept) via lstsq
        x_mean = x.mean()
        x_dev = x - x_mean
        denom = np.sum(x_dev ** 2)
        chunk_means = chunks.mean(axis=1)
        slopes = (chunks * x_dev).sum(axis=1) / denom
        intercepts = chunk_means - slopes * x_mean
        residuals = chunks - (slopes[:, None] * x[None, :] + intercepts[:, None])
        rms = np.sqrt((residuals ** 2).mean(axis=1))
        F[i] = rms.mean()

    # Step 4: H is the slope of log F(s) vs log s
    valid = np.isfinite(F) & (F > 0)
    if valid.sum() < 4:
        return float("nan")
    log_s = np.log(scales[valid])
    log_F = np.log(F[valid])
    slope, _intercept = np.polyfit(log_s, log_F, 1)
    return float(slope)


def hurst_rolling(returns: pd.Series, window: int, step: int = 22) -> pd.Series:
    """Rolling Hurst exponent over a series of returns.

    For performance, Hurst is computed every `step` observations and
    forward-filled in between. Default step=22 (~monthly for daily data)
    is reasonable because Hurst regime changes slowly — typical
    structural shifts happen over weeks-to-months, not days.

    Setting step=1 reproduces the per-day rolling version (slow, O(N) calls).

    For window=64 you'd typically want at least 128 prior observations
    for stable estimates. Returns NaN for indices before the first
    valid window.
    """
    if window < 64:
        raise ValueError(f"Hurst rolling window should be >= 64 for stability; got {window}")
    if step < 1:
        raise ValueError(f"step must be >= 1; got {step}")

    arr = returns.values
    n = len(arr)
    out = np.full(n, np.nan)
    # Compute at every `step`-th index (and the last index for freshness).
    sample_idx = list(range(window - 1, n, step))
    if not sample_idx or sample_idx[-1] != n - 1:
        sample_idx.append(n - 1)

    for i in sample_idx:
        if i < window - 1:
            continue
        out[i] = hurst_dfa(arr[i - window + 1: i + 1])

    # Forward-fill between sample points
    s = pd.Series(out, index=returns.index)
    return s.ffill()


def hurst_regime(h: float | pd.Series,
                 lower: float = 0.45,
                 upper: float = 0.55) -> str | pd.Series:
    """Categorize Hurst into TRENDING / RANDOM / MEAN_REVERT."""
    if isinstance(h, pd.Series):
        out = pd.Series("RANDOM", index=h.index, dtype="object")
        out[h > upper] = "TRENDING"
        out[h < lower] = "MEAN_REVERT"
        out[h.isna()] = "UNKNOWN"
        return out
    if pd.isna(h):
        return "UNKNOWN"
    if h > upper:
        return "TRENDING"
    if h < lower:
        return "MEAN_REVERT"
    return "RANDOM"


def add_hurst_features(returns: pd.Series) -> pd.DataFrame:
    """Compute the 3 Hurst features for a single-ticker return series.

    Args:
        returns: pd.Series of log returns indexed by date, sorted ascending.

    Returns columns: hurst_64d, hurst_252d, hurst_regime
    """
    h64 = hurst_rolling(returns, window=64)
    h252 = hurst_rolling(returns, window=252)
    return pd.DataFrame({
        "hurst_64d": h64,
        "hurst_252d": h252,
        "hurst_regime": hurst_regime(h64),  # use shorter window for regime tag
    }, index=returns.index)
