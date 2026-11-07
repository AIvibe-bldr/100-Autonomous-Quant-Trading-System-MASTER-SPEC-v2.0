"""Event calendar → pipeline (MASTER SPEC §16, §40): upcoming corporate actions
reach the Decision AI and the gap-risk score; an unfetched or earnings-less
calendar is stated honestly, never read as 'no events'."""
from datetime import date, datetime, time, timedelta, timezone

import pytest

from packages.common.clock import FrozenClock
from services.decision.models import DecisionContext
from services.decision.prompts import build_decision_prompt
from services.market_data.alpaca_events import AlpacaCorporateActionsSource
from services.market_data.event_calendar import EventCalendar, EventType, RiskEvent
from services.market_data.event_source import EventsUnavailable
from services.market_data.universe import UniverseManager, UniverseSymbol
from tests.conftest import SYMBOLS, build_pipeline

TODAY = date(2026, 8, 10)
START, END = TODAY, TODAY + timedelta(days=14)


def _payload():
    return {"corporate_actions": {
        "cash_dividends": [
            {"symbol": "AAPL", "ex_date": "2026-08-12", "rate": 0.27, "special": False},
            {"symbol": "MSFT", "ex_date": "2026-09-30", "rate": 0.9, "special": False}],  # outside window
        "forward_splits": [{"symbol": "NVDA", "ex_date": "2026-08-14", "new_rate": 10, "old_rate": 1}],
        "cash_mergers": [{"acquiree_symbol": "WBD", "effective_date": "2026-08-15", "rate": 31.0}],
        "stock_mergers": [{"acquiree_symbol": "HES", "acquirer_symbol": "CVX",
                           "effective_date": "2026-08-13"}],
        "weird_new_type": [{"symbol": "AAPL", "ex_date": "2026-08-11"}]}}


# ---- source -----------------------------------------------------------------

def test_source_maps_corporate_actions_to_events_inside_the_window_only():
    src = AlpacaCorporateActionsSource("k", "s", http_get=lambda u, h: _payload())
    ev = src.fetch(["AAPL", "MSFT", "NVDA", "WBD", "HES", "CVX"], START, END)
    got = {(e.symbol, e.event_type) for e in ev}
    assert ("AAPL", EventType.EX_DIVIDEND) in got and ("NVDA", EventType.STOCK_SPLIT) in got
    assert ("WBD", EventType.MERGER) in got and ("HES", EventType.MERGER) in got
    assert ("CVX", EventType.MERGER) in got                      # the acquirer is affected too
    assert not any(e.symbol == "MSFT" for e in ev)               # outside the window
    assert not any(e.description == "weird_new_type" for e in ev)  # unknown kinds are skipped


def test_source_ignores_symbols_nobody_asked_for():
    src = AlpacaCorporateActionsSource("k", "s", http_get=lambda u, h: _payload())
    assert {e.symbol for e in src.fetch(["AAPL"], START, END)} == {"AAPL"}


def test_source_failure_is_events_unavailable_and_credentials_never_leak():
    def boom(u, h):
        raise EventsUnavailable("Alpaca events HTTP 500")
    with pytest.raises(EventsUnavailable) as e:
        AlpacaCorporateActionsSource("SECRETK", "SECRETS", http_get=boom).fetch(["AAPL"], START, END)
    assert "SECRET" not in str(e.value)
    with pytest.raises(EventsUnavailable):
        AlpacaCorporateActionsSource("", "")


# ---- calendar ----------------------------------------------------------------

def test_ex_dividend_is_informational_but_a_split_counts_as_gap_risk():
    cal = EventCalendar()
    cal.add(RiskEvent(EventType.EX_DIVIDEND, on=date(2026, 8, 12), symbol="AAPL"))
    cal.add(RiskEvent(EventType.STOCK_SPLIT, on=date(2026, 8, 12), symbol="NVDA"))
    assert not cal.has_event_before("AAPL", TODAY, horizon_days=7)
    assert cal.has_event_before("NVDA", TODAY, horizon_days=7)
    assert len(cal.events_between(TODAY, END, symbol="AAPL")) == 1   # still listed


