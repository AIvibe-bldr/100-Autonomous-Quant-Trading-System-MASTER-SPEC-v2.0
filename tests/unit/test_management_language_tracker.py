"""Tests for services/fundamentals/management_language.py
(research-instruction §7-8)."""
from __future__ import annotations

from datetime import datetime, timezone

from packages.schemas.fundamentals import InflectionDirection, LanguageTone, ManagementStatement
from services.fundamentals.management_language import ManagementLanguageTracker

AT = datetime(2026, 8, 11, tzinfo=timezone.utc)


class _FixedSource:
    def __init__(self, texts: list[str]) -> None:
        self._texts = texts

    def fetch_history(self, symbol, as_of, quarters):
        return [
            ManagementStatement(
                symbol=symbol, fiscal_year=2025, fiscal_quarter=(i % 4) + 1, text=text,
                filing_timestamp=AT, publication_timestamp=AT, received_timestamp=AT,
                effective_timestamp=AT)
            for i, text in enumerate(self._texts)]


def test_increasingly_confident_statements_trend_improving():
    texts = [" ".join(["record accelerating strong momentum"] * (i + 1)) for i in range(5)]
    signal = ManagementLanguageTracker(source=_FixedSource(texts)).analyze("ACME", AT)
    assert signal.latest_tone is LanguageTone.CONFIDENT
    assert signal.confidence_trend.direction is InflectionDirection.IMPROVING
    assert signal.notable


def test_increasingly_hedging_statements_trend_deteriorating():
    texts = [" ".join(["headwind challenging pressure"] * (i + 1)) for i in range(5)]
    signal = ManagementLanguageTracker(source=_FixedSource(texts)).analyze("ACME", AT)
    assert signal.latest_tone is LanguageTone.HEDGING
    assert signal.confidence_trend.direction is InflectionDirection.DETERIORATING
    assert signal.notable


def test_neutral_statements_are_not_notable():
    texts = ["Results were in line with plan."] * 5
    signal = ManagementLanguageTracker(source=_FixedSource(texts)).analyze("ACME", AT)
    assert signal.latest_tone is LanguageTone.NEUTRAL
    assert not signal.notable


def test_no_statements_reads_as_insufficient_data_not_a_guess():
    signal = ManagementLanguageTracker(source=_FixedSource([])).analyze("ACME", AT)
    assert signal.quarters_observed == 0
    assert signal.latest_tone is LanguageTone.NEUTRAL
    assert signal.confidence_trend.direction is InflectionDirection.INSUFFICIENT_DATA
    assert not signal.notable


def test_a_statement_not_yet_received_as_of_the_query_date_is_invisible():
    """§16 Point-in-Time: same future-leakage guard as the Fundamental
    Inflection Engine — a statement filed after `as_of` must not count."""
    from datetime import timedelta

    future = ManagementStatement(
        symbol="ACME", fiscal_year=2026, fiscal_quarter=1,
        text="record accelerating strong momentum",
        filing_timestamp=AT + timedelta(days=400), publication_timestamp=AT + timedelta(days=400),
        received_timestamp=AT + timedelta(days=400), effective_timestamp=AT)

    class _Source:
        def fetch_history(self, symbol, as_of, quarters):
            return [future]

    signal = ManagementLanguageTracker(source=_Source()).analyze("ACME", AT)
    assert signal.quarters_observed == 0
