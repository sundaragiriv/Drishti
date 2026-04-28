"""Yang-Zhang volatility estimator (Yang & Zhang 2000).

Most efficient unbiased OHLC volatility estimator (Garman-Klass and
Rogers-Satchell are special cases). Decomposes total variance into:

    sigma^2_YZ = sigma^2_overnight  +  k * sigma^2_open-to-close  +  (1-k) * sigma^2_RS

where:
    sigma^2_overnight     = Var( ln(O_t / C_{t-1}) )
    sigma^2_open-to-close = Var( ln(C_t / O_t) )
    sigma^2_RS = mean( ln(H/C)*ln(H/O) + ln(L/C)*ln(L/O) )       (Rogers-Satchell)
    k         = 0.34 / (1.34 + (n+1)/(n-1))

Drift-independent, gap-aware, ~14x more efficient than close-to-close.

Reference: Yang, D. and Zhang, Q. (2000). "Drift-independent volatility
estimation based on high, low, open, and close prices." Journal of
Business 73(3): 477-491.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def _yz_k(n: int) -> float:
    """Optimal weight k as a function of window length n."""
    if n <= 2:
        return 0.0
    return 0.34 / (1.34 + (n + 1) / (n - 1))


def yang_zhang_components(df: pd.DataFrame) -> pd.DataFrame:
    """Compute the three YZ variance components as daily series.

    Args:
        df: DataFrame with columns ['open', 'high', 'low', 'close'] sorted by date.
            One row per trading day.

    Returns:
        DataFrame with columns:
            r_overnight   = ln(O_t / C_{t-1})
            r_oc          = ln(C_t / O_t)
            rs_term       = Rogers-Satchell single-day variance term
    """
    o = df["open"].astype(float).values
    h = df["high"].astype(float).values
    l = df["low"].astype(float).values
    c = df["close"].astype(float).values

    # Overnight return: ln(O_t / C_{t-1})
    c_prev = np.concatenate([[np.nan], c[:-1]])
    with np.errstate(invalid="ignore", divide="ignore"):
        r_overnight = np.log(o / c_prev)
        r_oc = np.log(c / o)

        # Rogers-Satchell single-day term (variance estimator, drift-free)
        rs_term = (
            np.log(h / c) * np.log(h / o) + np.log(l / c) * np.log(l / o)
        )

    return pd.DataFrame({
        "r_overnight": r_overnight,
        "r_oc": r_oc,
        "rs_term": rs_term,
    }, index=df.index)


def yang_zhang_vol(df: pd.DataFrame, window: int,
                   annualize: bool = True,
                   periods_per_year: int = 252) -> pd.Series:
    """Rolling Yang-Zhang volatility (annualized by default).

    Args:
        df: OHLC DataFrame sorted by date.
        window: rolling window (e.g. 14, 22).
        annualize: if True, multiply by sqrt(periods_per_year).
        periods_per_year: 252 for daily.

    Returns:
        pd.Series of annualized YZ volatility, indexed like df.
    """
    if window < 3:
        raise ValueError(f"YZ window must be >= 3, got {window}")
    comp = yang_zhang_components(df)
    k = _yz_k(window)

    # Use sample variance (ddof=1) per Yang-Zhang convention.
    var_overnight = comp["r_overnight"].rolling(window).var(ddof=1)
    var_oc = comp["r_oc"].rolling(window).var(ddof=1)
    var_rs = comp["rs_term"].rolling(window).mean()  # RS is already a variance

    yz_var = var_overnight + k * var_oc + (1 - k) * var_rs
    yz_var = yz_var.clip(lower=0.0)  # numerical safety
    yz_vol = np.sqrt(yz_var)

    if annualize:
        yz_vol = yz_vol * np.sqrt(periods_per_year)
    return yz_vol


def yz_overnight_share(df: pd.DataFrame, window: int) -> pd.Series:
    """Share of YZ variance attributable to overnight returns.

    Returns a value in [0, 1]. High values indicate gap-dominated risk
    (typical of biotech, single-stock-news names). Low values indicate
    intraday-dominated risk (typical of liquid index components).

    A 5-day swing position spans 4 overnight windows — names with high
    overnight share carry materially more gap risk than ATR suggests.
    """
    if window < 3:
        raise ValueError(f"window must be >= 3, got {window}")
    comp = yang_zhang_components(df)
    k = _yz_k(window)

    var_overnight = comp["r_overnight"].rolling(window).var(ddof=1)
    var_oc = comp["r_oc"].rolling(window).var(ddof=1)
    var_rs = comp["rs_term"].rolling(window).mean()

    total = (var_overnight + k * var_oc + (1 - k) * var_rs).clip(lower=1e-12)
    share = (var_overnight / total).clip(0.0, 1.0)
    return share


def add_yz_features(df: pd.DataFrame, atr14_pct: pd.Series | None = None) -> pd.DataFrame:
    """Append the 4 YZ feature columns to a per-ticker OHLC DataFrame.

    Caller is responsible for grouping by ticker; this function operates
    on a single ticker's chronologically-sorted bars.

    New columns:
        yz_vol_14d            — annualized YZ vol, 14d window
        yz_vol_5d             — annualized YZ vol, 5d window
        yz_overnight_share    — overnight var / total var (14d)
        yz_vs_atr_ratio_14    — yz_vol_14d / (atr14_pct * sqrt(252)) if atr14_pct given
                                (otherwise NaN; window-matched orthogonality test)
    """
    out = df.copy()
    out["yz_vol_14d"] = yang_zhang_vol(df, window=14)
    out["yz_vol_5d"] = yang_zhang_vol(df, window=5)
    out["yz_overnight_share"] = yz_overnight_share(df, window=14)

    if atr14_pct is not None:
        atr_annualized = atr14_pct.astype(float) / 100.0 * np.sqrt(252)
        with np.errstate(invalid="ignore", divide="ignore"):
            out["yz_vs_atr_ratio_14"] = out["yz_vol_14d"] / atr_annualized.replace(0, np.nan)
    else:
        out["yz_vs_atr_ratio_14"] = np.nan
    return out
