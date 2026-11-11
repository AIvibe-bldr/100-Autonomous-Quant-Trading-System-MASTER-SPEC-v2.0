"""SAFETY_AUDIT F5: operator kill switch. File-based so a separate CLI process
can reach the running system; fail-closed; explicit operator + reason on every
change; resume is never automatic."""
import json
import subprocess
import sys
from datetime import date, datetime, time, timezone
from pathlib import Path

import pytest

from packages.common.clock import FrozenClock
from packages.common.kill_switch import KillSwitch
from packages.schemas.core import Action
from services.market_data.universe import UniverseManager, UniverseSymbol
from services.risk.master_controller import RiskState
from tests.conftest import SYMBOLS, build_pipeline

REPO = Path(__file__).resolve().parents[2]
AT = datetime(2026, 8, 10, 15, 0, tzinfo=timezone.utc)


def test_default_is_not_engaged(tmp_path):
    assert not KillSwitch(tmp_path / "ks.json").status().engaged


def test_engage_records_operator_reason_and_clear_needs_them_too(tmp_path):
    ks = KillSwitch(tmp_path / "ks.json")
    ks.engage(operator="alice", reason="bad news", at=AT)
    st = ks.status()
    assert st.engaged and st.operator == "alice" and st.reason == "bad news"
    with pytest.raises(ValueError):
        ks.clear(operator="", reason="ok", at=AT)
    with pytest.raises(ValueError):
        ks.clear(operator="bob", reason="  ", at=AT)
    ks.clear(operator="bob", reason="investigated", at=AT)
    assert not ks.status().engaged


def test_every_change_is_appended_to_an_audit_log(tmp_path):
    ks = KillSwitch(tmp_path / "ks.json")
    ks.engage(operator="alice", reason="r1", at=AT)
    ks.clear(operator="bob", reason="r2", at=AT)
    lines = [json.loads(l) for l in ks.log_path.read_text(encoding="utf-8").splitlines()]
    assert [(l["action"], l["operator"]) for l in lines] == [("engage", "alice"), ("clear", "bob")]


@pytest.mark.parametrize("content", ["{not json", "", '{"engaged": "maybe"}', "[]"])
def test_unreadable_or_corrupt_flag_is_treated_as_engaged(tmp_path, content):
    p = tmp_path / "ks.json"
    p.write_text(content)
    st = KillSwitch(p).status()
    assert st.engaged and "unreadable" in st.reason


def _pipeline(ks):
    clock = FrozenClock(current=datetime.combine(date(2026, 8, 10), time(15, 0), tzinfo=timezone.utc))
    universe = UniverseManager()
    for s in SYMBOLS:
        universe.add(UniverseSymbol(symbol=s, listed_from=date(2015, 1, 1)))
    p = build_pipeline(clock, universe, kill_switch=ks)
    return p, clock


def test_engaged_switch_stops_the_session_before_any_decision_call(tmp_path):
    ks = KillSwitch(tmp_path / "ks.json")
    ks.engage(operator="alice", reason="manual stop", at=AT)
    p, clock = _pipeline(ks)
    calls = []
    real = p.decision_model.decide
    p.decision_model.decide = lambda ctx: (calls.append(1), real(ctx))[1]
    result = p.run_session(clock.now())
    assert calls == [] and not result.fills
    assert p.risk_controller.state is RiskState.HALT_NEW_ENTRIES
    assert any("kill switch" in r and "alice" in r for r in result.no_trade_reasons)


def test_switch_engaged_mid_session_blocks_the_next_entry(tmp_path):
    ks = KillSwitch(tmp_path / "ks.json")
    p, clock = _pipeline(ks)
    real = p.execution.submit
    n = []

    def submit(order, snapshot=None, **kw):
        state = real(order, snapshot=snapshot, **kw)
        if order.intent.side is Action.BUY:
            n.append(1)
            if len(n) == 1:                   # the first entry went out; now the operator stops
                ks.engage(operator="alice", reason="stop now", at=AT)
        return state                          # (its protective SELL stop still goes through)

    p.execution.submit = submit
    result = p.run_session(clock.now())
    assert len(result.fills) >= 1 and len(n) == 1       # exactly one entry went out; the rest were blocked
    assert any("kill switch" in r for r in result.no_trade_reasons)


def test_exits_still_work_while_engaged(tmp_path):
    ks = KillSwitch(tmp_path / "ks.json")
    p, clock = _pipeline(ks)
    p.run_session(clock.now())
    assert p.ledger.positions
    ks.engage(operator="alice", reason="stop", at=AT)
    calls = []
    real = p.manage_open_positions
    p.manage_open_positions = lambda now, result=None: (calls.append(1), real(now, result))[1]
    clock.advance(86400)
    p.run_session(clock.now())
    assert calls == [1]                                      # exit management still ran first
    assert p.risk_controller.state is RiskState.HALT_NEW_ENTRIES


def test_clearing_the_file_does_not_auto_resume_a_halted_process(tmp_path):
    ks = KillSwitch(tmp_path / "ks.json")
    ks.engage(operator="alice", reason="stop", at=AT)
    p, clock = _pipeline(ks)
    p.run_session(clock.now())
    ks.clear(operator="bob", reason="all clear", at=AT)
    clock.advance(86400)
    p.run_session(clock.now())
    assert p.risk_controller.state is RiskState.HALT_NEW_ENTRIES   # restart = explicit resume


def test_cli_engage_status_clear_roundtrip(tmp_path):
    flag = tmp_path / "ks.json"
    env = {"QUANT_KILL_SWITCH_FILE": str(flag), "PATH": "/usr/bin:/bin"}

    def run(*args):
        return subprocess.run([sys.executable, str(REPO / "scripts" / "kill_switch.py"), *args],
                              capture_output=True, text=True, env=env, cwd=REPO)

    assert run("status").returncode == 0
    r = run("engage", "--operator", "alice", "--reason", "cli test")
    assert r.returncode == 0 and KillSwitch(flag).status().engaged
    assert run("status").stdout.count("ENGAGED") >= 1
    assert run("clear", "--operator", "bob").returncode != 0          # reason is required
    assert run("clear", "--operator", "bob", "--reason", "done").returncode == 0
    assert not KillSwitch(flag).status().engaged


def test_status_api_shows_the_switch_read_only(tmp_path):
    from fastapi.testclient import TestClient

    from apps.api.main import create_app
    ks = KillSwitch(tmp_path / "ks.json")
    p, _ = _pipeline(ks)
    client = TestClient(create_app(p))
    assert client.get("/kill-switch").json()["engaged"] is False
    ks.engage(operator="alice", reason="api view", at=AT)
    body = client.get("/kill-switch").json()
    assert body["engaged"] is True and body["operator"] == "alice"
    assert client.post("/kill-switch").status_code == 405          # still no write surface
