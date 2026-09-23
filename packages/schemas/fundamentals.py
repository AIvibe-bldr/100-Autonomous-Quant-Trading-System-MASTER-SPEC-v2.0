"""Fundamental Inflection Engine schemas (research-instruction "Fundamental
Inflection Engine — 企業成長転換点検出機能", §4/§6/§16).

`FinancialStatement` is the raw filed data (what a real SEC/XBRL adapter
would eventually produce); `FinancialMetrics` is everything computed from
it in Python (§4: "計算はPythonの固定ロジックで行い、LLMに数値計算を丸投げ
しないこと"). §16's Point-in-Time fields live on the raw statement, since
that is the record whose timing actually matters for future-leakage
prevention — metrics computed from it inherit correctness by construction
(a metrics object is only ever built from statements already filtered by
`filing_timestamp`/`received_timestamp`).

Deliberately separate from `packages.schemas.core` (the AI-facing
Decision/Risk/Execution pipeline schemas): this is upstream research data,
consumed by `services.decision.models.DecisionContext` the same way
`InstitutionalSignal` already is, not itself part of the order pipeline.
"""
from __future__ import annotations

import enum
from datetime import datetime
from typing import Optional

from pydantic import Field, model_validator

from packages.schemas.core import StrictModel


class FinancialStatement(StrictModel):
    """One quarter's raw filed figures for one symbol (§4).

    `gross_profit` is deliberately NOT stored here — it is `revenue -
    cost_of_revenue`, computed once in FinancialMetrics, so there is no
    field that could silently disagree with its own inputs.
    """

    symbol: str = Field(min_length=1)
    fiscal_year: int = Field(ge=1990, le=2100)
    fiscal_quarter: int = Field(ge=1, le=4)
    revenue: float = Field(ge=0, allow_inf_nan=False)
    cost_of_revenue: float = Field(ge=0, allow_inf_nan=False)
    operating_income: float = Field(allow_inf_nan=False)   # may be negative
    net_income: float = Field(allow_inf_nan=False)         # may be negative
    eps_diluted: float = Field(allow_inf_nan=False)
    shares_diluted: float = Field(gt=0, allow_inf_nan=False)
    operating_cash_flow: float = Field(allow_inf_nan=False)
    capital_expenditure: float = Field(ge=0, allow_inf_nan=False)
    total_assets: float = Field(gt=0, allow_inf_nan=False)
    total_debt: float = Field(ge=0, allow_inf_nan=False)
    cash_and_equivalents: float = Field(ge=0, allow_inf_nan=False)
    ebitda: float = Field(allow_inf_nan=False)
    interest_expense: float = Field(ge=0, allow_inf_nan=False)
    effective_tax_rate: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)

    # §16: Point-in-Time — required so a backtest can never see a quarter's
    # numbers before they actually existed (Future Leakage).
    filing_timestamp: datetime      # when the filer submitted it (e.g. to EDGAR)
    publication_timestamp: datetime  # when it became publicly visible
    received_timestamp: datetime    # when THIS system ingested it — the one
                                    # PointInTimeStore.get_as_of() filters on
    effective_timestamp: datetime   # fiscal period end the figures describe
    data_version: str = "1"         # bumped on restatement, never overwritten in place
    source_url: str = ""
    source: str = "mock"

    @model_validator(mode="after")
    def _balance_sheet_sane(self) -> "FinancialStatement":
        # cash is by definition a component of total assets — this is a
        # true accounting invariant, not a heuristic (unlike, say, a
        # negative gross margin, which is unusual but real for distressed
        # or early-stage companies and is deliberately NOT rejected here).
        if self.cash_and_equivalents > self.total_assets:
            raise ValueError(
                f"{self.symbol} {self.fiscal_year}Q{self.fiscal_quarter}: "
                f"cash ({self.cash_and_equivalents}) cannot exceed total assets "
                f"({self.total_assets})")
        return self


class InflectionDirection(str, enum.Enum):
    IMPROVING = "IMPROVING"
    DETERIORATING = "DETERIORATING"
    FLAT = "FLAT"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"


