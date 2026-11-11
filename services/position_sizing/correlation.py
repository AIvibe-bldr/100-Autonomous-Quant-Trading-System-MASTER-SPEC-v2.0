"""Correlation of a candidate against the current book (MASTER SPEC §38).

Pure function over daily return series. It produces the single number
`PortfolioContext.correlation_to_book` documents — the max |corr| of the
candidate against any holding — which `PositionSizingEngine` compares with
`RiskConfig.correlation_penalty_threshold`.

An unmeasurable holding (too little data, or a flat series) is treated as
fully correlated (1.0), never as 0: "we could not tell" must not read as
"diversified"."""
from __future__ import annotations

import math

MIN_OBSERVATIONS = 15


def _pearson(xs: list[float], ys: list[float]) -> float | None:
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx <= 0 or syy <= 0:
        return None
    r = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / math.sqrt(sxx * syy)
    return max(-1.0, min(1.0, r))


def max_abs_correlation_to_book(candidate: list[float],
                                holdings: dict[str, list[float]]) -> float:
    """Series are aligned from the most recent observation backwards."""
    worst = 0.0
    for series in holdings.values():
        n = min(len(candidate), len(series))
        if n < MIN_OBSERVATIONS:
            return 1.0
        r = _pearson(candidate[-n:], series[-n:])
        if r is None:
            return 1.0
        worst = max(worst, abs(r))
    return worst
