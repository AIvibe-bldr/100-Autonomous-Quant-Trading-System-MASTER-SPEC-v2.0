"""LIVE gate (§105-106) and broker-environment binding (§73). No real broker exists;
a PaperBroker subclass that claims to be LIVE stands in for one."""
import json
from datetime import datetime, timedelta, timezone

import pytest

from packages.broker_adapters.paper import PaperBroker
from packages.common.calendar import TradingCalendar
from packages.common.clock import FrozenClock
from packages.common.environment import Environment, EnvironmentMismatchError
from packages.common.live_gate import MAX_APPROVAL_DAYS, LiveGate
from packages.common.risk_config import RiskConfig
from packages.schemas.audit import ApprovedOrderSnapshot
from packages.schemas.core import RiskApprovedOrder
from services.execution.engine import ExecutionEngine, LiveGateClosedError
from services.execution.state_machine import OrderStateMachine
from services.risk.master_controller import MasterRiskController, RiskState
from tests.unit.test_invariants import _default_view, _paper_intent

T0 = datetime(2026, 11, 9, 15, 0, tzinfo=timezone.utc)


class _Q:
    mid, bid, ask = 20.0, 19.95, 20.05


class FakeLiveBroker(PaperBroker):
    """Test double: behaves like the paper simulation but reports a LIVE endpoint."""
    @property
    def environment(self):
        return Environment.LIVE


def _gate(tmp_path):
    return LiveGate(tmp_path / "live_gate.json")


def _approve(g, days=7, stage=0.1):
    g.approve("alice", "criteria met", T0, T0 + timedelta(days=days), stage)


# ---- the gate file ----

def test_default_is_closed_and_unreadable_or_malformed_files_fail_closed(tmp_path):
    g = _gate(tmp_path)
    assert not g.status(T0).is_open and "no LIVE approval" in g.status(T0).detail
    for content in ["{not json", "[]", "", '{"approved": "yes"}', '{"approved": true}',
                    json.dumps({"approved": True, "operator": "a", "expires_at": "x",
                                "max_exposure_stage": 0.1}),
                    json.dumps({"approved": True, "operator": "", "expires_at": T0.isoformat(),
                                "max_exposure_stage": 0.1}),
                    json.dumps({"approved": True, "operator": "a",
                                "expires_at": (T0 + timedelta(days=1)).isoformat(),
                                "max_exposure_stage": 5})]:
        g.path.write_text(content)
        assert not g.status(T0).is_open, content


def test_approval_is_bounded_in_time_stage_and_needs_a_named_human(tmp_path):
    g = _gate(tmp_path)
    _approve(g)
    s = g.status(T0)
    assert s.is_open and s.operator == "alice" and s.max_exposure_stage == 0.1
    assert not g.status(T0 + timedelta(days=7)).is_open               # expired exactly at expiry
    for bad in [dict(operator=" ", reason="r"), dict(operator="a", reason=""),
                dict(operator="a", reason="r", stage=0.0), dict(operator="a", reason="r", stage=1.5),
                dict(operator="a", reason="r", days=0), dict(operator="a", reason="r",
                                                              days=MAX_APPROVAL_DAYS + 1)]:
        with pytest.raises(ValueError):
            g.approve(bad["operator"], bad["reason"], T0,
                      T0 + timedelta(days=bad.get("days", 7)), bad.get("stage", 0.1))


def test_revoke_closes_and_every_change_is_logged(tmp_path):
    g = _gate(tmp_path)
    _approve(g)
    g.revoke("bob", "incident", T0)
    assert not g.status(T0).is_open and "revoked" in g.status(T0).detail
    log = [json.loads(l) for l in g.path.with_name(g.path.name + ".log").read_text(encoding="utf-8").splitlines()]
    assert [(e["action"], e["operator"]) for e in log] == [("approve", "alice"), ("revoke", "bob")]


# ---- wiring into execution ----

def _engine(tmp_path, env=Environment.LIVE, broker_cls=FakeLiveBroker, gate=True, cfg=None):
    clock = FrozenClock(current=T0)
    risk = MasterRiskController(config=cfg or RiskConfig(), calendar=TradingCalendar(), clock=clock)
    broker = broker_cls(quote_fn=lambda s: _Q(), clock=clock, initial_cash=1000.0)
    g = _gate(tmp_path) if gate else None
    ex = ExecutionEngine(broker=broker, risk_controller=risk, state_machine=OrderStateMachine(clock=clock),
                         clock=clock, environment=env, live_gate=g)
    return ex, risk, g


def test_broker_and_engine_environments_must_match(tmp_path):
    with pytest.raises(EnvironmentMismatchError):
        _engine(tmp_path, env=Environment.LIVE, broker_cls=PaperBroker)     # simulation as LIVE
    with pytest.raises(EnvironmentMismatchError):
        _engine(tmp_path, env=Environment.PAPER, broker_cls=FakeLiveBroker)  # LIVE endpoint in PAPER


def test_a_live_engine_cannot_be_built_without_a_gate(tmp_path):
    with pytest.raises(LiveGateClosedError):
        _engine(tmp_path, gate=False)


def _order(risk, env=Environment.LIVE, cid="live-order-0001"):
    intent = _paper_intent(cid, qty=1).model_copy(update={"environment": env})
    verdict = risk.review(intent, _default_view(None, equity=1000.0, settled_cash=1000.0),
                          entry_price=20.0)
    order = RiskApprovedOrder(intent=intent, approval=verdict)
    return order, ApprovedOrderSnapshot.from_approved(order)


def test_live_entry_is_refused_while_the_gate_is_closed_and_the_state_halts(tmp_path):
    ex, risk, _ = _engine(tmp_path)
    order, snap = _order(risk)
    with pytest.raises(LiveGateClosedError):
        ex.submit(order, snapshot=snap)
    assert risk.state is RiskState.HALT_NEW_ENTRIES
    assert ex.entries_blocked() and "closed" in ex.entries_blocked()


def test_live_entry_passes_with_a_valid_approval_and_stops_when_it_expires(tmp_path):
    ex, risk, g = _engine(tmp_path)
    _approve(g, days=1, stage=1.0)
    order, snap = _order(risk)
    ex.submit(order, snapshot=snap)                                    # reaches the (fake) broker
    ex.clock.current = T0 + timedelta(days=2)
    order2, snap2 = _order(risk, cid="live-order-0002")
    with pytest.raises(LiveGateClosedError):
        ex.submit(order2, snapshot=snap2)


def test_configured_exposure_stage_may_not_exceed_the_approved_stage(tmp_path):
    ex, risk, g = _engine(tmp_path, cfg=RiskConfig(exposure_cap_stage=0.5))
    _approve(g, stage=0.1)
    order, snap = _order(risk)
    with pytest.raises(LiveGateClosedError, match="exceeds the approved"):
        ex.submit(order, snapshot=snap)


def test_the_gate_does_not_apply_outside_live(tmp_path):
    ex, risk, _ = _engine(tmp_path, env=Environment.PAPER, broker_cls=PaperBroker, gate=False)
    assert ex.entries_blocked_by_live_gate() is None
