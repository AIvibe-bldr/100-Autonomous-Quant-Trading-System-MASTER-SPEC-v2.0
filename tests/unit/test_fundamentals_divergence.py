"""Tests for services/fundamentals/divergence.py (research-instruction §10)."""
from __future__ import annotations

from datetime import datetime, timezone

from packages.schemas.fundamentals import DivergenceType, FundamentalAssessment, FundamentalSignal
from services.fundamentals.divergence import detect_divergence

AT = datetime(2026, 8, 11, tzinfo=timezone.utc)


def _fundamental(assessment: FundamentalAssessment) -> FundamentalSignal:
    return FundamentalSignal(symbol="AAPL", assessment=assessment, as_of=AT, quarters_observed=7)


def test_improving_fundamentals_with_flat_price_is_a_positive_divergence():
    signal = detect_divergence(
        _fundamental(FundamentalAssessment.STRUCTURAL_IMPROVEMENT), momentum_20d=0.0, as_of=AT)
    assert signal.divergence_type is DivergenceType.POSITIVE_DIVERGENCE
    assert signal.notable


def test_improving_fundamentals_with_already_strong_price_is_aligned_not_divergent():
    signal = detect_divergence(
        _fundamental(FundamentalAssessment.STRUCTURAL_IMPROVEMENT), momentum_20d=0.15, as_of=AT)
    assert signal.divergence_type is DivergenceType.ALIGNED
    assert not signal.notable


def test_deteriorating_fundamentals_with_a_price_runup_is_a_negative_divergence():
    signal = detect_divergence(
        _fundamental(FundamentalAssessment.DETERIORATION), momentum_20d=0.20, as_of=AT)
    assert signal.divergence_type is DivergenceType.NEGATIVE_DIVERGENCE
    assert signal.notable


def test_deteriorating_fundamentals_with_flat_price_is_aligned_not_divergent():
    signal = detect_divergence(
        _fundamental(FundamentalAssessment.DETERIORATION), momentum_20d=0.0, as_of=AT)
    assert signal.divergence_type is DivergenceType.ALIGNED


def test_insufficient_fundamental_data_never_reads_as_a_divergence():
    signal = detect_divergence(
        _fundamental(FundamentalAssessment.INSUFFICIENT_DATA), momentum_20d=0.50, as_of=AT)
    assert signal.divergence_type is DivergenceType.INSUFFICIENT_DATA
    assert not signal.notable


def test_uncertain_assessment_is_aligned_never_divergent():
    signal = detect_divergence(
        _fundamental(FundamentalAssessment.UNCERTAIN), momentum_20d=0.30, as_of=AT)
    assert signal.divergence_type is DivergenceType.ALIGNED
    assert not signal.notable
