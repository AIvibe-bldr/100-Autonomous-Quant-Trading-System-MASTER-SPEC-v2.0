"""Tests for services/fundamentals/detector.py (research-instruction §5)."""
from __future__ import annotations

from packages.schemas.fundamentals import InflectionDirection
from services.fundamentals.detector import detect_trend


def test_the_specs_own_three_quarter_example_is_improving():
    """§5's own worked example: Gross Margin Q1 61% -> Q2 63% -> Q3 66%."""
    trend = detect_trend("gross_margin", [0.61, 0.63, 0.66], min_periods=3)
    assert trend.direction is InflectionDirection.IMPROVING
    assert trend.consecutive_periods == 3


def test_a_single_quarter_improvement_is_not_enough():
    """§5: "1四半期だけの改善で構造的改善と断定しないこと" — two quarters
    (one step) with only min_periods=3 configured must read as
    INSUFFICIENT_DATA, not IMPROVING."""
    trend = detect_trend("gross_margin", [0.61, 0.66], min_periods=3)
    assert trend.direction is InflectionDirection.INSUFFICIENT_DATA


def test_deteriorating_trend():
    trend = detect_trend("gross_margin", [0.66, 0.63, 0.61], min_periods=3)
    assert trend.direction is InflectionDirection.DETERIORATING
    assert trend.consecutive_periods == 3


def test_mixed_direction_is_flat_not_improving():
    trend = detect_trend("gross_margin", [0.61, 0.66, 0.63], min_periods=3)
    assert trend.direction is InflectionDirection.FLAT
    assert trend.consecutive_periods == 0


def test_none_values_are_excluded_before_windowing():
    """A quarter with an undefined value (e.g. margin undefined because
    revenue was 0) must not silently break or fabricate a data point — it
    is dropped, and the trend runs on the remaining real values."""
    trend = detect_trend("gross_margin", [0.61, None, 0.63, 0.66], min_periods=3)
    assert trend.direction is InflectionDirection.IMPROVING
    assert trend.values == (0.61, 0.63, 0.66)


def test_insufficient_data_when_fewer_than_min_periods_available():
    trend = detect_trend("gross_margin", [], min_periods=3)
    assert trend.direction is InflectionDirection.INSUFFICIENT_DATA
    assert trend.latest_value is None


def test_streak_extends_past_the_minimum_window():
    """A 7-quarter improving run must report consecutive_periods=7, not
    just the minimum 3 the window check requires."""
    trend = detect_trend("gross_margin", [0.50, 0.52, 0.54, 0.56, 0.58, 0.60, 0.62],
                         min_periods=3)
    assert trend.direction is InflectionDirection.IMPROVING
    assert trend.consecutive_periods == 7


def test_a_broken_streak_only_counts_the_trailing_run():
    """Q1-Q3 improved, Q4 dropped sharply (breaking the streak), then
    Q4-Q7 improved again — only the CURRENT trailing run counts (4
    quarters, starting at the post-drop low), not the total across the
    break, and NOT the pre-drop run that no longer connects to today."""
    trend = detect_trend("gross_margin", [0.50, 0.55, 0.60, 0.40, 0.45, 0.50, 0.55],
                         min_periods=3)
    assert trend.direction is InflectionDirection.IMPROVING
    assert trend.consecutive_periods == 4
    assert trend.consecutive_periods < len(trend.values)   # the break IS excluded
