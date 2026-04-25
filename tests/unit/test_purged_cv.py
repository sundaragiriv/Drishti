"""Unit tests for PurgedKFold splitter."""

import numpy as np
import pandas as pd
import pytest

from signal_scanner.intelligence.purged_cv import PurgedKFold


def _daily_dates(n: int, start: str = "2024-01-01") -> pd.Series:
    return pd.Series(pd.date_range(start=start, periods=n, freq="B"))


def test_split_count_matches_n_splits():
    dates = _daily_dates(100)
    cv = PurgedKFold(n_splits=5, label_horizon_days=5, embargo_days=5)
    splits = list(cv.split(dates))
    assert len(splits) == 5


def test_test_folds_cover_all_indices():
    dates = _daily_dates(50)
    cv = PurgedKFold(n_splits=5, label_horizon_days=3, embargo_days=3)
    test_idx_union = set()
    for _, test_idx in cv.split(dates):
        test_idx_union.update(test_idx.tolist())
    assert test_idx_union == set(range(50))


def test_test_folds_are_disjoint():
    dates = _daily_dates(50)
    cv = PurgedKFold(n_splits=5, label_horizon_days=3, embargo_days=3)
    seen = set()
    for _, test_idx in cv.split(dates):
        s = set(test_idx.tolist())
        assert seen.isdisjoint(s), "test folds overlap"
        seen.update(s)


def test_purge_excludes_horizon_overlap():
    """Row at t=10 with horizon=5 has label window [10,15]. If test fold
    contains t=14, that row must be purged from train."""
    dates = _daily_dates(20)  # 0..19
    cv = PurgedKFold(n_splits=4, label_horizon_days=5, embargo_days=0)
    splits = list(cv.split(dates))
    # Pick the fold that covers indices ~10-14 (mid)
    for train_idx, test_idx in splits:
        if 12 in test_idx:
            # Any index in train must have label_end < test_min OR date > test_max+embargo
            test_min = dates.iloc[test_idx].min()
            for ti in train_idx:
                label_end = dates.iloc[ti] + pd.Timedelta(days=5)
                assert (label_end < test_min) or (dates.iloc[ti] > dates.iloc[test_idx].max()), (
                    f"train row {ti} has label_end {label_end} overlapping test starting {test_min}"
                )
            return
    pytest.fail("expected to find a fold containing index 12")


def test_embargo_excludes_post_fold_window():
    dates = _daily_dates(40)
    cv = PurgedKFold(n_splits=4, label_horizon_days=2, embargo_days=5)
    for train_idx, test_idx in cv.split(dates):
        test_max = dates.iloc[test_idx].max()
        # No train index should be in (test_max, test_max + 5 days]
        for ti in train_idx:
            d = dates.iloc[ti]
            if d > test_max:
                gap_days = (d - test_max).days
                assert gap_days > 5, f"train row {ti} at {d} is within embargo after {test_max}"


def test_raises_on_too_few_samples():
    dates = _daily_dates(3)
    cv = PurgedKFold(n_splits=5, label_horizon_days=2)
    with pytest.raises(ValueError):
        list(cv.split(dates))
