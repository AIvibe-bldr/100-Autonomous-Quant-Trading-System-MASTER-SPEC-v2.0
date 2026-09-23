"""Fundamental-Price Divergence Engine (research-instruction §10).

Compares an already-computed FundamentalSignal against the scanner's own
`momentum_20d` — a pure function with no new data fetch and no side
effects, the same "derive from what's already there" precedent as
`services.fundamentals.catalyst_tracker` (which classifies already-fetched
NewsSignals rather than fetching its own text).
"""
from __future__ import annotations

from datetime import datetime

from packages.schemas.fundamentals import (
    DivergenceSignal,
    DivergenceType,
    FundamentalAssessment,
    FundamentalSignal,
)

# "Price hasn't reacted yet" / "price has already run up" thresholds on 20d
# momentum. _FLAT_MOMENTUM deliberately matches MockDecisionModel's own BUY
# momentum bar (0.02) so "flat" means the same thing here as everywhere else
# in the pipeline, not a second, disagreeing definition.
_FLAT_MOMENTUM = 0.02
_STRONG_MOMENTUM = 0.10

_IMPROVING = frozenset({FundamentalAssessment.STRUCTURAL_IMPROVEMENT,
                        FundamentalAssessment.TEMPORARY_IMPROVEMENT})


def detect_divergence(fundamental: FundamentalSignal, momentum_20d: float,
                      as_of: datetime) -> DivergenceSignal:
    if fundamental.assessment is FundamentalAssessment.INSUFFICIENT_DATA:
        divergence_type = DivergenceType.INSUFFICIENT_DATA
    elif fundamental.assessment in _IMPROVING and momentum_20d < _FLAT_MOMENTUM:
        divergence_type = DivergenceType.POSITIVE_DIVERGENCE
    elif (fundamental.assessment is FundamentalAssessment.DETERIORATION
          and momentum_20d > _STRONG_MOMENTUM):
        divergence_type = DivergenceType.NEGATIVE_DIVERGENCE
    else:
        divergence_type = DivergenceType.ALIGNED
    return DivergenceSignal(symbol=fundamental.symbol, divergence_type=divergence_type,
                            fundamental_assessment=fundamental.assessment,
                            momentum_20d=momentum_20d, as_of=as_of)
