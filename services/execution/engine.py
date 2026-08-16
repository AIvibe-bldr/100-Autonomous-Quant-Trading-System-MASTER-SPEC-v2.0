"""Execution Engine (MASTER SPEC §44-47) — deterministic, no LLM.

Accepts only RiskApprovedOrder whose HMAC signature verifies against the
MasterRiskController (§7).  Owns the only reference to the BrokerAdapter, so
broker credentials never reach any AI component (§74).

Idempotency (§46): each client_order_id is registered once; after a timeout
the engine asks the broker for status instead of blindly resubmitting.
UNKNOWN state (§45) triggers reconciliation and halts new entries.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Callable, Optional

from packages.broker_adapters.base import (
    BrokerAdapter,
    BrokerDisconnectedError,
    BrokerTimeoutError,
    DuplicateClientOrderIdError,
)
from packages.common.clock import Clock
from packages.common.environment import Environment
from packages.schemas.audit import ApprovedOrderSnapshot
from packages.schemas.core import (
    BrokerFill,
    BrokerOrderRequest,
    OrderState,
    RiskApprovedOrder,
)
from services.execution.state_machine import OrderStateMachine
from services.risk.master_controller import MasterRiskController, RiskState


class UnauthorizedOrderError(RuntimeError):
    """Order lacked a valid risk approval — the pipeline was bypassed (§7)."""


class OrderTamperError(RuntimeError):
    """Order fields no longer match the approved snapshot hash (INV-17).
    The approval is void; the order must go back through Audit + Risk (A4)."""


def make_client_order_id(environment: Environment, proposal_id: str, seq: int) -> str:
    return f"{environment.value.lower()}-{proposal_id[:8]}-{seq}-{uuid.uuid4().hex[:6]}"


@dataclass
class ExecutionEngine:
    broker: BrokerAdapter
    risk_controller: MasterRiskController
    state_machine: OrderStateMachine
    clock: Clock
    environment: Environment
    on_fill: Optional[Callable[[BrokerFill], None]] = None
    _submitted: dict[str, RiskApprovedOrder] = field(default_factory=dict)
    _snapshots: dict[str, ApprovedOrderSnapshot] = field(default_factory=dict)

    def submit(self, order: RiskApprovedOrder,
               snapshot: Optional[ApprovedOrderSnapshot] = None) -> OrderState:
        intent = order.intent

        # §7: only risk-approved orders reach the broker — verify the token
        if not self.risk_controller.verify_signature(order.approval):
            raise UnauthorizedOrderError(
                f"{intent.client_order_id}: risk approval signature invalid")
        if intent.environment is not self.environment:
            raise UnauthorizedOrderError(
                f"environment mismatch: order={intent.environment} engine={self.environment}")
        if intent.client_order_id in self._submitted:
            raise DuplicateClientOrderIdError(intent.client_order_id)

        # A4 / INV-17: the intent must re-hash to the approved snapshot.
        # If no snapshot is supplied, freeze one now from the approved order.
        snapshot = snapshot or ApprovedOrderSnapshot.from_approved(order)
        if not snapshot.matches_intent(intent):
            raise OrderTamperError(
                f"{intent.client_order_id}: order fields do not match approved "
                f"snapshot hash — approval void, re-audit + re-risk required (A4)")
        self._snapshots[intent.client_order_id] = snapshot

        sm = self.state_machine
        sm.create(intent.client_order_id)
        sm.transition(intent.client_order_id, OrderState.RISK_APPROVED,
                      reason=f"approval {order.approval.approval_id} "
                             f"snapshot {snapshot.hash[:12]}")
        self._submitted[intent.client_order_id] = order

        # INV-18 / A4-2: the broker request is built from the SNAPSHOT, never
        # from mutable local state — the engine cannot alter qty/price/side.
        req = BrokerOrderRequest(client_order_id=snapshot.client_order_id,
                                 symbol=snapshot.symbol,
                                 side=snapshot.side, qty=snapshot.qty,
                                 order_type=snapshot.order_type,
                                 limit_price=snapshot.limit_price,
                                 stop_price=snapshot.stop_price)
        sm.transition(intent.client_order_id, OrderState.SUBMITTED)
        try:
            ack = self.broker.submit_order(req)
        except BrokerTimeoutError:
            # §45-46: outcome unknown — never resubmit blindly; reconcile
            sm.transition(intent.client_order_id, OrderState.UNKNOWN,
                          reason="broker timeout — reconciliation required")
            self.risk_controller.set_state(RiskState.HALT_NEW_ENTRIES,
                                           reason="order in UNKNOWN state")
            return OrderState.UNKNOWN
        except BrokerDisconnectedError:
            sm.transition(intent.client_order_id, OrderState.UNKNOWN,
                          reason="broker disconnected mid-submit")
            self.risk_controller.set_state(RiskState.FULL_BROKER_DISCONNECT,
                                           reason="broker disconnected")
            return OrderState.UNKNOWN

        if not ack.accepted:
            sm.transition(intent.client_order_id, OrderState.REJECTED, reason=ack.reason)
            return OrderState.REJECTED

        sm.transition(intent.client_order_id, OrderState.ACKNOWLEDGED,
                      broker_payload={"broker_order_id": ack.broker_order_id})
        return self.poll_order(intent.client_order_id)

    def poll_order(self, client_order_id: str) -> OrderState:
        """Sync our state machine with broker-reported status and route fills."""
        status = self.broker.get_order_status(client_order_id)
        sm = self.state_machine
        current = sm.get(client_order_id).state
        if status.state is not current and status.state in (
                OrderState.PARTIALLY_FILLED, OrderState.FILLED, OrderState.CANCELLED,
                OrderState.REJECTED, OrderState.EXPIRED):
            sm.transition(client_order_id, status.state, reason="broker status poll")
            if status.state in (OrderState.PARTIALLY_FILLED, OrderState.FILLED) and self.on_fill:
                for f in self.broker.get_fills(since=self.clock.now().replace(year=2000)):
                    if f.client_order_id == client_order_id:
                        self.on_fill(f)
        return sm.get(client_order_id).state

    def resolve_unknown(self, client_order_id: str) -> OrderState:
        """Reconciliation path for UNKNOWN orders (§45): ask the broker, then
        settle the state machine to the truth."""
        try:
            status = self.broker.get_order_status(client_order_id)
        except (BrokerDisconnectedError, KeyError):
            return OrderState.UNKNOWN
        sm = self.state_machine
        if sm.get(client_order_id).state is OrderState.UNKNOWN:
            sm.transition(client_order_id, status.state,
                          reason="resolved via broker reconciliation")
            if status.state in (OrderState.PARTIALLY_FILLED, OrderState.FILLED) and self.on_fill:
                for f in self.broker.get_fills(since=self.clock.now().replace(year=2000)):
                    if f.client_order_id == client_order_id:
                        self.on_fill(f)
        return sm.get(client_order_id).state

    def check_stale_orders(self) -> list[str]:
        """Stale Order Control (§47): re-evaluate resting entry orders."""
        stale: list[str] = []
        now = self.clock.now()
        for cid, order in self._submitted.items():
            rec = self.state_machine.get(cid)
            if rec.state is OrderState.ACKNOWLEDGED:
                age = (now - order.intent.created_at).total_seconds()
                if age > self.risk_controller.config.stale_order_after_sec:
                    self.broker.cancel_order(cid)
                    self.state_machine.transition(cid, OrderState.CANCEL_REQUESTED,
                                                  reason=f"stale after {age:.0f}s (§47)")
                    self.poll_order(cid)
                    stale.append(cid)
        return stale
