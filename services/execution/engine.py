"""Execution Engine (MASTER SPEC §44-47) — deterministic, no LLM.

Accepts only RiskApprovedOrder whose HMAC signature verifies against the
MasterRiskController (§7).  Owns the only reference to the BrokerAdapter, so
broker credentials never reach any AI component (§74).

Idempotency (§46): each client_order_id is registered once; after a timeout
the engine asks the broker for status instead of blindly resubmitting.
UNKNOWN state (§45) triggers reconciliation and halts new entries.
"""
from __future__ import annotations

import math
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
from packages.common.durable_store import DurableOrderStore
from packages.common.environment import Environment
from packages.common.kill_switch import KillSwitch
from packages.schemas.audit import ApprovedOrderSnapshot
from packages.schemas.core import (
    Action,
    BrokerFill,
    BrokerOrderRequest,
    OrderState,
    RiskApprovedOrder,
)
from services.execution.state_machine import OrderStateMachine
from packages.common.alerts import AlertSeverity, notify
from services.risk.circuit_breaker import (
    RejectionCircuitBreaker,
    SlippageCircuitBreaker,
    adverse_slippage_bps,
)
from services.risk.master_controller import MasterRiskController, RiskState


class UnauthorizedOrderError(RuntimeError):
    """Order lacked a valid risk approval — the pipeline was bypassed (§7)."""


