"""
Purged K-Fold cross-validation with an embargo period.

Standard K-Fold leaks information in financial time series because:
  1. Labels are built from FORWARD windows (a sample at time t is labelled using
     prices up to t+h). If t is in the training fold and t+h is in the test fold,
     the training label overlaps the test period — leakage.
  2. Serial correlation around the train/test boundary lets the model "see across"
     the split even without direct overlap.

Fix (López de Prado, "Advances in Financial Machine Learning", ch. 7):
  - PURGE: drop training samples whose label window overlaps the test fold.
  - EMBARGO: additionally drop training samples within a small window AFTER the
    test fold to break residual serial correlation.

This module is pure-Python (no numpy/sklearn) so it has no import cost and can be
unit-tested anywhere. The training script consumes `purged_kfold_indices()`.
"""

from typing import Iterator, List, Tuple


def purged_kfold_indices(
    n_samples: int,
    n_splits: int = 5,
    label_horizon: int = 1,
    embargo_pct: float = 0.01,
) -> Iterator[Tuple[List[int], List[int]]]:
    """Yield (train_indices, test_indices) for each fold.

    Assumes samples are ordered chronologically and that sample i's label is
    realised by sample i + label_horizon (forward-looking label window).

    Args:
        n_samples:     total number of chronologically ordered samples.
        n_splits:      number of contiguous test folds.
        label_horizon: number of bars forward each label looks. Training samples
                       whose [i, i+label_horizon] window intersects the test fold
                       are purged.
        embargo_pct:   fraction of n_samples to embargo immediately AFTER each
                       test fold (e.g. 0.01 = 1%).

    Yields:
        (train_idx, test_idx) lists of integer indices.
    """
    if n_samples <= 0 or n_splits < 2:
        raise ValueError("need n_samples > 0 and n_splits >= 2")

    embargo = int(n_samples * embargo_pct)
    indices = list(range(n_samples))

    # Contiguous, (almost) equal-size test folds preserving time order.
    fold_sizes = [n_samples // n_splits] * n_splits
    for i in range(n_samples % n_splits):
        fold_sizes[i] += 1

    current = 0
    bounds = []
    for fs in fold_sizes:
        start = current
        stop = current + fs
        bounds.append((start, stop))
        current = stop

    for (test_start, test_stop) in bounds:
        test_idx = indices[test_start:test_stop]

        # Purge: remove any training sample whose forward label window
        # [i, i + label_horizon] overlaps the test fold, plus the embargo tail.
        train_idx = []
        purge_lo = test_start - label_horizon
        purge_hi = test_stop + embargo
        for i in indices:
            if test_start <= i < test_stop:
                continue  # the test fold itself
            label_end = i + label_horizon
            # overlap if the label window enters the purge band
            if label_end >= purge_lo and i < purge_hi:
                # sample sits inside [purge_lo, purge_hi) danger band → drop
                if purge_lo <= i < purge_hi or purge_lo <= label_end < purge_hi:
                    continue
            train_idx.append(i)

        yield train_idx, test_idx


def train_test_split_purged(
    n_samples: int,
    test_pct: float = 0.2,
    label_horizon: int = 1,
    embargo_pct: float = 0.01,
) -> Tuple[List[int], List[int]]:
    """Single chronological train/test split with purge+embargo for a final
    hold-out evaluation. Test is the most recent `test_pct` of the data.

    Returns (train_idx, test_idx).
    """
    if not 0.0 < test_pct < 1.0:
        raise ValueError("test_pct must be in (0,1)")
    embargo = int(n_samples * embargo_pct)
    test_start = int(n_samples * (1.0 - test_pct))
    test_idx = list(range(test_start, n_samples))

    # train is everything before test_start, purged of samples whose label
    # window would reach into the test set, plus an embargo gap.
    train_cutoff = test_start - label_horizon - embargo
    train_idx = list(range(0, max(0, train_cutoff)))
    return train_idx, test_idx
