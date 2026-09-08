"""Anti-Overfitting toolkit (MASTER SPEC §24).

Train/Validation/Test separation, walk-forward windows, parameter
sensitivity, minimum sample size, Monte Carlo shuffle test and a
multiple-hypothesis (Bonferroni) helper.  "大量に試せば偶然当たる" is what
this module exists to prevent.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Callable, Sequence

MIN_SAMPLE_TRADES = 30  # below this, results are noise (§24 minimum sample size)


@dataclass(frozen=True)
class Split:
    train: tuple[int, int]       # [start, end) indices
    validation: tuple[int, int]
    test: tuple[int, int]


def train_val_test_split(n: int, train_frac: float = 0.6,
                         val_frac: float = 0.2) -> Split:
    if n < 10:
        raise ValueError("not enough data to split")
    t_end = int(n * train_frac)
    v_end = int(n * (train_frac + val_frac))
    return Split(train=(0, t_end), validation=(t_end, v_end), test=(v_end, n))


@dataclass(frozen=True)
class WalkForwardWindow:
    train: tuple[int, int]
    test: tuple[int, int]


def walk_forward_windows(n: int, train_len: int, test_len: int,
                         embargo: int = 0) -> list[WalkForwardWindow]:
    """Rolling windows with an optional embargo gap between train and test
    to stop leakage across the boundary (§24 embargo)."""
    windows: list[WalkForwardWindow] = []
    start = 0
    while start + train_len + embargo + test_len <= n:
        t0, t1 = start, start + train_len
        s0 = t1 + embargo
        windows.append(WalkForwardWindow(train=(t0, t1), test=(s0, s0 + test_len)))
        start += test_len
    if not windows:
        raise ValueError("series too short for requested walk-forward windows")
    return windows


def parameter_sensitivity(returns_by_param: dict[float, float],
                          tolerance: float = 0.5) -> bool:
    """An edge that only exists at one parameter value is curve-fit (§24).
    Passes when neighbouring parameter values keep >= (1-tolerance) of the
    best return."""
    if len(returns_by_param) < 3:
        raise ValueError("need >= 3 parameter values to judge sensitivity")
    best_param = max(returns_by_param, key=returns_by_param.__getitem__)
    best = returns_by_param[best_param]
    if best <= 0:
        return False
    params = sorted(returns_by_param)
    idx = params.index(best_param)
    neighbours = [params[i] for i in (idx - 1, idx + 1) if 0 <= i < len(params)]
    return all(returns_by_param[p] >= best * (1 - tolerance) for p in neighbours)


def _hash_float(seed: str, i: int) -> float:
    h = int(hashlib.sha256(f"{seed}:{i}".encode()).hexdigest()[:8], 16)
    return h / 0xFFFFFFFF


def monte_carlo_p_value(trade_returns: Sequence[float], n_shuffles: int = 500,
                        seed: str = "mc") -> float:
    """Sign-shuffle test: how often does a random-signed version of these
    trades beat the actual total?  High p → the 'edge' is luck (§24)."""
    if len(trade_returns) < MIN_SAMPLE_TRADES:
        return 1.0  # insufficient sample: assume no edge
    actual = sum(trade_returns)
    beat = 0
    for s in range(n_shuffles):
        total = 0.0
        for i, r in enumerate(trade_returns):
            sign = 1.0 if _hash_float(f"{seed}:{s}", i) < 0.5 else -1.0
            total += abs(r) * sign
        if total >= actual:
            beat += 1
    return beat / n_shuffles


def bonferroni_threshold(alpha: float, n_hypotheses: int) -> float:
    """Multiple hypothesis correction (§24): more alphas tried → stricter bar."""
    if n_hypotheses < 1:
        raise ValueError("n_hypotheses >= 1")
    return alpha / n_hypotheses
