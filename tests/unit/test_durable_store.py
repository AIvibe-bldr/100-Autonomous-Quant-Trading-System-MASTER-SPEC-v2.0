"""Regression tests for docs/SAFETY_AUDIT.md F9/F3 (durable idempotency).

The point of `DurableOrderStore` is that it survives what the in-memory
`ExecutionEngine._submitted` dict cannot: a process restart. So the
integration test below deliberately does NOT reuse one `ExecutionEngine` —
it builds a second one from scratch, pointed at the same store file, to
simulate exactly that.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from packages.broker_adapters.base import DuplicateClientOrderIdError
from packages.broker_adapters.paper import PaperBroker
from packages.common.clock import FrozenClock
from packages.common.durable_store import DurableOrderStore
from packages.common.environment import Environment
from packages.schemas.audit import ApprovedOrderSnapshot
from packages.schemas.core import OrderState
from services.execution.engine import ExecutionEngine
from services.execution.state_machine import OrderStateMachine
from services.risk.master_controller import MasterRiskController
from tests.conftest import SESSION_TIME
from tests.unit.helpers import submit_approved
from tests.unit.test_invariants import _default_view, _paper_intent


def _build_execution(clock: FrozenClock, store: DurableOrderStore | None,
                     initial_cash: float = 1000.0) -> tuple[ExecutionEngine, MasterRiskController]:
    from packages.common.calendar import TradingCalendar
    from packages.common.risk_config import RiskConfig

    risk = MasterRiskController(config=RiskConfig(), calendar=TradingCalendar(), clock=clock)
    broker = PaperBroker(quote_fn=lambda s: _Quote(20.0), clock=clock, initial_cash=initial_cash)
    sm = OrderStateMachine(clock=clock)
    execution = ExecutionEngine(broker=broker, risk_controller=risk, state_machine=sm,
                                clock=clock, environment=Environment.PAPER, store=store)
    return execution, risk


class _Quote:
    def __init__(self, mid: float) -> None:
        self.mid = mid
        self.bid = mid - 0.05
        self.ask = mid + 0.05


def _submit(execution: ExecutionEngine, risk: MasterRiskController, client_order_id: str):
    from packages.schemas.core import RiskApprovedOrder

    view = _default_view(None, equity=1000.0, settled_cash=1000.0)
    intent = _paper_intent(client_order_id, qty=1)
    verdict = risk.review(intent, view, entry_price=20.0)
    order = RiskApprovedOrder(intent=intent, approval=verdict)
    return execution.submit(order, snapshot=ApprovedOrderSnapshot.from_approved(order))


# --- DurableOrderStore itself -------------------------------------------------

def test_store_rejects_duplicate_client_order_id(tmp_path):
    store = DurableOrderStore(str(tmp_path / "orders.db"), Environment.PAPER)
    intent_kwargs = dict(client_order_id="dur-order-0001", decision_id="d1",
                         risk_approval_id="r1", state=OrderState.SUBMITTED,
                         at=SESSION_TIME)
    snapshot = ApprovedOrderSnapshot(
        client_order_id="dur-order-0001", symbol="AAPL",
        side="BUY", qty=1.0, order_type="MARKET", created_at=SESSION_TIME,
        hash="0" * 64)
    store.record_submission(snapshot=snapshot, **intent_kwargs)
    with pytest.raises(DuplicateClientOrderIdError):
        store.record_submission(snapshot=snapshot, **intent_kwargs)


def test_store_survives_reopening_the_same_file(tmp_path):
    """The whole point: a fresh connection to the same file sees prior rows —
    this is what "durable" means, as opposed to the in-memory dict it backs
    up."""
    path = str(tmp_path / "orders.db")
    snapshot = ApprovedOrderSnapshot(
        client_order_id="dur-order-0002", symbol="AAPL",
        side="BUY", qty=1.0, order_type="MARKET", created_at=SESSION_TIME,
        hash="0" * 64)
    store1 = DurableOrderStore(path, Environment.PAPER)
    store1.record_submission(client_order_id="dur-order-0002", decision_id="d1",
                             risk_approval_id="r1", snapshot=snapshot,
                             state=OrderState.SUBMITTED, at=SESSION_TIME)
    store1.close()

    store2 = DurableOrderStore(path, Environment.PAPER)
    assert store2.is_known("dur-order-0002")
    rec = store2.get("dur-order-0002")
    assert rec.state is OrderState.SUBMITTED
    assert rec.snapshot.symbol == "AAPL"


def test_store_namespaces_by_environment(tmp_path):
    """§73: PAPER and LIVE must never collide even in the same file."""
    path = str(tmp_path / "orders.db")
    snapshot = ApprovedOrderSnapshot(
        client_order_id="dur-order-0003", symbol="AAPL",
        side="BUY", qty=1.0, order_type="MARKET", created_at=SESSION_TIME,
        hash="0" * 64)
    paper_store = DurableOrderStore(path, Environment.PAPER)
    paper_store.record_submission(client_order_id="dur-order-0003", decision_id="d1",
                                  risk_approval_id="r1", snapshot=snapshot,
                                  state=OrderState.SUBMITTED, at=SESSION_TIME)

    live_store = DurableOrderStore(path, Environment.LIVE)
    assert not live_store.is_known("dur-order-0003")


# --- The actual F3 fix: idempotency across a simulated restart ---------------

def test_duplicate_submission_rejected_after_simulated_restart(tmp_path):
    """Reproduces the exact F3 failure scenario: a process crashes after
    submitting an order, restarts with a brand-new (empty) in-memory
    ExecutionEngine, and something retries the same client_order_id. Before
    this fix, nothing in the codebase would have caught this — reproduced by
    the mutation test below (store=None acts as "no fix")."""
    path = str(tmp_path / "orders.db")
    clock = FrozenClock(current=SESSION_TIME)

    store1 = DurableOrderStore(path, Environment.PAPER)
    execution1, risk1 = _build_execution(clock, store1)
    state = _submit(execution1, risk1, "dur-order-0004")
    assert state in (OrderState.ACKNOWLEDGED, OrderState.FILLED, OrderState.PARTIALLY_FILLED)

    # "restart": fresh state machine, fresh engine, fresh risk controller —
    # nothing in-memory survives — reopened against the SAME durable file.
    store2 = DurableOrderStore(path, Environment.PAPER)
    execution2, risk2 = _build_execution(clock, store2)
    with pytest.raises(DuplicateClientOrderIdError):
        _submit(execution2, risk2, "dur-order-0004")
    # The rejection must be the FIRST thing that happens — before any local
    # state-machine mutation — otherwise engine2 is left holding a phantom
    # order that was never really (re)submitted to anything.
    with pytest.raises(KeyError):
        execution2.state_machine.get("dur-order-0004")


def test_without_a_store_the_same_retry_is_not_caught_pre_fix_behavior():
    """Documents the gap this closes: with no store configured (the default,
    and the only option before this fix), a "restart" — a fresh engine —
    has no way to know dur-order-0005 was already submitted."""
    clock = FrozenClock(current=SESSION_TIME)
    execution1, risk1 = _build_execution(clock, store=None)
    _submit(execution1, risk1, "dur-order-0005")

    execution2, risk2 = _build_execution(clock, store=None)
    # no DuplicateClientOrderIdError — this is the pre-fix gap, not a bug in
    # this test: without a durable store, in-memory-only idempotency cannot
    # survive a restart by construction.
    state = _submit(execution2, risk2, "dur-order-0005")
    assert state in (OrderState.ACKNOWLEDGED, OrderState.FILLED, OrderState.PARTIALLY_FILLED)


# --- Recovery -----------------------------------------------------------------

def test_recover_from_store_rehydrates_state_machine(tmp_path):
    path = str(tmp_path / "orders.db")
    clock = FrozenClock(current=SESSION_TIME)

    store1 = DurableOrderStore(path, Environment.PAPER)
    execution1, risk1 = _build_execution(clock, store1)
    _submit(execution1, risk1, "dur-order-0006")

    store2 = DurableOrderStore(path, Environment.PAPER)
    execution2, risk2 = _build_execution(clock, store2)
    recovered = execution2.recover_from_store()
    assert recovered == 1
    rec = execution2.state_machine.get("dur-order-0006")
    assert rec.state in (OrderState.ACKNOWLEDGED, OrderState.FILLED, OrderState.PARTIALLY_FILLED)


def test_recover_from_store_is_a_noop_without_a_store():
    clock = FrozenClock(current=SESSION_TIME)
    execution, _ = _build_execution(clock, store=None)
    assert execution.recover_from_store() == 0
