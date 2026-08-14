"""Settlement Manager (MASTER SPEC §14).

Cash account. Tracks settled vs unsettled cash (US equities settle T+1).
Only settled cash counts as buying power — the conservative reading that
avoids good-faith violations (docs/MASTER_SPEC.md ISSUE-2).

The broker's reported balance is the external source of truth (§14, §49);
`reconcile_against_broker` compares and reports drift.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from packages.common.clock import ensure_utc
from packages.schemas.core import BrokerCashBalance

SETTLEMENT_LAG = timedelta(days=1)  # T+1


@dataclass
class PendingSettlement:
    amount: float
    settles_at: datetime


@dataclass
class SettlementManager:
    settled_cash: float
    pending: list[PendingSettlement] = field(default_factory=list)

    def on_buy(self, cost: float, at: datetime) -> None:
        """Buys consume settled cash immediately."""
        at = ensure_utc(at)
        if cost > self.settled_cash + 1e-9:
            raise ValueError("buy exceeds settled cash — unsettled funds may not be spent (§14)")
        self.settled_cash -= cost

    def on_sell(self, proceeds: float, at: datetime) -> None:
        """Sale proceeds are unsettled until T+1."""
        at = ensure_utc(at)
        self.pending.append(PendingSettlement(amount=proceeds, settles_at=at + SETTLEMENT_LAG))

    def process_settlements(self, now: datetime) -> float:
        now = ensure_utc(now)
        settled = 0.0
        keep: list[PendingSettlement] = []
        for p in self.pending:
            if p.settles_at <= now:
                settled += p.amount
            else:
                keep.append(p)
        self.pending = keep
        self.settled_cash += settled
        return settled

    @property
    def unsettled_cash(self) -> float:
        return sum(p.amount for p in self.pending)

    @property
    def buying_power(self) -> float:
        return self.settled_cash

    def balance(self) -> BrokerCashBalance:
        return BrokerCashBalance(settled_cash=max(0.0, self.settled_cash),
                                 unsettled_cash=self.unsettled_cash)

    def reconcile_against_broker(self, broker: BrokerCashBalance, tolerance: float = 0.01) -> bool:
        """Broker is source of truth (§14); mismatch beyond tolerance means halt entries (§48)."""
        return (abs(broker.settled_cash - self.settled_cash) <= tolerance
                and abs(broker.unsettled_cash - self.unsettled_cash) <= tolerance)
