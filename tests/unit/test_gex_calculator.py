"""Unit tests for GEXCalculator pure-function math.

These cover the deterministic helpers — sign convention, merge math,
and zero-gamma linear interpolation. They do NOT validate that the
formulation is *correct* (the OI-only assumption is broken; see review
doc). They lock down what the code currently does so refactors don't
silently change scoring outputs.
"""

import numpy as np
import pandas as pd

from signal_scanner.core.gex_calculator import GEXCalculator


def _df(strikes, gammas, oi):
    return pd.DataFrame({"strike": strikes, "gamma": gammas, "openInterest": oi})


def test_compute_strike_gex_call_positive_sign():
    df = _df([100.0, 110.0], [0.05, 0.04], [1000, 500])
    out = GEXCalculator._compute_strike_gex(df, is_call=True)
    assert list(out["strike"]) == [100.0, 110.0]
    # gex = +1 * gamma * OI * K^2 * 100
    assert out["gex"].iloc[0] == 0.05 * 1000 * 100 ** 2 * 100
    assert out["gex"].iloc[1] == 0.04 * 500 * 110 ** 2 * 100
    assert (out["gex"] > 0).all()


def test_compute_strike_gex_put_negative_sign():
    df = _df([100.0], [0.05], [1000])
    out = GEXCalculator._compute_strike_gex(df, is_call=False)
    assert out["gex"].iloc[0] == -1 * 0.05 * 1000 * 100 ** 2 * 100
    assert (out["gex"] < 0).all()


def test_compute_strike_gex_handles_nan_gamma_and_oi():
    df = pd.DataFrame({
        "strike": [100.0, 110.0],
        "gamma": [np.nan, 0.04],
        "openInterest": [1000, np.nan],
    })
    out = GEXCalculator._compute_strike_gex(df, is_call=True)
    assert out["gex"].iloc[0] == 0  # NaN gamma -> 0
    assert out["gex"].iloc[1] == 0  # NaN OI -> 0


def test_compute_strike_gex_empty_or_missing_columns():
    empty = pd.DataFrame()
    assert GEXCalculator._compute_strike_gex(empty, True).empty

    missing = pd.DataFrame({"strike": [100.0]})  # no gamma/openInterest cols
    assert GEXCalculator._compute_strike_gex(missing, True).empty


def test_merge_gex_outer_join_and_net_calc():
    calls = pd.DataFrame({"strike": [100.0, 110.0], "gex": [50.0, 30.0]})
    puts = pd.DataFrame({"strike": [100.0, 90.0], "gex": [-40.0, -20.0]})

    merged = GEXCalculator._merge_gex(calls, puts)

    # Outer join → 3 strikes (90, 100, 110)
    assert len(merged) == 3
    assert list(merged["strike"]) == [90.0, 100.0, 110.0]

    row100 = merged.loc[merged["strike"] == 100.0].iloc[0]
    assert row100["call_gex"] == 50.0
    assert row100["put_gex"] == -40.0
    assert row100["net_gex"] == 10.0

    # Strike present only on one side gets 0 on missing side
    row90 = merged.loc[merged["strike"] == 90.0].iloc[0]
    assert row90["call_gex"] == 0
    assert row90["put_gex"] == -20.0


def test_merge_gex_both_empty_returns_empty_with_schema():
    out = GEXCalculator._merge_gex(pd.DataFrame(), pd.DataFrame())
    assert out.empty
    assert set(out.columns) == {"strike", "call_gex", "put_gex", "net_gex"}


def test_find_zero_gamma_linear_interpolation():
    # Net GEX flips negative -> positive between strikes 100 and 110.
    # At -50 -> +50 the zero crossing is exactly halfway = 105.
    net_gex = pd.DataFrame({
        "strike": [90.0, 100.0, 110.0, 120.0],
        "net_gex": [-100.0, -50.0, 50.0, 100.0],
    })
    zg = GEXCalculator._find_zero_gamma(net_gex)
    assert zg is not None
    assert abs(zg - 105.0) < 1e-9


def test_find_zero_gamma_asymmetric_slope():
    # Crossing between (100, -10) and (110, 90): zero at 100 + 10*(10/100) = 101
    net_gex = pd.DataFrame({
        "strike": [100.0, 110.0],
        "net_gex": [-10.0, 90.0],
    })
    zg = GEXCalculator._find_zero_gamma(net_gex)
    assert abs(zg - 101.0) < 1e-9


def test_find_zero_gamma_no_sign_change_returns_none():
    # All positive
    net_gex = pd.DataFrame({"strike": [100.0, 110.0], "net_gex": [10.0, 20.0]})
    assert GEXCalculator._find_zero_gamma(net_gex) is None


def test_find_zero_gamma_too_few_rows_returns_none():
    net_gex = pd.DataFrame({"strike": [100.0], "net_gex": [-10.0]})
    assert GEXCalculator._find_zero_gamma(net_gex) is None


def test_find_zero_gamma_picks_first_crossing_when_multiple():
    # Two crossings: at 105 and 125. Spec says first crossing wins.
    net_gex = pd.DataFrame({
        "strike": [100.0, 110.0, 120.0, 130.0],
        "net_gex": [-50.0, 50.0, -50.0, 50.0],
    })
    zg = GEXCalculator._find_zero_gamma(net_gex)
    assert abs(zg - 105.0) < 1e-9
