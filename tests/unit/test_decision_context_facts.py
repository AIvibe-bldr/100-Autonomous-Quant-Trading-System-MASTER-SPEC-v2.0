"""Decision AI context facts: regime (universe composite), data timestamp,
quant-score definition (found by a live 6.1 run: it waited because all three
were missing/UNKNOWN)."""
from datetime import date, datetime, time, timedelta, timezone

import pytest

from packages.common.clock import FrozenClock
from packages.schemas.core import Bar
from services.decision.models import DecisionContext, MockDecisionModel
from services.decision.prompts import build_decision_prompt
from services.market_data.universe import UniverseManager, UniverseSymbol
from services.regime.engine import (Regime, RegimeEngine, describe_universe_regime,
                                    universe_composite_bars)
from tests.conftest import SYMBOLS, build_pipeline

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _bars(sym, closes):
    return [Bar(symbol=sym, ts=T0 + timedelta(days=i), open=c, high=c, low=c, close=c,
                volume=1000) for i, c in enumerate(closes)]


def _up(n=30, step=0.01):
    return [100 * (1 + step) ** i for i in range(n)]


def test_composite_classifies_uptrend_as_bull_and_is_labelled_an_estimate():
    data = {s: _bars(s, _up()) for s in "ABC"}
    reading = RegimeEngine().classify(universe_composite_bars(data))
    assert reading.primary in (Regime.BULL, Regime.EUPHORIA)
    text = describe_universe_regime(reading, 3)
    assert "ESTIMATE" in text and "not an official index" in text


def test_composite_uses_only_common_timestamps_and_rejects_thin_input():
    with pytest.raises(ValueError):
        universe_composite_bars({"A": _bars("A", _up()), "B": _bars("B", _up())})  # < 3 symbols
    short = {s: _bars(s, _up(10)) for s in "ABC"}
    with pytest.raises(ValueError):
        universe_composite_bars(short)                                              # < 21 stamps


def _pipeline():
    u = UniverseManager()
    for s in SYMBOLS:
        u.add(UniverseSymbol(symbol=s, listed_from=date(2015, 1, 1)))
    clock = FrozenClock(current=datetime.combine(date(2026, 8, 10), time(15, 0),
                                                 tzinfo=timezone.utc))
    return build_pipeline(clock, u), clock


def test_pipeline_feeds_a_real_regime_to_decision_ai_not_unknown():
    p, clock = _pipeline()
    seen = []
    inner = MockDecisionModel()

    class Spy:
        name, version = inner.name, inner.version

        def decide(self, ctx: DecisionContext):
            seen.append(ctx.regime)
            return inner.decide(ctx)

    p.decision_model = Spy()
    p.run_session(clock.now())
    assert seen and all(r != "UNKNOWN" and "ESTIMATE" in r for r in seen)
    assert p._session_regime != "UNKNOWN"


def test_regime_degrades_to_unknown_when_universe_too_small():
    p, clock = _pipeline()
    assert p._read_regime(SYMBOLS[:2], clock.now()) == "UNKNOWN"
    assert p._session_regime == "UNKNOWN"


def test_prompt_states_timestamp_source_and_score_definition():
    p, clock = _pipeline()
    scan = p.scanner.scan(p.universe.symbols_as_of(clock.now().date()), clock.now())[0]
    text = build_decision_prompt(DecisionContext(scan=scan, regime="BULL"))
    assert scan.bars[-1].ts.isoformat() in text and "data source: mock" in text
    assert "20-day momentum / 20-day daily volatility" in text and "uncalibrated" in text


def test_prompt_gives_decision_time_and_bar_age_so_freshness_is_not_guessed():
    p, clock = _pipeline()
    now = clock.now()
    scan = p.scanner.scan(p.universe.symbols_as_of(now.date()), now)[0]
    text = build_decision_prompt(DecisionContext(scan=scan, regime="BULL", as_of=now))
    age = (now - scan.bars[-1].ts).total_seconds() / 86400
    assert f"Decision time: {now.isoformat()}" in text and f"{age:.1f} days old" in text
    assert "Decision time" not in build_decision_prompt(DecisionContext(scan=scan))


def test_prompt_gives_current_quote_and_says_so_when_missing():
    p, clock = _pipeline()
    now = clock.now()
    scan = p.scanner.scan(p.universe.symbols_as_of(now.date()), now)[0]
    quote = p.market_data.quote(scan.symbol, now)
    text = build_decision_prompt(DecisionContext(scan=scan, as_of=now, quote=quote))
    assert f"bid {quote.bid:.4f}" in text and f"ask {quote.ask:.4f}" in text and "spread" in text
    missing = build_decision_prompt(DecisionContext(scan=scan, as_of=now))
    assert "Current quote: not available" in missing


def test_pipeline_passes_the_gated_quote_to_decision_ai():
    p, clock = _pipeline()
    seen = []
    real = p.decision_model.decide

    def spy(ctx):
        seen.append(ctx)
        return real(ctx)

    p.decision_model.decide = spy
    p.run_session(clock.now())
    assert seen and all(c.quote is not None and c.quote.symbol == c.scan.symbol for c in seen)
