"""Allocation Governor tests (docs/capital_cell_architecture.md §28-30,
§41 priority 12)."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from services.capital_cells.allocation_governor import (
    AllocationReserve,
    CellAllocationInputs,
    CellCreationGovernor,
    CellCreationProposal,
    allocate_with_reserve,
    rank_cells,
    uncertainty_adjusted_score,
)
from services.capital_cells.capacity_tail_risk import TailRiskMetrics
from services.capital_cells.estimation import PointEstimate, RangeEstimate

AT = datetime(2026, 8, 10, 15, 0, tzinfo=timezone.utc)


def _range(low, base, high, confidence=0.9, sample_size=40) -> RangeEstimate:
    return RangeEstimate(low=low, base=base, high=high, confidence=confidence,
                         sample_size=sample_size, method_version="v", as_of=AT)


def _point(value, confidence=0.9, sample_size=40) -> PointEstimate:
    return PointEstimate(value=value, confidence=confidence, sample_size=sample_size,
                         method_version="v", as_of=AT)


def _tail(es=0.05, sample_size=40) -> TailRiskMetrics:
    return TailRiskMetrics(confidence_level=0.95, var=es * 0.8, expected_shortfall=es,
                           sample_size=sample_size, method_version="v", as_of=AT)


def _inputs(cell_id="A", net_edge=None, regime_stability=0.9, capacity=None, tail_risk=None,
           correlation_penalty=0.1, drawdown=0.0, cost_drag=0.0) -> CellAllocationInputs:
    return CellAllocationInputs(
        cell_id=cell_id, net_edge=net_edge or _range(0.01, 0.02, 0.03),
        regime_stability=regime_stability, capacity=capacity or _range(1e6, 2e6, 3e6),
        tail_risk=tail_risk or _tail(), correlation_penalty=correlation_penalty,
        drawdown=drawdown, cost_drag=cost_drag)


class TestUncertaintyAdjustedScore:
    def test_low_confidence_edge_uses_conservative_low_bound(self):
        thin = _inputs(net_edge=_range(0.005, 0.05, 0.10, confidence=0.2))
        rich = _inputs(net_edge=_range(0.005, 0.05, 0.10, confidence=0.95))
        thin_score = uncertainty_adjusted_score(thin, as_of=AT)
        rich_score = uncertainty_adjusted_score(rich, as_of=AT)
        # same base edge, but thin uses .low (0.005) while rich uses .base (0.05)
        assert thin_score.value < rich_score.value

    def test_higher_correlation_reduces_score(self):
        low_corr = uncertainty_adjusted_score(_inputs(correlation_penalty=0.1), as_of=AT)
        high_corr = uncertainty_adjusted_score(_inputs(correlation_penalty=0.8), as_of=AT)
        assert high_corr.value < low_corr.value

    def test_higher_drawdown_reduces_score(self):
        no_dd = uncertainty_adjusted_score(_inputs(drawdown=0.0), as_of=AT)
        deep_dd = uncertainty_adjusted_score(_inputs(drawdown=0.5), as_of=AT)
        assert deep_dd.value < no_dd.value

    def test_higher_tail_risk_reduces_score(self):
        safe = uncertainty_adjusted_score(_inputs(tail_risk=_tail(es=0.01)), as_of=AT)
        risky = uncertainty_adjusted_score(_inputs(tail_risk=_tail(es=0.50)), as_of=AT)
        assert risky.value < safe.value

    def test_win_rate_alone_cannot_win_against_bad_fundamentals(self):
        """§28: a cell whose ONLY good quality is a high implied edge (the
        kind of number a naive 'recent win rate is up' read would produce)
        must lose to a modest-edge cell with good risk characteristics."""
        flashy_but_risky = _inputs(
            cell_id="flashy", net_edge=_range(0.02, 0.15, 0.20, confidence=0.3),
            correlation_penalty=0.9, drawdown=0.4, tail_risk=_tail(es=0.6))
        modest_but_sound = _inputs(
            cell_id="sound", net_edge=_range(0.02, 0.03, 0.04, confidence=0.9),
            correlation_penalty=0.05, drawdown=0.0, tail_risk=_tail(es=0.01))
        ranked = rank_cells([flashy_but_risky, modest_but_sound], as_of=AT)
        assert ranked[0][0] == "sound"

    def test_rank_cells_sorts_descending(self):
        ranked = rank_cells([
            _inputs(cell_id="low", net_edge=_range(0.001, 0.001, 0.001)),
            _inputs(cell_id="high", net_edge=_range(0.05, 0.05, 0.05)),
        ], as_of=AT)
        assert [cid for cid, _ in ranked] == ["high", "low"]

    def test_out_of_range_fraction_fields_rejected(self):
        with pytest.raises(ValueError):
            _inputs(regime_stability=1.5)
        with pytest.raises(ValueError):
            _inputs(correlation_penalty=-0.1)
        with pytest.raises(ValueError):
            _inputs(drawdown=2.0)


class TestAllocationReserve:
    def test_allocatable_to_cells_subtracts_reserves(self):
        r = AllocationReserve(total_capital=1000.0, cash_reserve=200.0, risk_reserve=100.0)
        assert r.allocatable_to_cells == pytest.approx(700.0)

    def test_reserves_exceeding_capital_rejected(self):
        with pytest.raises(ValueError):
            AllocationReserve(total_capital=100.0, cash_reserve=80.0, risk_reserve=50.0)


class TestAllocateWithReserve:
    def test_qualifying_cells_split_allocatable_equally(self):
        ranked = [("A", _point(0.5)), ("B", _point(0.3))]
        allocations, reserve = allocate_with_reserve(1000.0, ranked, min_score_to_fund=0.0)
        assert allocations == {"A": pytest.approx(500.0), "B": pytest.approx(500.0)}
        assert reserve.unallocated_capital == 0.0

    def test_reserves_are_carved_out_before_splitting(self):
        ranked = [("A", _point(0.5))]
        allocations, reserve = allocate_with_reserve(
            1000.0, ranked, cash_reserve_fraction=0.2, risk_reserve_fraction=0.1)
        assert reserve.cash_reserve == pytest.approx(200.0)
        assert reserve.risk_reserve == pytest.approx(100.0)
        assert allocations["A"] == pytest.approx(700.0)

    def test_below_threshold_cells_are_excluded(self):
        ranked = [("A", _point(0.5)), ("B", _point(-0.1))]
        allocations, _ = allocate_with_reserve(1000.0, ranked, min_score_to_fund=0.0)
        assert "B" not in allocations
        assert allocations["A"] == pytest.approx(1000.0)

    def test_no_qualifying_cells_holds_cash_instead_of_forcing_allocation(self):
        """§29: opportunity shortage -> unallocated capital, never a forced
        allocation to a cell that didn't earn it."""
        ranked = [("A", _point(-0.5)), ("B", _point(-0.2))]
        allocations, reserve = allocate_with_reserve(1000.0, ranked, min_score_to_fund=0.0)
        assert allocations == {}
        assert reserve.unallocated_capital == pytest.approx(1000.0)

    def test_invalid_fractions_rejected(self):
        with pytest.raises(ValueError):
            allocate_with_reserve(1000.0, [], cash_reserve_fraction=1.5)
        with pytest.raises(ValueError):
            allocate_with_reserve(1000.0, [], cash_reserve_fraction=0.7, risk_reserve_fraction=0.5)


