"""Marginal Alpha / Scale Simulation (docs/capital_cell_architecture.md
§24-25, §41 priority 13).

- **§24 Marginal Alpha as a Range.** "An extra ¥1M yields +5.4%" is exactly
  the kind of over-precise point estimate the doc distrusts.
  `estimate_marginal_alpha` returns a `RangeEstimate` (reusing the priority-9
  shared primitive) built from sample mean +/- standard error, never a bare
  number, and raises `InsufficientDataError` below the sample floor
  (§24's own INSUFFICIENT_DATA).
- **§25 Scale Simulation extrapolation warning.** Data from a ¥100k account
  cannot precisely predict a ¥1B account. `simulate_scale` fits a simple
  linear trend on observed (capital, outcome) pairs and labels the
  projection `LOW_CONFIDENCE_EXTRAPOLATION` — with confidence decaying as
  the target moves further outside the observed range — whenever the
  target capital sits far enough outside what was actually observed, even
  when the underlying data happens to be a perfect line (the warning is
  about how much evidence backs the range, not about whether the model
  happens to be numerically right for a given input).

`ScaleSimulationResult` is a read-only report: nothing in this module (or
`services.capital_cells.allocation_governor`) calls into `AllocationReserve`
or `allocate_with_reserve` automatically from a simulation result — a
caller has to explicitly decide to act on it, which is the doc's "this
result alone must not auto-change Capital Allocation" (§25) enforced
structurally by simply not wiring the two together.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import datetime

from services.capital_cells.estimation import RangeEstimate, require_sample_size

MARGINAL_ALPHA_METHOD_VERSION = "SAMPLE_MEAN_STANDARD_ERROR_V1"
SCALE_SIMULATION_METHOD_VERSION = "LINEAR_OLS_V1"


# ---------------------------------------------------------------------------
# §24: Marginal Alpha as a Range
# ---------------------------------------------------------------------------

def estimate_marginal_alpha(observations: list[tuple[float, float]], as_of: datetime,
                            min_sample_size: int = 10, z: float = 1.0) -> RangeEstimate:
    """`observations`: (capital_increment, resulting_edge_delta) pairs, same
    units throughout. base = sample mean of edge_delta/capital_increment;
    low/high = base +/- `z` standard errors. Never collapsed to the mean
    alone (§24)."""
    require_sample_size(len(observations), min_sample_size, "marginal alpha")
    if any(cap == 0 for cap, _ in observations):
        raise ValueError("capital_increment cannot be zero")

    marginals = [edge / cap for cap, edge in observations]
    n = len(marginals)
    mean = sum(marginals) / n
    if n > 1:
        variance = sum((m - mean) ** 2 for m in marginals) / (n - 1)
        standard_error = (variance / n) ** 0.5
    else:
        standard_error = 0.0

    confidence = min(1.0, n / (min_sample_size * 3))
    return RangeEstimate(
        low=mean - z * standard_error, base=mean, high=mean + z * standard_error,
        confidence=confidence, sample_size=n,
        method_version=MARGINAL_ALPHA_METHOD_VERSION, as_of=as_of)


# ---------------------------------------------------------------------------
# §25: Scale Simulation extrapolation warning
# ---------------------------------------------------------------------------

class ScaleConfidenceLabel(str, enum.Enum):
    IN_SAMPLE = "IN_SAMPLE"
    LOW_CONFIDENCE_EXTRAPOLATION = "LOW_CONFIDENCE_EXTRAPOLATION"


@dataclass(frozen=True)
class ScaleSimulationResult:
    observed_range: tuple[float, float]
    extrapolated_range: tuple[float, float]
    target_capital: float
    projected_value: float
    confidence: float
    label: ScaleConfidenceLabel
    method_version: str
    as_of: datetime


def _ols(xs: list[float], ys: list[float]) -> tuple[float, float]:
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    var = sum((x - mx) ** 2 for x in xs)
    if var <= 1e-15:
        return 0.0, my
    slope = cov / var
    return slope, my - slope * mx


def simulate_scale(observed_capital_levels: list[float], observed_outcomes: list[float],
                   target_capital: float, as_of: datetime,
                   extrapolation_factor_threshold: float = 3.0,
                   min_sample_size: int = 5) -> ScaleSimulationResult:
    """Linear projection of `observed_outcomes` vs `observed_capital_levels`
    to `target_capital`. Flags LOW_CONFIDENCE_EXTRAPOLATION whenever the
    target sits more than `extrapolation_factor_threshold`x beyond the
    observed range in either direction — ¥100k of data does not precisely
    predict ¥1B (§25)."""
    require_sample_size(len(observed_capital_levels), min_sample_size, "scale simulation")
    if len(observed_capital_levels) != len(observed_outcomes):
        raise ValueError(
            f"observed_capital_levels length {len(observed_capital_levels)} != "
            f"observed_outcomes length {len(observed_outcomes)}")
    if target_capital <= 0 or any(c <= 0 for c in observed_capital_levels):
        raise ValueError("capital levels must be positive")

    observed_min, observed_max = min(observed_capital_levels), max(observed_capital_levels)
    slope, intercept = _ols(observed_capital_levels, observed_outcomes)
    projected = slope * target_capital + intercept

    if target_capital > observed_max:
        excess_ratio = target_capital / observed_max
    elif target_capital < observed_min:
        excess_ratio = observed_min / target_capital
    else:
        excess_ratio = 1.0

    if excess_ratio > extrapolation_factor_threshold:
        label = ScaleConfidenceLabel.LOW_CONFIDENCE_EXTRAPOLATION
        confidence = max(0.0, 1.0 / excess_ratio)
    else:
        label = ScaleConfidenceLabel.IN_SAMPLE
        confidence = 0.8

    return ScaleSimulationResult(
        observed_range=(observed_min, observed_max),
        extrapolated_range=(min(observed_min, target_capital), max(observed_max, target_capital)),
        target_capital=target_capital, projected_value=projected, confidence=confidence,
        label=label, method_version=SCALE_SIMULATION_METHOD_VERSION, as_of=as_of)
