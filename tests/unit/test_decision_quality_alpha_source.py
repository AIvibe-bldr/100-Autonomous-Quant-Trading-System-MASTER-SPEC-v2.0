"""Tests for the §15 Decision Quality additions: DecisionQualityReporter.
by_alpha_source and the 12m EXTENDED_HORIZONS entry."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from services.pdca.decision_quality import (
    EXTENDED_HORIZONS,
    DecisionKind,
    DecisionQualityEngine,
    DecisionQualityReporter,
    DecisionSnapshot,
)

AT = datetime(2026, 8, 11, tzinfo=timezone.utc)


def test_extended_horizons_include_12_months():
    assert EXTENDED_HORIZONS["12m"] == timedelta(days=365)


def _snap(decision_id: str, alpha_scores: dict[str, float]) -> DecisionSnapshot:
    return DecisionSnapshot(decision_id=decision_id, symbol="AAPL", ts=AT,
                            reference_price=100.0, decision=DecisionKind.BUY,
                            confidence=0.7, expected_horizon="1w",
                            expected_return_range=(-0.05, 0.10),
                            had_stop_plan=True, skeptic_consulted=True,
                            alpha_scores=alpha_scores)


def test_by_alpha_source_buckets_decisions_by_which_signal_contributed():
    eng = DecisionQualityEngine()
    eng.record(_snap("d0", {"fundamental_inflection": 1.0}))
    eng.record(_snap("d1", {}))  # pure quant candidate, no research signal
    reporter = DecisionQualityReporter(eng)
    report = reporter.monthly(AT.year, AT.month)
    assert "fundamental_inflection" in report.by_alpha_source
    assert "none" in report.by_alpha_source
    assert report.by_alpha_source["fundamental_inflection"]["total"] == 1
    assert report.by_alpha_source["none"]["total"] == 1


def test_a_decision_with_multiple_signals_is_counted_under_each():
    eng = DecisionQualityEngine()
    eng.record(_snap("d0", {"fundamental_inflection": 1.0, "divergence": 1.0}))
    reporter = DecisionQualityReporter(eng)
    report = reporter.monthly(AT.year, AT.month)
    assert report.by_alpha_source["fundamental_inflection"]["total"] == 1
    assert report.by_alpha_source["divergence"]["total"] == 1
    assert "none" not in report.by_alpha_source


def test_trend_carries_by_alpha_source_through():
    eng = DecisionQualityEngine()
    eng.record(_snap("d0", {"institutional": 1.0}))
    reporter = DecisionQualityReporter(eng)
    trend = reporter.trend([(AT.year, AT.month)])
    assert "institutional" in trend[0]["by_alpha_source"]
