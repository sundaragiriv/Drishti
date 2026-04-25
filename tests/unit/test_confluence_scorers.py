"""Unit tests for ConfluenceEngine gradient scoring helpers.

Tests the per-factor scorers in isolation. These are pure functions
that take a TechnicalResult (and sometimes a GEXResult) and return
(long_pts, short_pts, breakdown_tuple). Locking down the gradient
buckets and sign conventions so future weight redistributions don't
silently change scoring outputs.
"""

from signal_scanner.config import ScannerConfig
from signal_scanner.core.confluence_engine import ConfluenceEngine
from signal_scanner.core.gex_calculator import GEXResult
from signal_scanner.core.technical_analyzer import TechnicalResult


def _t(**kw) -> TechnicalResult:
    return TechnicalResult(current_price=kw.pop("current_price", 100.0), **kw)


# ── _score_sma ─────────────────────────────────────────────────────────

def test_score_sma_neutral_when_no_pct():
    out = ConfluenceEngine._score_sma(_t(price_vs_sma_pct=None), max_pts=15)
    assert out == (0, 0, ("NEUTRAL", 0))


def test_score_sma_long_above_gradient_buckets():
    # < 0.5%: ratio 0.2 → 3 pts of 15
    long_pts, short_pts, _ = ConfluenceEngine._score_sma(
        _t(price_vs_sma="ABOVE", price_vs_sma_pct=0.3), max_pts=15
    )
    assert (long_pts, short_pts) == (3, 0)

    # 1-2%: ratio 0.6 → 9 pts
    long_pts, short_pts, _ = ConfluenceEngine._score_sma(
        _t(price_vs_sma="ABOVE", price_vs_sma_pct=1.5), max_pts=15
    )
    assert (long_pts, short_pts) == (9, 0)

    # 3%+: ratio 1.0 → 15 pts (max)
    long_pts, short_pts, _ = ConfluenceEngine._score_sma(
        _t(price_vs_sma="ABOVE", price_vs_sma_pct=5.0), max_pts=15
    )
    assert (long_pts, short_pts) == (15, 0)


def test_score_sma_short_below_mirrors_long():
    long_pts, short_pts, _ = ConfluenceEngine._score_sma(
        _t(price_vs_sma="BELOW", price_vs_sma_pct=-1.5), max_pts=15
    )
    assert (long_pts, short_pts) == (0, 9)


def test_score_sma_pdh_breakout_bonus():
    # 1.5% above SMA (9 pts base) + breaking PDH (+20% = +3) → 12 pts
    long_pts, _, _ = ConfluenceEngine._score_sma(
        _t(
            current_price=105.0,
            price_vs_sma="ABOVE",
            price_vs_sma_pct=1.5,
            prior_day_high=104.5,
            prior_day_close=103.0,
        ),
        max_pts=15,
    )
    assert long_pts == 12


def test_score_sma_capped_at_max_pts():
    # 5%+ already maxes at 15; bonus shouldn't push above
    long_pts, _, _ = ConfluenceEngine._score_sma(
        _t(
            current_price=110.0,
            price_vs_sma="ABOVE",
            price_vs_sma_pct=5.0,
            prior_day_high=109.0,
        ),
        max_pts=15,
    )
    assert long_pts == 15


# ── _score_gex ─────────────────────────────────────────────────────────

def test_score_gex_unknown_returns_zero():
    gex = GEXResult(gex_status="UNKNOWN", zero_gamma_level=None)
    out = ConfluenceEngine._score_gex(_t(), gex, max_pts=25)
    assert out == (0, 0, ("UNKNOWN", 0))


def test_score_gex_long_above_zero_gamma():
    # Price 105 vs ZG 100 = 5% above → ratio 1.0 → 25 pts long
    gex = GEXResult(gex_status="ABOVE_ZERO_GAMMA", zero_gamma_level=100.0)
    long_pts, short_pts, _ = ConfluenceEngine._score_gex(
        _t(current_price=105.0), gex, max_pts=25
    )
    assert (long_pts, short_pts) == (25, 0)


