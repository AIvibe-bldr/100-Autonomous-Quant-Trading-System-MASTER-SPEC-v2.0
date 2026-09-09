"""Correlation / Edge Lineage tests (docs/capital_cell_architecture.md §19,
§21, §41 priority 9)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from services.capital_cells.correlation import (
    AlphaOverlapInputs,
    CorrelationEngine,
    CorrelationRegime,
    effective_independent_alpha_count,
)
from services.capital_cells.estimation import InsufficientDataError

AT = datetime(2026, 8, 10, 15, 0, tzinfo=timezone.utc)


class TestCorrelationEngine:
    def test_identical_series_correlate_perfectly(self):
        series = [float(i) for i in range(1, 21)]
        engine = CorrelationEngine(min_sample_size=5)
        est = engine.estimate({"A": series, "B": list(series)}, market_returns=series,
                              lookback_days=20, as_of=AT)
        normal = est.regime(CorrelationRegime.NORMAL)
        assert normal.get("A", "B") == pytest.approx(1.0)
        assert normal.get("B", "A") == pytest.approx(1.0)   # symmetric regardless of order

    def test_inverted_series_correlate_perfectly_negative(self):
        xs = [float(i) for i in range(1, 21)]
        ys = list(reversed(xs))
        engine = CorrelationEngine(min_sample_size=5)
        est = engine.estimate({"A": xs, "B": ys}, market_returns=xs, lookback_days=20, as_of=AT)
        assert est.regime(CorrelationRegime.NORMAL).get("A", "B") == pytest.approx(-1.0)

    def test_get_diagonal_is_always_one(self):
        xs = [1.0, 2.0, 3.0, 4.0, 5.0]
        engine = CorrelationEngine(min_sample_size=3)
        est = engine.estimate({"A": xs}, market_returns=xs, lookback_days=5, as_of=AT)
        assert est.regime(CorrelationRegime.NORMAL).get("A", "A") == 1.0

    def test_mismatched_series_length_rejected(self):
        engine = CorrelationEngine(min_sample_size=3)
        with pytest.raises(ValueError):
            engine.estimate({"A": [1.0, 2.0, 3.0], "B": [1.0, 2.0]},
                            market_returns=[1.0, 2.0, 3.0], lookback_days=5, as_of=AT)

    def test_regime_with_too_few_days_is_omitted_not_fabricated(self):
        # market_returns has only ONE negative day -> DOWNSIDE sample_count=1,
        # below the default min_sample_size -> must be absent, not a fake number.
        market = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, -1.0]
        a = [float(i) for i in range(len(market))]
        b = list(reversed(a))
        engine = CorrelationEngine(min_sample_size=5)
        est = engine.estimate({"A": a, "B": b}, market_returns=market, lookback_days=8, as_of=AT)
        assert CorrelationRegime.NORMAL in est.by_regime
        with pytest.raises(InsufficientDataError):
            est.regime(CorrelationRegime.DOWNSIDE)

    def test_all_regimes_below_minimum_raises(self):
        market = [0.1, 0.2, 0.3]
        a = [1.0, 2.0, 3.0]
        engine = CorrelationEngine(min_sample_size=20)
        with pytest.raises(InsufficientDataError):
            engine.estimate({"A": a}, market_returns=market, lookback_days=3, as_of=AT)

    def test_decay_weighting_shifts_correlation_toward_recent_days(self):
        """First half of the series: A and B move together. Second half:
        they invert. A short decay half-life should pull the estimate
        noticeably more negative than equal weighting, because it discounts
        the earlier agreeing days."""
        n = 40
        first_a = [float(i) for i in range(20)]
        first_b = list(first_a)                       # agree early on
        second_a = [float(i) for i in range(20)]
        second_b = list(reversed(second_a))            # invert recently
        a = first_a + second_a
        b = first_b + second_b
        market = [0.01] * n   # keep everything in the NORMAL regime only

        engine = CorrelationEngine(min_sample_size=5)
        equal_weight = engine.estimate({"A": a, "B": b}, market_returns=market,
                                       lookback_days=n, as_of=AT).regime(
            CorrelationRegime.NORMAL).get("A", "B")
        decayed = engine.estimate({"A": a, "B": b}, market_returns=market, lookback_days=n,
                                  as_of=AT, decay_halflife_days=3.0).regime(
            CorrelationRegime.NORMAL).get("A", "B")
        assert decayed < equal_weight

    def test_is_stale(self):
        engine = CorrelationEngine(min_sample_size=3)
        xs = [1.0, 2.0, 3.0, 4.0]
        est = engine.estimate({"A": xs}, market_returns=xs, lookback_days=4, as_of=AT)
        normal = est.regime(CorrelationRegime.NORMAL)
        assert not normal.is_stale(AT + timedelta(days=1), max_age_days=5)
        assert normal.is_stale(AT + timedelta(days=10), max_age_days=5)


class TestAlphaIndependence:
    def _overlap(self, level: float, observation_count: int = 30) -> AlphaOverlapInputs:
        return AlphaOverlapInputs(
            feature_overlap=level, signal_correlation=level, return_correlation=level,
            universe_overlap=level, timing_overlap=level, factor_exposure_overlap=level,
            training_data_overlap=level, observation_count=observation_count)

    def test_fully_independent_alphas_give_effective_count_near_n(self):
        overlaps = {frozenset(("A", "B")): self._overlap(0.0)}
        est = effective_independent_alpha_count(["A", "B"], overlaps, as_of=AT)
        assert est.value == pytest.approx(2.0)

    def test_fully_overlapping_alphas_collapse_to_one(self):
        overlaps = {frozenset(("A", "B")): self._overlap(1.0)}
        est = effective_independent_alpha_count(["A", "B"], overlaps, as_of=AT)
        assert est.value == pytest.approx(1.0)

    def test_single_cell_is_trivially_one(self):
        est = effective_independent_alpha_count(["A"], {}, as_of=AT)
        assert est.value == 1.0 and est.confidence == 1.0

    def test_zero_cells_is_insufficient_data(self):
        with pytest.raises(InsufficientDataError):
            effective_independent_alpha_count([], {}, as_of=AT)

    def test_missing_pair_raises_rather_than_assuming_independence(self):
        """§19: 'different name' must never silently become overlap=0."""
        overlaps = {frozenset(("A", "B")): self._overlap(0.0)}
        with pytest.raises(InsufficientDataError):
            effective_independent_alpha_count(["A", "B", "C"], overlaps, as_of=AT)

    def test_zero_observation_pair_treated_as_missing(self):
        overlaps = {frozenset(("A", "B")): self._overlap(0.0, observation_count=0)}
        with pytest.raises(InsufficientDataError):
            effective_independent_alpha_count(["A", "B"], overlaps, as_of=AT)

    def test_negative_correlation_counts_as_overlap_via_absolute_value(self):
        """A perfectly inverse pair of signals is the same bet levered
        backwards, not diversification -- must NOT reduce composite_overlap."""
        anti = AlphaOverlapInputs(
            feature_overlap=0.0, signal_correlation=-1.0, return_correlation=-1.0,
            universe_overlap=0.0, timing_overlap=0.0, factor_exposure_overlap=0.0,
            training_data_overlap=0.0, observation_count=30)
        assert anti.composite_overlap == pytest.approx(2.0 / 7.0)   # abs(-1)+abs(-1) / 7

    def test_out_of_range_fields_rejected(self):
        with pytest.raises(ValueError):
            AlphaOverlapInputs(feature_overlap=1.5, signal_correlation=0, return_correlation=0,
                               universe_overlap=0, timing_overlap=0, factor_exposure_overlap=0,
                               training_data_overlap=0, observation_count=10)
        with pytest.raises(ValueError):
            AlphaOverlapInputs(feature_overlap=0, signal_correlation=-2.0, return_correlation=0,
                               universe_overlap=0, timing_overlap=0, factor_exposure_overlap=0,
                               training_data_overlap=0, observation_count=10)

    def test_confidence_is_capped_by_the_weakest_pair(self):
        overlaps = {
            frozenset(("A", "B")): self._overlap(0.2, observation_count=30),  # >= threshold
            frozenset(("A", "C")): self._overlap(0.2, observation_count=5),   # thin
            frozenset(("B", "C")): self._overlap(0.2, observation_count=30),
        }
        est = effective_independent_alpha_count(["A", "B", "C"], overlaps, as_of=AT)
        assert est.confidence == pytest.approx(5 / 20)   # MIN_ALPHA_OVERLAP_SAMPLES = 20

    def test_overlap_keys_are_order_independent(self):
        overlaps = {frozenset(("B", "A")): self._overlap(0.0)}   # inserted B,A not A,B
        est = effective_independent_alpha_count(["A", "B"], overlaps, as_of=AT)
        assert est.value == pytest.approx(2.0)
