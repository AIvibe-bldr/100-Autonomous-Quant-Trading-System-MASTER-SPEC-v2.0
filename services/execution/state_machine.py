"""Order State Machine (MASTER SPEC §45).

Transitions are whitelisted; anything else raises.  Every transition is
appended to an event log (Event Sourcing, §49).  UNKNOWN is a critical state:
reaching it must trigger broker reconciliation (§45-46).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from packages.common.clock import Clock
from packages.schemas.core import OrderState

S = OrderState

ALLOWED_TRANSITIONS: dict[OrderState, set[OrderState]] = {
    S.CREATED: {S.RISK_APPROVED, S.REJECTED},
    S.RISK_APPROVED: {S.SUBMITTED, S.EXPIRED, S.REJECTED},
    S.SUBMITTED: {S.ACKNOWLEDGED, S.REJECTED, S.UNKNOWN},
    S.ACKNOWLEDGED: {S.PARTIALLY_FILLED, S.FILLED, S.CANCEL_REQUESTED, S.EXPIRED,
                     S.REJECTED, S.UNKNOWN},
    S.PARTIALLY_FILLED: {S.FILLED, S.CANCEL_REQUESTED, S.UNKNOWN},
    S.CANCEL_REQUESTED: {S.CANCELLED, S.FILLED, S.PARTIALLY_FILLED, S.UNKNOWN},
    S.UNKNOWN: {S.ACKNOWLEDGED, S.PARTIALLY_FILLED, S.FILLED, S.CANCELLED, S.REJECTED,
                S.EXPIRED},  # resolved via reconciliation only
    S.FILLED: set(),
    S.CANCELLED: set(),
    S.REJECTED: set(),
    S.EXPIRED: set(),
}

TERMINAL_STATES = {S.FILLED, S.CANCELLED, S.REJECTED, S.EXPIRED}


class IllegalTransitionError(RuntimeError):
    pass


@dataclass(frozen=True)
class StateTransition:
    client_order_id: str
    from_state: OrderState
    to_state: OrderState
    at: datetime
    reason: str
    broker_payload: Optional[dict[str, Any]] = None


@dataclass
class OrderRecord:
    client_order_id: str
    state: OrderState = S.CREATED
    version: int = 1
    transitions: list[StateTransition] = field(default_factory=list)


class OrderStateMachine:
    def __init__(self, clock: Clock) -> None:
        self._clock = clock
        self._orders: dict[str, OrderRecord] = {}
        self.event_log: list[StateTransition] = []

    def create(self, client_order_id: str) -> OrderRecord:
        if client_order_id in self._orders:
            raise IllegalTransitionError(f"order {client_order_id} already exists")
        rec = OrderRecord(client_order_id=client_order_id)
        self._orders[client_order_id] = rec
        return rec

    def get(self, client_order_id: str) -> OrderRecord:
        return self._orders[client_order_id]

    def transition(self, client_order_id: str, to_state: OrderState, reason: str = "",
                   broker_payload: Optional[dict[str, Any]] = None) -> OrderRecord:
        rec = self._orders[client_order_id]
        if to_state not in ALLOWED_TRANSITIONS[rec.state]:
            raise IllegalTransitionError(
                f"{client_order_id}: {rec.state.value} → {to_state.value} not allowed")
        t = StateTransition(client_order_id=client_order_id, from_state=rec.state,
                            to_state=to_state, at=self._clock.now(), reason=reason,
                            broker_payload=broker_payload)
        rec.transitions.append(t)
        self.event_log.append(t)
        rec.state = to_state
        rec.version += 1
        return rec

    def orders_in_state(self, *states: OrderState) -> list[OrderRecord]:
        return [r for r in self._orders.values() if r.state in states]

    @property
    def has_unknown(self) -> bool:
        return bool(self.orders_in_state(S.UNKNOWN))
