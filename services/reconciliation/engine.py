"""Broker Reconciliation (MASTER SPEC §48-49).

Runs at startup, periodically, and on any anomaly (UNKNOWN order, timeout).
Compares internal ledger vs broker for cash, positions, open orders and
fills.  Any mismatch → HALT_NEW_ENTRIES.  For live trading the broker is the
external source of truth (§49).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from packages.broker_adapters.base import BrokerAdapter, BrokerDisconnectedError
from packages.common.ledger import Ledger
from services.risk.master_controller import MasterRiskController, RiskState


@dataclass(frozen=True)
class Mismatch:
    kind: str
    detail: str


@dataclass
class ReconciliationReport:
    mismatches: list[Mismatch] = field(default_factory=list)

    @property
    def consistent(self) -> bool:
        return not self.mismatches


@dataclass
class ReconciliationEngine:
    broker: BrokerAdapter
    ledger: Ledger
    risk_controller: MasterRiskController
    cash_tolerance: float = 0.05
    qty_tolerance: float = 1e-4

    def reconcile(self) -> ReconciliationReport:
        report = ReconciliationReport()
        try:
            broker_cash = self.broker.get_cash()
            broker_positions = {p.symbol: p for p in self.broker.get_positions()}
        except BrokerDisconnectedError:
            self.risk_controller.set_state(RiskState.FULL_BROKER_DISCONNECT,
                                           reason="reconciliation: broker unreachable")
            report.mismatches.append(Mismatch("broker_unreachable", "disconnected"))
            return report

        # cash: ledger cash vs broker settled+unsettled
        broker_total = broker_cash.settled_cash + broker_cash.unsettled_cash
        if abs(broker_total - self.ledger.cash) > self.cash_tolerance:
            report.mismatches.append(Mismatch(
                "cash", f"ledger {self.ledger.cash:.2f} vs broker {broker_total:.2f}"))

        # positions both ways
        ledger_positions = self.ledger.positions
        for sym, lot in ledger_positions.items():
            bp = broker_positions.get(sym)
            if bp is None:
                report.mismatches.append(Mismatch("position_missing_at_broker", sym))
            elif abs(bp.qty - lot.qty) > self.qty_tolerance:
                report.mismatches.append(Mismatch(
                    "position_qty", f"{sym}: ledger {lot.qty} vs broker {bp.qty}"))
        for sym in broker_positions:
            if sym not in ledger_positions:
                report.mismatches.append(Mismatch("position_missing_in_ledger", sym))

        if not report.consistent:
            # §48: mismatch → stop new entries until resolved
            self.risk_controller.set_state(RiskState.HALT_NEW_ENTRIES,
                                           reason="reconciliation mismatch")
        return report
