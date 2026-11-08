"""Session runner: schedule, heartbeats, fail-closed on a blown-up session."""
from datetime import date, datetime, time, timedelta, timezone

import pytest

from packages.common.alerts import AlertSeverity
from packages.common.calendar import TradingCalendar
from packages.common.clock import FrozenClock
from services.market_data.universe import UniverseManager, UniverseSymbol
from services.risk.master_controller import RiskState
from services.supervisor.heartbeat import HeartbeatRegistry, ServiceStatus
from services.supervisor.runner import RunnerConfig, SessionRunner, next_session_at, session_due
from tests.conftest import SYMBOLS, build_pipeline

CAL = TradingCalendar()
T = time(9, 45)


def utc(y, m, d, h, mi=0):
    return datetime(y, m, d, h, mi, tzinfo=timezone.utc)


# ---- pure schedule ----

@pytest.mark.parametrize("now,last,expected", [
    (utc(2026, 8, 11, 13, 40), None, None),                     # 09:40 ET: before the start time
    (utc(2026, 8, 11, 13, 45), None, date(2026, 8, 11)),        # 09:45 ET exactly
    (utc(2026, 8, 11, 19, 59), None, date(2026, 8, 11)),        # 15:59 ET: still open
    (utc(2026, 8, 11, 20, 0), None, None),                      # 16:00 ET: closed — no stale-quote session
    (utc(2026, 8, 11, 15, 0), date(2026, 8, 11), None),         # already ran today
    (utc(2026, 8, 15, 15, 0), None, None),                      # Saturday
    (utc(2026, 9, 7, 15, 0), None, None),                       # Labor Day
    (utc(2026, 11, 27, 17, 59), None, date(2026, 11, 27)),      # early-close day, 12:59 ET: open
])
def test_session_due(now, last, expected):
    assert session_due(CAL, now, last, T) == expected


def test_early_close_day_stops_at_one_pm():
    assert session_due(CAL, utc(2026, 11, 27, 18, 1), None, T) is None      # 13:01 ET


def test_next_session_skips_weekends_and_holidays():
    fri_after_close = utc(2026, 9, 4, 21, 0)
    assert next_session_at(CAL, fri_after_close, None, T) == utc(2026, 9, 8, 13, 45)   # Mon 9/7 holiday
    assert next_session_at(CAL, utc(2026, 8, 11, 12, 0), None, T) == utc(2026, 8, 11, 13, 45)
    assert next_session_at(CAL, utc(2026, 8, 11, 15, 0), date(2026, 8, 11), T) == utc(2026, 8, 12, 13, 45)


# ---- the runner ----

class Sink:
    def __init__(self):
        self.sent = []

    def send(self, severity, message):
        self.sent.append((severity, message))


def _setup(start=utc(2026, 8, 11, 12, 0), **kw):
    clock = FrozenClock(current=start)
    universe = UniverseManager()
    for s in SYMBOLS:
        universe.add(UniverseSymbol(symbol=s, listed_from=date(2015, 1, 1)))
    pipeline = build_pipeline(clock, universe)
    reg, sink = HeartbeatRegistry(clock), Sink()
    runner = SessionRunner(pipeline, CAL, clock, reg, sink, RunnerConfig(),
                           sleep=lambda s: clock.advance(s), **kw)
    return runner, pipeline, clock, reg, sink


def test_one_session_per_trading_day_across_a_week_of_ticks():
    sessions = []
    runner, pipeline, clock, reg, sink = _setup(after_session=lambda at, r: sessions.append(at))
    end = utc(2026, 8, 14, 22, 0)                                  # Tue 12:00Z .. Fri night
    assert runner.run_forever(lambda: clock.now() >= end) is None
    assert [a.date() for a in sessions] == [date(2026, 8, 11), date(2026, 8, 12),
                                            date(2026, 8, 13), date(2026, 8, 14)]
    assert all(a.hour * 60 + a.minute >= 13 * 60 + 45 for a in sessions)       # none before 09:45 ET
    assert reg.snapshot() == {"pipeline": ServiceStatus.OK, "runner": ServiceStatus.OK}
    assert sink.sent == []


