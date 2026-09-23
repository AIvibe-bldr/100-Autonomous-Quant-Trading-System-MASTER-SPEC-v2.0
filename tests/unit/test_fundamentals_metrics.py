"""Tests for services/fundamentals/metrics.py (research-instruction §4).

§21's own required tests this file targets directly:
- "Gross MarginとGross Profitabilityの混同防止" (test_gross_margin_and_gross_profitability_are_different_ratios)
- undefined ratios must read as None, never a fabricated 0/inf
  (implicit throughout — the whole point of returning Optional)
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from packages.schemas.fundamentals import FinancialStatement
from services.fundamentals.metrics import compute_metrics

AT = datetime(2026, 8, 11, tzinfo=timezone.utc)


def _stmt(**overrides) -> FinancialStatement:
    base = dict(
        symbol="TEST", fiscal_year=2026, fiscal_quarter=2,
        revenue=1_000_000.0, cost_of_revenue=400_000.0,
        operating_income=200_000.0, net_income=150_000.0,
        eps_diluted=1.5, shares_diluted=100_000.0,
        operating_cash_flow=180_000.0, capital_expenditure=50_000.0,
        total_assets=2_000_000.0, total_debt=500_000.0,
        cash_and_equivalents=300_000.0, ebitda=250_000.0,
        interest_expense=20_000.0, effective_tax_rate=0.21,
        filing_timestamp=AT, publication_timestamp=AT,
        received_timestamp=AT, effective_timestamp=AT)
    base.update(overrides)
    return FinancialStatement(**base)


def test_gross_margin_and_gross_profitability_are_different_ratios():
    """§4: Gross Margin = gross_profit / revenue; Gross Profitability =
    gross_profit / total_assets. Same numerator, deliberately different
    denominators — they must not collapse to the same number just because
    a test happens to use round inputs."""
    stmt = _stmt(revenue=1_000_000.0, cost_of_revenue=400_000.0,   # gross_profit = 600,000
                total_assets=2_000_000.0)
    m = compute_metrics(stmt)
    assert m.gross_margin == pytest.approx(0.6)          # 600,000 / 1,000,000
    assert m.gross_profitability == pytest.approx(0.3)   # 600,000 / 2,000,000
    assert m.gross_margin != m.gross_profitability


def test_margins_are_none_not_zero_when_revenue_is_zero():
    """0/0 is undefined, not 0% — a pre-revenue company's margin must read
    as "no data," never as a (false) 0%."""
    stmt = _stmt(revenue=0.0, cost_of_revenue=0.0, operating_income=-50_000.0,
                net_income=-60_000.0)
    m = compute_metrics(stmt)
    assert m.gross_margin is None
    assert m.operating_margin is None
    assert m.net_margin is None
    assert m.operating_cash_flow_margin is None
    # gross_profitability's denominator is total_assets (schema-guaranteed > 0),
    # so it stays defined even when revenue is zero
    assert m.gross_profitability is not None


def test_roic_is_none_when_invested_capital_is_zero():
    stmt = _stmt(total_assets=500_000.0, cash_and_equivalents=500_000.0)
    m = compute_metrics(stmt)
    assert m.roic is None
    assert m.roic_minus_wacc is None


def test_growth_rates_require_a_comparison_period():
    stmt = _stmt()
    m = compute_metrics(stmt)   # no prior_quarter/year_ago supplied
    assert m.revenue_growth_yoy is None
    assert m.revenue_growth_qoq is None
    assert m.gross_profit_growth_yoy is None
    assert m.eps_growth_yoy is None


def test_revenue_growth_yoy_computed_correctly():
    current = _stmt(revenue=1_100_000.0)
    year_ago = _stmt(revenue=1_000_000.0)
    m = compute_metrics(current, year_ago=year_ago)
    assert m.revenue_growth_yoy == pytest.approx(0.10)


def test_operating_income_growth_handles_a_negative_base_period():
    """A turnaround from a loss to a profit must not report a nonsensical
    negative "growth" just because the denominator was negative — dividing
    by |old| keeps the sign meaningful."""
    current = _stmt(operating_income=50_000.0)
    year_ago = _stmt(operating_income=-100_000.0)
    m = compute_metrics(current, year_ago=year_ago)
    # (50,000 - (-100,000)) / |-100,000| = 1.5 (a genuine, large improvement)
    assert m.operating_income_growth_yoy == pytest.approx(1.5)


def test_debt_to_ebitda_and_interest_coverage_are_none_when_undefined():
    stmt = _stmt(ebitda=0.0, interest_expense=0.0)
    m = compute_metrics(stmt)
    assert m.debt_to_ebitda is None
    assert m.interest_coverage is None


def test_revenue_growth_acceleration_needs_two_years_of_history():
    current = _stmt(revenue=1_200_000.0)
    year_ago = _stmt(revenue=1_100_000.0)
    prior_year_ago = _stmt(revenue=1_000_000.0)
    m = compute_metrics(current, year_ago=year_ago, prior_year_ago=prior_year_ago)
    # current YoY growth: (1.2M - 1.1M)/1.1M ~= 0.0909
    # year_ago's own YoY growth: (1.1M - 1.0M)/1.0M = 0.10
    # acceleration = 0.0909 - 0.10 = -0.0091 (growth actually decelerated slightly)
    assert m.revenue_growth_acceleration == pytest.approx(-0.0091, abs=1e-3)


def test_all_float_fields_reject_infinite_or_nan_inputs():
    with pytest.raises(Exception):
        _stmt(revenue=float("inf"))
    with pytest.raises(Exception):
        _stmt(operating_income=float("nan"))


def test_cash_cannot_exceed_total_assets():
    with pytest.raises(Exception):
        _stmt(total_assets=100.0, cash_and_equivalents=200.0)
