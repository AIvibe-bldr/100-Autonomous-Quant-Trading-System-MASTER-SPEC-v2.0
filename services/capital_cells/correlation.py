"""Correlation / Edge Lineage (docs/capital_cell_architecture.md §19, §21,
§41 priority 9).

Two things this deliberately refuses to do:

- **Treat "different name" as evidence of independence (§19).** Two alphas
  are not independent because they were built separately; independence is
  measured across seven overlap dimensions the doc names explicitly
  (feature/signal/return/universe/timing/factor/training-data), and a pair
  with no measurement is a missing input, not an assumed zero.
- **Collapse correlation into one number (§21).** Correlation is not
  stationary and rises exactly when it matters most (a crash correlates
  everything toward 1). `CorrelationEngine` produces normal/downside/stress
  estimates separately, each carrying its own lookback/decay/sample_count,
  so a caller has to pick which regime is relevant instead of getting a
  blended average that hides tail behavior.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from services.capital_cells.estimation import InsufficientDataError, PointEstimate

MIN_CORRELATION_SAMPLE_SIZE = 20   # below this a Pearson r is noise, not signal
MIN_ALPHA_OVERLAP_SAMPLES = 20     # same bar for the overlap components below

CORRELATION_METHOD_VERSION = "PEARSON_V1"
ALPHA_OVERLAP_METHOD_VERSION = "EQUAL_WEIGHTED_7DIM_V1"


class CorrelationRegime(str, enum.Enum):
    NORMAL = "NORMAL"
    DOWNSIDE = "DOWNSIDE"
    STRESS = "STRESS"


def _pair_key(a: str, b: str) -> frozenset:
    return frozenset((a, b))


@dataclass(frozen=True)
class RegimeCorrelation:
    """One regime's correlation matrix (§21) — never THE correlation
    matrix, because there isn't one that's always right."""

    regime: CorrelationRegime
    pairwise: dict[frozenset, float]      # symmetric, keyed order-independent
    lookback_days: int
    decay_halflife_days: Optional[float]  # None = equal-weighted
    sample_count: int
    as_of: datetime
    method_version: str = CORRELATION_METHOD_VERSION

    def get(self, a: str, b: str) -> float:
        if a == b:
            return 1.0
        return self.pairwise.get(_pair_key(a, b), 0.0)

    def has(self, a: str, b: str) -> bool:
        """Whether this pair was actually measured. Callers that count
        independence MUST check — `get`'s 0.0 default would otherwise read
        an unmeasured pair as perfectly uncorrelated, i.e. as evidence of
        diversification that was never observed (§19's rule, applied to
        §18's counting)."""
        return a == b or _pair_key(a, b) in self.pairwise

    def is_stale(self, now: datetime, max_age_days: float) -> bool:
        """§21: 'do not allocate large capital off an old correlation'.
        Callers gate large reallocations on this before trusting the
        matrix."""
        return (now - self.as_of).total_seconds() > max_age_days * 86400.0


@dataclass(frozen=True)
class CorrelationEstimate:
    by_regime: dict[CorrelationRegime, RegimeCorrelation]

    def regime(self, r: CorrelationRegime) -> RegimeCorrelation:
        try:
            return self.by_regime[r]
        except KeyError:
            raise InsufficientDataError(
                f"no correlation estimate for regime {r.value}") from None


def _weighted_pearson(xs: list[float], ys: list[float], weights: list[float]) -> float:
    sw = sum(weights)
    if sw <= 0:
        return 0.0
    mx = sum(w * x for w, x in zip(weights, xs)) / sw
    my = sum(w * y for w, y in zip(weights, ys)) / sw
    cov = sum(w * (x - mx) * (y - my) for w, x, y in zip(weights, xs, ys))
    vx = sum(w * (x - mx) ** 2 for w, x in zip(weights, xs))
    vy = sum(w * (y - my) ** 2 for w, y in zip(weights, ys))
    if vx <= 0 or vy <= 0:
        return 0.0
    return max(-1.0, min(1.0, cov / (vx * vy) ** 0.5))


class CorrelationEngine:
    """Deterministic — no AI ever picks a correlation number, same
    no-AI-in-the-risk-path principle §7/§42 already apply to execution."""

    def __init__(self, min_sample_size: int = MIN_CORRELATION_SAMPLE_SIZE,
                 stress_quantile: float = 0.05) -> None:
        self._min_sample_size = min_sample_size
        self._stress_quantile = stress_quantile

    def estimate(self, returns: dict[str, list[float]], market_returns: list[float],
                 lookback_days: int, as_of: datetime,
                 decay_halflife_days: Optional[float] = None) -> CorrelationEstimate:
        """`returns`: cell_id -> daily return series, oldest first, aligned
        index-for-index with `market_returns` (used only to classify each
        day into normal/downside/stress; it is never itself correlated
        against). `decay_halflife_days`, if given, downweights older days —
        day k (0=oldest) gets weight 0.5 ** ((n-1-k) / halflife)."""
        n = len(market_returns)
        for cell_id, series in returns.items():
            if len(series) != n:
                raise ValueError(
                    f"{cell_id}: return series length {len(series)} != market length {n}")

        base_weights = (
            [0.5 ** ((n - 1 - k) / decay_halflife_days) for k in range(n)]
            if decay_halflife_days else [1.0] * n)

        # STRESS is selected by RANK, not by a value threshold. Comparing
        # `r <= cutoff_value` sweeps in every day tied with the cutoff, and
        # ties at the bottom are ordinary in real series (flat/halted/zero-
        # return days) — a 5% stress window would silently become most of
        # the sample, so the "stress correlation" §21 exists to isolate
        # would actually be measured over quiet days.
        stress_count = max(1, round(self._stress_quantile * n)) if n else 0
        worst_first = sorted(range(n), key=lambda k: market_returns[k])
        stress_indices = set(worst_first[:stress_count])
        masks: dict[CorrelationRegime, list[bool]] = {
            CorrelationRegime.NORMAL: [True] * n,
            CorrelationRegime.DOWNSIDE: [r < 0 for r in market_returns],
            CorrelationRegime.STRESS: [k in stress_indices for k in range(n)],
        }

        by_regime: dict[CorrelationRegime, RegimeCorrelation] = {}
        cell_ids = sorted(returns)
        for regime, mask in masks.items():
            sample_count = sum(mask)
            if sample_count < self._min_sample_size:
                continue   # too few days in this regime — omitted, not fabricated
            weights = [w for w, keep in zip(base_weights, mask) if keep]
            pairwise: dict[frozenset, float] = {}
            for i, a in enumerate(cell_ids):
                xs_a = [x for x, keep in zip(returns[a], mask) if keep]
                for b in cell_ids[i + 1:]:
                    xs_b = [x for x, keep in zip(returns[b], mask) if keep]
                    pairwise[_pair_key(a, b)] = _weighted_pearson(xs_a, xs_b, weights)
            by_regime[regime] = RegimeCorrelation(
                regime=regime, pairwise=pairwise, lookback_days=lookback_days,
                decay_halflife_days=decay_halflife_days, sample_count=sample_count, as_of=as_of)

        if not by_regime:
            raise InsufficientDataError(
                f"no regime reached the {self._min_sample_size}-sample minimum out of {n} days")
        return CorrelationEstimate(by_regime=by_regime)


# ---------------------------------------------------------------------------
# §19: Alpha Independence
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AlphaOverlapInputs:
    """Raw overlap measurement between two cells' alphas (§19's own list),
    each in [0,1] — 0 fully independent, 1 fully overlapping/identical.
    `observation_count` is how many aligned observations backed the
    correlation-based components; a pair with zero is not a measurement,
    it's a guess wearing a number."""

    feature_overlap: float
    signal_correlation: float       # raw (signed) correlation of the two AI signals
    return_correlation: float       # raw (signed) correlation of realized returns
    universe_overlap: float
    timing_overlap: float
    factor_exposure_overlap: float
    training_data_overlap: float
    observation_count: int

    def __post_init__(self) -> None:
        for name in ("feature_overlap", "universe_overlap", "timing_overlap",
                     "factor_exposure_overlap", "training_data_overlap"):
            v = getattr(self, name)
            if not (0.0 <= v <= 1.0):
                raise ValueError(f"{name} must be in [0,1], got {v}")
        for name in ("signal_correlation", "return_correlation"):
            v = getattr(self, name)
            if not (-1.0 <= v <= 1.0):
                raise ValueError(f"{name} must be in [-1,1], got {v}")
        if self.observation_count < 0:
            raise ValueError("observation_count cannot be negative")

    @property
    def composite_overlap(self) -> float:
        """Equal-weighted mean across all seven dimensions. Correlations are
        taken in absolute value: a strong NEGATIVE signal/return correlation
        is just as much "the same bet" (one is a levered inverse of the
        other) as a strong positive one — it is not diversification."""
        values = (self.feature_overlap, abs(self.signal_correlation),
                  abs(self.return_correlation), self.universe_overlap,
                  self.timing_overlap, self.factor_exposure_overlap,
                  self.training_data_overlap)
        return sum(values) / len(values)


def effective_independent_alpha_count(
        cell_ids: list[str], overlaps: dict[frozenset, AlphaOverlapInputs],
        as_of: datetime) -> PointEstimate:
    """§19: 'different name' is not evidence of independence — every pair
    among `cell_ids` must have a measured `AlphaOverlapInputs`, or this
    raises rather than silently treating the missing pair as overlap=0.

    Effective count shrinks from N (fully independent) toward 1 (all
    identical) as the average pairwise overlap rises toward 1. Confidence
    is the weakest-link across pairs — one badly under-sampled pair caps
    confidence for the whole estimate, matching §22's "use the safe-side
    bound when precision is low" philosophy rather than averaging a bad
    measurement away."""
    n = len(cell_ids)
    if n == 0:
        raise InsufficientDataError("no cells to evaluate")
    if n == 1:
        return PointEstimate(value=1.0, confidence=1.0, sample_size=0,
                             method_version=ALPHA_OVERLAP_METHOD_VERSION, as_of=as_of)

    missing = []
    used: list[AlphaOverlapInputs] = []
    total_overlap = 0.0
    for i, a in enumerate(cell_ids):
        for b in cell_ids[i + 1:]:
            key = _pair_key(a, b)
            if key not in overlaps:
                missing.append((a, b))
                continue
            ov = overlaps[key]
            if ov.observation_count == 0:
                missing.append((a, b))
                continue
            used.append(ov)
            total_overlap += ov.composite_overlap
    if missing:
        raise InsufficientDataError(
            f"missing or zero-observation alpha overlap for pairs {missing} — "
            f"'different name' is not evidence of independence (§19)")

    avg_overlap = total_overlap / len(used)
    effective = 1.0 + (n - 1) * (1.0 - avg_overlap)
    confidence = min(min(1.0, ov.observation_count / MIN_ALPHA_OVERLAP_SAMPLES) for ov in used)
    return PointEstimate(
        value=effective, confidence=confidence, sample_size=len(used),
        method_version=ALPHA_OVERLAP_METHOD_VERSION, as_of=as_of)
