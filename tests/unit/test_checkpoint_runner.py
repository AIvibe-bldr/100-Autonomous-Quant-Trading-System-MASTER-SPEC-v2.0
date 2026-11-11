"""The runner and the script with a durable account: resume, no double session, fail closed, mirror re-seeded."""
import importlib.util
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from packages.common.calendar import TradingCalendar
from packages.common.clock import FrozenClock
from packages.common.durable_store import DurableCheckpointStore, DurableOrderStore, DurableSessionStore
from packages.common.environment import Environment
from services.checkpoint import Checkpointer, restore_latest
from services.composition import attach_learning_observers
from services.market_data.universe import UniverseManager, UniverseSymbol
from services.supervisor.heartbeat import HeartbeatRegistry
from services.supervisor.incident_log import IncidentLog
from services.supervisor.recovery import RecoveryManager
from services.supervisor.runner import RunnerConfig, SessionRunner
from services.supervisor.session_record import SessionRecorder
from tests.conftest import SYMBOLS, build_pipeline
from tests.unit.test_checkpoint import fingerprint

START = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)


def stack(tmp_path, clock, resume=False, last_run=None, checkpoint_fn=None, incident_log=None):
    universe = UniverseManager()
    for s in SYMBOLS:
        universe.add(UniverseSymbol(symbol=s, listed_from=date(2015, 1, 1)))
    orders = DurableOrderStore(str(tmp_path / "orders.db"), Environment.PAPER)
    ckpt = DurableCheckpointStore(str(tmp_path / "checkpoints.db"), Environment.PAPER)
    sessions = DurableSessionStore(str(tmp_path / "sessions.db"), Environment.PAPER)
    p = build_pipeline(clock, universe, initial_cash=670.0, order_store=orders)
    report = restore_latest(p, ckpt) if resume else None
    cp = Checkpointer(p, ckpt)
    p.checkpointer = checkpoint_fn(cp) if checkpoint_fn else cp
    obs = attach_learning_observers(p, 670.0, resumed_equity=(sessions.latest_equity() or 670.0) if resume else None)
    recorder = SessionRecorder(p, sessions, "mock-ai")
    sink = type("S", (), {"send": lambda self, a, b: None})()
    runner = SessionRunner(
        p, TradingCalendar(), clock, HeartbeatRegistry(clock), sink, RunnerConfig(), sleep=lambda s: clock.advance(s),
        after_session=lambda at, r: (recorder(at, r), p.execution.broker.settle()),
        checkpoint=p.checkpointer, initial_last_run=last_run,
        recovery=RecoveryManager(clock) if incident_log else None, incident_log=incident_log)
    return p, runner, sessions, obs, report


def run_until(runner, clock, until):
    return runner.run_forever(lambda: clock.now() >= until)


def test_a_resumed_runner_continues_the_account_without_running_the_same_day_twice(tmp_path):
    clock_a = FrozenClock(current=START)
    (tmp_path / "a").mkdir()
    a, ra, sa, _, _ = stack(tmp_path / "a", clock_a)
    run_until(ra, clock_a, datetime(2026, 8, 21, 0, 0, tzinfo=timezone.utc))              # Aug 11 .. 20: 8 sessions

    (tmp_path / "b").mkdir()
    clock_b = FrozenClock(current=START)
    b1, rb1, sb1, _, _ = stack(tmp_path / "b", clock_b)
    run_until(rb1, clock_b, datetime(2026, 8, 15, 0, 0, tzinfo=timezone.utc))             # 4 sessions, then it "dies"
    assert len(sb1.all()) == 4

    # a new process, same files, started late on the SAME day it last ran (Aug 14, 16:00 ET)
    clock_b.current = datetime(2026, 8, 14, 20, 0, tzinfo=timezone.utc)
    last = date.fromisoformat(sb1.all()[-1].session_date)
    b2, rb2, sb2, obs, report = stack(tmp_path / "b", clock_b, resume=True, last_run=last)
    assert report.lost_orders == [] and fingerprint(b2) == fingerprint(b1)
    clock_b.current = datetime(2026, 8, 14, 13, 50, tzinfo=timezone.utc)                  # market open, session time passed
    rb2.tick()
    assert len(sb2.all()) == 4                                                            # Aug 14 is NOT run a second time
    clock_b.current = datetime(2026, 8, 15, 0, 0, tzinfo=timezone.utc)
    run_until(rb2, clock_b, datetime(2026, 8, 21, 0, 0, tzinfo=timezone.utc))
    assert [r.session_date for r in sb2.all()] == [r.session_date for r in sa.all()]
    assert fingerprint(b2) == fingerprint(a)                                              # same account as never stopping
    cells = next(o for o in obs if o.name == "capital_cells").summary()
    assert cells["reconciliation"]["sessions_inconsistent"] == 0 and cells["errors"] == []   # the mirror was re-seeded
    assert cells["seeded_at_resume"]                                                      # positions were carried over


def test_a_checkpoint_that_cannot_be_written_stops_the_runner_instead_of_trading_unsaved(tmp_path):
    clock = FrozenClock(current=START)
    log = IncidentLog(tmp_path / "i.jsonl")

    def broken(real):
        def fn(reason):
            if reason.startswith("session_end"):
                raise OSError("disk full")
            real(reason)
        return fn
    p, runner, sessions, _, _ = stack(tmp_path, clock, checkpoint_fn=broken, incident_log=log)
    reason = run_until(runner, clock, datetime(2026, 8, 14, 0, 0, tzinfo=timezone.utc))
    assert "checkpoint failed" in reason and "disk full" in reason
    assert p.risk_controller.state.value == "HALT_NEW_ENTRIES" and log.open_major()


