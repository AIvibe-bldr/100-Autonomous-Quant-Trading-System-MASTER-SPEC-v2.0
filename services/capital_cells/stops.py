"""Same-Symbol Multi-Cell Stop Management (docs/capital_cell_architecture.md
§14-16, §41 priority 8).

Several cells can hold the same symbol for different reasons — Cell A on a
momentum thesis with a $170 stop, Cell B on a news thesis with a $160 stop —
while the Master Broker Position is one aggregate number. Stops are
therefore tracked per (cell_id, symbol), and firing one produces
`CellOrderIntent` SELL(s) that flow through the SAME pipeline as any other
cell order (§14):

    Stop Trigger -> Cell Exit Intent -> Master Netting -> Master Risk -> Execution

This module only produces those exit intents — applying them is the existing
`services.capital_cells.netting` / `fill_allocation` machinery, unchanged.

Scope (§15) decides how far a firing stop reaches:

- CELL: only the cell whose stop fired exits. Every other cell's virtual
  position in that symbol is left alone — §14 is explicit that a firing
  stop must never unilaterally liquidate another cell's position.
- SYMBOL: something is wrong with the symbol itself (the doc's example is a
  fraud disclosure) — every cell currently holding it gets an exit intent,
  whether or not that cell's own stop has been reached.
- PORTFOLIO: a portfolio-wide risk event — every cell's every held symbol
  exits.

Oversell prevention (§16): `SellReservationLedger` enforces
`open_sell_qty + reserved_sell_qty <= master_long_position_qty` per symbol.
Concretely, it stops a second overlapping trigger (e.g. a SYMBOL-scope event
arriving before a CELL-scope exit it overlaps with has settled) from also
claiming shares of a cell's position that a still-unsettled exit already
claimed — a cell's exit is always sized against what is not already
reserved, never against its raw current holding.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from packages.common.clock import ensure_utc
from packages.schemas.capital_cell import CellOrderIntent, CellStopPlan, StopScope
from packages.schemas.core import Action
from services.capital_cells.ledger import CellLedger


class DuplicateStopIdError(ValueError):
    pass


class UnknownStopError(KeyError):
    pass


class OversellError(RuntimeError):
    """A reservation would push open+reserved SELL past the Master's actual
    long position for a symbol (§16) — the invariant this module exists to
    enforce, structurally."""


class DuplicateSellReservationError(ValueError):
    pass


class UnknownSellReservationError(KeyError):
    pass


@dataclass(frozen=True)
class SellReservation:
    reservation_id: str
    cell_id: str
    symbol: str
    qty: float


@dataclass
class SellReservationLedger:
    """`open_sell_qty + reserved_sell_qty <= master_long_position_qty` (§16),
    tracked per symbol and, within a symbol, per cell — so a second trigger
    can tell exactly how much of *this cell's* position is already spoken
    for by an exit still in flight."""

    master_long_qty: dict[str, float] = field(default_factory=dict)
    _reservations: dict[str, SellReservation] = field(default_factory=dict)

    def sync_master_long_qty(self, symbol: str, qty: float) -> None:
        """Re-sync to the Master Ledger's actual position (the source of
        truth this ledger tracks availability on top of, same relationship
        `CapitalReservationLedger.sync_total_cash` has to cash)."""
        self.master_long_qty[symbol] = qty

    def reserved_qty(self, symbol: str, cell_id: Optional[str] = None) -> float:
        return sum(r.qty for r in self._reservations.values()
                  if r.symbol == symbol and (cell_id is None or r.cell_id == cell_id))

    def available_to_reserve(self, symbol: str) -> float:
        """How much more of this symbol can still be reserved for exit,
        given what all still-open reservations (across every cell) already
        claim against the Master's long position."""
        master_qty = self.master_long_qty.get(symbol, 0.0)
        return max(0.0, master_qty - self.reserved_qty(symbol))

    def check_can_reserve(self, symbol: str, qty: float) -> None:
        """Raise if reserving `qty` more of `symbol` would breach §16.
        Split out from `reserve_sell` so a caller planning several
        reservations at once can validate the whole batch BEFORE recording
        any of it (see `CellStopRegistry.trigger`)."""
        if qty > self.available_to_reserve(symbol) + 1e-9:
            raise OversellError(
                f"{symbol}: reserving {qty} would exceed master_long_position_qty="
                f"{self.master_long_qty.get(symbol, 0.0)} "
                f"(already reserved: {self.reserved_qty(symbol):.4f}) (§16)")

    def reserve_sell(self, reservation_id: str, cell_id: str, symbol: str,
                     qty: float) -> SellReservation:
        if qty <= 0:
            raise ValueError("reservation qty must be positive")
        if reservation_id in self._reservations:
            raise DuplicateSellReservationError(reservation_id)
        self.check_can_reserve(symbol, qty)
        reservation = SellReservation(reservation_id, cell_id, symbol, qty)
        self._reservations[reservation_id] = reservation
        return reservation

    def release(self, reservation_id: str) -> None:
        """The reserved exit filled (shares are now actually gone — the
        Master Ledger's own position update is what should shrink
        `master_long_qty` via `sync_master_long_qty`) or was cancelled."""
        if reservation_id not in self._reservations:
            raise UnknownSellReservationError(reservation_id)
        del self._reservations[reservation_id]


