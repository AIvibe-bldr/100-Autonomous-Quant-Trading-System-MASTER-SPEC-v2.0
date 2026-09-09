"""Capital Cell virtual ledger (docs/capital_cell_architecture.md §2-3, §8,
§41 priority 2 and 5).

`CellLedger` tracks one cell's virtual cash and positions the same way
`packages.common.ledger.Ledger` tracks the real Master account — but nothing
here ever touches a broker. It exists purely for internal attribution, and
its arithmetic must reconcile against the single real Ledger
(`services.capital_cells.reconciliation`).

Two write paths, kept deliberately distinct (§8: Virtual Attribution vs
Actual Accounting must never be conflated):

- `record_broker_fill`: this cell's share of a REAL broker fill, as decided
  by the Fill Allocation Engine (§11). Fees are real.
- `record_internal_cross`: a purely virtual transfer against another cell's
  opposing intent, decided by the Netting Engine (§7). Fees are always 0 —
  an internal cross is not a real trade and must never generate a real fee,
  a real tax event, or real Master realized P&L (§7-8, §39 invariants
  `internal_cross_does_not_create_fake_master_trade` /
  `..._fake_tax_event`). That guarantee is structural here, not a flag to
  remember to check: neither path ever calls into `packages.common.ledger`.

Both paths enforce the same cell-level invariants as the real Ledger enforces
Master-side (§2, §39): `cell_position_qty >= 0` and
`cell_sell_qty <= cell_current_long_qty` — Cell short-selling is forbidden
exactly like Master short-selling is (INV-3), independently.
"""
from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from packages.common.clock import ensure_utc, utcnow


class CellEntryKind(str, enum.Enum):
    ALLOCATION = "ALLOCATION"          # capital handed to the cell
    BROKER_FILL = "BROKER_FILL"        # this cell's share of a real broker fill
    BROKER_FEE = "BROKER_FEE"          # this cell's share of a real broker fee
    INTERNAL_CROSS = "INTERNAL_CROSS"  # purely virtual — never a real trade


@dataclass(frozen=True)
class CellLedgerEntry:
    at: datetime
    cell_id: str
    kind: CellEntryKind
    amount: float                       # signed virtual cash delta
    symbol: Optional[str] = None
    qty: float = 0.0                    # signed virtual share delta
    price: float = 0.0
    virtual_realized_pnl: float = 0.0   # NEVER fed into the real Ledger (§8)
    note: str = ""
    entry_id: str = field(default_factory=lambda: str(uuid.uuid4()))


@dataclass
class CellPositionLot:
    symbol: str
    qty: float
    avg_cost: float


class CellShortSellError(ValueError):
    """A cell tried to sell more than its own virtual long position (§2)."""


class CellOverdrawError(ValueError):
    """A cell tried to spend virtual cash it doesn't have. Should be caught
    earlier by the Capital Reservation Ledger (§12) — this is defense in
    depth, the same layering the real Ledger uses for margin (INV-2)."""