class KillSwitchEngagedError(RuntimeError):
    """An operator kill switch is engaged: new entries are not sent (F5)."""


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
    store: Optional[DurableOrderStore] = None
    _submitted: dict[str, RiskApprovedOrder] = field(default_factory=dict)
    _snapshots: dict[str, ApprovedOrderSnapshot] = field(default_factory=dict)
    # F6: rate trigger for broker rejections; built from RiskConfig unless injected
    circuit_breaker: Optional[RejectionCircuitBreaker] = None
    # F6: abnormal-slippage trigger (needs the decision-time reference price)
    slippage_breaker: Optional[SlippageCircuitBreaker] = None
    _reference_prices: dict[str, float] = field(default_factory=dict)
    _slippage_checked: set[str] = field(default_factory=set)
    # F5: operator kill switch (flag file). None = not configured (tests).
    kill_switch: Optional[KillSwitch] = None

    def __post_init__(self) -> None:
        if self.circuit_breaker is None:
            cfg = self.risk_controller.config
            self.circuit_breaker = RejectionCircuitBreaker(
                cfg.circuit_breaker_max_rejections, cfg.circuit_breaker_window_sec, self.clock)
        if self.slippage_breaker is None:
            cfg = self.risk_controller.config
            self.slippage_breaker = SlippageCircuitBreaker(
                cfg.circuit_breaker_max_slippage_fills, cfg.circuit_breaker_window_sec, self.clock)

    def entries_blocked_by_kill_switch(self) -> Optional[str]:
        """The reason when the operator kill switch is engaged, else None. When
        engaged it also moves the controller to HALT_NEW_ENTRIES, which (unlike
        the flag) stays until a human restarts the runner — no auto-resume."""
        if self.kill_switch is None:
            return None
        status = self.kill_switch.status()
        if not status.engaged:
            return None
        reason = f"kill switch engaged by {status.operator}: {status.reason}"
        if self.risk_controller.state is not RiskState.HALT_NEW_ENTRIES:
            self.risk_controller.set_state(RiskState.HALT_NEW_ENTRIES, reason=reason)
        return reason

    def submit(self, order: RiskApprovedOrder,
               snapshot: Optional[ApprovedOrderSnapshot] = None,
               reference_price: Optional[float] = None) -> OrderState:
        """`reference_price` is the price the decision assumed (e.g. the ask when the
        entry was planned). It is an OBSERVATION used only to measure slippage after
        the fill — not an order field, so it is neither hashed nor signed."""
        intent = order.intent
        if reference_price is not None and math.isfinite(reference_price) and reference_price > 0:
            self._reference_prices[intent.client_order_id] = reference_price

        # §7: only risk-approved orders reach the broker — verify the token
        if not self.risk_controller.verify_signature(order.approval):
            raise UnauthorizedOrderError(
                f"{intent.client_order_id}: risk approval signature invalid")
        if intent.environment is not self.environment:
            raise UnauthorizedOrderError(
                f"environment mismatch: order={intent.environment} engine={self.environment}")
        # F5: last line of defence — an ENTRY never reaches the broker while the
        # kill switch is engaged. Protective stops and exits (SELL) are unaffected.
        if intent.side is Action.BUY:
            blocked = self.entries_blocked_by_kill_switch()
            if blocked:
                raise KillSwitchEngagedError(f"{intent.client_order_id}: {blocked}")
        # F3: the in-memory dict alone doesn't survive a restart — when a
        # durable store is configured, it is the source of truth for
        # idempotency across a crash, not just within this process. Checked
        # BEFORE any state-machine mutation below: store.record_submission()
        # would also reject a duplicate (UNIQUE constraint), but only after
        # sm.create()/transition() already ran, leaving this engine's local
        # state machine holding a phantom order that was never really
        # submitted. Fail fast, before touching local state.
        if intent.client_order_id in self._submitted or (
                self.store is not None and self.store.is_known(intent.client_order_id)):
            raise DuplicateClientOrderIdError(intent.client_order_id)

        # A4 / INV-17: the intent must re-hash to the approved snapshot.
        #
        # The snapshot is REQUIRED. Minting one here from the incoming order
        # made the check self-fulfilling: a tampered intent produced a snapshot
        # of the tampered intent, which matched itself. A witness the accused
        # writes is not a witness.
        if snapshot is None:
            raise OrderTamperError(
                f"{intent.client_order_id}: no approved order snapshot supplied — "
                f"execution cannot mint its own (A4-2, INV-18)")
        if snapshot.risk_approval_id != order.approval.approval_id:
            raise OrderTamperError(
                f"{intent.client_order_id}: snapshot was frozen against approval "
                f"{snapshot.risk_approval_id}, not {order.approval.approval_id} — "
                f"approval/snapshot mismatch (A4)")
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
        # F3/F9: persist BEFORE calling the broker, so a crash between "we
        # decided to submit" and "the broker acknowledged" still leaves a
        # durable record — the exact gap a blind retry could otherwise turn
        # into a duplicate live order.
        if self.store is not None:
            self.store.record_submission(
                client_order_id=intent.client_order_id, decision_id=snapshot.decision_id,
                risk_approval_id=order.approval.approval_id, snapshot=snapshot,
                state=OrderState.SUBMITTED, at=self.clock.now())
        try:
            ack = self.broker.submit_order(req)
        except BrokerTimeoutError:
            # §45-46: outcome unknown — never resubmit blindly; reconcile
            sm.transition(intent.client_order_id, OrderState.UNKNOWN,
                          reason="broker timeout — reconciliation required")
            self._sync_store(intent.client_order_id, OrderState.UNKNOWN)
            self.risk_controller.set_state(RiskState.HALT_NEW_ENTRIES,
                                           reason="order in UNKNOWN state")
            return OrderState.UNKNOWN
        except BrokerDisconnectedError:
            sm.transition(intent.client_order_id, OrderState.UNKNOWN,
                          reason="broker disconnected mid-submit")
            self._sync_store(intent.client_order_id, OrderState.UNKNOWN)
            self.risk_controller.set_state(RiskState.FULL_BROKER_DISCONNECT,
                                           reason="broker disconnected")
            return OrderState.UNKNOWN

        if not ack.accepted:
            sm.transition(intent.client_order_id, OrderState.REJECTED, reason=ack.reason)
            self._sync_store(intent.client_order_id, OrderState.REJECTED)
            if self.circuit_breaker.record_rejection():
                self.risk_controller.set_state(
                    RiskState.HALT_NEW_ENTRIES,
                    reason=f"circuit breaker: repeated broker rejections (last: {ack.reason})")
            return OrderState.REJECTED

        sm.transition(intent.client_order_id, OrderState.ACKNOWLEDGED,
                      broker_payload={"broker_order_id": ack.broker_order_id})
        self._sync_store(intent.client_order_id, OrderState.ACKNOWLEDGED)
        return self.poll_order(intent.client_order_id)

    def _sync_store(self, client_order_id: str, state: OrderState) -> None:
        """Mirror a state-machine transition into the durable store, when
        one is configured. A KeyError here means the order was never
        durably recorded (store configured only after submission, or a
        recovered/legacy order) — that is not this helper's problem to
        solve, so it is left to propagate rather than silently swallowed."""
        if self.store is not None:
            self.store.update_state(client_order_id, state, self.clock.now())

    def poll_order(self, client_order_id: str) -> OrderState:
        """Sync our state machine with broker-reported status and route fills."""
        status = self.broker.get_order_status(client_order_id)
        sm = self.state_machine
        current = sm.get(client_order_id).state
        if status.state is not current and status.state in (
                OrderState.PARTIALLY_FILLED, OrderState.FILLED, OrderState.CANCELLED,
                OrderState.REJECTED, OrderState.EXPIRED):
            sm.transition(client_order_id, status.state, reason="broker status poll")
            self._sync_store(client_order_id, status.state)
            if status.state in (OrderState.PARTIALLY_FILLED, OrderState.FILLED):
                self._route_fills(client_order_id)
        return sm.get(client_order_id).state

    def _route_fills(self, client_order_id: str) -> None:
        for f in self.broker.get_fills(since=self.clock.now().replace(year=2000)):
            if f.client_order_id == client_order_id:
                self._check_slippage(f)
                if self.on_fill:
                    self.on_fill(f)

    def _check_slippage(self, fill: BrokerFill) -> None:
        """F6: a fill far worse than the decision-time reference is counted once;
        enough of them inside the window halt new entries (never un-halts)."""
        ref = self._reference_prices.get(fill.client_order_id)
        if ref is None or fill.broker_fill_id in self._slippage_checked:
            return
        self._slippage_checked.add(fill.broker_fill_id)
        cfg = self.risk_controller.config
        bps = adverse_slippage_bps(fill.side, ref, fill.price)
        if bps <= cfg.max_slippage_bps:
            return
        notify(self.risk_controller.alert_sink, AlertSeverity.WARNING,
               f"abnormal slippage on {fill.symbol}: {bps:.0f} bps worse than reference "
               f"{ref:.2f} (fill {fill.price:.2f}, limit {cfg.max_slippage_bps:.0f} bps)")
        if self.slippage_breaker.record_abnormal_fill():
            self.risk_controller.set_state(
                RiskState.HALT_NEW_ENTRIES,
                reason=f"circuit breaker: repeated abnormal slippage (last {bps:.0f} bps on {fill.symbol})")

    def sync_open_orders(self) -> list[BrokerFill]:
        """Re-check resting orders (e.g. protective stops) and return any new
        fills.  Without this a stop placed yesterday could never trigger.

        A broker outage here is not fatal to the session: it is recorded as a
        disconnect so the risk controller blocks new entries (§43, §48)."""
        broker_poll = getattr(self.broker, "poll_resting_orders", None)
        try:
            new_fills: list[BrokerFill] = list(broker_poll()) if broker_poll else []
            for cid, rec in list(self.state_machine._orders.items()):   # noqa: SLF001
                if rec.state in (OrderState.ACKNOWLEDGED, OrderState.PARTIALLY_FILLED):
                    self.poll_order(cid)
        except BrokerDisconnectedError:
            self.risk_controller.set_state(RiskState.FULL_BROKER_DISCONNECT,
                                           reason="broker unreachable while syncing orders")
            return []
        return new_fills

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
            self._sync_store(client_order_id, status.state)
            if status.state in (OrderState.PARTIALLY_FILLED, OrderState.FILLED):
                self._route_fills(client_order_id)
        return sm.get(client_order_id).state

    def recover_from_store(self) -> int:
        """Rehydrate in-memory order state from the durable store after a
        process restart (F3/F9). Recovered orders are NOT re-submitted or
        re-polled here — this only makes them visible to
        `resolve_unknown`/`poll_order`/the duplicate-submission check again.
        The caller is responsible for reconciling against the broker before
        resuming normal trading (F4 — startup reconciliation is a separate,
        still-open finding this does not by itself close).

        Returns the number of orders recovered."""
        if self.store is None:
            return 0
        recovered = 0
        for rec in self.store.all():
            try:
                self.state_machine.get(rec.client_order_id)
                continue  # already known in-memory (e.g. re-recovery, no-op)
            except KeyError:
                pass
            self.state_machine.restore(rec.client_order_id, rec.state)
            self._snapshots[rec.client_order_id] = rec.snapshot
            recovered += 1
        return recovered

    def check_stale_orders(self) -> list[str]:
        """Stale Order Control (§47): re-evaluate resting **entry** orders.

        Protective stops are excluded deliberately. A resting stop sits in
        ACKNOWLEDGED for the entire life of the position — that is what it is
        *for* — so age-based cancellation would reliably strip every open
        position of its stop and violate INV-15 ("the planned stop must exist
        at the broker"). Staleness is a property of an entry whose signal has
        decayed, not of a stop whose job has not happened yet.
        """
        stale: list[str] = []
        now = self.clock.now()
        for cid, order in self._submitted.items():
            rec = self.state_machine.get(cid)
            if rec.state is OrderState.ACKNOWLEDGED and not order.intent.is_protective_exit:
                age = (now - order.intent.created_at).total_seconds()
                if age > self.risk_controller.config.stale_order_after_sec:
                    self.broker.cancel_order(cid)
                    self.state_machine.transition(cid, OrderState.CANCEL_REQUESTED,
                                                  reason=f"stale after {age:.0f}s (§47)")
                    self.poll_order(cid)
                    stale.append(cid)
        return stale
