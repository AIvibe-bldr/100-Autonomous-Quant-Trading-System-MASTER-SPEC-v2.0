"""Session observers (read-only listeners) and the forecast observer (§32)."""
import json
from datetime import date, datetime, timedelta, timezone

import pytest

from packages.common.clock import FrozenClock
from packages.common.durable_store import DurableSessionStore
from packages.common.environment import Environment
from services.market_data.universe import UniverseManager, UniverseSymbol
from services.pdca.forecast_observer import ForecastObserver
from services.pdca.observer import SessionObserver
from services.supervisor.session_record import SessionRecorder
from tests.conftest import SYMBOLS, build_pipeline


def _pipeline(observers, start=datetime(2026, 8, 11, 15, 0, tzinfo=timezone.utc)):
    clock = FrozenClock(current=start)
    universe = UniverseManager()
    for s in SYMBOLS:
        universe.add(UniverseSymbol(symbol=s, listed_from=date(2015, 1, 1)))
    return build_pipeline(clock, universe, observers=observers), clock


class Boom(SessionObserver):
    name = "boom"

    def on_decision(self, event):
        raise RuntimeError("observer bug")

    def on_session_end(self, event):
        raise RuntimeError("observer bug 2")


class Counting(SessionObserver):
    name = "counting"

    def __init__(self):
        self.decisions = self.ends = 0

    def on_decision(self, event):
        self.decisions += 1

    def on_session_end(self, event):
        self.ends += 1


def test_a_failing_observer_never_stops_trading_or_hides_the_others():
    counting = Counting()
    p, clock = _pipeline([Boom(), counting])
    baseline, _ = _pipeline([])
    result = p.run_session(clock.now())
    ref = baseline.run_session(clock.now())
    assert counting.decisions > 0 and counting.ends == 1                    # the others still ran
    assert (result.scanned, result.orders_filled) == (ref.scanned, ref.orders_filled)   # trading unchanged
    assert "observer boom failed: RuntimeError" in result.data_warnings


def test_observers_get_plain_facts_and_no_handle_on_the_pipeline():
    seen = []

    class Spy(SessionObserver):
        name = "spy"

        def on_decision(self, event):
            seen.append(event)
    p, clock = _pipeline([Spy()])
    p.run_session(clock.now())
    ev = seen[0]
    assert ev.symbol and ev.decision.symbol == ev.symbol and ev.price and ev.context.scan
    assert not any(hasattr(ev, a) for a in ("pipeline", "risk_controller", "broker", "execution"))


def test_forecast_observer_records_every_decision_and_grades_them_when_due():
    fo = ForecastObserver()
    p, clock = _pipeline([fo])
    first = p.run_session(clock.now())
    assert fo.summary()["recorded"] == first.candidates and fo.summary()["resolved"] == 0
    for _ in range(10):                                                     # move past the longest mock horizon
        clock.advance(86400)
        p.run_session(clock.now())
    s = fo.summary()
    assert s["resolved"] > 0 and s["resolved"] + s["pending"] == s["recorded"]
    cal = s["calibration"]
    assert set(cal) == {"bear", "base", "bull"}
    assert sum(c["realized"] for c in cal.values()) == pytest.approx(1.0, abs=1e-3)
    assert sum(c["predicted"] for c in cal.values()) == pytest.approx(1.0, abs=1e-2)


def test_a_forecast_whose_price_is_unavailable_stays_pending():
    from services.decision.forecast import Forecast, ForecastTracker
    from packages.schemas.core import ScenarioCase
    t = ForecastTracker()
    sc = lambda px, pr: ScenarioCase(description="x", target_price=px, probability=pr)
    t.record(Forecast("AAPL", datetime(2026, 8, 1, tzinfo=timezone.utc), "1d", 100.0,
                      sc(90, 0.2), sc(100, 0.5), sc(110, 0.3)))
    now = datetime(2026, 8, 10, tzinfo=timezone.utc)
    assert t.resolve_due(now, lambda s, at: None) == 0 and not t.forecasts[0].resolved
    assert t.resolve_due(now, lambda s, at: 111.0) == 1 and t.forecasts[0].realized_case == "bull"


def test_a_decision_with_an_ungradable_horizon_is_counted_as_skipped_not_dropped_silently():
    fo = ForecastObserver()
    p, clock = _pipeline([fo])
    real = p.decision_model.decide

    def odd(ctx):
        out = real(ctx)
        return {**out, "expected_horizon": "2y"}
    p.decision_model.decide = odd
    r = p.run_session(clock.now())
    assert fo.summary()["recorded"] == 0 and fo.summary()["skipped"] == r.candidates


def test_session_record_carries_the_observer_summaries(tmp_path):
    fo = ForecastObserver()
    p, clock = _pipeline([fo])
    store = DurableSessionStore(str(tmp_path / "s.db"), Environment.PAPER)
    result = p.run_session(clock.now())
    SessionRecorder(p, store, "t")(clock.now(), result)
    detail = json.loads(store.all()[0].detail_json)
    assert detail["observers"]["forecast"]["recorded"] == fo.summary()["recorded"]
