"""Tests for services/fundamentals/{mock_source,engine}.py.

§21's own required test this file targets directly:
- "未来の決算データを過去へ持ち込まないこと" (Future Leakage)
  -> test_a_quarter_is_invisible_before_its_own_filing_date
"""
from __future__ import annotations

from datetime import datetime, timezone

from packages.schemas.fundamentals import FundamentalAssessment, InflectionDirection
from services.fundamentals.engine import FundamentalInflectionEngine
from services.fundamentals.mock_source import MockFinancialDataSource, _seed

AT = datetime(2026, 8, 11, tzinfo=timezone.utc)


# --- MockFinancialDataSource --------------------------------------------------

def test_same_symbol_and_date_produce_identical_history():
    source = MockFinancialDataSource()
    first = source.fetch_history("AAPL", AT, quarters=8)
    second = source.fetch_history("AAPL", AT, quarters=8)
    assert first == second


def test_history_is_ordered_oldest_to_newest_with_no_gaps():
    source = MockFinancialDataSource()
    history = source.fetch_history("AAPL", AT, quarters=8)
    periods = [(s.fiscal_year, s.fiscal_quarter) for s in history]
    assert periods == sorted(periods)
    for i in range(1, len(periods)):
        y0, q0 = periods[i - 1]
        y1, q1 = periods[i]
        expected = (y0, q0 + 1) if q0 < 4 else (y0 + 1, 1)
        assert (y1, q1) == expected


def test_most_recent_quarter_is_not_yet_filed_as_of_its_own_quarter_end():
    """The whole point of a filing lag: a quarter's own statement isn't
    received the instant the quarter ends."""
    source = MockFinancialDataSource()
    history = source.fetch_history("AAPL", AT, quarters=1)
    latest = history[-1]
    assert latest.received_timestamp > latest.effective_timestamp


def test_flags_are_deterministic_and_occasional():
    source = MockFinancialDataSource()
    first = source.fetch_flags("AAPL", 2026, 2)
    second = source.fetch_flags("AAPL", 2026, 2)
    assert first == second
    total_quarters = 40
    flagged = sum(1 for q in range(1, total_quarters + 1)
                  if source.fetch_flags("SYM_FLAG_TEST", 2020 + q // 4, (q % 4) + 1))
    assert 0 < flagged < total_quarters


# --- FundamentalInflectionEngine ----------------------------------------------

def test_a_quarter_is_invisible_before_its_own_filing_date():
    """§16/§21 Future Leakage: a quarter's numbers must not influence the
    signal before that quarter has actually been filed. Q2 2026 (Apr-Jun)
    ends 2026-06-30 and — with the mock's 45-day filing lag — is received
    2026-08-14. A date one day before that must see strictly fewer visible
    quarters than a date one day after it, with everything else (which
    fiscal-quarter bucket `as_of` falls in) held constant."""
    engine = FundamentalInflectionEngine()
    just_before_filing = datetime(2026, 8, 13, tzinfo=timezone.utc)
    just_after_filing = datetime(2026, 8, 15, tzinfo=timezone.utc)

    before = engine.analyze("SYM_PIT_TEST", just_before_filing)
    after = engine.analyze("SYM_PIT_TEST", just_after_filing)

    assert before.quarters_observed == after.quarters_observed - 1


def test_insufficient_history_reads_as_insufficient_data():
    """Requesting only the CURRENT quarter, analyzed before that quarter's
    own filing lag has elapsed, must see zero visible statements — not a
    guess extrapolated from nothing."""
    engine = FundamentalInflectionEngine(min_periods=3, lookback_quarters=1)
    quarter_end_not_yet_filed = datetime(2026, 7, 5, tzinfo=timezone.utc)  # Q3 2026 just started
    result = engine.analyze("SYM_EARLY_TEST", quarter_end_not_yet_filed)
    assert result.quarters_observed == 0
    assert result.assessment is FundamentalAssessment.INSUFFICIENT_DATA


def test_a_structurally_improving_archetype_is_detected():
    """archetype 0 (see mock_source._seed) is built to be genuinely and
    consistently improving — the whole pipeline must actually find it."""
    engine = FundamentalInflectionEngine()
    symbol = next(f"PROBE{i}" for i in range(200) if _seed(f"PROBE{i}", "archetype") % 6 == 0)
    signal = engine.analyze(symbol, AT)
    assert signal.assessment is FundamentalAssessment.STRUCTURAL_IMPROVEMENT
    assert signal.actionable
    improving = [t for t in signal.trends if t.direction is InflectionDirection.IMPROVING]
    assert len(improving) >= 3


def test_a_deteriorating_archetype_is_detected():
    engine = FundamentalInflectionEngine()
    symbol = next(f"PROBE{i}" for i in range(200) if _seed(f"PROBE{i}", "archetype") % 6 == 1)
    signal = engine.analyze(symbol, AT)
    assert signal.assessment is FundamentalAssessment.DETERIORATION
    assert not signal.actionable


def test_result_is_deterministic_across_repeated_calls():
    engine = FundamentalInflectionEngine()
    first = engine.analyze("AAPL", AT)
    second = engine.analyze("AAPL", AT)
    assert first == second
