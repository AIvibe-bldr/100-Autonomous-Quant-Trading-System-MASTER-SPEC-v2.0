"""Broker Adapter Interface (MASTER SPEC §100).

Only the Execution Engine holds a reference to a BrokerAdapter; nothing in the
decision path imports this package (enforced by tests/unit/test_invariants.py,
INV-6).  V1 target: IBKR Paper → Live; any broker plugs in by implementing
this interface.
"""
from __future__ import annotations

import abc
from datetime import datetime

from packages.schemas.core import (
    BrokerAck,
    BrokerCashBalance,
    BrokerFill,
    BrokerOrderRequest,
    BrokerOrderStatus,
    BrokerPosition,
)


class DuplicateClientOrderIdError(RuntimeError):
    """Structural duplicate-order prevention (§46, INV-7)."""


class BrokerTimeoutError(RuntimeError):
    """Submission outcome unknown — order must move to UNKNOWN and reconcile (§45-46)."""


class BrokerDisconnectedError(RuntimeError):
    """Connection lost — system records FULL_BROKER_DISCONNECT (§43)."""


class BrokerAdapter(abc.ABC):
    @abc.abstractmethod
    def submit_order(self, order: BrokerOrderRequest) -> BrokerAck: ...

    @abc.abstractmethod
    def cancel_order(self, client_order_id: str) -> BrokerAck: ...

    @abc.abstractmethod
    def get_order_status(self, client_order_id: str) -> BrokerOrderStatus: ...

    @abc.abstractmethod
    def get_positions(self) -> list[BrokerPosition]: ...

    @abc.abstractmethod
    def get_cash(self) -> BrokerCashBalance: ...

    @abc.abstractmethod
    def get_open_orders(self) -> list[BrokerOrderStatus]: ...

    @abc.abstractmethod
    def get_fills(self, since: datetime) -> list[BrokerFill]: ...
