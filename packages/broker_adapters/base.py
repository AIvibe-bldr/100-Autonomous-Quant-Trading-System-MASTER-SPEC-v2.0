"""Broker Adapter Interface (MASTER SPEC §100).

Only the Execution Engine holds a reference to a BrokerAdapter; nothing in the
decision path imports this package (enforced by tests/unit/test_invariants.py,
INV-6).  V1 target: IBKR Paper → Live; any broker plugs in by implementing
this interface.
"""
from __future__ import annotations

import abc
from datetime import datetime

from packages.common.environment import Environment

from packages.schemas.core import (
    BrokerAccountStatus,
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
    @property
    @abc.abstractmethod
    def environment(self) -> Environment:
        """The environment this adapter's endpoint actually is (§73). An adapter reports
        what it is connected to — never what the caller wishes — so the Execution Engine
        can refuse a LIVE endpoint inside a PAPER graph and vice versa."""

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
    def get_fills(self, since: datetime) -> list[BrokerFill]:
        """Fills at or after `since`. Callers must pass a bounded `since`."""

    @abc.abstractmethod
    def get_order_fills(self, client_order_id: str) -> list[BrokerFill]:
        """Every fill of ONE order. The execution path uses this (never a whole-history
        scan): a real broker answers it per order, and it is what lets a fill of a
        resting order be discovered after the fact."""

    @abc.abstractmethod
    def get_account_status(self) -> BrokerAccountStatus:
        """Account-level restrictions (e.g. trading blocked). Raises
        BrokerDisconnectedError when unreachable; reconciliation treats a blocked account
        as a mismatch and halts new entries."""

    def poll_resting_orders(self) -> list[BrokerFill]:
        """Simulation hook. A real broker evaluates resting stop/limit orders itself, so
        the default does nothing; fills of resting orders are then discovered through
        get_order_status/get_order_fills. Only a simulator that must be driven to
        re-evaluate its resting orders overrides this (and returns the fills it produced)."""
        return []
