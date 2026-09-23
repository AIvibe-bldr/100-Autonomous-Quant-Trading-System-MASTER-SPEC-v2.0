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
            # same one-session window TradingPipeline uses
            sig = engine.signal(symbol, as_of=at, max_age=timedelta(days=1))
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


# --- InstitutionalFlowEngine.signal() time window ----------------------------

def _obs(feature, value, at):
    from services.institutional.engine import FlowObservation
    return FlowObservation(symbol="AAPL", feature=feature, value=value,
                           observed_at=at, source="test")


def test_stale_single_features_from_different_days_do_not_add_up_to_agreement():
    """§20: "never trade on a single feature". Two single-feature days a
    week apart are not two features agreeing — without a window the engine
    kept both as "latest for their feature" forever and reported an
    actionable two-feature signal."""
    from services.institutional.engine import FlowFeature

    engine = InstitutionalFlowEngine()
    engine.ingest(_obs(FlowFeature.BLOCK_TRADES, 0.6, AT - timedelta(days=7)))
    engine.ingest(_obs(FlowFeature.ETF_FLOW, 0.6, AT))

    assert engine.signal("AAPL").actionable          # the unwindowed failure mode
    windowed = engine.signal("AAPL", as_of=AT, max_age=timedelta(days=1))
    assert windowed.contributing == [FlowFeature.ETF_FLOW]
    assert not windowed.actionable


def test_an_observation_after_as_of_never_leaks_into_the_signal():
    from services.institutional.engine import FlowFeature

    engine = InstitutionalFlowEngine()
    engine.ingest(_obs(FlowFeature.BLOCK_TRADES, 0.6, AT + timedelta(days=1)))
    assert engine.signal("AAPL", as_of=AT) is None


def test_pipeline_never_shows_more_features_than_one_day_can_produce():
    """The mock emits at most two features per symbol per day, so across
    many sessions no DecisionContext may ever carry more than two."""
    from datetime import date

    from packages.common.clock import FrozenClock
    from services.decision.models import MockDecisionModel
    from services.market_data.universe import UniverseManager, UniverseSymbol
    from tests.conftest import SESSION_TIME, SYMBOLS as UNIVERSE, build_pipeline

    universe = UniverseManager()
    for s in UNIVERSE:
        universe.add(UniverseSymbol(symbol=s, listed_from=date(2015, 1, 1)))
    pipeline = build_pipeline(FrozenClock(current=SESSION_TIME), universe)
    seen = []

    class _Spy(MockDecisionModel):
        def decide(self, context):
            seen.append(context)
            return super().decide(context)

    pipeline.decision_model = _Spy()
    for d in range(8):
        pipeline.run_session(SESSION_TIME + timedelta(days=d))
    counts = [len(c.institutional.contributing) for c in seen if c.institutional]
    assert counts and max(counts) <= 2
