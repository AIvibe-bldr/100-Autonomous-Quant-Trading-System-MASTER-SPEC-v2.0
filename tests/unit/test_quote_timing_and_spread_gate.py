"""(1) A live quote is fetched AFTER the session's `as_of` was fixed, so its
timestamp is a few seconds "in the future". That must neither trip the
data-integrity HALT_ENTRIES nor be shown to the Decision AI as unsynchronised;
a genuinely future quote must still be flagged.
(2) A quote whose spread already exceeds the Risk cap is dropped BEFORE any
LLM call (the Master Risk Controller still decides for everything that passes)."""
from datetime import date, datetime, time, timedelta, timezone

from packages.common.clock import FrozenClock
from packages.schemas.core import Quote
from services.data_validation.integrity import DataHealth
from services.market_data.service import MockProvider
from services.market_data.universe import UniverseManager, UniverseSymbol
from tests.conftest import SYMBOLS, build_pipeline


class _Skewed(MockProvider):
    """Quotes stamped `skew` after `at`; symbols in `wide` get a 5% spread."""

    def __init__(self, skew: timedelta, wide: set[str] = frozenset()):
        super().__init__()
        self.skew, self.wide = skew, set(wide)

    def get_quote(self, symbol, at):
        q = super().get_quote(symbol, at)
        mid = q.mid
        half = mid * 0.025 if symbol in self.wide else (q.ask - q.bid) / 2
        return Quote(symbol=symbol, ts=q.ts + self.skew, bid=mid - half, ask=mid + half,
                     bid_size=q.bid_size, ask_size=q.ask_size, source="mock")


def _pipeline(provider):
    clock = FrozenClock(current=datetime.combine(date(2026, 8, 10), time(15, 0), tzinfo=timezone.utc))
    universe = UniverseManager()
    for s in SYMBOLS:
        universe.add(UniverseSymbol(symbol=s, listed_from=date(2015, 1, 1)))
    return build_pipeline(clock, universe, provider=provider), clock


def _spy(p):
    seen = []
    real = p.decision_model.decide
    p.decision_model.decide = lambda ctx: (seen.append(ctx), real(ctx))[1]
    return seen


def test_quote_a_few_seconds_after_as_of_is_not_a_data_anomaly():
    p, clock = _pipeline(_Skewed(timedelta(seconds=5)))
    seen = _spy(p)
    p.run_session(clock.now())
    assert p.integrity.health is not DataHealth.HALT_ENTRIES
    assert seen and all(c.as_of >= c.quote.ts for c in seen)   # quote age never negative


def test_a_quote_minutes_in_the_future_is_still_flagged():
    p, clock = _pipeline(_Skewed(timedelta(minutes=10)))
    p.run_session(clock.now())
    assert p.integrity.health is DataHealth.HALT_ENTRIES


def test_wide_spread_candidates_get_no_decision_call():
    wide = set(SYMBOLS[:3])
    p, clock = _pipeline(_Skewed(timedelta(0), wide=wide))
    seen = _spy(p)
    result = p.run_session(clock.now())
    assert seen and not ({c.scan.symbol for c in seen} & wide)
    assert any("spread" in r and "no Decision call" in r for r in result.no_trade_reasons)
