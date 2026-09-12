"""Tests for services/institutional/mock_source.py (MASTER SPEC §20)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from services.institutional.engine import InstitutionalFlowEngine
from services.institutional.mock_source import MockInstitutionalFlowSource

SYMBOLS = [f"SYM{i}" for i in range(30)]
AT = datetime(2026, 8, 11, 15, 0, tzinfo=timezone.utc)


def test_same_symbol_and_date_produce_identical_observations():
    source = MockInstitutionalFlowSource()
    first = source.fetch(SYMBOLS, AT)
    second = source.fetch(SYMBOLS, AT)
    assert first == second


def test_different_dates_can_change_the_output():
    source = MockInstitutionalFlowSource()
    day1 = source.fetch(SYMBOLS, AT)
    day2 = source.fetch(SYMBOLS, AT + timedelta(days=1))
    assert day1 != day2


def test_most_symbols_are_quiet_on_a_given_day():
    source = MockInstitutionalFlowSource()
    obs = source.fetch(SYMBOLS, AT)
    observed_symbols = {o.symbol for o in obs}
    assert 0 < len(observed_symbols) < len(SYMBOLS)


def test_values_are_within_the_expected_magnitude_range():
    source = MockInstitutionalFlowSource()
    for o in source.fetch(SYMBOLS, AT):
        assert 0.2 <= abs(o.value) < 0.8


def test_actionable_signals_are_reachable_but_not_the_common_case():
    """§20: never trade on a single feature — some symbol/day combinations
    across a wide enough sample must produce a >=2-feature agreeing signal,
    but most must not."""
    source = MockInstitutionalFlowSource()
    engine = InstitutionalFlowEngine()
    actionable_count = 0
    total = 0
    for day_offset in range(30):
        at = AT + timedelta(days=day_offset)
        for o in source.fetch(SYMBOLS, at):
            engine.ingest(o)
        for symbol in SYMBOLS:
            sig = engine.signal(symbol)
            if sig is not None:
                total += 1
                if sig.actionable:
                    actionable_count += 1
    assert actionable_count > 0, "no actionable signal ever produced across 30 days"
    assert actionable_count < total, "every signal was actionable — not realistic (§20)"


def test_two_feature_days_have_agreeing_direction():
    """A two-feature observation day must have both features pushing the
    same way — §20's bar is "at least two independent features agreeing",
    not just two features existing."""
    source = MockInstitutionalFlowSource()
    for day_offset in range(60):
        at = AT + timedelta(days=day_offset)
        obs = source.fetch(SYMBOLS, at)
        by_symbol: dict[str, list[float]] = {}
        for o in obs:
            by_symbol.setdefault(o.symbol, []).append(o.value)
        for values in by_symbol.values():
            if len(values) == 2:
                assert (values[0] > 0) == (values[1] > 0)