def test_runner_started_after_the_close_runs_nothing_until_the_next_open():
    sessions = []
    runner, _, clock, reg, _ = _setup(start=utc(2026, 8, 11, 21, 0), after_session=lambda a, r: sessions.append(a))
    runner.run_forever(lambda: clock.now() >= utc(2026, 8, 12, 12, 0))
    assert sessions == []
    runner.run_forever(lambda: clock.now() >= utc(2026, 8, 12, 14, 0))
    assert [a.date() for a in sessions] == [date(2026, 8, 12)]


def test_a_blown_up_session_halts_entries_alerts_and_stops_the_runner():
    runner, pipeline, clock, reg, sink = _setup()

    def boom(now):
        raise RuntimeError("quote feed exploded")
    pipeline.run_session = boom
    reason = runner.run_forever(lambda: clock.now() >= utc(2026, 8, 12, 12, 0))
    assert "RuntimeError" in reason and "quote feed exploded" in reason
    assert pipeline.risk_controller.state is RiskState.HALT_NEW_ENTRIES
    assert reg.snapshot()["pipeline"] is ServiceStatus.ERROR
    sevs = [s for s, _ in sink.sent]
    assert AlertSeverity.CRITICAL in sevs
    assert runner.last_run is None                                  # the failed day is not "done"
    assert clock.now() < utc(2026, 8, 11, 14, 0)                    # stopped at once, did not keep ticking


def test_missing_calendar_data_fails_closed_instead_of_guessing():
    runner, pipeline, clock, _, sink = _setup(start=utc(2030, 3, 7, 15, 0))
    reason = runner.run_forever(lambda: False)
    assert "calendar" in reason and pipeline.risk_controller.state is RiskState.HALT_NEW_ENTRIES
    assert any(s is AlertSeverity.CRITICAL for s, _ in sink.sent)


def test_data_warnings_make_the_pipeline_heartbeat_a_warning():
    runner, pipeline, clock, reg, _ = _setup(start=utc(2026, 8, 11, 15, 0))
    real = pipeline.run_session

    def with_warning(now):
        r = real(now)
        r.data_warnings.append("news fetch failed")
        return r
    pipeline.run_session = with_warning
    runner.tick()
    assert reg.snapshot()["pipeline"] is ServiceStatus.WARNING


def test_heartbeats_stay_fresh_between_daily_sessions():
    runner, _, clock, reg, _ = _setup(start=utc(2026, 8, 11, 15, 0))
    runner.tick()                                                   # runs today's session
    for _ in range(300):                                            # ~5 hours of ticks, no session due
        clock.advance(60)
        runner.tick()
    assert reg.overall() is ServiceStatus.OK


def test_tick_interval_must_keep_heartbeats_fresh():
    with pytest.raises(ValueError):
        RunnerConfig(tick_seconds=600)
    with pytest.raises(ValueError):
        RunnerConfig(tick_seconds=0)


# ---- the runner script ----

def _load_script():
    import importlib.util
    from pathlib import Path
    path = Path(__file__).resolve().parents[2] / "scripts" / "run_paper_runner.py"
    spec = importlib.util.spec_from_file_location("run_paper_runner", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_script_refuses_to_start_on_a_store_with_orders_from_an_earlier_run(tmp_path, capsys):
    from packages.common.durable_store import DurableOrderStore
    from packages.common.environment import Environment
    from packages.schemas.audit import ApprovedOrderSnapshot
    from packages.schemas.core import OrderState

    store = DurableOrderStore(str(tmp_path / "orders.db"), Environment.PAPER)
    snap = ApprovedOrderSnapshot(client_order_id="old-run-0000001", symbol="AAPL", side="BUY", qty=1.0,
                                 order_type="MARKET", created_at=utc(2026, 8, 11, 15), hash="0" * 64)
    store.record_submission(client_order_id="old-run-0000001", decision_id="d", risk_approval_id="r",
                            snapshot=snap, state=OrderState.SUBMITTED, at=utc(2026, 8, 11, 15))
    assert _load_script().main(["--state-dir", str(tmp_path)]) == 2
    assert "refusing to start" in capsys.readouterr().err


def test_script_shows_the_schedule_without_touching_state(tmp_path, capsys):
    assert _load_script().main(["--show-schedule", "--state-dir", str(tmp_path / "x")]) == 0
    out = capsys.readouterr().out
    assert "09:45" in out and "JST" in out and not (tmp_path / "x").exists()
