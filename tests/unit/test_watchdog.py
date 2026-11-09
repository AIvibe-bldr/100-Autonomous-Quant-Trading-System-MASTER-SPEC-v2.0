"""Runner status file, watchdog findings, edge-triggered alerts, and the CLI end to end."""
import importlib.util
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from packages.common.alerts import AlertSeverity
from packages.common.calendar import TradingCalendar
from packages.common.clock import FrozenClock
from services.market_data.universe import UniverseManager, UniverseSymbol
from services.supervisor.heartbeat import HeartbeatRegistry
from services.supervisor.runner import RunnerConfig, SessionRunner
from services.supervisor.status_file import (
    FAILED, RUNNING, STARTING, STOPPED, WAITING, RunnerStatus, StatusUnreadable, read_status, write_status,
)
from services.supervisor.watchdog import (
    Finding, Inputs, Level, WatchdogConfig, alert_on_change, evaluate, exit_code, worst,
)
from tests.conftest import SYMBOLS, build_pipeline

CAL = TradingCalendar()


def utc(y, m, d, h, mi=0):
    return datetime(y, m, d, h, mi, tzinfo=timezone.utc)


NOW = utc(2026, 8, 11, 15, 0)                                    # Tue 11:00 ET, market open


def status(state=WAITING, age_s=30, at=NOW, **kw):
    """A status written `age_s` seconds before `at`."""
    base = dict(pid=1, environment="PAPER", state=state, started_at=(at - timedelta(hours=2)).isoformat(),
                updated_at=(at - timedelta(seconds=age_s)).isoformat(),
                last_session_date="2026-08-11", sessions_run=1)
    base.update(kw)
    return RunnerStatus(**base)


def codes(inputs, **kw):
    return {f.code: f.level for f in evaluate(inputs, CAL, **kw)}


# ---- the file ----

def test_status_roundtrips_atomically_and_a_missing_file_is_none(tmp_path):
    p = tmp_path / "s.json"
    assert read_status(p) is None
    write_status(p, status())
    assert read_status(p) == status() and not list(tmp_path.glob("*.tmp"))


@pytest.mark.parametrize("content", ["{not json", "[]", '{"pid": 1}'])
def test_a_corrupt_status_file_raises_instead_of_reading_as_no_runner(tmp_path, content):
    (tmp_path / "s.json").write_text(content)
    with pytest.raises(StatusUnreadable):
        read_status(tmp_path / "s.json")


# ---- findings ----

def test_a_healthy_runner_has_no_findings():
    assert codes(Inputs(now=NOW, status=status())) == {}
    assert worst([]) is Level.OK and exit_code([]) == 0


def test_no_status_and_unreadable_status_are_critical():
    assert codes(Inputs(now=NOW, status=None)) == {"no_status": Level.CRITICAL}
    assert codes(Inputs(now=NOW, status=None, status_error="corrupt")) == {"status_unreadable": Level.CRITICAL}


def test_a_stale_heartbeat_is_critical_but_a_session_in_progress_gets_longer():
    assert codes(Inputs(now=NOW, status=status(age_s=301)))["heartbeat_stale"] is Level.CRITICAL
    assert "heartbeat_stale" not in codes(Inputs(now=NOW, status=status(age_s=299)))
    running = status(RUNNING, age_s=600, session_started_at=(NOW - timedelta(minutes=10)).isoformat())
    assert "heartbeat_stale" not in codes(Inputs(now=NOW, status=running))              # an LLM session takes minutes
    assert codes(Inputs(now=NOW, status=status(RUNNING, age_s=31 * 60)))["heartbeat_stale"] is Level.CRITICAL


def test_failed_stopped_and_not_normal_risk_state():
    assert codes(Inputs(now=NOW, status=status(FAILED, failure="boom")))["runner_failed"] is Level.CRITICAL
    assert codes(Inputs(now=NOW, status=status(STOPPED)))["runner_stopped"] is Level.WARNING
    assert "runner_stopped" not in codes(Inputs(now=NOW, status=status(STOPPED)), expect_running=False)
    assert codes(Inputs(now=NOW, status=status(risk_state="HALT_NEW_ENTRIES")))["risk_not_normal"] is Level.WARNING


def test_the_operator_files_become_findings():
    i = Inputs(now=NOW, status=status(), open_major_incidents=["INC-0002"], kill_switch_engaged=True,
               live_gate_open=False)
    assert codes(i) == {"open_incident": Level.CRITICAL, "kill_switch": Level.WARNING,
                        "live_gate_closed": Level.WARNING}
    assert codes(Inputs(now=NOW, status=status(), live_gate_open=None)) == {}             # not a LIVE system


def test_an_alive_runner_that_missed_todays_session_is_a_warning_only_once_overdue():
    early, late = utc(2026, 8, 11, 14, 0), utc(2026, 8, 11, 14, 30)                  # 10:00 ET and 10:30 ET
    missed = dict(last_session_date="2026-08-10")
    assert "session_overdue" not in codes(Inputs(now=early, status=status(at=early, **missed)))   # before 10:15
    assert codes(Inputs(now=late, status=status(at=late, **missed)))["session_overdue"] is Level.WARNING
    assert "session_overdue" not in codes(Inputs(now=late, status=status(at=late, last_session_date="2026-08-11")))
    sat = utc(2026, 8, 15, 15, 0)                                                      # Saturday: no session expected
    assert "session_overdue" not in codes(Inputs(now=sat, status=status(at=sat, last_session_date="2026-08-14")))


# ---- alerting on change ----

