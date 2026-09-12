"""Capacity / Tail Risk tests (docs/capital_cell_architecture.md §20, §22,
§23, §41 priority 11)."""
from __future__ import annotations

import random
from datetime import datetime, timezone

import pytest

from services.capital_cells.capacity_tail_risk import (
    capacity_constrained_allocation,
    compute_tail_risk,
    estimate_capacity,
    estimate_risk_contribution,
    safe_side_risk_contribution,
)
from services.capital_cells.estimation import InsufficientDataError, RangeEstimate

AT = datetime(2026, 8, 10, 15, 0, tzinfo=timezone.utc)

RETURNS = [-0.10, -0.08, -0.05, -0.02, 0.01, 0.02, 0.03, 0.04, 0.05, 0.06]


class TestTailRisk:
    def test_known_var_and_es_at_90pct(self):
        m = compute_tail_risk(RETURNS, confidence_level=0.9, as_of=AT, min_sample_size=5)
        assert m.var == pytest.approx(0.10)
        assert m.expected_shortfall == pytest.approx(0.10)

    def test_known_var_and_es_at_80pct(self):
        m = compute_tail_risk(RETURNS, confidence_level=0.8, as_of=AT, min_sample_size=5)
        assert m.var == pytest.approx(0.08)
        assert m.expected_shortfall == pytest.approx(0.09)   # mean(-0.10,-0.08) negated

    def test_cvar_is_an_alias_for_expected_shortfall(self):
        """§20: ES and CVaR must be unmistakably the same metric."""
        m = compute_tail_risk(RETURNS, confidence_level=0.8, as_of=AT, min_sample_size=5)
        assert m.cvar == m.expected_shortfall

    def test_expected_shortfall_is_never_milder_than_var(self):
        for trial_returns in ([RETURNS], [list(reversed(RETURNS))]):
            m = compute_tail_risk(trial_returns[0], confidence_level=0.85, as_of=AT,
                                  min_sample_size=5)
            assert m.expected_shortfall >= m.var - 1e-9

    def test_insufficient_sample_size_raises(self):
        with pytest.raises(InsufficientDataError):
            compute_tail_risk(RETURNS, confidence_level=0.9, as_of=AT, min_sample_size=50)

    def test_confidence_level_out_of_range_rejected(self):
        with pytest.raises(ValueError):
            compute_tail_risk(RETURNS, confidence_level=1.0, as_of=AT, min_sample_size=5)
        with pytest.raises(ValueError):
            compute_tail_risk(RETURNS, confidence_level=0.0, as_of=AT, min_sample_size=5)

    def test_fuzz_es_never_milder_than_var(self):
        random.seed(11)
        for _ in range(200):
            n = random.randint(20, 60)
            returns = [random.uniform(-0.15, 0.15) for _ in range(n)]
            cl = random.uniform(0.5, 0.99)
            m = compute_tail_risk(returns, confidence_level=cl, as_of=AT, min_sample_size=5)
            assert m.expected_shortfall >= m.var - 1e-9


class TestRiskContribution:
    def test_cell_identical_to_portfolio_has_full_contribution(self):
        pnl = [float(i % 7) - 3 for i in range(40)]
        est = estimate_risk_contribution(pnl, list(pnl), as_of=AT, min_sample_size=10)
        assert est.base == pytest.approx(1.0)
        assert est.low == pytest.approx(1.0)
        assert est.high == pytest.approx(1.0)

    def test_flat_cell_contributes_zero_risk(self):
        portfolio = [float(i % 7) - 3 for i in range(40)]
        flat = [0.0] * 40
        est = estimate_risk_contribution(flat, portfolio, as_of=AT, min_sample_size=10)
        assert est.base == pytest.approx(0.0)

    def test_mismatched_lengths_rejected(self):
        with pytest.raises(ValueError):
            estimate_risk_contribution([1.0, 2.0], [1.0], as_of=AT, min_sample_size=1)

    def test_insufficient_sample_size_raises(self):
        with pytest.raises(InsufficientDataError):
            estimate_risk_contribution([1.0, 2.0], [1.0, 2.0], as_of=AT, min_sample_size=20)

    def test_thin_blocks_report_reduced_confidence_not_a_fabricated_range(self):
        pnl = [float(i) for i in range(12)]
        est = estimate_risk_contribution(pnl, list(pnl), as_of=AT, min_sample_size=10, n_blocks=4)
        assert est.confidence == 0.5
        assert est.low == est.base == est.high

    def test_two_alternating_regimes_widen_the_range(self):
        """Contribution should differ across sub-periods when the
        relationship between cell and portfolio genuinely changes --
        confirms low/high are not just copies of base."""
        n = 40
        portfolio = [float(i) for i in range(n)]
        # first half: cell tracks portfolio; second half: cell is flat
        cell = [float(i) for i in range(n // 2)] + [0.0] * (n // 2)
        est = estimate_risk_contribution(cell, portfolio, as_of=AT, min_sample_size=10)
        assert est.high > est.low

    def test_safe_side_uses_high_bound_when_confidence_is_low(self):
        est = RangeEstimate(low=0.1, base=0.3, high=0.6, confidence=0.4,
                            sample_size=40, method_version="v", as_of=AT)
        assert safe_side_risk_contribution(est, min_confidence=0.6) == pytest.approx(0.6)

    def test_safe_side_uses_base_when_confidence_is_high_enough(self):
        est = RangeEstimate(low=0.1, base=0.3, high=0.6, confidence=0.8,
                            sample_size=40, method_version="v", as_of=AT)
        assert safe_side_risk_contribution(est, min_confidence=0.6) == pytest.approx(0.3)


class TestCapacity:
    def test_known_capacity_range(self):
        est = estimate_capacity({"NVDA": 100_000_000.0, "MSFT": 50_000_000.0}, as_of=AT)
        assert est.low == pytest.approx(1_500_000.0)
        assert est.base == pytest.approx(3_000_000.0)
        assert est.high == pytest.approx(4_500_000.0)

    def test_empty_universe_is_insufficient_data(self):
        with pytest.raises(InsufficientDataError):
            estimate_capacity({}, as_of=AT)

    def test_misordered_participation_rates_rejected(self):
        with pytest.raises(ValueError):
            estimate_capacity({"NVDA": 1_000_000.0}, as_of=AT,
                              participation_rate_low=0.05, participation_rate_high=0.01)

    def test_allocation_uses_base_when_confidence_is_sufficient(self):
        cap = estimate_capacity({"NVDA": 100_000_000.0}, as_of=AT, confidence=0.9)  # base=2M
        assert capacity_constrained_allocation(5_000_000.0, cap) == pytest.approx(cap.base)

    def test_allocation_falls_back_to_low_bound_when_confidence_is_insufficient(self):
        cap = estimate_capacity({"NVDA": 100_000_000.0}, as_of=AT, confidence=0.2)  # low=1M
        assert capacity_constrained_allocation(5_000_000.0, cap) == pytest.approx(cap.low)

    def test_allocation_never_exceeds_the_request_itself(self):
        cap = estimate_capacity({"NVDA": 100_000_000.0}, as_of=AT, confidence=0.9)  # base=2M
        assert capacity_constrained_allocation(500_000.0, cap) == pytest.approx(500_000.0)