class TestCellCreationGovernor:
    def _proposal(self, **overrides) -> CellCreationProposal:
        defaults = dict(
            proposed_cell_id="new-cell",
            alpha_hypothesis="20-day momentum with volume confirmation",
            existing_hypotheses=("news sentiment alpha", "mean reversion on gap-down"),
            opportunity_availability=_point(5.0, confidence=0.8),
            independence_evidence=_point(0.9, confidence=0.8),
            expected_incremental_value=_range(0.01, 0.05, 0.10, confidence=0.8),
            operational_capacity_available=True,
        )
        defaults.update(overrides)
        return CellCreationProposal(**defaults)

    def test_well_supported_proposal_is_approved(self):
        decision = CellCreationGovernor().review(self._proposal())
        assert decision.approved
        assert decision.reasons == ()

    def test_duplicate_hypothesis_rejected(self):
        decision = CellCreationGovernor().review(self._proposal(
            alpha_hypothesis="  News Sentiment Alpha  "))   # case/whitespace variant of an existing one
        assert not decision.approved
        assert any("not distinct" in r for r in decision.reasons)

    def test_low_opportunity_confidence_rejected(self):
        decision = CellCreationGovernor().review(self._proposal(
            opportunity_availability=_point(5.0, confidence=0.1)))
        assert not decision.approved
        assert any("opportunity availability" in r for r in decision.reasons)

    def test_low_independence_confidence_rejected(self):
        decision = CellCreationGovernor().review(self._proposal(
            independence_evidence=_point(0.9, confidence=0.1)))
        assert not decision.approved
        assert any("independence evidence" in r for r in decision.reasons)

    def test_negative_incremental_value_rejected(self):
        decision = CellCreationGovernor().review(self._proposal(
            expected_incremental_value=_range(-0.05, -0.01, 0.02, confidence=0.9)))
        assert not decision.approved
        assert any("incremental value" in r for r in decision.reasons)

    def test_thin_incremental_value_evidence_uses_conservative_low_bound(self):
        """Low confidence -> the .low bound gates approval, even if .base
        looks positive."""
        decision = CellCreationGovernor().review(self._proposal(
            expected_incremental_value=_range(-0.01, 0.05, 0.10, confidence=0.1)))
        assert not decision.approved

    def test_no_operational_capacity_rejected(self):
        decision = CellCreationGovernor().review(self._proposal(
            operational_capacity_available=False))
        assert not decision.approved
        assert any("operational capacity" in r for r in decision.reasons)

    def test_multiple_failures_all_reported(self):
        decision = CellCreationGovernor().review(self._proposal(
            operational_capacity_available=False,
            opportunity_availability=_point(5.0, confidence=0.1)))
        assert len(decision.reasons) >= 2

    def test_cell_count_maximization_is_not_rewarded(self):
        """Proposing many cells with identical (good) evidence does not make
        any single proposal score higher -- review() only ever returns
        approved/reasons, never a count-based bonus."""
        governor = CellCreationGovernor()
        first = governor.review(self._proposal())
        again = governor.review(self._proposal(proposed_cell_id="yet-another"))
        assert first.approved == again.approved   # no advantage to proposing more
