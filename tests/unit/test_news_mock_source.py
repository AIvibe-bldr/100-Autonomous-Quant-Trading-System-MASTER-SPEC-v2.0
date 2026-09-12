"""Tests for services/news/mock_source.py (MASTER SPEC §17).

The point of a Mock DATA SOURCE (as opposed to NewsEngine's own logic,
which was already exercised implicitly via other tests) is determinism —
replay (§62) requires the same symbol+date to always produce the same
headlines.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from services.news.mock_source import MockNewsSource
from services.news.engine import NewsEngine

SYMBOLS = [f"SYM{i}" for i in range(20)]
AT = datetime(2026, 8, 11, 15, 0, tzinfo=timezone.utc)


def test_same_symbol_and_date_produce_identical_items():
    source = MockNewsSource()
    first = source.fetch(SYMBOLS, AT)
    second = source.fetch(SYMBOLS, AT)
    assert first == second


def test_different_dates_can_change_the_output():
    source = MockNewsSource()
    day1 = source.fetch(SYMBOLS, AT)
    day2 = source.fetch(SYMBOLS, AT + timedelta(days=1))
    assert day1 != day2


def test_most_symbols_are_quiet_on_a_given_day():
    """§17: event-driven, not 全銘柄×LLM検索 — most symbols get no headline."""
    source = MockNewsSource()
    items = source.fetch(SYMBOLS, AT)
    assert 0 < len(items) < len(SYMBOLS)


def test_every_item_is_attributed_to_exactly_its_own_symbol():
    source = MockNewsSource()
    for item in source.fetch(SYMBOLS, AT):
        assert len(item.tickers) == 1
        assert item.tickers[0] in SYMBOLS
        assert item.tickers[0] in item.title


def test_output_feeds_the_real_news_engine_without_error():
    """Integration sanity: the mock source's NewsItems are valid input to
    the actual clustering/scoring engine, not just structurally similar."""
    source = MockNewsSource()
    items = source.fetch(SYMBOLS, AT)
    signals = NewsEngine().process(items)
    assert len(signals) == len(items)   # one cluster per single-symbol item, same day
    for sig in signals:
        assert -1.0 <= sig.direction <= 1.0
        assert 0.0 <= sig.impact <= 1.0
