"""Capital-cell MIRROR — phase A of the integration proposal (docs/capital_cell_architecture.md).

Two virtual cells, AI and HUMAN, shadow the single real account: every fill the pipeline books is also
posted to the cell that originated it, and each session the cells are reconciled against the Master Ledger
(§4: sum of cells == master). It changes nothing the system does — no sizing, no allocation, no order path
(that is phases B and C, which need approval). What it gives:

- AI-vs-human accounting that adds up to the real books (virtual realized P&L and marked equity per cell);
- a continuous test of the cell machinery (CellLedger, FillAllocationEngine, CellReconciliationEngine) against
  real flows — if the mirror ever disagrees with the Master, that is found here, before anything depends on it.

Deliberately OBSERVE-ONLY: the reconciliation engine is built WITHOUT a risk controller, so a mismatch is
reported (summary + log), never a HALT. Promoting it to a halt is a decision for the phase that makes cells
load-bearing.

Funding: the cells share the one real pool. A buy first draws exactly what it needs from the unallocated pool
into its cell (so cells never overdraw); sale proceeds stay in the cell. Sells go to the cell(s) holding the
symbol, pro-rata by holding (the engine's PRO_RATA_V1 rule)."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

from packages.schemas.capital_cell import CellOrderIntent
from packages.schemas.core import Action
from services.capital_cells.fill_allocation import (
    CellFillAllocation,
    FillAllocationEngine,
    apply_allocations_to_cells,
)
from services.capital_cells.ledger import CellLedger
from services.capital_cells.reconciliation import CellReconciliationEngine
from services.pdca.observer import FillEvent, SessionEndEvent, SessionObserver

log = logging.getLogger("quant.cells")

CELL_AI = "AI"
CELL_HUMAN = "HUMAN"
_MAX_ERRORS_KEPT = 10


@dataclass
class _MasterView:
    """The Master Ledger's numbers as the reconciliation engine reads them."""
    cash: float
    qty: dict[str, float]

    @property
    def positions(self) -> dict[str, float]:
        return {s: q for s, q in self.qty.items() if q > 1e-9}

    def position_qty(self, symbol: str) -> float:
        return self.qty.get(symbol, 0.0)


