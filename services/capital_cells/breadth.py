"""Opportunity Breadth / Effective Independent Cells (docs/capital_cell_architecture.md
§17-18, §41 priority 10).

Both sections reduce to the same question — "given N things (candidate
opportunities, or active cells) and how correlated they are, how many of
them are actually independent?" — so this module implements the counting
primitives once and exposes them under both names (§17's Effective
Opportunity Count, §18's Effective Independent Cells) rather than
duplicating the math.

§18 is explicit that no single method may be the permanent answer:
"Eigenvalue based effective rank", "Correlation cluster count" and
"Risk-factor clustering" are all named as candidates to keep comparable, not
to pick one and discard the others. All three are implemented here and
returned side by side.

This repo has no numpy dependency (`pyproject.toml` only lists pydantic +
optional extras), so `_symmetric_eigenvalues` is a small pure-Python Jacobi
eigenvalue solver — fine at the cell/opportunity counts this system deals
in (tens, not thousands).
"""
from __future__ import annotations

import enum
import math
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from services.capital_cells.correlation import RegimeCorrelation
from services.capital_cells.estimation import InsufficientDataError, PointEstimate

EIGENVALUE_METHOD_VERSION = "EIGENVALUE_EFFECTIVE_RANK_V1"
CORRELATION_CLUSTER_METHOD_VERSION = "CORRELATION_CLUSTER_COUNT_V1"
FACTOR_CLUSTER_METHOD_VERSION = "RISK_FACTOR_CLUSTERING_V1"

DEFAULT_CLUSTER_THRESHOLD = 0.7   # |correlation| / cosine similarity at or above this = "same cluster"


class EffectiveCountMethod(str, enum.Enum):
    EIGENVALUE_EFFECTIVE_RANK = "EIGENVALUE_EFFECTIVE_RANK"
    CORRELATION_CLUSTER_COUNT = "CORRELATION_CLUSTER_COUNT"
    RISK_FACTOR_CLUSTERING = "RISK_FACTOR_CLUSTERING"


@dataclass(frozen=True)
class EffectiveCountEstimate:
    """§18: multiple methods, kept comparable side by side — never
    collapsed to one永久固定 (permanently fixed) number."""

    by_method: dict[EffectiveCountMethod, PointEstimate]

    def method(self, m: EffectiveCountMethod) -> PointEstimate:
        try:
            return self.by_method[m]
        except KeyError:
            raise InsufficientDataError(f"no estimate for method {m.value}") from None


def _correlation_confidence(regime: RegimeCorrelation, min_sample_size: int) -> float:
    return min(1.0, regime.sample_count / min_sample_size)


def _require_measured_pairs(item_ids: list[str], regime: RegimeCorrelation) -> None:
    """Every pair among `item_ids` must have been measured. Without this,
    `RegimeCorrelation.get`'s 0.0 default silently reads an unmeasured pair
    as perfectly uncorrelated, so adding an item with NO data at all RAISES
    the effective-independence count — claiming diversification that was
    never observed. §19 forbids exactly that inference ("different name" is
    not evidence of independence); it applies just as much to counting
    cells (§18) as to comparing alphas."""
    missing = [(a, b) for i, a in enumerate(item_ids) for b in item_ids[i + 1:]
               if not regime.has(a, b)]
    if missing:
        raise InsufficientDataError(
            f"no measured correlation for pairs {missing} — an unmeasured pair "
            f"is missing data, not evidence of independence (§18-19)")


