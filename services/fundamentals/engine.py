"""Fundamental Inflection Engine orchestrator (research-instruction §0/§4-6).

Financial Data -> Financial Metrics (§4) -> Quarterly Trend Detector (§5)
-> Fundamental Quality Validator (§6) -> FundamentalSignal.

This is the one object services.pipeline.TradingPipeline calls — everything
upstream (statement fetching, metric arithmetic, trend/quality logic) is
composed here so the pipeline only ever sees the final structured signal,
the same shape as RegimeEngine.classify()/InstitutionalFlowEngine.signal().
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional, Protocol

from packages.common.clock import ensure_utc
from packages.schemas.fundamentals import (
    FinancialStatement,
    FundamentalAssessment,
    FundamentalSignal,
    TemporaryFactorFlag,
)
from services.fundamentals.detector import compute_trends
from services.fundamentals.metrics import DEFAULT_WACC, compute_metrics
from services.fundamentals.mock_source import MockFinancialDataSource
from services.fundamentals.quality_validator import validate


class FinancialDataSource(Protocol):
    def fetch_history(self, symbol: str, as_of: datetime,
                      quarters: int) -> list[FinancialStatement]: ...

    def fetch_flags(self, symbol: str, fiscal_year: int,
                    fiscal_quarter: int) -> tuple[TemporaryFactorFlag, ...]: ...


class FundamentalInflectionEngine:
    def __init__(self, source: Optional[FinancialDataSource] = None,
                min_periods: int = 3, wacc: float = DEFAULT_WACC,
                lookback_quarters: int = 9) -> None:
        self.source = source or MockFinancialDataSource()
        self.min_periods = min_periods
        self.wacc = wacc
        self.lookback_quarters = lookback_quarters

    def analyze(self, symbol: str, as_of: datetime) -> FundamentalSignal:
        as_of = ensure_utc(as_of)
        statements = self.source.fetch_history(symbol, as_of, quarters=self.lookback_quarters)
        # §16 Point-in-Time: a quarter that hasn't actually been FILED yet as
        # of `as_of` must not be visible, however far back it "belongs" —
        # this is what stops a backtest from reading a quarter's results
        # before they existed (Future Leakage).
        visible = [s for s in statements if s.received_timestamp <= as_of]
        if len(visible) < self.min_periods:
            return FundamentalSignal(
                symbol=symbol, assessment=FundamentalAssessment.INSUFFICIENT_DATA,
                as_of=as_of, quarters_observed=len(visible))

        metrics_history = []
        for i, stmt in enumerate(visible):
            prior_quarter = visible[i - 1] if i >= 1 else None
            year_ago = visible[i - 4] if i >= 4 else None
            prior_year_ago = visible[i - 8] if i >= 8 else None
            metrics_history.append(compute_metrics(
                stmt, prior_quarter=prior_quarter, year_ago=year_ago,
                prior_year_ago=prior_year_ago, wacc=self.wacc))

        trends = compute_trends(metrics_history, min_periods=self.min_periods)
        latest = visible[-1]
        flags = self.source.fetch_flags(symbol, latest.fiscal_year, latest.fiscal_quarter)
        assessment = validate(trends, flags)
        return FundamentalSignal(symbol=symbol, assessment=assessment, flags=flags,
                                 trends=trends, as_of=as_of, quarters_observed=len(visible))
