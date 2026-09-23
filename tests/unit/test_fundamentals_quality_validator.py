"""Tests for services/fundamentals/quality_validator.py (research-instruction §6).

§21's own required tests this file targets:
- "一時的利益を構造的改善として無条件採用しないこと"
  (test_a_flagged_improvement_downgrades_to_temporary)
- "データ欠損時に改善したと推測してはいけません"
  (test_insufficient_data_never_reads_as_improvement, and friends)
"""
from __future__ import annotations

from packages.schemas.fundamentals import (
    FundamentalAssessment,
    InflectionDirection,
    MetricTrend,
    TemporaryFactorFlag,
)
from services.fundamentals.quality_validator import validate


def _trend(name: str, direction: InflectionDirection, periods: int = 3) -> MetricTrend:
    return MetricTrend(metric_name=name, direction=direction, consecutive_periods=periods,
                       latest_value=0.5, values=(0.4, 0.45, 0.5))


IMPROVING_TRENDS = [
    _trend("gross_margin", InflectionDirection.IMPROVING),
    _trend("operating_margin", InflectionDirection.IMPROVING),
    _trend("revenue_growth_yoy", InflectionDirection.IMPROVING),
    _trend("free_cash_flow", InflectionDirection.IMPROVING),
    _trend("roic", InflectionDirection.IMPROVING),
]


def test_all_metrics_improving_with_no_flags_is_structural():
    assert validate(IMPROVING_TRENDS, flags=()) is FundamentalAssessment.STRUCTURAL_IMPROVEMENT


def test_a_flagged_improvement_downgrades_to_temporary():
    """§6/§21: an otherwise-improving picture explained by a one-off factor
    must NOT be accepted as structural."""
    result = validate(IMPROVING_TRENDS, flags=(TemporaryFactorFlag.ONE_TIME_GAIN,))
    assert result is FundamentalAssessment.TEMPORARY_IMPROVEMENT
    assert result is not FundamentalAssessment.STRUCTURAL_IMPROVEMENT


def test_every_disqualifying_flag_downgrades_the_verdict():
    for flag in (TemporaryFactorFlag.ONE_TIME_GAIN, TemporaryFactorFlag.FX_IMPACT,
                TemporaryFactorFlag.ACCOUNTING_CHANGE, TemporaryFactorFlag.MERGER_OR_ACQUISITION,
                TemporaryFactorFlag.DIVESTITURE, TemporaryFactorFlag.ONE_TIME_LICENSE_INCOME,
                TemporaryFactorFlag.RESTRUCTURING_COST_CUT,
                TemporaryFactorFlag.NON_GAAP_ADJUSTMENT):
        assert validate(IMPROVING_TRENDS, flags=(flag,)) is \
            FundamentalAssessment.TEMPORARY_IMPROVEMENT, flag


def test_insufficient_data_never_reads_as_improvement():
    trends = [_trend("gross_margin", InflectionDirection.INSUFFICIENT_DATA, 0),
             _trend("operating_margin", InflectionDirection.INSUFFICIENT_DATA, 0),
             _trend("revenue_growth_yoy", InflectionDirection.INSUFFICIENT_DATA, 0)]
    result = validate(trends, flags=())
    assert result is FundamentalAssessment.INSUFFICIENT_DATA
    assert result is not FundamentalAssessment.STRUCTURAL_IMPROVEMENT


def test_mostly_insufficient_data_is_still_insufficient_even_with_one_improving():
    """One real metric alone is not enough context to call anything (§6:
    _MIN_METRICS_WITH_DATA) — even if that one metric happens to be
    improving."""
    trends = [_trend("gross_margin", InflectionDirection.IMPROVING),
             _trend("operating_margin", InflectionDirection.INSUFFICIENT_DATA, 0),
             _trend("revenue_growth_yoy", InflectionDirection.INSUFFICIENT_DATA, 0)]
    assert validate(trends, flags=()) is FundamentalAssessment.INSUFFICIENT_DATA


def test_majority_deteriorating_is_deterioration():
    trends = [_trend("gross_margin", InflectionDirection.DETERIORATING),
             _trend("operating_margin", InflectionDirection.DETERIORATING),
             _trend("revenue_growth_yoy", InflectionDirection.DETERIORATING),
             _trend("free_cash_flow", InflectionDirection.IMPROVING)]
    assert validate(trends, flags=()) is FundamentalAssessment.DETERIORATION


def test_deterioration_is_reported_even_with_an_unrelated_flag():
    """A flag explains why an IMPROVEMENT might not last — it must not
    excuse an actual decline."""
    trends = [_trend("gross_margin", InflectionDirection.DETERIORATING),
             _trend("operating_margin", InflectionDirection.DETERIORATING),
             _trend("revenue_growth_yoy", InflectionDirection.DETERIORATING)]
    result = validate(trends, flags=(TemporaryFactorFlag.FX_IMPACT,))
    assert result is FundamentalAssessment.DETERIORATION


def test_mixed_signal_with_no_clear_majority_is_uncertain():
    trends = [_trend("gross_margin", InflectionDirection.IMPROVING),
             _trend("operating_margin", InflectionDirection.DETERIORATING),
             _trend("revenue_growth_yoy", InflectionDirection.FLAT),
             _trend("free_cash_flow", InflectionDirection.FLAT)]
    assert validate(trends, flags=()) is FundamentalAssessment.UNCERTAIN