def _symmetric_eigenvalues(matrix: list[list[float]], max_iter: int = 200,
                           tol: float = 1e-10) -> list[float]:
    """Eigenvalues of a real symmetric matrix via the classic cyclic-Jacobi
    rotation method, largest-off-diagonal-pivot variant, using the
    numerically stable rotation update (Golub & Van Loan / Numerical
    Recipes `jacobi`): solving directly for `t = tan(theta)` from the
    quadratic `t^2 + 2*t*tau - 1 = 0` (choosing the root of smaller
    magnitude) avoids the `atan2`/`sin`/`cos` formulation, whose naive
    sign convention is easy to get wrong (verified against known matrices
    in tests/unit/test_capital_cell_breadth.py — an earlier atan2-based
    version of this function passed review but silently returned the wrong
    eigenvalues for anything larger than 2x2)."""
    n = len(matrix)
    a = [row[:] for row in matrix]
    for _ in range(max_iter):
        p, q, max_val = 0, 1, 0.0
        for i in range(n):
            for j in range(i + 1, n):
                if abs(a[i][j]) > max_val:
                    max_val, p, q = abs(a[i][j]), i, j
        if max_val < tol:
            break
        app, aqq, apq = a[p][p], a[q][q], a[p][q]
        tau = (aqq - app) / (2.0 * apq)
        t = (1.0 if tau >= 0 else -1.0) / (abs(tau) + math.sqrt(1.0 + tau * tau))
        c = 1.0 / math.sqrt(1.0 + t * t)
        s = t * c
        a[p][p] = app - t * apq
        a[q][q] = aqq + t * apq
        a[p][q] = a[q][p] = 0.0
        s_over_1_plus_c = s / (1.0 + c)
        for i in range(n):
            if i != p and i != q:
                aip, aiq = a[i][p], a[i][q]
                a[i][p] = a[p][i] = aip - s * (aiq + s_over_1_plus_c * aip)
                a[i][q] = a[q][i] = aiq + s * (aip - s_over_1_plus_c * aiq)
    return [a[i][i] for i in range(n)]


def _connected_component_count(item_ids: list[str], similarity: "callable",
                                threshold: float) -> int:
    """Union-find over pairs whose similarity meets `threshold`. Two items
    below threshold on every pair are their own singleton clusters."""
    parent = {item: item for item in item_ids}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x: str, y: str) -> None:
        rx, ry = find(x), find(y)
        if rx != ry:
            parent[rx] = ry

    for i, a in enumerate(item_ids):
        for b in item_ids[i + 1:]:
            if similarity(a, b) >= threshold:
                union(a, b)
    return len({find(item) for item in item_ids})


def eigenvalue_effective_rank(item_ids: list[str], regime: RegimeCorrelation,
                              as_of: datetime,
                              min_sample_size: int = 20) -> PointEstimate:
    """Participation ratio N^2 / sum(lambda_i^2) of the correlation matrix.
    N (fully independent, identity matrix) when every eigenvalue is equal;
    1 (fully redundant) when one eigenvalue carries the entire trace."""
    n = len(item_ids)
    if n == 0:
        raise InsufficientDataError("no items to evaluate")
    if n == 1:
        return PointEstimate(value=1.0, confidence=1.0, sample_size=0,
                             method_version=EIGENVALUE_METHOD_VERSION, as_of=as_of)
    _require_measured_pairs(item_ids, regime)
    matrix = [[regime.get(a, b) for b in item_ids] for a in item_ids]
    eigenvalues = _symmetric_eigenvalues(matrix)
    sum_sq = sum(e * e for e in eigenvalues)
    effective = (n * n) / sum_sq if sum_sq > 1e-12 else 1.0
    effective = max(1.0, min(float(n), effective))
    return PointEstimate(
        value=effective, confidence=_correlation_confidence(regime, min_sample_size),
        sample_size=regime.sample_count, method_version=EIGENVALUE_METHOD_VERSION, as_of=as_of)


def correlation_cluster_count(item_ids: list[str], regime: RegimeCorrelation, as_of: datetime,
                              threshold: float = DEFAULT_CLUSTER_THRESHOLD,
                              min_sample_size: int = 20) -> PointEstimate:
    """Connected-components count under |correlation| >= threshold."""
    n = len(item_ids)
    if n == 0:
        raise InsufficientDataError("no items to evaluate")
    if n == 1:
        return PointEstimate(value=1.0, confidence=1.0, sample_size=0,
                             method_version=CORRELATION_CLUSTER_METHOD_VERSION, as_of=as_of)
    _require_measured_pairs(item_ids, regime)
    count = _connected_component_count(
        item_ids, lambda a, b: abs(regime.get(a, b)), threshold)
    return PointEstimate(
        value=float(count), confidence=_correlation_confidence(regime, min_sample_size),
        sample_size=regime.sample_count, method_version=CORRELATION_CLUSTER_METHOD_VERSION,
        as_of=as_of)


