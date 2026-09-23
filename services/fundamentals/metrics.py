"""Financial Metrics calculator (research-instruction §4) — fixed Python
arithmetic only, never an LLM estimate.

Every growth/ratio field that can be undefined (a zero or missing
denominator, no prior-period comparison available yet) returns `None`
rather than 0.0, inf, or a fabricated number — "no data" and "zero growth"
are different claims, and conflating them is exactly the kind of silent
placeholder this codebase has repeatedly had to fix elsewhere (see
docs/SAFETY_AUDIT.md F2/F8).

Invested capital is simplified to `total_assets - cash_and_equivalents`
(capital deployed in the operating business, excluding non-earning cash) —
`FinancialStatement` does not model a full balance sheet (no separate
liabilities/equity split), so a more granular textbook definition isn't
available from this data. Documented here, not hidden in the formula.
"""
from __future__ import annotations

from typing import Optional

from packages.schemas.fundamentals import FinancialMetrics, FinancialStatement

DEFAULT_WACC = 0.09   # fixed research assumption; not per-company (no cost-of-capital
                       # model exists here) — revisit if this becomes a real product input


def _safe_ratio(numerator: float, denominator: float) -> Optional[float]:
    if denominator == 0:
        return None
    return numerator / denominator


def _safe_growth(new: float, old: float) -> Optional[float]:
    """(new - old) / |old| — the |old| denominator keeps the result
    meaningful when the base period was negative (e.g. operating income
    turning from a loss to a profit), which a plain old-denominator growth
    rate would report with a misleading sign."""
    if old == 0:
        return None
    return (new - old) / abs(old)


def _invested_capital(stmt: FinancialStatement) -> float:
    return stmt.total_assets - stmt.cash_and_equivalents


def _nopat(stmt: FinancialStatement) -> float:
    return stmt.operating_income * (1 - stmt.effective_tax_rate)


def compute_metrics(current: FinancialStatement,
                    prior_quarter: Optional[FinancialStatement] = None,
                    year_ago: Optional[FinancialStatement] = None,
                    prior_year_ago: Optional[FinancialStatement] = None,
                    wacc: float = DEFAULT_WACC) -> FinancialMetrics:
    """`prior_quarter` (immediately preceding quarter) drives QoQ figures;
    `year_ago` (same fiscal quarter, one year prior) drives YoY figures —
    §5 requires both, since a QoQ-only view can't tell seasonal swings from
    real improvement. `prior_year_ago` (same quarter, two years prior) is
    only used for revenue_growth_acceleration (needs year_ago's OWN YoY
    growth, which itself needs a comparison point)."""
    gross_profit = current.revenue - current.cost_of_revenue
    invested_capital = _invested_capital(current)
    nopat = _nopat(current)
    roic = _safe_ratio(nopat, invested_capital)
    free_cash_flow = current.operating_cash_flow - current.capital_expenditure

    revenue_growth_yoy = (_safe_growth(current.revenue, year_ago.revenue)
                          if year_ago else None)
    revenue_growth_qoq = (_safe_growth(current.revenue, prior_quarter.revenue)
                          if prior_quarter else None)
    revenue_growth_acceleration = None
    if year_ago is not None and prior_year_ago is not None:
        prior_yoy = _safe_growth(year_ago.revenue, prior_year_ago.revenue)
        if prior_yoy is not None and revenue_growth_yoy is not None:
            revenue_growth_acceleration = revenue_growth_yoy - prior_yoy

    gross_profit_growth_yoy = None
    operating_income_growth_yoy = None
    eps_growth_yoy = None
    free_cash_flow_growth_yoy = None
    share_dilution_yoy = None
    incremental_roic = None
    if year_ago is not None:
        year_ago_gross_profit = year_ago.revenue - year_ago.cost_of_revenue
        gross_profit_growth_yoy = _safe_growth(gross_profit, year_ago_gross_profit)
        operating_income_growth_yoy = _safe_growth(
            current.operating_income, year_ago.operating_income)
        eps_growth_yoy = _safe_growth(current.eps_diluted, year_ago.eps_diluted)
        year_ago_fcf = year_ago.operating_cash_flow - year_ago.capital_expenditure
        free_cash_flow_growth_yoy = _safe_growth(free_cash_flow, year_ago_fcf)
        share_dilution_yoy = _safe_growth(current.shares_diluted, year_ago.shares_diluted)

        year_ago_ic = _invested_capital(year_ago)
        delta_ic = invested_capital - year_ago_ic
        if abs(delta_ic) > 1e-6:
            delta_nopat = nopat - _nopat(year_ago)
            incremental_roic = delta_nopat / delta_ic

    reinvestment_rate = _safe_ratio(current.capital_expenditure, nopat) if nopat > 0 else None

    return FinancialMetrics(
        symbol=current.symbol, fiscal_year=current.fiscal_year,
        fiscal_quarter=current.fiscal_quarter,
        gross_margin=_safe_ratio(gross_profit, current.revenue),
        operating_margin=_safe_ratio(current.operating_income, current.revenue),
        net_margin=_safe_ratio(current.net_income, current.revenue),
        gross_profitability=gross_profit / current.total_assets,   # total_assets > 0 by schema
        roic=roic,
        roic_minus_wacc=(roic - wacc) if roic is not None else None,
        revenue_growth_yoy=revenue_growth_yoy,
        revenue_growth_qoq=revenue_growth_qoq,
        revenue_growth_acceleration=revenue_growth_acceleration,
        gross_profit_growth_yoy=gross_profit_growth_yoy,
        operating_income_growth_yoy=operating_income_growth_yoy,
        eps_growth_yoy=eps_growth_yoy,
        free_cash_flow_growth_yoy=free_cash_flow_growth_yoy,
        free_cash_flow=free_cash_flow,
        operating_cash_flow_margin=_safe_ratio(current.operating_cash_flow, current.revenue),
        cash_conversion=_safe_ratio(current.operating_cash_flow, current.net_income),
        net_debt=current.total_debt - current.cash_and_equivalents,
        debt_to_ebitda=(_safe_ratio(current.total_debt, current.ebitda)
                        if current.ebitda > 0 else None),
        interest_coverage=(_safe_ratio(current.ebitda, current.interest_expense)
                           if current.interest_expense > 0 else None),
        share_dilution_yoy=share_dilution_yoy,
        reinvestment_rate=reinvestment_rate,
        incremental_roic=incremental_roic,
    )
