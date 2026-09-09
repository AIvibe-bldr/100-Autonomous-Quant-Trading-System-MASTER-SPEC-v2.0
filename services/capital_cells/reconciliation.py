"""Master Reconciliation Invariant for Capital Cells (docs/capital_cell_architecture.md
§4, §41 priority 3).

Mirrors `services.reconciliation.engine.ReconciliationEngine`'s broker-vs-
ledger pattern, but compares SUM(cell virtual state) against the single real
`packages.common.ledger.Ledger` instead of against the broker. The doc is
explicit that a mismatch gets the same response a broker mismatch already
gets (§48-49) — HALT_NEW_ENTRIES — not a new severity level.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from packages.common.ledger import Ledger
from services.capital_cells.ledger import CellLedger
from services.risk.master_controller import MasterRiskController, RiskState


@dataclass(frozen=True)
class CellMismatch:
    kind: str
    detail: str


@dataclass
class CellReconciliationReport:
    mismatches: list[CellMismatch] = field(default_factory=list)

    @property
    def consistent(self) -> bool:
        return not self.mismatches


@dataclass
class CellReconciliationEngine:
    master_ledger: Ledger
    cells: dict[str, CellLedger]
    risk_controller: Optional[MasterRiskController] = None
    unallocated_master_cash: float = 0.0   # not yet handed to any cell
    adjustments: float = 0.0               # explicit corp-action/fee/FX deltas (§4)
    cash_tolerance: float = 0.01
    qty_tolerance: float = 1e-6

    def reconcile(self) -> CellReconciliationReport:
        report = CellReconciliationReport()

        # SUM(cell_cash) + unallocated_master_cash + adjustments == master_cash (§4)
        cell_cash_total = sum(c.cash for c in self.cells.values())
        expected_cash = cell_cash_total + self.unallocated_master_cash + self.adjustments
        if abs(expected_cash - self.master_ledger.cash) > self.cash_tolerance:
            report.mismatches.append(CellMismatch(
                "cash",
                f"sum(cell_cash)+unallocated+adjustments={expected_cash:.2f} "
                f"vs master_ledger.cash={self.master_ledger.cash:.2f}"))

        # SUM(cell_virtual_position[symbol]) == master_broker_position[symbol] (§4)
        symbols = set(self.master_ledger.positions) | {
            sym for cell in self.cells.values() for sym in cell.positions}
        for symbol in symbols:
            cell_total = sum(cell.position_qty(symbol) for cell in self.cells.values())
            master_qty = self.master_ledger.position_qty(symbol)
            if abs(cell_total - master_qty) > self.qty_tolerance:
                report.mismatches.append(CellMismatch(
                    "position",
                    f"{symbol}: sum(cells)={cell_total} vs master={master_qty}"))

        if not report.consistent and self.risk_controller is not None:
            self.risk_controller.set_state(
                RiskState.HALT_NEW_ENTRIES,
                reason="capital cell reconciliation mismatch (§4)")
        return report
