"""Internal Netting / Collision Engine (docs/capital_cell_architecture.md
§5-9, §41 priority 6).

Turns many `CellOrderIntent`s for the same symbol into one deterministic net
order for the broker, crossing what can be crossed internally at a fixed,
version-tagged Transfer Price (§9) instead of paying real spread/fees/market
impact twice when Cell A wants to buy what Cell B wants to sell.

Three things this deliberately preserves rather than discards (§6-8):

- **Gross flow.** `NettingResult` keeps `gross_buy_flow` / `gross_sell_flow` /
  `internal_cross_volume` / `net_broker_flow` and every contributing intent —
  a net BUY 20 from "Cell A BUY 100, Cell B SELL 80" must never be read as
  "only 20 shares of risk existed" (§6). Master Risk Controller integration
  is left to whoever wires this in; this module's job is to not lose the
  gross numbers before that can happen.
- **Determinism.** `net()` sorts every symbol's intents by `intent_id`
  before matching, so the same gross intents always net to the same
  `net_broker_order` and the same internal-cross pairing, satisfying the
  §39 invariant `net_broker_order == deterministic_net(gross_cell_intents)`.
- **No fake Master trade / tax event.** An `InternalCross` here is a record
  only — applying it to `CellLedger`s uses `record_internal_cross` (fees
  forced to 0, kind=INTERNAL_CROSS), and this module never touches
  `packages.common.ledger.Ledger` at all. The real Master Ledger only ever
  sees the residual broker order's real fill, via the Fill Allocation Engine
  (`services.capital_cells.fill_allocation`) — satisfying
  `internal_cross_does_not_create_fake_master_trade` /
  `..._fake_tax_event` (§39) structurally, not by convention.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Optional

from packages.schemas.capital_cell import CellOrderIntent
from packages.schemas.core import Action
from services.capital_cells.ledger import CellLedger


class TransferPriceMethod(str, enum.Enum):
    """Version-tagged Transfer Price rule (§9) — an internal cross's price is
    never picked by an AI or by whichever cell wants a better number."""

    ARRIVAL_MID_V1 = "ARRIVAL_MID_V1"


@dataclass(frozen=True)
class InternalCross:
    symbol: str
    buy_cell_id: str
    sell_cell_id: str
    qty: float
    transfer_price: float
    transfer_price_method: TransferPriceMethod


@dataclass(frozen=True)
class SymbolNetResult:
    symbol: str
    gross_buy_qty: float
    gross_sell_qty: float
    internal_cross_qty: float
    net_broker_side: Optional[Action]     # None if fully internally crossed
    net_broker_qty: float                 # 0.0 if fully internally crossed
    crosses: tuple[InternalCross, ...]
    contributing_intents: tuple[CellOrderIntent, ...]   # gross intents, preserved (§6)


@dataclass(frozen=True)
class NettingResult:
    by_symbol: dict[str, SymbolNetResult]

    @property
    def gross_buy_flow(self) -> float:
        return sum(r.gross_buy_qty for r in self.by_symbol.values())

    @property
    def gross_sell_flow(self) -> float:
        return sum(r.gross_sell_qty for r in self.by_symbol.values())

    @property
    def internal_cross_volume(self) -> float:
        return sum(r.internal_cross_qty for r in self.by_symbol.values())

    @property
    def net_broker_flow(self) -> float:
        return sum(r.net_broker_qty for r in self.by_symbol.values())

    def broker_orders(self) -> list[tuple[str, Action, float]]:
        """(symbol, side, qty) for every symbol that still needs a real
        broker order after internal crossing."""
        return [(r.symbol, r.net_broker_side, r.net_broker_qty)
                for r in self.by_symbol.values()
                if r.net_broker_side is not None and r.net_broker_qty > 1e-9]


class NettingEngine:
    """Deterministic `net(gross_cell_intents)` (§39 invariant) — no AI in
    this call path (§7, §9)."""

    def __init__(self, transfer_price_fn: Callable[[str], float],
                 method: TransferPriceMethod = TransferPriceMethod.ARRIVAL_MID_V1) -> None:
        self._transfer_price_fn = transfer_price_fn
        self._method = method

    def net(self, intents: list[CellOrderIntent]) -> NettingResult:
        by_symbol: dict[str, list[CellOrderIntent]] = {}
        for intent in intents:
            by_symbol.setdefault(intent.symbol, []).append(intent)
        return NettingResult(
            by_symbol={symbol: self._net_symbol(symbol, symbol_intents)
                       for symbol, symbol_intents in by_symbol.items()})

    def _net_symbol(self, symbol: str, intents: list[CellOrderIntent]) -> SymbolNetResult:
        # Deterministic ordering: sort by intent_id, so replaying the same
        # gross intents always crosses the same cells against each other in
        # the same order regardless of input list order.
        buys = sorted((i for i in intents if i.side is Action.BUY), key=lambda i: i.intent_id)
        sells = sorted((i for i in intents if i.side is Action.SELL), key=lambda i: i.intent_id)
        gross_buy = sum(i.qty for i in buys)
        gross_sell = sum(i.qty for i in sells)

        price = self._transfer_price_fn(symbol)
        crosses: list[InternalCross] = []
        buy_remaining = [i.qty for i in buys]
        sell_remaining = [i.qty for i in sells]
        bi = si = 0
        while bi < len(buys) and si < len(sells):
            cross_qty = min(buy_remaining[bi], sell_remaining[si])
            if cross_qty > 1e-9:
                crosses.append(InternalCross(
                    symbol=symbol, buy_cell_id=buys[bi].cell_id,
                    sell_cell_id=sells[si].cell_id, qty=cross_qty,
                    transfer_price=price, transfer_price_method=self._method))
            buy_remaining[bi] -= cross_qty
            sell_remaining[si] -= cross_qty
            if buy_remaining[bi] <= 1e-9:
                bi += 1
            if sell_remaining[si] <= 1e-9:
                si += 1

        net_qty = gross_buy - gross_sell
        net_side = Action.BUY if net_qty > 1e-9 else (Action.SELL if net_qty < -1e-9 else None)

        return SymbolNetResult(
            symbol=symbol, gross_buy_qty=gross_buy, gross_sell_qty=gross_sell,
            internal_cross_qty=sum(c.qty for c in crosses),
            net_broker_side=net_side, net_broker_qty=abs(net_qty),
            crosses=tuple(crosses), contributing_intents=tuple(intents))


def apply_crosses_to_cells(cells: dict[str, CellLedger], result: NettingResult,
                            at: datetime) -> None:
    """Post the purely-virtual internal-cross legs to both sides' CellLedger.
    Never touches the real Master Ledger (§7-8) — see module docstring."""
    for symbol_result in result.by_symbol.values():
        for cross in symbol_result.crosses:
            note = f"internal cross vs {{peer}} @ {cross.transfer_price:.4g} ({cross.transfer_price_method.value})"
            cells[cross.buy_cell_id].record_internal_cross(
                cross.symbol, cross.qty, cross.transfer_price, at,
                note=note.format(peer=cross.sell_cell_id))
            cells[cross.sell_cell_id].record_internal_cross(
                cross.symbol, -cross.qty, cross.transfer_price, at,
                note=note.format(peer=cross.buy_cell_id))