class CellLedger:
    """One cell's virtual position/cash ledger (§2-3). Same shape as
    `packages.common.ledger.Ledger` on purpose: reconciliation compares the
    two structures field for field."""

    def __init__(self, cell_id: str) -> None:
        self.cell_id = cell_id
        self.entries: list[CellLedgerEntry] = []
        self._cash = 0.0
        self._positions: dict[str, CellPositionLot] = {}
        self._virtual_realized_pnl = 0.0

    # -- capital in/out -------------------------------------------------
    def allocate_cash(self, amount: float, at: Optional[datetime] = None,
                       note: str = "") -> None:
        """Master hands this cell virtual budget (or claws some back with a
        negative amount, e.g. an Allocation Governor rebalance)."""
        at = ensure_utc(at) if at else utcnow()
        candidate = self._cash + amount
        if candidate < -1e-9:
            raise CellOverdrawError(
                f"cell {self.cell_id}: allocation would leave virtual cash negative")
        self.entries.append(CellLedgerEntry(
            at=at, cell_id=self.cell_id, kind=CellEntryKind.ALLOCATION,
            amount=amount, note=note))
        self._cash = candidate

    # -- position mutations (append-only, validate before mutate) -------
    def _apply_fill(self, kind: CellEntryKind, symbol: str, side_qty: float,
                     price: float, fees: float, at: datetime, note: str) -> None:
        """side_qty: positive = buy, negative = sell. Shared by both the
        real-fill and internal-cross paths so the no-short / no-overdraw
        invariants are enforced identically regardless of which one is
        virtual (§2, §8)."""
        at = ensure_utc(at)
        realized = 0.0
        lot = self._positions.get(symbol)

        remove_position = False
        if side_qty > 0:
            new_qty = (lot.qty if lot else 0.0) + side_qty
            new_cost = ((lot.qty * lot.avg_cost if lot else 0.0) + side_qty * price) / new_qty
            new_lot: Optional[CellPositionLot] = CellPositionLot(symbol, new_qty, new_cost)
        else:
            sell_qty = -side_qty
            if lot is None or lot.qty + 1e-9 < sell_qty:
                raise CellShortSellError(
                    f"cell {self.cell_id}: short selling forbidden (§2) — "
                    f"{symbol} virtual qty={lot.qty if lot else 0} sell={sell_qty}")
            realized = (price - lot.avg_cost) * sell_qty
            remaining = lot.qty - sell_qty
            remove_position = remaining <= 1e-9
            new_lot = None if remove_position else CellPositionLot(symbol, remaining, lot.avg_cost)

        trade_amount = -side_qty * price
        if self._cash + trade_amount - (fees or 0.0) < -1e-9:
            raise CellOverdrawError(
                f"cell {self.cell_id}: fill would leave virtual cash negative")

        # commit — every leg above is now known to succeed
        if remove_position:
            del self._positions[symbol]
        else:
            assert new_lot is not None
            self._positions[symbol] = new_lot
        self._cash += trade_amount
        self._virtual_realized_pnl += realized
        self.entries.append(CellLedgerEntry(
            at=at, cell_id=self.cell_id, kind=kind, amount=trade_amount, symbol=symbol,
            qty=side_qty, price=price, virtual_realized_pnl=realized, note=note))
        if fees:
            self._cash -= fees
            self.entries.append(CellLedgerEntry(
                at=at, cell_id=self.cell_id, kind=CellEntryKind.BROKER_FEE,
                amount=-fees, symbol=symbol, note=f"fees for {note or symbol}"))

    def record_broker_fill(self, symbol: str, side_qty: float, price: float,
                            fees: float, at: datetime, note: str = "") -> None:
        """This cell's allocated share of a REAL broker fill (§11). Real
        fees, real cash movement — this is the only path that should ever be
        summed against the Master Ledger's own fills at reconciliation."""
        self._apply_fill(CellEntryKind.BROKER_FILL, symbol, side_qty, price, fees, at, note)

    def record_internal_cross(self, symbol: str, side_qty: float, price: float,
                               at: datetime, note: str = "") -> None:
        """A purely virtual transfer against another cell's opposing intent
        (§7). Always zero fees by construction — never call this with a fee;
        an internal cross is not a real trade (§8)."""
        self._apply_fill(CellEntryKind.INTERNAL_CROSS, symbol, side_qty, price, 0.0, at, note)

    # -- views ------------------------------------------------------------
    @property
    def cash(self) -> float:
        return self._cash

    @property
    def virtual_realized_pnl(self) -> float:
        return self._virtual_realized_pnl

    @property
    def positions(self) -> dict[str, CellPositionLot]:
        return dict(self._positions)

    def position_qty(self, symbol: str) -> float:
        lot = self._positions.get(symbol)
        return lot.qty if lot else 0.0