def test_score_gex_short_below_zero_gamma():
    # Price 99.7 vs ZG 100 = 0.3% below → ratio 0.3 → 7 pts short
    gex = GEXResult(gex_status="BELOW_ZERO_GAMMA", zero_gamma_level=100.0)
    long_pts, short_pts, _ = ConfluenceEngine._score_gex(
        _t(current_price=99.7), gex, max_pts=25
    )
    assert long_pts == 0
    assert short_pts == int(25 * 0.3)


def test_score_gex_invalid_prices_returns_unknown():
    gex = GEXResult(gex_status="ABOVE_ZERO_GAMMA", zero_gamma_level=100.0)
    out = ConfluenceEngine._score_gex(_t(current_price=0.0), gex, max_pts=25)
    assert out == (0, 0, ("UNKNOWN", 0))

    gex2 = GEXResult(gex_status="ABOVE_ZERO_GAMMA", zero_gamma_level=0.0)
    out2 = ConfluenceEngine._score_gex(_t(current_price=100.0), gex2, max_pts=25)
    assert out2 == (0, 0, ("UNKNOWN", 0))


# ── _score_rsi ─────────────────────────────────────────────────────────

def test_score_rsi_none_returns_neutral():
    out = ConfluenceEngine._score_rsi(_t(rsi=None), max_pts=20)
    assert out == (0, 0, ("NEUTRAL", 0))


def test_score_rsi_at_50_no_distance():
    long_pts, short_pts, label = ConfluenceEngine._score_rsi(_t(rsi=50.0), max_pts=20)
    # rsi == 50 → falls into "NEUTRAL" return at the bottom
    assert (long_pts, short_pts) == (0, 0)
    assert label == ("NEUTRAL", 0)


def test_score_rsi_long_bucketed_distance():
    # rsi 60: dist=10, bucket 8-15 → ratio 0.6 → 12 pts long
    long_pts, short_pts, _ = ConfluenceEngine._score_rsi(_t(rsi=60.0), max_pts=20)
    assert (long_pts, short_pts) == (12, 0)


def test_score_rsi_extreme_overbought_dampened():
    # rsi 80: dist=30 → ratio 1.0 then ×0.7 (overbought) → 14 pts long
    long_pts, _, _ = ConfluenceEngine._score_rsi(_t(rsi=80.0), max_pts=20)
    assert long_pts == 14


def test_score_rsi_short_below_50_with_negative_slope_bonus():
    # rsi 35: dist=15, bucket 15-25 → ratio 0.85 → 17 pts
    # rsi_slope < 0 confirms direction → +20% = 20 pts (max)
    long_pts, short_pts, _ = ConfluenceEngine._score_rsi(
        _t(rsi=35.0, rsi_slope=-0.5), max_pts=20
    )
    assert (long_pts, short_pts) == (0, 20)


def test_score_rsi_bull_divergence_bonus_long():
    # rsi 55, dist 5 → ratio 0.3 → 6 pts long; bull div +10% = +2 → 8 pts
    long_pts, _, _ = ConfluenceEngine._score_rsi(
        _t(rsi=55.0, rsi_bull_divergence=True), max_pts=20
    )
    assert long_pts == 8


# ── Smoke: full score() returns expected fields ─────────────────────────

def test_full_score_runs_with_minimal_inputs():
    """Ensure score() doesn't crash when most fields are None and the
    GEX-unavailable normalization path is exercised."""
    tech = _t(
        rsi=58.0,
        adx=25.0,
        atr=1.5,
        price_vs_sma="ABOVE",
        price_vs_sma_pct=1.0,
        vwap=99.5,
        vwap_status="ABOVE_VWAP",
        volume_ratio=1.5,
    )
    gex = GEXResult()  # UNKNOWN/None — triggers normalization branch
    res = ConfluenceEngine().score(tech, gex, ScannerConfig(), market_regime="NEUTRAL")

    assert res.score >= 0
    assert res.score <= 100
    assert res.signal in ("LONG", "SHORT", "NEUTRAL")
    assert res.recommendation in ("BUY", "SELL", "HOLD")
