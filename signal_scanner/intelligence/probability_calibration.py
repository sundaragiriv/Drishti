"""Isotonic calibration for ML model probabilities.

A LightGBM classifier's `predict_proba` outputs are well-ranked but
poorly calibrated — when it says "80% probability", the actual hit
rate at that threshold is rarely 80%. Isotonic regression maps raw
probabilities to actual hit rates using a held-out validation set.

After calibration, "73% confidence" on the dashboard means the trade
historically won 73% of the time at that score.

Reference: Niculescu-Mizil & Caruana (2005), "Predicting Good
Probabilities With Supervised Learning."

Usage:
    from sklearn.isotonic import IsotonicRegression
    from signal_scanner.intelligence.probability_calibration import (
        fit_isotonic, calibrate, calibration_report,
    )

    # Train iso on validation fold
    iso = fit_isotonic(y_val, val_proba)

    # At inference, transform raw model probs
    calibrated = calibrate(iso, raw_proba)

    # Diagnostic: bucket and compare
    report = calibration_report(y_val, val_proba, iso)
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

import numpy as np

try:
    from sklearn.isotonic import IsotonicRegression
    _SKLEARN_OK = True
except ImportError:
    IsotonicRegression = None  # type: ignore
    _SKLEARN_OK = False


@dataclass
class CalibrationBucket:
    """One row in the calibration report."""
    bucket_low: float
    bucket_high: float
    n: int
    raw_mean: float          # avg raw probability in this bucket
    actual_hit_rate: float   # observed positive rate
    calibrated_mean: float   # avg post-iso probability
    error_raw: float         # raw_mean - actual_hit_rate (signed)
    error_calibrated: float  # calibrated_mean - actual_hit_rate (signed)


def fit_isotonic(y_true: np.ndarray, y_proba: np.ndarray) -> "IsotonicRegression":
    """Fit isotonic regression on (raw probability, actual outcome)."""
    if not _SKLEARN_OK:
        raise RuntimeError("scikit-learn is required for calibration")
    y_true = np.asarray(y_true, dtype=float)
    y_proba = np.asarray(y_proba, dtype=float)
    if len(y_true) != len(y_proba):
        raise ValueError("y_true and y_proba must have equal length")
    if len(y_true) < 100:
        raise ValueError(
            f"Calibration sample too small ({len(y_true)}). Need >=100."
        )
    iso = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0)
    iso.fit(y_proba, y_true)
    return iso


def calibrate(iso: "IsotonicRegression", proba: np.ndarray) -> np.ndarray:
    """Apply fitted isotonic to raw probabilities."""
    return iso.predict(np.asarray(proba, dtype=float))


def calibration_report(y_true: np.ndarray, y_proba_raw: np.ndarray,
                       iso: Optional["IsotonicRegression"] = None,
                       n_buckets: int = 10) -> List[CalibrationBucket]:
    """Bucket probabilities into deciles, report raw vs actual vs calibrated."""
    y_true = np.asarray(y_true, dtype=float)
    y_proba_raw = np.asarray(y_proba_raw, dtype=float)
    if iso is not None:
        y_proba_cal = calibrate(iso, y_proba_raw)
    else:
        y_proba_cal = y_proba_raw

    # Equal-width buckets in [0, 1]
    edges = np.linspace(0.0, 1.0, n_buckets + 1)
    buckets: List[CalibrationBucket] = []
    for i in range(n_buckets):
        lo, hi = edges[i], edges[i + 1]
        mask = (y_proba_raw >= lo) & (y_proba_raw < hi if i < n_buckets - 1
                                       else y_proba_raw <= hi)
        n = int(mask.sum())
        if n == 0:
            continue
        raw_mean = float(y_proba_raw[mask].mean())
        actual = float(y_true[mask].mean())
        cal_mean = float(y_proba_cal[mask].mean())
        buckets.append(CalibrationBucket(
            bucket_low=lo,
            bucket_high=hi,
            n=n,
            raw_mean=raw_mean,
            actual_hit_rate=actual,
            calibrated_mean=cal_mean,
            error_raw=raw_mean - actual,
            error_calibrated=cal_mean - actual,
        ))
    return buckets


def print_calibration_report(buckets: List[CalibrationBucket]) -> None:
    """Stdout-friendly tabular dump."""
    print(f"{'Bucket':>12} {'N':>7} {'Raw':>7} {'Actual':>7} {'Calibd':>7} "
          f"{'ErrRaw':>8} {'ErrCal':>8}")
    print("-" * 65)
    sum_err_raw = 0.0
    sum_err_cal = 0.0
    n_total = 0
    for b in buckets:
        print(f"  {b.bucket_low:.2f}-{b.bucket_high:.2f}  {b.n:>7,} "
              f"{b.raw_mean:>6.3f} {b.actual_hit_rate:>6.3f} "
              f"{b.calibrated_mean:>6.3f} {b.error_raw:>+7.3f} "
              f"{b.error_calibrated:>+7.3f}")
        sum_err_raw += abs(b.error_raw) * b.n
        sum_err_cal += abs(b.error_calibrated) * b.n
        n_total += b.n
    if n_total:
        print(f"\n  Mean abs calibration error (raw): {sum_err_raw/n_total:.4f}")
        print(f"  Mean abs calibration error (cal): {sum_err_cal/n_total:.4f}")