def test_an_order_checkpoint_failure_inside_a_session_is_a_halted_failure_too(tmp_path):
    clock = FrozenClock(current=START)

    def broken(real):
        def fn(reason):
            if reason == "order":
                raise OSError("disk full")
            real(reason)
        return fn
    p, runner, _, _, _ = stack(tmp_path, clock, checkpoint_fn=broken)
    reason = run_until(runner, clock, datetime(2026, 8, 12, 0, 0, tzinfo=timezone.utc))
    assert "OSError" in reason and runner.failed


def test_the_runner_saves_at_the_start_and_end_of_every_session(tmp_path):
    clock = FrozenClock(current=START)
    reasons = []

    def spy(real):
        def fn(reason):
            reasons.append(reason)
            real(reason)
        return fn
    p, runner, _, _, _ = stack(tmp_path, clock, checkpoint_fn=spy)
    run_until(runner, clock, datetime(2026, 8, 13, 0, 0, tzinfo=timezone.utc))
    assert reasons[0] == "session_start:2026-08-11" and "session_end:2026-08-11" in reasons
    assert reasons.index("session_start:2026-08-11") < reasons.index("order") < reasons.index("session_end:2026-08-11")


# ---- the script ----

def script():
    path = Path(__file__).resolve().parents[2] / "scripts" / "run_paper_runner.py"
    spec = importlib.util.spec_from_file_location("run_paper_runner", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_script_starts_fresh_refuses_a_used_state_dir_and_resumes_only_with_the_flag(tmp_path, monkeypatch, capsys):
    mod = script()
    monkeypatch.setattr(mod.SessionRunner, "run_forever", lambda self, should_stop=None: None)
    state = str(tmp_path / "s")
    assert mod.main(["--state-dir", state]) == 0                                           # a fresh account ...
    assert DurableCheckpointStore(state + "/checkpoints.db", Environment.PAPER).info()["reason"] == "startup"
    assert mod.main(["--state-dir", state]) == 2 and "--resume" in capsys.readouterr().err   # ... is not silently reused
    assert mod.main(["--state-dir", state, "--resume"]) == 0
    assert DurableCheckpointStore(state + "/checkpoints.db", Environment.PAPER).info()["reason"] == "resumed"


def test_resume_needs_something_to_resume_and_a_damaged_account_is_left_to_a_human(tmp_path, monkeypatch, capsys):
    mod = script()
    monkeypatch.setattr(mod.SessionRunner, "run_forever", lambda self, should_stop=None: None)
    assert mod.main(["--state-dir", str(tmp_path / "empty"), "--resume"]) == 2
    assert "nothing to resume" in capsys.readouterr().err
    state = tmp_path / "s"
    assert mod.main(["--state-dir", str(state)]) == 0
    import sqlite3
    con = sqlite3.connect(state / "checkpoints.db")
    con.execute("UPDATE paper_checkpoints SET checksum = 'bad'")
    con.commit()
    assert mod.main(["--state-dir", str(state), "--resume"]) == 2
    assert "cannot resume" in capsys.readouterr().err


def test_a_halt_survives_a_resume_until_a_named_human_clears_it(tmp_path, monkeypatch, capsys):
    from packages.common.durable_store import DurableCheckpointStore as CS
    from services.risk.master_controller import RiskState
    mod = script()
    monkeypatch.setattr(mod.SessionRunner, "run_forever", lambda self, should_stop=None: None)
    state = tmp_path / "s"
    assert mod.main(["--state-dir", str(state)]) == 0
    rec = CS(str(state / "checkpoints.db"), Environment.PAPER).latest_valid()
    halted = dict(rec.state, risk_state="HALT_NEW_ENTRIES")
    CS(str(state / "checkpoints.db"), Environment.PAPER).save(halted, "halted", datetime.now(timezone.utc))
    # resuming keeps the halt
    assert mod.main(["--state-dir", str(state), "--resume"]) == 0
    assert CS(str(state / "checkpoints.db"), Environment.PAPER).latest_valid().state["risk_state"] == "HALT_NEW_ENTRIES"
    # clearing needs a named operator and a reason
    assert mod.main(["--state-dir", str(state), "--resume", "--clear-halt"]) == 2
    assert "--operator" in capsys.readouterr().err
    assert mod.main(["--state-dir", str(state), "--resume", "--clear-halt", "--operator", "alice",
                     "--reason", "broker was back; checked the books"]) == 0
    assert "cleared on resume by alice" in capsys.readouterr().out
    assert CS(str(state / "checkpoints.db"), Environment.PAPER).latest_valid().state["risk_state"] == RiskState.NORMAL.value
    (inc,) = IncidentLog.from_env().incidents()
    assert inc["severity"] == "MINOR" and inc["resolution"]["operator"] == "alice" and inc["resolution"]["human_approved"]


def test_resume_if_exists_starts_new_when_empty_and_resumes_when_not(tmp_path, monkeypatch):
    mod = script()
    monkeypatch.setattr(mod.SessionRunner, "run_forever", lambda self, should_stop=None: None)
    state = str(tmp_path / "s")
    assert mod.main(["--state-dir", state, "--resume-if-exists"]) == 0                      # nothing there: new account
    assert DurableCheckpointStore(state + "/checkpoints.db", Environment.PAPER).info()["reason"] == "startup"
    assert mod.main(["--state-dir", state, "--resume-if-exists"]) == 0                      # there is one: resumed
    assert DurableCheckpointStore(state + "/checkpoints.db", Environment.PAPER).info()["reason"] == "resumed"