# ---- prompt ------------------------------------------------------------------

def _ctx(**kw):
    p, clock = _pipeline(None)
    scan = p.scanner.scan(p.universe.symbols_as_of(clock.now().date()), clock.now())[0]
    return DecisionContext(scan=scan, as_of=clock.now(), **kw)


def test_prompt_states_events_and_always_warns_that_earnings_are_not_covered():
    text = build_decision_prompt(_ctx(events_fetched=True,
                                      upcoming_events=["2026-08-12 EX_DIVIDEND $0.27"]))
    assert "2026-08-12 EX_DIVIDEND" in text
    assert "earnings" in text.lower() and "not evidence" in text.lower()


def test_prompt_distinguishes_no_events_from_events_not_fetched():
    assert "none" in build_decision_prompt(_ctx(events_fetched=True)).lower()
    assert "not available" in build_decision_prompt(_ctx()).lower()


# ---- pipeline ----------------------------------------------------------------

class _FakeEvents:
    def __init__(self, events=None, fail=False):
        self.events, self.fail, self.calls = events or [], fail, []

    def fetch(self, symbols, start, end):
        self.calls.append((tuple(symbols), start, end))
        if self.fail:
            raise EventsUnavailable("down")
        return list(self.events)


def _pipeline(source):
    clock = FrozenClock(current=datetime.combine(TODAY, time(15, 0), tzinfo=timezone.utc))
    uni = UniverseManager()
    for s in SYMBOLS:
        uni.add(UniverseSymbol(symbol=s, listed_from=date(2015, 1, 1)))
    return build_pipeline(clock, uni, event_source=source), clock


def _spy(p):
    ctxs, gaps = [], {}
    real_d = p.decision_model.decide
    p.decision_model.decide = lambda c: (ctxs.append(c), real_d(c))[1]
    real_p = p.loss_control.plan
    p.loss_control.plan = lambda prop, bars, entry, gap: (
        gaps.__setitem__(prop.symbol, gap), real_p(prop, bars, entry, gap))[1]
    return ctxs, gaps


def test_pipeline_gives_decision_its_symbols_events_and_raises_gap_risk_for_a_split():
    base, c0 = _pipeline(None)
    _, base_gaps = _spy(base)
    base.run_session(c0.now())
    sym = next(iter(base_gaps))                       # a symbol that reaches loss control
    feed = _FakeEvents([RiskEvent(EventType.STOCK_SPLIT, on=TODAY + timedelta(days=2),
                                  symbol=sym, description="split 10-for-1"),
                        RiskEvent(EventType.EX_DIVIDEND, on=TODAY + timedelta(days=1),
                                  symbol=SYMBOLS[-1], description="ex-dividend $0.27")])
    p, c1 = _pipeline(feed)
    ctxs, gaps = _spy(p)
    p.run_session(c1.now())
    assert len(feed.calls) == 1
    ctx = next(c for c in ctxs if c.scan.symbol == sym)
    assert ctx.events_fetched and any("STOCK_SPLIT" in e for e in ctx.upcoming_events)
    assert gaps[sym] > base_gaps[sym] or base_gaps[sym] == 1.0
    others = [s for s in gaps if s not in (sym, SYMBOLS[-1])]
    assert others and all(gaps[s] == pytest.approx(base_gaps[s]) for s in others)


def test_events_outage_is_stated_and_does_not_stop_the_session():
    p, clock = _pipeline(_FakeEvents(fail=True))
    ctxs, _ = _spy(p)
    result = p.run_session(clock.now())
    assert ctxs and all(not c.events_fetched for c in ctxs)
    assert any("events unavailable" in w for w in result.data_warnings)


def test_no_event_source_keeps_previous_behaviour():
    p, clock = _pipeline(None)
    ctxs, _ = _spy(p)
    p.run_session(clock.now())
    assert ctxs and all(not c.events_fetched and c.upcoming_events == [] for c in ctxs)