class CapitalCellMirrorObserver(SessionObserver):
    name = "capital_cells"

    def __init__(self, initial_cash: float) -> None:
        self.cells = {CELL_AI: CellLedger(CELL_AI), CELL_HUMAN: CellLedger(CELL_HUMAN)}
        self._unallocated = initial_cash
        self._allocator = FillAllocationEngine()
        self._order_cell: dict[str, str] = {}
        self._seq = 0
        self.fills_attributed = {CELL_AI: 0, CELL_HUMAN: 0}
        self.defaulted_to_ai = 0         # a buy whose originator was not stated
        self.errors: list[str] = []      # fills the mirror could not post (the reconciliation will show it)
        self.sessions_checked = 0
        self.sessions_inconsistent = 0
        self._last_report: Optional[dict[str, Any]] = None

    # -- fills ------------------------------------------------------------------------------
    def on_fill(self, e: FillEvent) -> None:
        try:
            if e.side is Action.BUY:
                self._buy(e)
            else:
                self._sell(e)
        except (ValueError, KeyError) as err:       # CellOverdraw/ShortSell are ValueErrors
            self._error(f"{e.side.value} {e.qty:g} {e.symbol}: {type(err).__name__}: {err}")

    def _buy(self, e: FillEvent) -> None:
        cell_id = self._order_cell.get(e.client_order_id)
        if cell_id is None:
            if e.source is None:
                self.defaulted_to_ai += 1
            cell_id = CELL_HUMAN if e.source == "HUMAN" else CELL_AI
            self._order_cell[e.client_order_id] = cell_id
        cell = self.cells[cell_id]
        need = e.qty * e.price + e.fees
        topup = max(0.0, need - cell.cash)
        if topup > self._unallocated + 1e-9:
            raise ValueError(f"cell {cell_id} needs {topup:.2f} but only {self._unallocated:.2f} is unallocated")
        if topup > 0:
            cell.allocate_cash(topup, e.ts, note="draw for a buy (mirror)")
            self._unallocated -= topup
        apply_allocations_to_cells(self.cells, [CellFillAllocation(
            cell_id=cell_id, symbol=e.symbol, side=Action.BUY, qty=e.qty, price=e.price, fees=e.fees)], e.ts)
        self.fills_attributed[cell_id] += 1

    def _sell(self, e: FillEvent) -> None:
        holders = {cid: c.position_qty(e.symbol) for cid, c in self.cells.items() if c.position_qty(e.symbol) > 1e-9}
        if not holders:
            raise KeyError(f"no cell holds {e.symbol}")
        total = sum(holders.values())
        qty = min(e.qty, total)                               # never sell more than the cells hold
        fee = e.fees * (qty / e.qty)
        intents = []
        for cid, held in sorted(holders.items()):
            self._seq += 1
            intents.append(CellOrderIntent(intent_id=f"mirror-{self._seq}-{cid}", cell_id=cid, symbol=e.symbol,
                                           side=Action.SELL, qty=held, created_at=e.ts))
        allocations = self._allocator.allocate(intents, qty, e.price, fee)
        apply_allocations_to_cells(self.cells, allocations, e.ts)
        for a in allocations:
            self.fills_attributed[a.cell_id] += 1
        if qty < e.qty - 1e-9:
            raise ValueError(f"sold {e.qty:g} {e.symbol} but the cells held only {total:g}")

    def _error(self, text: str) -> None:
        log.warning("capital-cell mirror could not post a fill: %s", text)
        self.errors.append(text)
        del self.errors[:-_MAX_ERRORS_KEPT]

    # -- reconciliation ---------------------------------------------------------------------------
    def on_session_end(self, e: SessionEndEvent) -> None:
        master = _MasterView(cash=e.ledger_cash, qty=dict(e.ledger_positions or {}))
        report = CellReconciliationEngine(
            master_ledger=master, cells=self.cells, risk_controller=None,     # observe only: never a halt
            unallocated_master_cash=self._unallocated).reconcile()
        self.sessions_checked += 1
        if not report.consistent:
            self.sessions_inconsistent += 1
            log.warning("capital-cell mirror does not reconcile with the Master Ledger: %s",
                        "; ".join(f"{m.kind}: {m.detail}" for m in report.mismatches))
        marks = {}
        for cell in self.cells.values():
            for sym in cell.positions:
                if sym not in marks:
                    marks[sym] = e.price_on(sym, e.as_of)
        self._last_report = {
            "at": e.as_of.isoformat(), "consistent": report.consistent,
            "mismatches": [{"kind": m.kind, "detail": m.detail} for m in report.mismatches],
            "cells": {cid: self._cell_view(c, marks) for cid, c in self.cells.items()}}

    @staticmethod
    def _cell_view(cell: CellLedger, marks: dict[str, Optional[float]]) -> dict[str, Any]:
        value, unpriced = 0.0, []
        for sym, lot in cell.positions.items():
            px = marks.get(sym)
            if px:
                value += lot.qty * px
            else:
                value += lot.qty * lot.avg_cost                     # no price: valued at cost, and said so
                unpriced.append(sym)
        return {"cash": round(cell.cash, 4), "positions": {s: round(l.qty, 6) for s, l in cell.positions.items()},
                "virtual_realized_pnl": round(cell.virtual_realized_pnl, 4),
                "marked_equity": round(cell.cash + value, 4), "valued_at_cost": unpriced}

    def summary(self) -> dict[str, Any]:
        return {
            "phase": "A: observe-only mirror (no effect on sizing, allocation or orders)",
            "unallocated_cash": round(self._unallocated, 4),
            "fills_attributed": dict(self.fills_attributed), "defaulted_to_ai": self.defaulted_to_ai,
            "errors": list(self.errors),
            "reconciliation": {"sessions_checked": self.sessions_checked,
                               "sessions_inconsistent": self.sessions_inconsistent,
                               "latest": self._last_report},
            "note": "marked equity uses the last daily close; a mismatch is reported, never a halt"}
