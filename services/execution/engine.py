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
from packages.common.environment import Environment, require_same_environment
from packages.common.live_gate import LiveGate
from packages.common.kill_switch import KillSwitch
from packages.schemas.audit import ApprovedOrderSnapshot
from packages.schemas.core import (
    Action,
    BrokerFill,
    BrokerOrderRequest,
    OrderState,
    RiskApprovedOrder,
    TimeInForce,
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


class LiveGateClosedError(RuntimeError):
    """LIVE entry attempted without a valid, unexpired human approval (MASTER SPEC §105-106)."""


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
    # every fill already handed on (slippage check, on_fill, or returned by a sync), so a
    # fill is reported once no matter how many times its order is polled
    _seen_fill_ids: set[str] = field(default_factory=set)
    # F5: operator kill switch (flag file). None = not configured (tests).
    kill_switch: Optional[KillSwitch] = None
    # §105-106: human approval required for LIVE entries; mandatory when environment is LIVE
    live_gate: Optional[LiveGate] = None

    def __post_init__(self) -> None:
        # §73: the broker's own endpoint must be the environment this engine runs in
        require_same_environment(self.environment, self.broker.environment)
        if self.environment.is_live and self.live_gate is None:
            raise LiveGateClosedError("a LIVE execution engine cannot be built without a LiveGate")
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

    def entries_blocked_by_live_gate(self) -> Optional[str]:
        """In LIVE: the reason entries are not approved right now, else None. Moves the
        controller to HALT_NEW_ENTRIES so the closed gate is visible as a risk state.
        Never blocks exits. Outside LIVE the gate does not apply."""
        if not self.environment.is_live:
            return None
        status = self.live_gate.status(self.clock.now())
        if not status.is_open:
            reason = f"LIVE gate closed: {status.detail}"
        elif self.risk_controller.config.exposure_cap_stage > status.max_exposure_stage:
            reason = (f"LIVE gate: configured exposure stage "
                      f"{self.risk_controller.config.exposure_cap_stage} exceeds the approved "
                      f"{status.max_exposure_stage}")
        else:
            return None
        if self.risk_controller.state is not RiskState.HALT_NEW_ENTRIES:
            self.risk_controller.set_state(RiskState.HALT_NEW_ENTRIES, reason=reason)
        return reason

    def entries_blocked(self) -> Optional[str]:
        """First operator-level reason new entries must not go out (kill switch, LIVE gate)."""
        return self.entries_blocked_by_kill_switch() or self.entries_blocked_by_live_gate()

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
            gate_blocked = self.entries_blocked_by_live_gate()
            if gate_blocked:
                raise LiveGateClosedError(f"{intent.client_order_id}: {gate_blocked}")
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
                                 stop_price=snapshot.stop_price,
                                 time_in_force=TimeInForce(snapshot.time_in_force))
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
        return self._poll(client_order_id)[0]

    def _poll(self, client_order_id: str) -> tuple[OrderState, list[BrokerFill]]:
        status = self.broker.get_order_status(client_order_id)
        sm = self.state_machine
        current = sm.get(client_order_id).state
        if status.state is not current and status.state in (
                OrderState.PARTIALLY_FILLED, OrderState.FILLED, OrderState.CANCELLED,
                OrderState.REJECTED, OrderState.EXPIRED):
            sm.transition(client_order_id, status.state, reason="broker status poll")
            self._sync_store(client_order_id, status.state)
        fills: list[BrokerFill] = []
        if status.state in (OrderState.PARTIALLY_FILLED, OrderState.FILLED):
            fills = self._route_fills(client_order_id)     # also later partials of a resting order
        return sm.get(client_order_id).state, fills

    def _route_fills(self, client_order_id: str) -> list[BrokerFill]:
        """The order's fills not handed on before (each fill exactly once)."""
        new: list[BrokerFill] = []
        for f in self.broker.get_order_fills(client_order_id):
            if f.broker_fill_id in self._seen_fill_ids:
                continue
            self._seen_fill_ids.add(f.broker_fill_id)
            self._check_slippage(f)
            if self.on_fill:
                self.on_fill(f)
            new.append(f)
        return new

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
        try:
            resting = self.broker.poll_resting_orders()      # simulator hook; real brokers: []
            new_fills: list[BrokerFill] = []
            for cid, rec in list(self.state_machine._orders.items()):   # noqa: SLF001
                if rec.state in (OrderState.ACKNOWLEDGED, OrderState.PARTIALLY_FILLED):
                    new_fills.extend(self._poll(cid)[1])
            for f in resting:                                # fills of orders outside the loop above
                if f.broker_fill_id not in self._seen_fill_ids:
                    self._seen_fill_ids.add(f.broker_fill_id)
                    new_fills.append(f)
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

    def export_state(self) -> dict:
        """What must survive a restart so a fill is never booked twice and slippage is still measured against
        the right plan. (Rate-limit windows of the circuit breakers restart empty, by design.)"""
        return {"seen_fill_ids": sorted(self._seen_fill_ids), "slippage_checked": sorted(self._slippage_checked),
                "reference_prices": dict(self._reference_prices)}

    def restore_state(self, state: dict) -> None:
        self._seen_fill_ids = set(state["seen_fill_ids"])
        self._slippage_checked = set(state["slippage_checked"])
        self._reference_prices = dict(state["reference_prices"])

    def recover_from_store(self, truth: Optional[Callable[[str], Optional[OrderState]]] = None) -> int:
        """Rehydrate in-memory order state from the durable store after a
        process restart (F3/F9). Recovered orders are NOT re-submitted or
        re-polled here — this only makes them visible to
        `resolve_unknown`/`poll_order`/the duplicate-submission check again.
        The caller is responsible for reconciling against the broker before
        resuming normal trading (F4 — startup reconciliation is a separate,
        still-open finding this does not by itself close).

        `truth(client_order_id)` is for a SIMULATED broker restored from a checkpoint: it returns the state the
        restored broker holds for the order, or None if the broker has never heard of it (the process died
        between recording the submission and the next checkpoint, so that part of the simulated world never
        happened). The order is then restored as REJECTED and the store is updated to say so. A real broker is
        reconciled instead of rewound, so it does not use this.

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
            state = rec.state
            if truth is not None:
                known = truth(rec.client_order_id)
                state = known if known is not None else OrderState.REJECTED
                if state is not rec.state:
                    self.store.update_state(rec.client_order_id, state, self.clock.now())
            self.state_machine.restore(rec.client_order_id, state)
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