def risk_factor_cluster_count(item_ids: list[str], factor_exposures: dict[str, dict[str, float]],
                              as_of: datetime, threshold: float = DEFAULT_CLUSTER_THRESHOLD,
                              sample_size: int = 0, confidence: float = 1.0) -> PointEstimate:
    """Connected-components count under factor-exposure cosine similarity
    >= threshold. Independent of return correlation entirely — two cells
    can have uncorrelated historical returns yet share the same underlying
    risk factors (and vice versa), which is exactly why §18 asks for this
    as a distinct method rather than a re-derivation of the correlation one."""
    n = len(item_ids)
    if n == 0:
        raise InsufficientDataError("no items to evaluate")
    # A present-but-degenerate exposure vector (empty, or all zeros) is not a
    # measurement: cosine similarity against it is 0 for every peer, so the
    # item would read as independent of the entire book and inflate the
    # count. Same trap as an unmeasured correlation pair (§18-19) — checking
    # only that the key exists is not enough.
    missing = [i for i in item_ids
               if i not in factor_exposures
               or not any(v != 0.0 for v in factor_exposures[i].values())]
    if missing:
        raise InsufficientDataError(
            f"missing or all-zero factor exposures for {missing} — no measured "
            f"exposure is missing data, not evidence of independence (§18-19)")
    if n == 1:
        return PointEstimate(value=1.0, confidence=1.0, sample_size=sample_size,
                             method_version=FACTOR_CLUSTER_METHOD_VERSION, as_of=as_of)

    def cosine(a: str, b: str) -> float:
        va, vb = factor_exposures[a], factor_exposures[b]
        factors = set(va) | set(vb)
        dot = sum(va.get(f, 0.0) * vb.get(f, 0.0) for f in factors)
        na = math.sqrt(sum(v * v for v in va.values()))
        nb = math.sqrt(sum(v * v for v in vb.values()))
        if na <= 0 or nb <= 0:
            return 0.0
        return dot / (na * nb)

    count = _connected_component_count(item_ids, lambda a, b: abs(cosine(a, b)), threshold)
    return PointEstimate(value=float(count), confidence=confidence, sample_size=sample_size,
                         method_version=FACTOR_CLUSTER_METHOD_VERSION, as_of=as_of)


def _combine(item_ids: list[str], regime: Optional[RegimeCorrelation],
            factor_exposures: Optional[dict[str, dict[str, float]]], as_of: datetime,
            threshold: float, min_sample_size: int) -> EffectiveCountEstimate:
    by_method: dict[EffectiveCountMethod, PointEstimate] = {}
    if regime is not None:
        by_method[EffectiveCountMethod.EIGENVALUE_EFFECTIVE_RANK] = eigenvalue_effective_rank(
            item_ids, regime, as_of, min_sample_size)
        by_method[EffectiveCountMethod.CORRELATION_CLUSTER_COUNT] = correlation_cluster_count(
            item_ids, regime, as_of, threshold, min_sample_size)
    if factor_exposures is not None:
        by_method[EffectiveCountMethod.RISK_FACTOR_CLUSTERING] = risk_factor_cluster_count(
            item_ids, factor_exposures, as_of, threshold)
    if not by_method:
        raise InsufficientDataError(
            "need at least a correlation regime or factor exposures to estimate")
    return EffectiveCountEstimate(by_method=by_method)


def estimate_opportunity_breadth(
        opportunity_ids: list[str], regime: Optional[RegimeCorrelation] = None,
        factor_exposures: Optional[dict[str, dict[str, float]]] = None,
        as_of: datetime = None, threshold: float = DEFAULT_CLUSTER_THRESHOLD,
        min_sample_size: int = 20) -> EffectiveCountEstimate:
    """§17: Effective Opportunity Count, treated as an ESTIMATE (never
    absolute truth), computed from correlation and/or clustering — same
    toolkit as §18's Effective Independent Cells, applied to candidate
    opportunities instead of active cells."""
    return _combine(opportunity_ids, regime, factor_exposures, as_of, threshold, min_sample_size)


def estimate_effective_independent_cells(
        cell_ids: list[str], regime: Optional[RegimeCorrelation] = None,
        factor_exposures: Optional[dict[str, dict[str, float]]] = None,
        as_of: datetime = None, threshold: float = DEFAULT_CLUSTER_THRESHOLD,
        min_sample_size: int = 20) -> EffectiveCountEstimate:
    """§18: e.g. Active Cells=30, Effective Independent Cells=11.8 — the
    11.8 is one ESTIMATE among (up to) three comparable methods, not a
    fixed formula's permanent output."""
    return _combine(cell_ids, regime, factor_exposures, as_of, threshold, min_sample_size)