class Sink:
    def __init__(self):
        self.sent = []

    def send(self, severity, message):
        self.sent.append((severity, message))


def test_alerts_fire_on_new_findings_stay_quiet_while_unchanged_and_announce_recovery(tmp_path):
    state, sink = tmp_path / "w.json", Sink()
    crit = [Finding(Level.CRITICAL, "heartbeat_stale", "no heartbeat for 9 min")]
    assert alert_on_change(crit, state, sink, NOW) == ["heartbeat_stale"]
    assert alert_on_change(crit, state, sink, NOW + timedelta(minutes=1)) == []          # unchanged: silent
    assert [s for s, _ in sink.sent] == [AlertSeverity.CRITICAL]
    assert alert_on_change([], state, sink, NOW + timedelta(minutes=2)) == ["recovered:heartbeat_stale"]
    assert sink.sent[-1][0] is AlertSeverity.WARNING and "recovered" in sink.sent[-1][1]
    assert alert_on_change([], state, sink, NOW + timedelta(minutes=3)) == []


def test_a_warning_that_escalates_alerts_again_and_a_long_critical_is_repeated(tmp_path):
    state, sink = tmp_path / "w.json", Sink()
    warn = [Finding(Level.WARNING, "x", "m")]
    crit = [Finding(Level.CRITICAL, "x", "m")]
    alert_on_change(warn, state, sink, NOW)
    assert alert_on_change(crit, state, sink, NOW + timedelta(minutes=1)) == ["x"]       # escalated
    assert alert_on_change(crit, state, sink, NOW + timedelta(hours=5)) == []
    assert alert_on_change(crit, state, sink, NOW + timedelta(hours=7)) == ["x"]         # the reminder


# ---- the runner publishes proof of life ----

def _runner(tmp_path, start=NOW, sink_fn=None):
    clock = FrozenClock(current=start)
    universe = UniverseManager()
    for s in SYMBOLS:
        universe.add(UniverseSymbol(symbol=s, listed_from=date(2015, 1, 1)))
    p = build_pipeline(clock, universe)
    seen = []
    sink = sink_fn or (lambda st: (seen.append(st), write_status(tmp_path / "runner_status.json", st)))
    alerts = type("S", (), {"send": lambda self, a, b: None})()
    r = SessionRunner(p, CAL, clock, HeartbeatRegistry(clock), alerts, RunnerConfig(),
                      sleep=lambda s: clock.advance(s), status_sink=sink)
    return r, p, clock, seen


def test_the_runner_reports_starting_running_waiting_and_stopped(tmp_path):
    r, p, clock, seen = _runner(tmp_path)
    r.run_forever(lambda: clock.now() >= NOW + timedelta(minutes=5))
    states = [s.state for s in seen]
    assert states[0] == STARTING and RUNNING in states and states[-1] == STOPPED and WAITING in states
    during = next(s for s in seen if s.state == RUNNING)
    assert during.session_started_at                                                  # a session in progress is marked
    last = seen[-1]
    assert last.sessions_run == 1 and last.last_session_date == "2026-08-11" and last.risk_state == "NORMAL"
    assert last.environment == "PAPER" and last.next_session_at and last.pid > 0


def test_a_failed_runner_publishes_failed_with_the_reason(tmp_path):
    r, p, clock, seen = _runner(tmp_path)

    def boom(now):
        raise RuntimeError("feed exploded")
    p.run_session = boom
    r.run_forever(lambda: False)
    assert seen[-1].state == FAILED and "feed exploded" in seen[-1].failure and seen[-1].risk_state == "HALT_NEW_ENTRIES"


def test_a_status_write_failure_never_stops_the_runner(tmp_path):
    def broken(st):
        raise OSError("disk full")
    r, p, clock, _ = _runner(tmp_path, sink_fn=broken)
    assert r.run_forever(lambda: clock.now() >= NOW + timedelta(minutes=3)) is None


# ---- the CLI, end to end ----

def _cli():
    path = Path(__file__).resolve().parents[2] / "scripts" / "watchdog.py"
    spec = importlib.util.spec_from_file_location("watchdog_cli", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_cli_ok_then_a_dead_runner_is_critical_alerted_once_and_recorded(tmp_path, capsys):
    cli = _cli()
    r, p, clock, _ = _runner(tmp_path)
    r.run_forever(lambda: clock.now() >= NOW + timedelta(minutes=3))
    r._publish(WAITING)                                                               # last proof of life
    t = clock.now()
    assert cli.check_once(tmp_path, True, True, now=t + timedelta(seconds=30)) == 0
    assert "OK" in capsys.readouterr().out
    # the runner dies: its status stops being refreshed
    dead = t + timedelta(minutes=10)
    assert cli.check_once(tmp_path, True, True, now=dead) == 2
    out = capsys.readouterr().out
    assert "heartbeat_stale" in out and "CRITICAL" in out
    assert cli.check_once(tmp_path, True, True, now=dead + timedelta(minutes=1)) == 2     # still critical ...
    history = [json.loads(l) for l in (tmp_path / "alerts.jsonl").read_text().splitlines()]
    assert [h["severity"] for h in history].count("CRITICAL") == 1                        # ... but alerted once
    assert "heartbeat_stale" in history[0]["message"]


def test_cli_with_no_status_file_is_critical_and_no_alert_mode_stays_silent(tmp_path, capsys):
    cli = _cli()
    assert cli.check_once(tmp_path, True, False, now=NOW) == 2
    assert "no_status" in capsys.readouterr().out and not (tmp_path / "alerts.jsonl").exists()
