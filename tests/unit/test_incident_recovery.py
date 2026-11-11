"""Recovery (§68-69, §71): a major fault stays open across restarts until a named human resolves it."""
import importlib.util
import json
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from packages.common.calendar import TradingCalendar
from packages.common.clock import FrozenClock
from services.market_data.universe import UniverseManager, UniverseSymbol
from services.supervisor.heartbeat import HeartbeatRegistry
from services.supervisor.incident_log import UNREADABLE_ID, IncidentLog
from services.supervisor.recovery import FaultSeverity, RecoveryManager, ResumeForbiddenError
from services.supervisor.runner import RunnerConfig, SessionRunner
from tests.conftest import SYMBOLS, build_pipeline

T = datetime(2026, 8, 11, 15, 0, tzinfo=timezone.utc)


def _script(name):
    path = Path(__file__).resolve().parents[2] / "scripts" / name
    spec = importlib.util.spec_from_file_location(name[:-3], path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _major(rm, log, desc="boom"):
    inc = rm.report_fault("runner", desc, FaultSeverity.MAJOR)
    log.record_fault(inc, rm.postmortem(inc))
    return inc


# ---- the log ----

def test_a_major_fault_is_open_until_a_named_human_resolves_it(tmp_path):
    clock = FrozenClock(current=T)
    log, rm = IncidentLog(tmp_path / "i.jsonl"), RecoveryManager(clock)
    inc = _major(rm, log)
    assert [i["incident_id"] for i in log.open_major()] == [inc.incident_id]
    with pytest.raises(ValueError):
        log.record_resolution(inc.incident_id, "  ", "fixed", T, True)
    with pytest.raises(ValueError):
        log.record_resolution(inc.incident_id, "alice", "", T, True)
    with pytest.raises(KeyError):
        log.record_resolution("INC-9999", "alice", "x", T, True)
    log.record_resolution(inc.incident_id, "alice", "root cause: feed outage; fixed", T, True)
    assert log.open_major() == []
    assert log.incidents()[0]["resolution"]["operator"] == "alice"


def test_incident_ids_stay_unique_across_restarts(tmp_path):
    clock = FrozenClock(current=T)
    log = IncidentLog(tmp_path / "i.jsonl")
    first = _major(RecoveryManager(clock, start_seq=log.next_seq()), log)
    second = _major(RecoveryManager(clock, start_seq=log.next_seq()), log)
    assert (first.incident_id, second.incident_id) == ("INC-0001", "INC-0002")


def test_the_postmortem_skeleton_is_saved_with_the_fault(tmp_path):
    log = IncidentLog(tmp_path / "i.jsonl")
    _major(RecoveryManager(FrozenClock(current=T)), log, "quote feed exploded")
    pm = log.incidents()[0]["postmortem"]
    assert pm["incident_id"] == "INC-0001" and pm["root_cause"].startswith("TODO")
    assert "quote feed exploded" in json.dumps(pm["timeline"])


@pytest.mark.parametrize("content", ["{not json\n", "\x00\x01"])
def test_an_unreadable_log_counts_as_an_open_major_incident(tmp_path, content):
    p = tmp_path / "i.jsonl"
    p.write_text(content)
    assert [i["incident_id"] for i in IncidentLog(p).open_major()] == [UNREADABLE_ID]


def test_a_missing_log_means_no_incidents(tmp_path):
    assert IncidentLog(tmp_path / "none.jsonl").open_major() == []


def test_the_recovery_rule_itself_major_cannot_resume_without_a_human():
    rm = RecoveryManager(FrozenClock(current=T))
    inc = rm.report_fault("x", "y", FaultSeverity.MAJOR)
    with pytest.raises(ResumeForbiddenError):
        rm.resume(inc)
    rm.resume(inc, human_approved=True)


# ---- the runner ----

def _runner(tmp_path, start=T):
    clock = FrozenClock(current=start)
    universe = UniverseManager()
    for s in SYMBOLS:
        universe.add(UniverseSymbol(symbol=s, listed_from=date(2015, 1, 1)))
    pipeline = build_pipeline(clock, universe)
    log = IncidentLog(tmp_path / "i.jsonl")
    sink = type("S", (), {"send": lambda self, sev, msg: None})()
    runner = SessionRunner(pipeline, TradingCalendar(), clock, HeartbeatRegistry(clock), sink, RunnerConfig(),
                           sleep=lambda s: clock.advance(s), recovery=RecoveryManager(clock, log.next_seq()),
                           incident_log=log)
    return runner, pipeline, log


def test_a_blown_up_session_leaves_an_open_major_incident_with_a_postmortem(tmp_path):
    runner, pipeline, log = _runner(tmp_path)

    def boom(now):
        raise RuntimeError("feed exploded")
    pipeline.run_session = boom
    runner.run_forever(lambda: False)
    (inc,) = log.open_major()
    assert inc["service"] == "runner" and "feed exploded" in inc["description"]
    assert inc["postmortem"]["corrective_action"] == "TODO"


def test_data_warnings_are_a_minor_fault_that_resolves_itself(tmp_path):
    runner, pipeline, log = _runner(tmp_path)
    real = pipeline.run_session

    def warn(now):
        r = real(now)
        r.data_warnings.append("news fetch failed")
        return r
    pipeline.run_session = warn
    runner.tick()
    (inc,) = log.incidents()
    assert inc["severity"] == "MINOR" and inc["resolved"] and inc["resolution"]["human_approved"] is False
    assert log.open_major() == []


# ---- the scripts ----

def test_the_runner_script_refuses_to_start_while_a_major_incident_is_open_then_starts_after_resolution(
        tmp_path, monkeypatch, capsys):
    log_path = tmp_path / "incidents.jsonl"
    monkeypatch.setenv("QUANT_INCIDENT_LOG", str(log_path))
    log = IncidentLog(log_path)
    inc = _major(RecoveryManager(FrozenClock(current=T)), log)
    mod = _script("run_paper_runner.py")
    state = str(tmp_path / "state")
    assert mod.main(["--state-dir", state]) == 2
    assert inc.incident_id in capsys.readouterr().err
    assert _script("incidents.py").main(["resolve", inc.incident_id, "--operator", "alice",
                                         "--reason", "fixed the feed"]) == 0
    monkeypatch.setattr(mod.SessionRunner, "run_forever", lambda self, should_stop=None: None)
    assert mod.main(["--state-dir", state]) == 0                  # now it starts (and stops at once)


def test_the_incidents_cli_lists_shows_and_refuses_an_anonymous_resolution(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("QUANT_INCIDENT_LOG", str(tmp_path / "i.jsonl"))
    inc = _major(RecoveryManager(FrozenClock(current=T)), IncidentLog(tmp_path / "i.jsonl"), "disk full")
    cli = _script("incidents.py")
    assert cli.main(["list"]) == 0 and "OPEN" in capsys.readouterr().out
    assert cli.main(["show", inc.incident_id]) == 0 and "disk full" in capsys.readouterr().out
    assert cli.main(["show", "INC-9999"]) == 2
    assert cli.main(["resolve", inc.incident_id, "--operator", " ", "--reason", "x"]) == 2
    assert IncidentLog(tmp_path / "i.jsonl").open_major()                        # still open
