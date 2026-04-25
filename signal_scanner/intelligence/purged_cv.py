"""Purged K-Fold cross-validation with embargo.

Implements López de Prado, "Advances in Financial Machine Learning"
Ch. 7. Standard CV biases AUC upward when labels span multiple bars
because consecutive observations share future-return paths. Purged CV
fixes this by:

  1. PURGING: drop training rows whose label horizon overlaps with the
     test fold's date range.
  2. EMBARGOING: drop training rows that fall within `embargo_days`
     after the test fold (the post-fold window is contaminated by
     leakage in the opposite direction — features computed from prices
     close to the test fold may already encode info from inside it).

Designed for daily bar data. `dates` should be a pandas Series of
datetime-like values aligned with the feature/label arrays.

Usage:
    cv = PurgedKFold(n_splits=5, label_horizon_days=5, embargo_days=5)
    for train_idx, test_idx in cv.split(dates):
        ...
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator, Tuple

import numpy as np
import pandas as pd


@dataclass
class PurgedKFold:
    """Time-ordered K-fold splitter with purging and embargo.

    Args:
        n_splits: Number of folds. Each fold = a contiguous date range.
        label_horizon_days: How many trading days forward the label looks.
            Used to purge rows whose label window enters a test fold.
        embargo_days: Trading days after each test fold to drop from
            subsequent training (default = label_horizon_days).
    """
    n_splits: int = 5
    label_horizon_days: int = 5
    embargo_days: int | None = None  # None → use label_horizon_days

    def split(self, dates: pd.Series) -> Iterator[Tuple[np.ndarray, np.ndarray]]:
        """Yield (train_idx, test_idx) for each fold.

        `dates` must be a pandas Series of datetime values aligned with
        feature / label arrays (one entry per row).
        """
        embargo = self.embargo_days if self.embargo_days is not None else self.label_horizon_days
        dates = pd.to_datetime(dates).reset_index(drop=True)
        n = len(dates)
        if n < self.n_splits:
            raise ValueError(f"Not enough samples ({n}) for {self.n_splits} splits")

        # Order indices chronologically
        order = np.argsort(dates.values)
        # Split chronologically into n_splits contiguous chunks
        fold_sizes = np.full(self.n_splits, n // self.n_splits, dtype=int)
        fold_sizes[: n % self.n_splits] += 1

        starts = np.cumsum(np.concatenate([[0], fold_sizes[:-1]]))
        ends = starts + fold_sizes

        for fold_i in range(self.n_splits):
            test_indices = order[starts[fold_i]: ends[fold_i]]
            test_dates = dates.iloc[test_indices]
            test_min, test_max = test_dates.min(), test_dates.max()

            # Train candidates = all rows
            all_idx = np.arange(n)

            # Purge: drop rows whose label window overlaps with the test fold.
            # A row at date t has label looking forward to t + horizon.
            # If t + horizon >= test_min and t <= test_max, the label is
            # contaminated by data from the test fold.
            label_end = dates + pd.Timedelta(days=self.label_horizon_days)
            purge_mask = (label_end >= test_min) & (dates <= test_max)

            # Embargo: drop rows that fall within `embargo` days AFTER
            # the test fold ends (these rows' features may have been
            # influenced by data inside the test fold via slow indicators).
            embargo_end = test_max + pd.Timedelta(days=embargo)
            embargo_mask = (dates > test_max) & (dates <= embargo_end)

            train_mask = ~purge_mask & ~embargo_mask
            # Exclude test indices themselves
            train_mask.iloc[test_indices] = False

            train_idx = all_idx[train_mask.values]
            yield train_idx, test_indices

    def get_n_splits(self) -> int:
        return self.n_splits
