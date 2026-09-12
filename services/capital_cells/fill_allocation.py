"""Deterministic Fill Allocation Engine (docs/capital_cell_architecture.md
§11, §41 priority 7).

After Internal Netting, only the residual net order reaches the broker
(`services.capital_cells.netting.NettingResult.broker_orders`). When that
broker order partially fills, this decides which cell(s) the fill belongs
to — never an AI, and never ad hoc: a fixed, version-tagged pro-rata rule so
the same inputs always produce the same allocation.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import datetime

from packages.schemas.capital_cell import CellOrderIntent
from packages.schemas.core import Action
from services.capital_cells.ledger import CellLedger, CellOverdrawError, CellShortSellError


class FillAllocationRule(str, enum.Enum):
    PRO_RATA_V1 = "PRO_RATA_V1"


@dataclass(frozen=True)
class CellFillAllocation:
    cell_id: str
    symbol: str
    side: Action
    qty: float
    price: float
    fees: float


class FillAllocationEngine:
    def __init__(self, rule: FillAllocationRule = FillAllocationRule.PRO_RATA_V1) -> None:
        self._rule = rule

    def allocate(self, residual_intents: list[CellOrderIntent], fill_qty: float,
                 fill_price: float, fill_fees: float) -> list[CellFillAllocation]:
        """`residual_intents`: the cell intents still owed a broker fill for
        one symbol/side after internal crossing (the contributors to that
        symbol's `net_broker_qty`). `fill_qty` may be a partial fill of the
        broker order those intents were netted into.

        Pro-rata by each intent's share of total residual demand, with the
        rounding residual absorbed by the last intent in a deterministic
        (`intent_id`) order — so allocated qty and allocated fees always sum
        EXACTLY to `fill_qty` / `fill_fees`, never losing or inventing a
        share or a cent."""
        if not residual_intents or fill_qty <= 0:
            return []
        # One broker order is one symbol and one side. Mixing them here would
        # post an NVDA fill onto an MSFT position, or a buy onto a sell.
        symbols = {i.symbol for i in residual_intents}
        sides = {i.side for i in residual_intents}
        if len(symbols) > 1 or len(sides) > 1:
            raise ValueError(
                f"residual intents must share one symbol and one side, got "
                f"symbols={sorted(symbols)} sides={sorted(s.value for s in sides)}")
        total_requested = sum(i.qty for i in residual_intents)
        if total_requested <= 0:
            return []
        if fill_qty > total_requested + 1e-6:
            raise ValueError(
                "fill_qty exceeds total residual cell demand — netting and "
                "fill-allocation inputs are inconsistent")

        ordered = sorted(residual_intents, key=lambda i: i.intent_id)
        allocations: list[CellFillAllocation] = []
        allocated_qty = 0.0
        for idx, intent in enumerate(ordered):
            if idx == len(ordered) - 1:
                qty = fill_qty - allocated_qty
            else:
                qty = round(intent.qty / total_requested * fill_qty, 6)
                allocated_qty += qty
            if qty <= 1e-9:
                continue
            fee_share = fill_fees * (qty / fill_qty)
            allocations.append(CellFillAllocation(
                cell_id=intent.cell_id, symbol=intent.symbol, side=intent.side,
                qty=qty, price=fill_price, fees=fee_share))
        return allocations


def apply_allocations_to_cells(cells: dict[str, CellLedger],
                                allocations: list[CellFillAllocation],
                                at: datetime) -> None:
    """Post each cell's allocated share of a REAL broker fill to its
    CellLedger via `record_broker_fill` — real fees, real cash movement,
    the only path summed against the Master Ledger at reconciliation.

    All-or-nothing: every leg is checked before any of them writes. Posting
    some cells and then raising would leave SUM(cell positions) != master
    position, i.e. it would itself create the §4 reconciliation break that
    reconciliation exists to catch."""
    _validate_allocations(cells, allocations)
    # Sell legs first, so a cell whose purchase is funded by its own
    # simultaneous sale is not rejected on a transient cash dip.
    ordered = ([a for a in allocations if a.side is Action.SELL]
               + [a for a in allocations if a.side is Action.BUY])
    for a in ordered:
        signed_qty = a.qty if a.side is Action.BUY else -a.qty
        cells[a.cell_id].record_broker_fill(
            a.symbol, signed_qty, a.price, a.fees, at,
            note="broker fill allocation (§11)")


def _validate_allocations(cells: dict[str, CellLedger],
                           allocations: list[CellFillAllocation]) -> None:
    sell_qty: dict[tuple[str, str], float] = {}
    cash_delta: dict[str, float] = {}
    for a in allocations:
        if a.cell_id not in cells:
            raise KeyError(f"allocation names cell {a.cell_id!r}, which has no ledger")
        if a.side is Action.SELL:
            key = (a.cell_id, a.symbol)
            sell_qty[key] = sell_qty.get(key, 0.0) + a.qty
            cash_delta[a.cell_id] = cash_delta.get(a.cell_id, 0.0) + a.qty * a.price - a.fees
        else:
            cash_delta[a.cell_id] = cash_delta.get(a.cell_id, 0.0) - a.qty * a.price - a.fees

    for (cell_id, symbol), qty in sell_qty.items():
        held = cells[cell_id].position_qty(symbol)
        if qty > held + 1e-9:
            raise CellShortSellError(
                f"cell {cell_id}: allocated fill would sell {qty} {symbol} but it holds "
                f"{held} (§2)")

    for cell_id, delta in cash_delta.items():
        if cells[cell_id].cash + delta < -1e-9:
            raise CellOverdrawError(
                f"cell {cell_id}: allocated fill needs {-delta:.2f} but it holds "
                f"{cells[cell_id].cash:.2f} of virtual cash")
