"""Tests for services/fundamentals/mock_management_source.py."""
from __future__ import annotations

from datetime import datetime, timezone

from services.fundamentals.mock_management_source import MockManagementStatementSource

AT = datetime(2026, 8, 11, tzinfo=timezone.utc)


def test_fetch_history_is_deterministic_for_the_same_symbol_and_date():
    source = MockManagementStatementSource()
    first = source.fetch_history("AAPL", AT, quarters=9)
    second = source.fetch_history("AAPL", AT, quarters=9)
    assert [s.text for s in first] == [s.text for s in second]


def test_fetch_history_returns_the_requested_number_of_quarters():
    statements = MockManagementStatementSource().fetch_history("AAPL", AT, quarters=6)
    assert len(statements) == 6


def test_every_statement_is_filed_after_its_own_fiscal_quarter_ends():
    """§16 Point-in-Time: a statement must never claim to exist before the
    quarter it describes has even ended."""
    statements = MockManagementStatementSource().fetch_history("AAPL", AT, quarters=9)
    for s in statements:
        assert s.received_timestamp > s.effective_timestamp


def test_different_symbols_get_different_tone_archetypes():
    """Not every symbol should read as the same tone — otherwise the
    tracker would have nothing to distinguish between companies."""
    source = MockManagementStatementSource()
    texts_by_symbol = {sym: tuple(s.text for s in source.fetch_history(sym, AT, quarters=9))
                       for sym in ("AAPL", "TSLA", "AMD", "PLTR", "NVDA", "JPM")}
    assert len(set(texts_by_symbol.values())) > 1
