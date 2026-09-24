"""Tests for scripts/run_live_dashboard.py's advance_one_session()."""
from __future__ import annotations

from datetime import date, datetime, time, timezone

from apps.api.main import create_app
from packages.common.calendar import TradingCalendar
from packages.common.clock import FrozenClock
from scripts.run_live_dashboard import advance_one_session
from tests.conftest import SESSION_TIME, SYMBOLS, build_pipeline
from services.market_data.universe import UniverseManager, UniverseSymbol


def _universe() -> UniverseManager:
    um = UniverseManager()
    for s in SYMBOLS:
        um.add(UniverseSymbol(symbol=s, listed_from=date(2015, 1, 1)))
    return um


def test_a_trading_day_runs_a_session_and_advances_to_the_next_trading_day():
    clock = FrozenClock(current=SESSION_TIME)  # a Tuesday
    pipeline = build_pipeline(clock, _universe())
    calendar = TradingCalendar()
    app = create_app(pipeline)

    advance_one_session(pipeline, calendar, clock, app)

    assert clock.current.date() == SESSION_TIME.date().replace(
        day=SESSION_TIME.day + 1)
    assert app.state.record_session  # callable was wired, doesn't raise


def test_a_weekend_is_skipped_without_running_a_session():
    """Land exactly on a Saturday; a session must not run for it, and the
    clock must jump straight to the following Monday."""
    saturday = datetime(2026, 8, 15, 15, 0, tzinfo=timezone.utc)
    assert saturday.weekday() == 5
    clock = FrozenClock(current=saturday)
    pipeline = build_pipeline(clock, _universe())
    calendar = TradingCalendar()
    app = create_app(pipeline)

    calls = []
    app.state.record_session = lambda result: calls.append(result)

    advance_one_session(pipeline, calendar, clock, app)

    assert calls == [], "a session was recorded for a non-trading day"
    assert clock.current.date().weekday() == 0  # Monday
    assert clock.current.time() == time(15, 0)


def test_repeated_calls_only_ever_land_on_trading_days():
    clock = FrozenClock(current=SESSION_TIME)
    pipeline = build_pipeline(clock, _universe())
    calendar = TradingCalendar()
    app = create_app(pipeline)
    app.state.record_session = lambda result: None

    for _ in range(10):
        advance_one_session(pipeline, calendar, clock, app)
        assert calendar.is_trading_day(clock.current.date())


def test_a_session_error_does_not_kill_the_background_loop():
    """A transient failure (network blip, rate limit) inside one session
    must cost that one session, not silently end the whole run — leaving a
    dashboard that looks alive but has permanently stopped advancing."""
    import threading

    from scripts.run_live_dashboard import _run_forever

    clock = FrozenClock(current=SESSION_TIME)
    pipeline = build_pipeline(clock, _universe())
    calendar = TradingCalendar()
    app = create_app(pipeline)

    call_count = 0
    original_run_session = pipeline.run_session

    def flaky_run_session(now):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("simulated transient API failure")
        return original_run_session(now)

    pipeline.run_session = flaky_run_session

    lock = threading.Lock()
    stop_event = threading.Event()
    thread = threading.Thread(
        target=_run_forever,
        args=(pipeline, calendar, clock, app, 0.05, lock, stop_event),
        daemon=True)
    thread.start()
    thread.join(timeout=2)   # loop keeps going past the first error on its own
    stop_event.set()
    thread.join(timeout=2)

    assert call_count >= 2, "the loop stopped after the first error instead of retrying"
