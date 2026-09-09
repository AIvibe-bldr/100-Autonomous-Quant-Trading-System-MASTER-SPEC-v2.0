"""Capital Reservation Ledger (docs/capital_cell_architecture.md §12-13,
§41 priority 4).

Multiple cells can generate order intents in the same session; without this,
each cell would see the same Master cash as "available" and could
collectively overcommit it. `available_cash` here is the single source of
truth every cell must check before an order intent is allowed to size
itself, and a reservation removes that capital from `available_cash` for
every other cell until it is released.

Concurrency (§13): this codebase is single-threaded and processes candidates
sequentially (see `services.pipeline.TradingPipeline.run_session`) — there is
no thread pool or async scheduler anywhere in it. `reserve()` is therefore
atomic the same way `packages.common.ledger.Ledger._append` is atomic: check
fully, then make exactly one dict write, so a Capital Cell process built on
top of this later (e.g. a real async scheduler) inherits a call that either
fully succeeds or fully fails, never partial state. Idempotency is enforced
via unique `reservation_id`s (§13) rather than via a lock.
"""
from __future__ import annotations

from dataclasses import dataclass, field


class InsufficientCapitalError(RuntimeError):
    """A cell tried to reserve more than currently available (§12)."""


class DuplicateReservationError(RuntimeError):
    """Same reservation_id reserved twice. The caller must mint a fresh id
    per attempt (§13: unique order intent ids) rather than rely on this
    ledger to silently deduplicate or double-book."""


class UnknownReservationError(RuntimeError):
    pass


@dataclass(frozen=True)
class CapitalReservation:
    reservation_id: str
    cell_id: str
    amount: float
    note: str = ""


@dataclass
class CapitalReservationLedger:
    """`available_cash` / `reserved_cash` / `cell_reserved_capital` (§12)."""

    total_cash: float
    _reservations: dict[str, CapitalReservation] = field(default_factory=dict)

    @property
    def reserved_cash(self) -> float:
        return sum(r.amount for r in self._reservations.values())

    @property
    def available_cash(self) -> float:
        return self.total_cash - self.reserved_cash

    def cell_reserved_capital(self, cell_id: str) -> float:
        return sum(r.amount for r in self._reservations.values() if r.cell_id == cell_id)

    def pending_orders(self, cell_id: str) -> int:
        return sum(1 for r in self._reservations.values() if r.cell_id == cell_id)

    def sync_total_cash(self, total_cash: float) -> None:
        """Re-sync to the Master Ledger's actual settled cash after each
        fill/deposit/withdrawal. This ledger tracks *availability* on top of
        that truth, it is never itself the source of truth for cash."""
        self.total_cash = total_cash

    def reserve(self, reservation_id: str, cell_id: str, amount: float,
                note: str = "") -> CapitalReservation:
        if amount <= 0:
            raise ValueError("reservation amount must be positive")
        if reservation_id in self._reservations:
            raise DuplicateReservationError(reservation_id)
        if amount > self.available_cash + 1e-9:
            raise InsufficientCapitalError(
                f"cell {cell_id} requested {amount:.2f}, only "
                f"{self.available_cash:.2f} available (§12)")
        reservation = CapitalReservation(reservation_id, cell_id, amount, note)
        # Single dict write: no other state is touched between the checks
        # above and this line, so there is nothing to leave half-done (§13).
        self._reservations[reservation_id] = reservation
        return reservation

    def release(self, reservation_id: str) -> None:
        """A reservation's order filled (capital is now real position, not
        reserved cash) or was rejected/cancelled (capital returns to
        available) — either way it must stop being reserved."""
        if reservation_id not in self._reservations:
            raise UnknownReservationError(reservation_id)
        del self._reservations[reservation_id]
