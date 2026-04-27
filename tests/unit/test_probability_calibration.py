"""Unit tests for probability calibration helpers."""

import numpy as np
import pytest

from signal_scanner.intelligence.probability_calibration import (
    CalibrationBucket,
    calibrate,
    calibration_report,
    fit_isotonic,
)


def _synth(n: int = 1000, miscalibration: float = 0.2, seed: int = 42):
    """Generate (y_true, raw_proba) where raw_proba is systematically biased
    high by `miscalibration` on average. fit_isotonic should pull it back."""
    rng = np.random.default_rng(seed)
    actual = rng.uniform(0, 1, size=n)
    y = (rng.uniform(0, 1, size=n) < actual).astype(float)
    raw = np.clip(actual + miscalibration, 0.0, 1.0)
    return y, raw


def test_fit_isotonic_returns_increasing_mapping():
    y, raw = _synth(n=1000)
    iso = fit_isotonic(y, raw)
    test_grid = np.linspace(0.0, 1.0, 21)
    out = iso.predict(test_grid)
    # Isotonic ⇒ monotonically non-decreasing
    assert np.all(np.diff(out) >= -1e-9)


def test_calibrate_clips_to_unit_interval():
    y, raw = _synth(n=500)
    iso = fit_isotonic(y, raw)
    extreme = np.array([-0.5, 0.0, 1.0, 1.5])
    out = calibrate(iso, extreme)
    assert np.all((out >= 0.0) & (out <= 1.0))


def test_fit_isotonic_rejects_small_sample():
    y = np.array([0, 1, 0])
    raw = np.array([0.2, 0.8, 0.4])
    with pytest.raises(ValueError):
        fit_isotonic(y, raw)


def test_fit_isotonic_rejects_length_mismatch():
    with pytest.raises(ValueError):
        fit_isotonic(np.zeros(100), np.zeros(99))


def test_calibration_reduces_mean_abs_error():
    """Synthetic case: raw is biased +0.2, isotonic should fix most of it."""
    y, raw = _synth(n=2000, miscalibration=0.2)
    iso = fit_isotonic(y, raw)
    buckets = calibration_report(y, raw, iso, n_buckets=10)
    n_total = sum(b.n for b in buckets)
    raw_mae = sum(abs(b.error_raw) * b.n for b in buckets) / n_total
    cal_mae = sum(abs(b.error_calibrated) * b.n for b in buckets) / n_total
    assert cal_mae < raw_mae, (
        f"calibration should reduce mean abs error: raw={raw_mae:.3f} "
        f"cal={cal_mae:.3f}"
    )


def test_report_skips_empty_buckets():
    # All probas in [0.4, 0.5) — most buckets empty
    n = 500
    y = np.zeros(n)
    raw = np.full(n, 0.45)
    buckets = calibration_report(y, raw, iso=None, n_buckets=10)
    # Only the bucket containing 0.45 should appear
    assert len(buckets) == 1
    assert buckets[0].bucket_low <= 0.45 < buckets[0].bucket_high
    assert buckets[0].n == n


def test_calibration_bucket_dataclass_fields():
    b = CalibrationBucket(
        bucket_low=0.7, bucket_high=0.8, n=100,
        raw_mean=0.75, actual_hit_rate=0.60,
        calibrated_mean=0.62, error_raw=0.15, error_calibrated=0.02,
    )
    assert b.error_raw > b.error_calibrated