class FinancialMetrics(StrictModel):
    """Everything derived from one or more `FinancialStatement`s for one
    quarter, via fixed Python arithmetic (§4) — never an LLM estimate.

    Growth/trend fields are Optional: a symbol's first observed quarter (or
    a quarter with no matching prior-year quarter yet) has no growth rate to
    report, and "no growth rate available" must never silently read as 0%
    growth (a materially different, false claim)."""

    symbol: str = Field(min_length=1)
    fiscal_year: int = Field(ge=1990, le=2100)
    fiscal_quarter: int = Field(ge=1, le=4)

    # Profitability (§4) — Gross Margin and Gross Profitability are DIFFERENT
    # ratios with the same numerator and different denominators; never
    # conflate them (§21's own required test). Margins are Optional: a
    # pre-revenue company (revenue=0, legal per FinancialStatement) has an
    # undefined margin, not a 0% one.
    gross_margin: Optional[float] = Field(default=None, allow_inf_nan=False)  # gross_profit/revenue
    operating_margin: Optional[float] = Field(default=None, allow_inf_nan=False)
    net_margin: Optional[float] = Field(default=None, allow_inf_nan=False)
    gross_profitability: float = Field(allow_inf_nan=False)     # gross_profit / total_assets
    roic: Optional[float] = Field(default=None, allow_inf_nan=False)  # NOPAT / invested_capital
    roic_minus_wacc: Optional[float] = Field(default=None, allow_inf_nan=False)

    # Growth (§4) — QoQ and YoY, since a single baseline can't answer both
    # "did it just improve" (§5 QoQ) and "is it actually bigger than a year
    # ago, seasonality included" (§5 YoY).
    revenue_growth_yoy: Optional[float] = Field(default=None, allow_inf_nan=False)
    revenue_growth_qoq: Optional[float] = Field(default=None, allow_inf_nan=False)
    revenue_growth_acceleration: Optional[float] = Field(default=None, allow_inf_nan=False)
    gross_profit_growth_yoy: Optional[float] = Field(default=None, allow_inf_nan=False)
    operating_income_growth_yoy: Optional[float] = Field(default=None, allow_inf_nan=False)
    eps_growth_yoy: Optional[float] = Field(default=None, allow_inf_nan=False)
    free_cash_flow_growth_yoy: Optional[float] = Field(default=None, allow_inf_nan=False)

    # Cash Flow (§4)
    free_cash_flow: float = Field(allow_inf_nan=False)          # OCF - capex
    operating_cash_flow_margin: Optional[float] = Field(default=None, allow_inf_nan=False)
    cash_conversion: Optional[float] = Field(default=None, allow_inf_nan=False)  # OCF / net_income

    # Balance Sheet (§4)
    net_debt: float = Field(allow_inf_nan=False)                # total_debt - cash
    debt_to_ebitda: Optional[float] = Field(default=None, allow_inf_nan=False)
    interest_coverage: Optional[float] = Field(default=None, allow_inf_nan=False)
    share_dilution_yoy: Optional[float] = Field(default=None, allow_inf_nan=False)

    # Capital Efficiency (§4)
    reinvestment_rate: Optional[float] = Field(default=None, allow_inf_nan=False)
    incremental_roic: Optional[float] = Field(default=None, allow_inf_nan=False)


class TemporaryFactorFlag(str, enum.Enum):
    """§6: reasons an improvement might not be structural."""

    SEASONAL = "SEASONAL"
    ONE_TIME_GAIN = "ONE_TIME_GAIN"
    FX_IMPACT = "FX_IMPACT"
    ACCOUNTING_CHANGE = "ACCOUNTING_CHANGE"
    MERGER_OR_ACQUISITION = "MERGER_OR_ACQUISITION"
    DIVESTITURE = "DIVESTITURE"
    ONE_TIME_LICENSE_INCOME = "ONE_TIME_LICENSE_INCOME"
    RESTRUCTURING_COST_CUT = "RESTRUCTURING_COST_CUT"
    SHARE_DILUTION = "SHARE_DILUTION"
    NON_GAAP_ADJUSTMENT = "NON_GAAP_ADJUSTMENT"


class FundamentalAssessment(str, enum.Enum):
    """§6: the Quality Validator's verdict — never inferred as an
    improvement when data is insufficient (§21's own required test)."""

    STRUCTURAL_IMPROVEMENT = "STRUCTURAL_IMPROVEMENT"
    TEMPORARY_IMPROVEMENT = "TEMPORARY_IMPROVEMENT"
    UNCERTAIN = "UNCERTAIN"
    DETERIORATION = "DETERIORATION"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"


class MetricTrend(StrictModel):
    """One tracked metric's trend across the available quarters (§5)."""

    metric_name: str
    direction: InflectionDirection
    consecutive_periods: int = Field(ge=0)
    latest_value: Optional[float] = Field(default=None, allow_inf_nan=False)
    values: tuple[float, ...] = ()   # most recent last, oldest first


class FundamentalSignal(StrictModel):
    """The one structured object Decision AI actually sees (mirrors
    `InstitutionalSignal`'s role in `DecisionContext`) — never a raw
    statement or metrics dump. `assessment` is the Quality Validator's
    verdict; `trends` names which specific metrics support it."""

    symbol: str = Field(min_length=1)
    assessment: FundamentalAssessment
    flags: tuple[TemporaryFactorFlag, ...] = ()
    trends: tuple[MetricTrend, ...] = ()
    as_of: datetime
    quarters_observed: int = Field(ge=0)

    @property
    def actionable(self) -> bool:
        """Never STRUCTURAL_IMPROVEMENT off a guess — §11/§24: this is
        research input, not a BUY signal on its own regardless of this
        flag; downstream (Decision AI → ... → Master Risk Controller) is
        what actually gates an order."""
        return self.assessment is FundamentalAssessment.STRUCTURAL_IMPROVEMENT


class DivergenceType(str, enum.Enum):
    """§10: how price/momentum has (or hasn't) reacted to the fundamental
    picture. This compares an already-computed FundamentalSignal against
    price action — it never restates either input on its own."""

    POSITIVE_DIVERGENCE = "POSITIVE_DIVERGENCE"  # fundamentals improving, price hasn't reacted yet
    NEGATIVE_DIVERGENCE = "NEGATIVE_DIVERGENCE"  # price has run up without fundamental support
    ALIGNED = "ALIGNED"                          # price and fundamentals point the same way
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"


class DivergenceSignal(StrictModel):
    """Output of the Fundamental-Price Divergence Engine (§10). Mirrors
    `FundamentalSignal`'s role in `DecisionContext` — a structured verdict,
    never a raw comparison the AI has to interpret itself."""

    symbol: str = Field(min_length=1)
    divergence_type: DivergenceType
    fundamental_assessment: FundamentalAssessment
    momentum_20d: float = Field(allow_inf_nan=False)
    as_of: datetime

    @property
    def notable(self) -> bool:
        """§10/§11: flags where price and fundamentals disagree — still
        research input only, never a BUY/SELL trigger on its own."""
        return self.divergence_type in (DivergenceType.POSITIVE_DIVERGENCE,
                                        DivergenceType.NEGATIVE_DIVERGENCE)
