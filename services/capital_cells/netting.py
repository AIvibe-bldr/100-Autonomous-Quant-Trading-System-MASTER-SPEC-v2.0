"""Internal Netting / Collision Engine (docs/capital_cell_architecture.md
§5-9, §41 priority 6).

Turns many `CellOrderIntent`s for the same symbol into one deterministic net
order for the broker, crossing what can be crossed internally at a fixed,
version-tagged Transfer Price (§9) instead of paying real spread/fees/market
impact twice when Cell A wants to buy what Cell B wants to sell.

Four things this deliberately guarantees (§5-9, §13, §39):

- **A cross is backed by a real virtual long.** §7 allows Cell B's SELL to
  cross against Cell A's BUY *only if B actually holds those shares*.
  `net()` therefore takes the cell ledgers and rejects a batch whose SELL
  intents exceed the selling cell's virtual position, before any plan is
  produced — a cross that would manufacture a cell-level short is not a
  fill that fails later, it is a plan that is never drawn up (§2).
- **Determinism.** Intent ids must be unique (§13) and every symbol's
  intents are matched in `intent_id` order, so the same gross intents
  always produce the same `net_broker_order` and the same cross pairing —
  the §39 invariant `net_broker_order == deterministic_net(gross_cell_intents)`.
  Without the uniqueness check, duplicate ids fall back on input order and
  the "deterministic" claim quietly stops being true.
- **Gross flow survives netting.** `NettingResult` keeps `gross_buy_flow` /
  `gross_sell_flow` / `internal_cross_volume` / `net_broker_flow` and every
  contributing intent — a net BUY 20 from "Cell A BUY 100, Cell B SELL 80"
  must never be read as "only 20 shares of risk existed" (§6).
- **No fake Master trade / tax event.** An `InternalCross` is a record only;
  applying it uses `CellLedger.record_internal_cross` (fees forced to 0),
  and this module never touches `packages.common.ledger.Ledger` at all. The
  real Master Ledger only ever sees the residual broker order's real fill,
  via `services.capital_cells.fill_allocation` — so
  `internal_cross_does_not_create_fake_master_trade` /
  `..._fake_tax_event` (§39) hold structurally, not by convention.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Optional

from packages.schemas.capital_cell import CellOrderIntent
from packages.schemas.core import Action
from services.capital_cells.ledger import CellLedger, CellOverdrawError, CellShortSellError


class TransferPriceMethod(str, enum.Enum):
    """Version-tagged Transfer Price rule (§9) — an internal cross's price is
    never picked by an AI or by whichever cell wants a better number."""

    ARRIVAL_MID_V1 = "ARRIVAL_MID_V1"


class DuplicateIntentIdError(ValueError):
    """Two cell order intents share an intent_id (§13). Ordering — and with
    it the netting result — would depend on input order."""


class UnknownCellError(KeyError):
    """An intent names a cell with no ledger."""


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
    residual_intents: tuple[CellOrderIntent, ...]       # what the broker order owes each cell

    def __post_init__(self) -> None:
        residual_total = sum(i.qty for i in self.residual_intents)
        if abs(residual_total - self.net_broker_qty) > 1e-6:
            raise ValueError(
                f"{self.symbol}: residual intents sum to {residual_total} but the net "
                f"broker order is {self.net_broker_qty} — fill allocation would "
                f"misattribute the fill")


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
        orders: list[tuple[str, Action, float]] = []
        for r in self.by_symbol.values():
            if r.net_broker_side is not None and r.net_broker_qty > 1e-9:
                orders.append((r.symbol, r.net_broker_side, r.net_broker_qty))
        return orders


class NettingEngine:
    """Deterministic `net(gross_cell_intents)` (§39 invariant) — no AI in
    this call path (§7, §9)."""

    def __init__(self, transfer_price_fn: Callable[[str], float],
                 method: TransferPriceMethod = TransferPriceMethod.ARRIVAL_MID_V1) -> None:
        self._transfer_price_fn = transfer_price_fn
        self._method = method

    def net(self, intents: list[CellOrderIntent],
            cells: dict[str, CellLedger]) -> NettingResult:
        """`cells` is required, not optional: §7 makes a cell's actual virtual
        long the precondition for crossing its SELL, so a netting plan cannot
        be drawn up without it."""
        self._validate_batch(intents, cells)
        by_symbol: dict[str, list[CellOrderIntent]] = {}
        for intent in intents:
            by_symbol.setdefault(intent.symbol, []).append(intent)
        return NettingResult(
            by_symbol={symbol: self._net_symbol(symbol, symbol_intents)
                       for symbol, symbol_intents in by_symbol.items()})

    def _validate_batch(self, intents: list[CellOrderIntent],
                        cells: dict[str, CellLedger]) -> None:
        seen: set[str] = set()
        sell_demand: dict[tuple[str, str], float] = {}
        for intent in intents:
            if intent.intent_id in seen:
                raise DuplicateIntentIdError(
                    f"intent_id {intent.intent_id!r} appears twice — intent ids must "
                    f"be unique (§13) or netting stops being deterministic")
            seen.add(intent.intent_id)
            if intent.cell_id not in cells:
                raise UnknownCellError(
                    f"intent {intent.intent_id!r} names cell {intent.cell_id!r}, "
                    f"which has no ledger")
            if intent.side is Action.SELL:
                key = (intent.cell_id, intent.symbol)
                sell_demand[key] = sell_demand.get(key, 0.0) + intent.qty

        # §2/§7: a cell's SELL is only ever a reduction of its own virtual
        # long. Checked on the cell's TOTAL demand per symbol, not per intent:
        # two 30-share SELLs from a cell holding 50 each look legal alone.
        for (cell_id, symbol), demand in sell_demand.items():
            held = cells[cell_id].position_qty(symbol)
            if demand > held + 1e-9:
                raise CellShortSellError(
                    f"cell {cell_id}: SELL intents total {demand} {symbol} but it holds "
                    f"{held} — a cell may only sell what it holds (§2, §7)")

    def _net_symbol(self, symbol: str, intents: list[CellOrderIntent]) -> SymbolNetResult:
        # Deterministic ordering: sort by intent_id (unique, per _validate_batch),
        # so replaying the same gross intents always crosses the same cells
        # against each other in the same order regardless of input list order.
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

        # Whatever the greedy match could not cross is what the broker order
        # actually owes each cell — exposed so Fill Allocation (§11) is driven
        # from the netting result instead of the caller re-deriving it.
        leftovers = zip(buys, buy_remaining) if net_side is Action.BUY else zip(sells, sell_remaining)
        residual = tuple(intent.model_copy(update={"qty": remaining})
                         for intent, remaining in leftovers if remaining > 1e-9)

        return SymbolNetResult(
            symbol=symbol, gross_buy_qty=gross_buy, gross_sell_qty=gross_sell,
            internal_cross_qty=sum(c.qty for c in crosses),
            net_broker_side=net_side, net_broker_qty=abs(net_qty),
            crosses=tuple(crosses), contributing_intents=tuple(intents),
            residual_intents=residual)


def apply_crosses_to_cells(cells: dict[str, CellLedger], result: NettingResult,
                            at: datetime) -> None:
    """Post the purely-virtual internal-cross legs to both sides' CellLedger.
    Never touches the real Master Ledger (§7-8) — see module docstring.

    All-or-nothing, like every other write in this codebase (see
    `Ledger._append`: "a rejected write must leave no trace"). Applying leg
    by leg and letting one raise would leave some cells credited and others
    not, which is precisely the state the §4 reconciliation invariant exists
    to declare broken — a partial apply would *cause* the HALT it is meant
    to detect."""
    _validate_crosses(cells, result)

    # SELL legs first: they only add cash and consume positions already
    # verified above, so no BUY leg can transiently overdraw a cell that its
    # own sale funds. With validation done, nothing here can raise.
    all_crosses = [cross for symbol_result in result.by_symbol.values()
                   for cross in symbol_result.crosses]
    sell_legs = [(c.sell_cell_id, -c.qty, c.buy_cell_id, c) for c in all_crosses]
    buy_legs = [(c.buy_cell_id, c.qty, c.sell_cell_id, c) for c in all_crosses]
    for cell_id, signed_qty, peer, cross in sell_legs + buy_legs:
        cells[cell_id].record_internal_cross(
            cross.symbol, signed_qty, cross.transfer_price, at,
            note=f"internal cross vs {peer} @ {cross.transfer_price:.4g} "
                 f"({cross.transfer_price_method.value})")


def _validate_crosses(cells: dict[str, CellLedger], result: NettingResult) -> None:
    """Verify every leg BEFORE any of them mutates a ledger."""
    sell_qty: dict[tuple[str, str], float] = {}
    cash_delta: dict[str, float] = {}
    for symbol_result in result.by_symbol.values():
        for cross in symbol_result.crosses:
            for cell_id in (cross.buy_cell_id, cross.sell_cell_id):
                if cell_id not in cells:
                    raise UnknownCellError(f"cross names cell {cell_id!r}, which has no ledger")
            key = (cross.sell_cell_id, cross.symbol)
            sell_qty[key] = sell_qty.get(key, 0.0) + cross.qty
            notional = cross.qty * cross.transfer_price
            cash_delta[cross.buy_cell_id] = cash_delta.get(cross.buy_cell_id, 0.0) - notional
            cash_delta[cross.sell_cell_id] = cash_delta.get(cross.sell_cell_id, 0.0) + notional

    for (cell_id, symbol), qty in sell_qty.items():
        held = cells[cell_id].position_qty(symbol)
        if qty > held + 1e-9:
            raise CellShortSellError(
                f"cell {cell_id}: internal cross would sell {qty} {symbol} but it holds "
                f"{held} (§2, §7)")

    for cell_id, delta in cash_delta.items():
        if cells[cell_id].cash + delta < -1e-9:
            raise CellOverdrawError(
                f"cell {cell_id}: internal crosses need {-delta:.2f} but it holds "
                f"{cells[cell_id].cash:.2f} of virtual cash")