@dataclass
class CellStopRegistry:
    """Tracks each cell's stop plans and turns a trigger into the correctly-
    scoped set of Cell Exit Intents (§14-15), reserving against oversell
    (§16) as it does. Never mutates a `CellLedger` itself — same
    plan/apply split as `services.capital_cells.netting`."""

    sell_reservations: SellReservationLedger
    _stops: dict[str, CellStopPlan] = field(default_factory=dict)

    def register(self, plan: CellStopPlan) -> CellStopPlan:
        if plan.stop_id in self._stops:
            raise DuplicateStopIdError(plan.stop_id)
        self._stops[plan.stop_id] = plan
        return plan

    def get(self, stop_id: str) -> CellStopPlan:
        try:
            return self._stops[stop_id]
        except KeyError:
            raise UnknownStopError(stop_id) from None

    def cancel(self, stop_id: str) -> None:
        """A cell exited some other way (e.g. profit target) — its stop plan
        no longer guards anything and must stop being tracked."""
        if stop_id not in self._stops:
            raise UnknownStopError(stop_id)
        del self._stops[stop_id]

    def stops_for_cell(self, cell_id: str) -> list[CellStopPlan]:
        return [s for s in self._stops.values() if s.cell_id == cell_id]

    def trigger(self, stop_id: str, cells: dict[str, CellLedger],
               at: datetime) -> list[CellOrderIntent]:
        """Fire a registered stop. Returns the Cell Exit Intent(s) its scope
        produces — CELL: just the owning cell. SYMBOL: every cell holding
        that symbol. PORTFOLIO: every cell's every held symbol.

        Each intent sells exactly what is not already reserved by another
        in-flight exit, so the same cell/symbol pair is never double-claimed
        across overlapping triggers (§16). A cell already fully reserved
        (nothing left to exit) is silently skipped rather than raising —
        it means a previous trigger already has this position covered.

        All-or-nothing: the whole batch is validated against §16 capacity
        before ANY reservation is recorded. Reserving cell by cell and
        letting a later one raise would strand the earlier reservations —
        the caller never receives their ids (the exception discards the
        return value), so nothing could ever release them, and the
        capacity they hold would be subtracted from every future
        protective exit for good."""
        plan = self.get(stop_id)
        at = ensure_utc(at)

        targets: list[tuple[str, str]]   # (cell_id, symbol)
        if plan.scope is StopScope.CELL:
            targets = [(plan.cell_id, plan.symbol)]
        elif plan.scope is StopScope.SYMBOL:
            targets = [(cid, plan.symbol) for cid, cell in cells.items()
                      if cell.position_qty(plan.symbol) > 1e-9]
        else:  # PORTFOLIO
            targets = [(cid, sym) for cid, cell in cells.items()
                      for sym in cell.positions if cell.position_qty(sym) > 1e-9]

        # 1. plan every exit this trigger wants, touching no state
        planned: list[tuple[str, str, float]] = []   # (cell_id, symbol, exit_qty)
        for cell_id, symbol in targets:
            if cell_id not in cells:
                continue
            held = cells[cell_id].position_qty(symbol)
            already_reserved_for_cell = self.sell_reservations.reserved_qty(symbol, cell_id)
            exit_qty = held - already_reserved_for_cell
            if exit_qty <= 1e-9:
                continue
            planned.append((cell_id, symbol, exit_qty))

        # 2. validate the batch's total demand per symbol against §16 capacity
        demand_by_symbol: dict[str, float] = {}
        for _, symbol, exit_qty in planned:
            demand_by_symbol[symbol] = demand_by_symbol.get(symbol, 0.0) + exit_qty
        for symbol, total_demand in demand_by_symbol.items():
            self.sell_reservations.check_can_reserve(symbol, total_demand)

        # 3. commit — every reservation above is now known to fit
        intents: list[CellOrderIntent] = []
        for cell_id, symbol, exit_qty in planned:
            reservation_id = f"{stop_id}:{cell_id}:{symbol}:{uuid.uuid4().hex[:8]}"
            self.sell_reservations.reserve_sell(reservation_id, cell_id, symbol, exit_qty)
            intents.append(CellOrderIntent(
                intent_id=reservation_id, cell_id=cell_id, symbol=symbol,
                side=Action.SELL, qty=exit_qty, created_at=at))
        return intents
