"""Chaos / failure injection (MASTER SPEC §70).

Simulates broker timeout, disconnect, partial fill and duplicate submission,
and verifies the system fails SAFE: UNKNOWN state + reconciliation + entry
halts, never duplicate or unauthorized orders.
"""
from __future__ import annotations

import pytest

from packages.broker_adapters.base import DuplicateClientOrderIdError
from packages.broker_adapters.paper import Fault
from packages.schemas.core import (
    Action,
    OrderState,
    RiskApproval,
    RiskApprovedOrder,
)
from services.reconciliation.engine import ReconciliationEngine
from services.risk.master_controller import RiskState
from tests.conftest import SESSION_TIME
from tests.unit.helpers import submit_approved
from tests.unit.test_invariants import _default_view, _paper_intent


def _approved_order(pipeline, cid: str, qty: float = 1.0) -> RiskApprovedOrder:
    intent = _paper_intent(cid, qty=qty)
    verdict = pipeline.risk_controller.review(intent, _default_view(pipeline),
                                              entry_price=20.0)
    assert isinstance(verdict, RiskApproval)
    return RiskApprovedOrder(intent=intent, approval=verdict)


def test_order_timeout_goes_unknown_and_halts(pipeline):
    pipeline.execution.broker.fault = Fault.TIMEOUT
    state = submit_approved(pipeline, _approved_order(pipeline, "chaos-timeout-01"))
    assert state is OrderState.UNKNOWN
    assert pipeline.risk_controller.state is RiskState.HALT_NEW_ENTRIES

    # §46: no blind resubmit — resolution goes through broker status check
    pipeline.execution.broker.fault = Fault.NONE
    resolved = pipeline.execution.resolve_unknown("chaos-timeout-01")
    assert resolved in (OrderState.FILLED, OrderState.ACKNOWLEDGED,
                        OrderState.PARTIALLY_FILLED)
    # and the broker holds exactly one order for that id
    status = pipeline.execution.broker.get_order_status("chaos-timeout-01")
    assert status.client_order_id == "chaos-timeout-01"


def test_broker_disconnect_recorded(pipeline):
    pipeline.execution.broker.fault = Fault.DISCONNECT
    state = submit_approved(pipeline, _approved_order(pipeline, "chaos-disc-0001"))
    assert state is OrderState.UNKNOWN
    assert pipeline.risk_controller.state is RiskState.FULL_BROKER_DISCONNECT


def test_partial_fill_tracked(pipeline):
    pipeline.execution.broker.fault = Fault.PARTIAL_FILL
    state = submit_approved(pipeline, _approved_order(pipeline, "chaos-part-0001", qty=10))
    assert state is OrderState.PARTIALLY_FILLED
    status = pipeline.execution.broker.get_order_status("chaos-part-0001")
    assert status.filled_qty == pytest.approx(5.0)
    assert status.remaining_qty == pytest.approx(5.0)


def test_duplicate_submission_structurally_blocked(pipeline):
    order = _approved_order(pipeline, "chaos-dup-00001")
    submit_approved(pipeline, order)
    with pytest.raises(DuplicateClientOrderIdError):
        submit_approved(pipeline, order)
    # even hitting the broker directly with the same id fails (§46)
    from packages.schemas.core import BrokerOrderRequest, OrderType

    with pytest.raises(DuplicateClientOrderIdError):
        pipeline.execution.broker.submit_order(BrokerOrderRequest(
            client_order_id="chaos-dup-00001", symbol="AAPL", side=Action.BUY, qty=1.0,
            order_type=OrderType.MARKET))


def test_reconciliation_mismatch_halts_entries(pipeline):
    # create a phantom ledger position the broker doesn't know about
    pipeline.ledger.record_fill("MSFT", side_qty=1.0, price=10.0, fees=0.0, at=SESSION_TIME)
    recon = ReconciliationEngine(broker=pipeline.execution.broker, ledger=pipeline.ledger,
                                 risk_controller=pipeline.risk_controller)
    report = recon.reconcile()
    assert not report.consistent
    assert pipeline.risk_controller.state is RiskState.HALT_NEW_ENTRIES


def test_disconnect_during_reconciliation(pipeline):
    pipeline.execution.broker.fault = Fault.DISCONNECT
    recon = ReconciliationEngine(broker=pipeline.execution.broker, ledger=pipeline.ledger,
                                 risk_controller=pipeline.risk_controller)
    report = recon.reconcile()
    assert not report.consistent
    assert pipeline.risk_controller.state is RiskState.FULL_BROKER_DISCONNECT
