"""Opportunity Breadth / Effective Independent Cells tests
(docs/capital_cell_architecture.md §17-18, §41 priority 10)."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from services.capital_cells.breadth import (
    EffectiveCountMethod,
    _symmetric_eigenvalues,
    correlation_cluster_count,
    eigenvalue_effective_rank,
    estimate_effective_independent_cells,
    estimate_opportunity_breadth,
    risk_factor_cluster_count,
)
from services.capital_cells.correlation import CorrelationRegime, RegimeCorrelation
from services.capital_cells.estimation import InsufficientDataError

AT = datetime(2026, 8, 10, 15, 0, tzinfo=timezone.utc)


def _regime(pairwise: dict[tuple, float], sample_count: int = 30,
           lookback_days: int = 60) -> RegimeCorrelation:
    return RegimeCorrelation(
        regime=CorrelationRegime.NORMAL,
        pairwise={frozenset(k): v for k, v in pairwise.items()},
        lookback_days=lookback_days, decay_halflife_days=None,
        sample_count=sample_count, as_of=AT)


class TestSymmetricEigenvalues:
    def test_identity_matrix_has_all_ones(self):
        m = [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]
        eigs = sorted(_symmetric_eigenvalues(m))
        assert eigs == pytest.approx([1.0, 1.0, 1.0])

    def test_all_ones_matrix_is_rank_one(self):
        m = [[1.0, 1.0, 1.0], [1.0, 1.0, 1.0], [1.0, 1.0, 1.0]]
        eigs = sorted(_symmetric_eigenvalues(m))
        assert eigs == pytest.approx([0.0, 0.0, 3.0], abs=1e-8)

    def test_known_2x2_eigenvalues(self):
        # [[1, 0.5], [0.5, 1]] -> eigenvalues 1.5 and 0.5
        m = [[1.0, 0.5], [0.5, 1.0]]
        eigs = sorted(_symmetric_eigenvalues(m))
        assert eigs == pytest.approx([0.5, 1.5])


class TestEigenvalueEffectiveRank:
    def test_fully_independent_items_give_effective_rank_n(self):
        ids = ["A", "B", "C"]
        regime = _regime({("A", "B"): 0.0, ("A", "C"): 0.0, ("B", "C"): 0.0})
        est = eigenvalue_effective_rank(ids, regime, as_of=AT)
        assert est.value == pytest.approx(3.0)

    def test_fully_correlated_items_collapse_to_one(self):
        ids = ["A", "B", "C"]
        regime = _regime({("A", "B"): 1.0, ("A", "C"): 1.0, ("B", "C"): 1.0})
        est = eigenvalue_effective_rank(ids, regime, as_of=AT)
        assert est.value == pytest.approx(1.0)

    def test_single_item_is_trivially_one(self):
        est = eigenvalue_effective_rank(["A"], _regime({}), as_of=AT)
        assert est.value == 1.0 and est.confidence == 1.0

    def test_zero_items_is_insufficient_data(self):
        with pytest.raises(InsufficientDataError):
            eigenvalue_effective_rank([], _regime({}), as_of=AT)

    def test_confidence_scales_with_sample_count(self):
        ids = ["A", "B"]
        thin = _regime({("A", "B"): 0.0}, sample_count=5)
        est = eigenvalue_effective_rank(ids, thin, as_of=AT, min_sample_size=20)
        assert est.confidence == pytest.approx(0.25)

    def test_two_block_clusters_give_effective_rank_between_one_and_n(self):
        ids = ["A", "B", "C", "D"]
        regime = _regime({
            ("A", "B"): 0.95, ("C", "D"): 0.95,
            ("A", "C"): 0.0, ("A", "D"): 0.0, ("B", "C"): 0.0, ("B", "D"): 0.0,
        })
        est = eigenvalue_effective_rank(ids, regime, as_of=AT)
        assert 1.5 < est.value < 2.5   # ~2 independent blocks, not 4, not 1


class TestCorrelationClusterCount:
    def test_fully_independent_items_are_n_singleton_clusters(self):
        ids = ["A", "B", "C"]
        regime = _regime({("A", "B"): 0.0, ("A", "C"): 0.0, ("B", "C"): 0.0})
        est = correlation_cluster_count(ids, regime, as_of=AT)
        assert est.value == 3.0

    def test_fully_correlated_items_are_one_cluster(self):
        ids = ["A", "B", "C"]
        regime = _regime({("A", "B"): 1.0, ("A", "C"): 1.0, ("B", "C"): 1.0})
        est = correlation_cluster_count(ids, regime, as_of=AT)
        assert est.value == 1.0

    def test_two_block_clusters_counted_exactly(self):
        ids = ["A", "B", "C", "D"]
        regime = _regime({
            ("A", "B"): 0.95, ("C", "D"): 0.95,
            ("A", "C"): 0.1, ("A", "D"): 0.1, ("B", "C"): 0.1, ("B", "D"): 0.1,
        })
        est = correlation_cluster_count(ids, regime, as_of=AT, threshold=0.7)
        assert est.value == 2.0

    def test_negative_correlation_also_clusters_via_absolute_value(self):
        """A near -1 correlation is just as much 'the same bet' as +1 for
        clustering purposes (matches §19's abs() treatment)."""
        ids = ["A", "B"]
        regime = _regime({("A", "B"): -0.95})
        est = correlation_cluster_count(ids, regime, as_of=AT, threshold=0.7)
        assert est.value == 1.0


class TestRiskFactorClusterCount:
    def test_identical_factor_exposure_is_one_cluster(self):
        exposures = {"A": {"momentum": 1.0, "value": 0.2}, "B": {"momentum": 1.0, "value": 0.2}}
        est = risk_factor_cluster_count(["A", "B"], exposures, as_of=AT, threshold=0.9)
        assert est.value == 1.0

    def test_orthogonal_factor_exposure_is_two_clusters(self):
        exposures = {"A": {"momentum": 1.0}, "B": {"value": 1.0}}
        est = risk_factor_cluster_count(["A", "B"], exposures, as_of=AT, threshold=0.5)
        assert est.value == 2.0

    def test_missing_exposure_raises_insufficient_data(self):
        exposures = {"A": {"momentum": 1.0}}
        with pytest.raises(InsufficientDataError):
            risk_factor_cluster_count(["A", "B"], exposures, as_of=AT)


class TestCombinedEstimates:
    def test_effective_independent_cells_reports_multiple_comparable_methods(self):
        ids = ["A", "B", "C"]
        regime = _regime({("A", "B"): 0.2, ("A", "C"): 0.2, ("B", "C"): 0.2})
        factor_exposures = {cid: {"momentum": 1.0} for cid in ids}   # all identical exposure
        est = estimate_effective_independent_cells(
            ids, regime=regime, factor_exposures=factor_exposures, as_of=AT, threshold=0.5)
        assert EffectiveCountMethod.EIGENVALUE_EFFECTIVE_RANK in est.by_method
        assert EffectiveCountMethod.CORRELATION_CLUSTER_COUNT in est.by_method
        assert EffectiveCountMethod.RISK_FACTOR_CLUSTERING in est.by_method
        # correlation-based methods see low overlap (near-independent);
        # factor-based method sees identical exposure (fully overlapping) --
        # exactly why the doc wants them kept side by side, not averaged.
        assert est.method(EffectiveCountMethod.CORRELATION_CLUSTER_COUNT).value == 3.0
        assert est.method(EffectiveCountMethod.RISK_FACTOR_CLUSTERING).value == 1.0

    def test_no_inputs_at_all_is_insufficient_data(self):
        with pytest.raises(InsufficientDataError):
            estimate_effective_independent_cells(["A", "B"], as_of=AT)

    def test_opportunity_breadth_uses_the_same_machinery(self):
        ids = ["opp1", "opp2"]
        regime = _regime({("opp1", "opp2"): 0.0})
        est = estimate_opportunity_breadth(ids, regime=regime, as_of=AT)
        assert est.method(EffectiveCountMethod.EIGENVALUE_EFFECTIVE_RANK).value == pytest.approx(2.0)

    def test_correlation_only_omits_factor_method(self):
        ids = ["A", "B"]
        regime = _regime({("A", "B"): 0.0})
        est = estimate_effective_independent_cells(ids, regime=regime, as_of=AT)
        assert EffectiveCountMethod.RISK_FACTOR_CLUSTERING not in est.by_method
