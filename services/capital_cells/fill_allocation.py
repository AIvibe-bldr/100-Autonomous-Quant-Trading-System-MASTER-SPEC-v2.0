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
from services.capital_cells.ledger import CellLedger


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
    the only path summed against the Master Ledger at reconciliation."""
    for a in allocations:
        signed_qty = a.qty if a.side is Action.BUY else -a.qty
        cells[a.cell_id].record_broker_fill(
            a.symbol, signed_qty, a.price, a.fees, at,
            note="broker fill allocation (§11)")
