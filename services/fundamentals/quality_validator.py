"""Fundamental Quality Validator (research-instruction §6).

Distinguishes a structurally-improving company from one whose numbers moved
for a reason that will not repeat — never inferring STRUCTURAL_IMPROVEMENT
when the underlying trend is short, mixed, or explained by a flagged
one-off factor. Flags themselves are not computed here: §7's Management
Language Tracker / a real filing-text analysis is the eventual source
(Phase B); Phase A's mock source (services.fundamentals.mock_source)
stands in for that with synthetic flags so this validator has real input
to react to.
"""
from __future__ import annotations

from typing import Sequence

from packages.schemas.fundamentals import (
    FundamentalAssessment,
    InflectionDirection,
    MetricTrend,
    TemporaryFactorFlag,
)

# A flag here means an observed improvement might not repeat — it downgrades
# STRUCTURAL_IMPROVEMENT to TEMPORARY_IMPROVEMENT rather than confirming it.
_DISQUALIFYING_FLAGS = frozenset({
    TemporaryFactorFlag.ONE_TIME_GAIN,
    TemporaryFactorFlag.FX_IMPACT,
    TemporaryFactorFlag.ACCOUNTING_CHANGE,
    TemporaryFactorFlag.MERGER_OR_ACQUISITION,
    TemporaryFactorFlag.DIVESTITURE,
    TemporaryFactorFlag.ONE_TIME_LICENSE_INCOME,
    TemporaryFactorFlag.RESTRUCTURING_COST_CUT,
    TemporaryFactorFlag.NON_GAAP_ADJUSTMENT,
})

# Minimum number of tracked metrics that must show a real (non-INSUFFICIENT_DATA)
# reading before this validator will render any verdict other than
# INSUFFICIENT_DATA — one lone metric with data is not enough context (§6).
_MIN_METRICS_WITH_DATA = 3


def validate(trends: Sequence[MetricTrend],
            flags: Sequence[TemporaryFactorFlag] = ()) -> FundamentalAssessment:
    with_data = [t for t in trends if t.direction is not InflectionDirection.INSUFFICIENT_DATA]
    if len(with_data) < _MIN_METRICS_WITH_DATA:
        return FundamentalAssessment.INSUFFICIENT_DATA

    improving = [t for t in with_data if t.direction is InflectionDirection.IMPROVING]
    deteriorating = [t for t in with_data if t.direction is InflectionDirection.DETERIORATING]

    disqualifying = [f for f in flags if f in _DISQUALIFYING_FLAGS]

    # Deterioration is reported even in the presence of unrelated flags —
    # a flag explains why an IMPROVEMENT might not last, it does not excuse
    # an actual decline.
    if len(deteriorating) > len(improving) and deteriorating:
        return FundamentalAssessment.DETERIORATION

    if improving and len(improving) >= len(with_data) / 2:
        if disqualifying:
            return FundamentalAssessment.TEMPORARY_IMPROVEMENT
        return FundamentalAssessment.STRUCTURAL_IMPROVEMENT

    return FundamentalAssessment.UNCERTAIN
