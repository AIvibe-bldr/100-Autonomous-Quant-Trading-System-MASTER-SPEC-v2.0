"""Marginal Alpha / Scale Simulation tests (docs/capital_cell_architecture.md
§24-25, §41 priority 13)."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from services.capital_cells.estimation import InsufficientDataError
from services.capital_cells.scale_simulation import (
    ScaleConfidenceLabel,
    estimate_marginal_alpha,
    simulate_scale,
)

AT = datetime(2026, 8, 10, 15, 0, tzinfo=timezone.utc)


class TestMarginalAlpha:
    def test_constant_ratio_has_zero_spread(self):
        observations = [(100.0, 5.0)] * 10   # marginal edge exactly 0.05 every time
        est = estimate_marginal_alpha(observations, as_of=AT, min_sample_size=5)
        assert est.base == pytest.approx(0.05)
        assert est.low == pytest.approx(0.05)
        assert est.high == pytest.approx(0.05)

    def test_varying_ratio_produces_a_real_spread(self):
        observations = [(100.0, 3.0), (100.0, 7.0), (100.0, 5.0), (100.0, 4.0), (100.0, 6.0)]
        est = estimate_marginal_alpha(observations, as_of=AT, min_sample_size=5)
        assert est.low < est.base < est.high

    def test_insufficient_sample_size_raises(self):
        with pytest.raises(InsufficientDataError):
            estimate_marginal_alpha([(100.0, 5.0)], as_of=AT, min_sample_size=10)

    def test_zero_capital_increment_rejected(self):
        with pytest.raises(ValueError):
            estimate_marginal_alpha([(0.0, 5.0)] * 10, as_of=AT, min_sample_size=5)

    def test_confidence_scales_with_sample_size(self):
        small = estimate_marginal_alpha([(100.0, 5.0)] * 5, as_of=AT, min_sample_size=5)
        large = estimate_marginal_alpha([(100.0, 5.0)] * 30, as_of=AT, min_sample_size=5)
        assert large.confidence > small.confidence


class TestScaleSimulation:
    def _linear_data(self, n=10, lo=100_000.0, hi=1_000_000.0, slope=2.0, intercept=1.0):
        step = (hi - lo) / (n - 1)
        levels = [lo + step * i for i in range(n)]
        outcomes = [slope * x + intercept for x in levels]
        return levels, outcomes

    def test_in_sample_target_is_labeled_in_sample(self):
        levels, outcomes = self._linear_data()
        result = simulate_scale(levels, outcomes, target_capital=500_000.0, as_of=AT)
        assert result.label is ScaleConfidenceLabel.IN_SAMPLE
        assert result.confidence == pytest.approx(0.8)

    def test_far_above_observed_range_is_low_confidence_extrapolation(self):
        levels, outcomes = self._linear_data(lo=100_000.0, hi=1_000_000.0)
        # observed max ~1,000,000; target is 100,000,000,000 -- ~100,000x beyond
        result = simulate_scale(levels, outcomes, target_capital=100_000_000_000.0, as_of=AT)
        assert result.label is ScaleConfidenceLabel.LOW_CONFIDENCE_EXTRAPOLATION
        assert result.confidence < 0.05

    def test_far_below_observed_range_is_also_low_confidence_extrapolation(self):
        levels, outcomes = self._linear_data(lo=1_000_000.0, hi=10_000_000.0)
        result = simulate_scale(levels, outcomes, target_capital=1_000.0, as_of=AT)
        assert result.label is ScaleConfidenceLabel.LOW_CONFIDENCE_EXTRAPOLATION

    def test_extrapolation_label_holds_even_for_a_perfectly_linear_fit(self):
        """The label is about how far the target sits outside the observed
        range, not about whether the projected number happens to be
        numerically correct for this synthetic case (§25's actual point:
        confidence in the EVIDENCE, not in the arithmetic)."""
        levels, outcomes = self._linear_data(slope=2.0, intercept=0.0)
        result = simulate_scale(levels, outcomes, target_capital=1_000_000_000.0, as_of=AT)
        assert result.label is ScaleConfidenceLabel.LOW_CONFIDENCE_EXTRAPOLATION
        assert result.projected_value == pytest.approx(2.0 * 1_000_000_000.0, rel=1e-6)

    def test_confidence_decays_monotonically_with_extrapolation_distance(self):
        levels, outcomes = self._linear_data()
        near = simulate_scale(levels, outcomes, target_capital=5_000_000.0, as_of=AT)
        far = simulate_scale(levels, outcomes, target_capital=500_000_000.0, as_of=AT)
        farther = simulate_scale(levels, outcomes, target_capital=50_000_000_000.0, as_of=AT)
        assert near.confidence >= far.confidence >= farther.confidence

    def test_result_never_reaches_into_allocation_reserve_automatically(self):
        """§25: 'this result alone must not auto-change Capital Allocation.'
        Structural check: ScaleSimulationResult carries no allocation-side
        effect -- it is a plain frozen dataclass with only report fields."""
        levels, outcomes = self._linear_data()
        result = simulate_scale(levels, outcomes, target_capital=500_000.0, as_of=AT)
        import dataclasses
        field_names = {f.name for f in dataclasses.fields(result)}
        assert field_names == {"observed_range", "extrapolated_range", "target_capital",
                               "projected_value", "confidence", "label", "method_version", "as_of"}

    def test_insufficient_sample_size_raises(self):
        with pytest.raises(InsufficientDataError):
            simulate_scale([100.0, 200.0], [1.0, 2.0], target_capital=150.0, as_of=AT,
                           min_sample_size=5)

    def test_mismatched_lengths_rejected(self):
        with pytest.raises(ValueError):
            simulate_scale([100.0, 200.0, 300.0, 400.0, 500.0], [1.0, 2.0],
                           target_capital=150.0, as_of=AT, min_sample_size=2)

    def test_non_positive_capital_rejected(self):
        with pytest.raises(ValueError):
            simulate_scale([100.0, 200.0, 300.0, 400.0, 500.0],
                           [1.0, 2.0, 3.0, 4.0, 5.0], target_capital=-1.0, as_of=AT,
                           min_sample_size=5)
