"""Unit tests for Yang-Zhang volatility implementation."""
import numpy as np
import pandas as pd
import pytest

from signal_scanner.features.yang_zhang import (
    _yz_k,
    add_yz_features,
    yang_zhang_components,
    yang_zhang_vol,
    yz_overnight_share,
)


def _synth_ohlc(n: int = 300, seed: int = 42, sigma: float = 0.02) -> pd.DataFrame:
    """Manufacture deterministic OHLC bars from a GBM close-price series."""
    rng = np.random.default_rng(seed)
    log_rets = rng.normal(0.0, sigma, n)
    close = 100.0 * np.exp(np.cumsum(log_rets))
    # Add small intraday noise to construct OHL from close
    intraday = rng.normal(0.0, sigma * 0.4, (n, 4))
    open_ = close * np.exp(intraday[:, 0])
    high = np.maximum.reduce([
        close * np.exp(np.abs(intraday[:, 1])),
        open_ * np.exp(np.abs(intraday[:, 2])),
        close, open_,
    ])
    low = np.minimum.reduce([
        close * np.exp(-np.abs(intraday[:, 1])),
        open_ * np.exp(-np.abs(intraday[:, 3])),
        close, open_,
    ])
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close})


def test_yz_k_within_bounds():
    # k must be in (0, 0.34) for n in (3, 252)
    for n in (3, 5, 14, 22, 64, 252):
        k = _yz_k(n)
        assert 0.0 < k < 0.34


def test_yz_components_shape():
    df = _synth_ohlc(100)
    comp = yang_zhang_components(df)
    assert list(comp.columns) == ["r_overnight", "r_oc", "rs_term"]
    assert len(comp) == 100
    # First row's overnight return is NaN (no prior close)
    assert pd.isna(comp["r_overnight"].iloc[0])


def test_rs_term_nonnegative_in_expectation():
    """Rogers-Satchell term is a positive variance — its rolling mean
    should stay non-negative (single days can be slightly negative due to
    the cross-product structure, but the mean shouldn't be)."""
    df = _synth_ohlc(500)
    comp = yang_zhang_components(df)
    assert comp["rs_term"].rolling(50).mean().dropna().min() > 0


def test_yz_vol_is_positive_and_reasonable():
    # Generate with sigma=0.02 (per-day) → annualized ~ 0.02 * sqrt(252) ≈ 0.317
    df = _synth_ohlc(500, sigma=0.02)
    yz = yang_zhang_vol(df, window=22, annualize=True)
    yz_clean = yz.dropna()
    assert (yz_clean > 0).all()
    # Should land in the right ballpark — relax to 0.15 - 0.55
    assert 0.15 < yz_clean.mean() < 0.55


def test_yz_vol_annualization_toggle():
    df = _synth_ohlc(500, sigma=0.02)
    yz_a = yang_zhang_vol(df, window=22, annualize=True).dropna()
    yz_d = yang_zhang_vol(df, window=22, annualize=False).dropna()
    ratio = (yz_a / yz_d).mean()
    assert abs(ratio - np.sqrt(252)) < 0.01


def test_yz_vol_window_validates():
    df = _synth_ohlc(50)
    with pytest.raises(ValueError):
        yang_zhang_vol(df, window=2)


def test_overnight_share_in_unit_interval():
    df = _synth_ohlc(500)
    share = yz_overnight_share(df, window=22).dropna()
    assert (share >= 0.0).all()
    assert (share <= 1.0).all()


def test_overnight_share_higher_for_gappy_stock():
    """A stock with large gaps but quiet intraday should have higher overnight share."""
    rng = np.random.default_rng(7)
    n = 300
    # Quiet stock: small intraday + small overnight
    quiet_close = 100.0 * np.exp(np.cumsum(rng.normal(0, 0.005, n)))
    quiet = pd.DataFrame({
        "open": quiet_close * np.exp(rng.normal(0, 0.001, n)),
        "high": quiet_close * np.exp(np.abs(rng.normal(0, 0.002, n))),
        "low":  quiet_close * np.exp(-np.abs(rng.normal(0, 0.002, n))),
        "close": quiet_close,
    })
    # Gappy stock: big overnight, small intraday
    gappy_close = 100.0 * np.exp(np.cumsum(rng.normal(0, 0.025, n)))
    gappy = pd.DataFrame({
        "open": gappy_close * np.exp(rng.normal(0, 0.020, n)),
        "high": gappy_close * np.exp(np.abs(rng.normal(0, 0.005, n))),
        "low":  gappy_close * np.exp(-np.abs(rng.normal(0, 0.005, n))),
        "close": gappy_close,
    })
    quiet_share = yz_overnight_share(quiet, window=30).dropna().mean()
    gappy_share = yz_overnight_share(gappy, window=30).dropna().mean()
    # Gappy stock should have visibly higher overnight share
    assert gappy_share > quiet_share
    assert (gappy_share - quiet_share) > 0.10


def test_add_yz_features_columns():
    df = _synth_ohlc(200)
    atr_pct = pd.Series(np.full(len(df), 2.0))  # 2% ATR/close
    out = add_yz_features(df, atr14_pct=atr_pct)
    for col in ("yz_vol_14d", "yz_vol_5d", "yz_overnight_share", "yz_vs_atr_ratio_14"):
        assert col in out.columns
    # Ratio should be a reasonable positive number once warmed up
    ratio_clean = out["yz_vs_atr_ratio_14"].dropna()
    assert (ratio_clean > 0).all()
    assert ratio_clean.mean() > 0.5  # YZ shouldn't collapse vs ATR
    assert ratio_clean.mean() < 5.0
