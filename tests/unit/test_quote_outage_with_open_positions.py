"""A missing quote for a HELD symbol (market closed, bid/ask 0, feed blip) must
not crash the session: exit management and mark-to-market are what protect
open positions, so they have to degrade, not abort (SAFETY: fail closed for
entries, keep managing exits)."""
from datetime import date, datetime, time, timezone

from packages.common.clock import FrozenClock
from services.market_data.service import MarketDataUnavailable, MockProvider
from services.market_data.universe import UniverseManager, UniverseSymbol
from tests.conftest import SYMBOLS, build_pipeline


class _FlakyQuotes(MockProvider):
    def __init__(self):
        super().__init__()
        self.down: set[str] = set()

    def get_quote(self, symbol, at):
        if symbol in self.down:
            raise MarketDataUnavailable(f"{symbol}: no valid bid/ask (bid=0, ask=0)")
        return super().get_quote(symbol, at)


def _held_pipeline():
    clock = FrozenClock(current=datetime.combine(date(2026, 8, 10), time(15, 0), tzinfo=timezone.utc))
    universe = UniverseManager()
    for s in SYMBOLS:
        universe.add(UniverseSymbol(symbol=s, listed_from=date(2015, 1, 1)))
    provider = _FlakyQuotes()
    p = build_pipeline(clock, universe, provider=provider)
    first = p.run_session(clock.now())
    assert first.fills, "setup: need at least one open position"
    return p, clock, provider, first.fills[0].symbol


def test_resting_stop_poll_survives_quote_outage_for_held_symbol():
    p, clock, provider, held = _held_pipeline()
    provider.down.add(held)
    triggered, _ = p.manage_open_positions(clock.now())     # must not raise
    assert triggered == 0


def test_session_with_held_symbol_quote_outage_does_not_abort():
    p, clock, provider, held = _held_pipeline()
    provider.down.add(held)
    result = p.run_session(clock.now())                      # must not raise
    assert result is not None


def test_held_symbol_is_still_valued_when_its_quote_is_missing():
    p, clock, provider, held = _held_pipeline()
    provider.down.add(held)
    marks = p.mark_prices({}, clock.now())
    assert marks[held] > 0                      # last close / cost basis, not a crash
