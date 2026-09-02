"""Operating Cost Engine & Two P&L (MASTER SPEC §80-83).

Trading P&L (broker account result) and Project Net P&L (minus system
operating costs) are always shown separately (§81) — even when costs are not
actually paid from the broker account.

Data ROI (§82): premium data services are judged by incremental edge minus
cost → KEEP / TESTING / DORMANT / CANCEL.  AI can never subscribe to a paid
service on its own (§83) — this engine only *records* costs and *recommends*.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime


class CostCategory(str, enum.Enum):
    AI = "AI"
    MARKET_DATA = "MARKET_DATA"
    NEWS = "NEWS"
    INSTITUTIONAL = "INSTITUTIONAL"
    OPTIONS_DATA = "OPTIONS_DATA"
    SERVER = "SERVER"
    DB = "DB"
    MONITORING = "MONITORING"
    BROKER_FEE = "BROKER_FEE"
    TRANSACTION_FEE = "TRANSACTION_FEE"
    FX = "FX"


@dataclass(frozen=True)
class CostEntry:
    at: datetime
    category: CostCategory
    amount: float
    note: str = ""


@dataclass
class TwoPnL:
    """§81: 必ず分離表示."""

    trading_pnl: float
    operating_costs: float

    @property
    def project_net_pnl(self) -> float:
        return self.trading_pnl - self.operating_costs


# Trading/broker fees are already deducted from the broker account balance —
# Ledger._append subtracts them from cash the moment a fill lands, so they are
# baked into `trading_pnl` before it ever reaches this engine. Recording them
# here too (via record_transaction_fee, for the §80-83 cost breakdown/Data ROI
# picture) must never also subtract them a second time in two_pnl(), or
# project_net_pnl would double-count every fee paid.
_ALREADY_IN_TRADING_PNL = frozenset({CostCategory.TRANSACTION_FEE, CostCategory.BROKER_FEE})


class OperatingCostEngine:
    def __init__(self) -> None:
        self.entries: list[CostEntry] = []

    def record(self, entry: CostEntry) -> None:
        if entry.amount < 0:
            raise ValueError("costs are non-negative")
        self.entries.append(entry)

    def record_transaction_fee(self, at: datetime, amount: float, note: str = "") -> None:
        """Record a per-trade broker/transaction fee for visibility in the cost
        breakdown (by_category). Purely informational: total() and two_pnl()
        exclude this category because the fee already reduced trading_pnl via
        the ledger — recording it is not a second charge."""
        if amount <= 0:
            return
        self.record(CostEntry(at=at, category=CostCategory.TRANSACTION_FEE,
                              amount=amount, note=note))

    def total(self) -> float:
        """Operating costs actually deducted in two_pnl(). Excludes
        transaction/broker fees, which are already netted into trading_pnl —
        see `_ALREADY_IN_TRADING_PNL`."""
        return sum(e.amount for e in self.entries if e.category not in _ALREADY_IN_TRADING_PNL)

    def trading_fees_total(self) -> float:
        """Transaction/broker fees recorded for visibility — already reflected
        in trading_pnl, not part of `total()`."""
        return sum(e.amount for e in self.entries if e.category in _ALREADY_IN_TRADING_PNL)

    def by_category(self) -> dict[CostCategory, float]:
        """Full breakdown incl. transaction/broker fees — for display only.
        Do not sum this and subtract it from trading_pnl; use total() for
        that (see two_pnl)."""
        out: dict[CostCategory, float] = {}
        for e in self.entries:
            out[e.category] = out.get(e.category, 0.0) + e.amount
        return out

    def two_pnl(self, trading_pnl: float) -> TwoPnL:
        return TwoPnL(trading_pnl=trading_pnl, operating_costs=self.total())


class DataRoiVerdict(str, enum.Enum):
    KEEP = "KEEP"
    TESTING = "TESTING"
    DORMANT = "DORMANT"
    CANCEL = "CANCEL"


@dataclass
class DataRoiEngine:
    """§82: Premium ON vs Premium OFF shadow → incremental edge − cost."""

    min_observations: int = 10
    on_returns: list[float] = field(default_factory=list)
    off_returns: list[float] = field(default_factory=list)

    def record_session(self, premium_on_return: float, premium_off_return: float) -> None:
        self.on_returns.append(premium_on_return)
        self.off_returns.append(premium_off_return)

    def evaluate(self, monthly_cost: float, equity: float) -> DataRoiVerdict:
        if len(self.on_returns) < self.min_observations:
            return DataRoiVerdict.TESTING
        incremental = (sum(self.on_returns) - sum(self.off_returns)) / len(self.on_returns)
        monthly_edge_value = incremental * equity * 21  # ~21 sessions/month
        net = monthly_edge_value - monthly_cost
        if net > monthly_cost:          # clearly pays for itself
            return DataRoiVerdict.KEEP
        if net > 0:
            return DataRoiVerdict.TESTING
        if net > -monthly_cost / 2:
            return DataRoiVerdict.DORMANT
        return DataRoiVerdict.CANCEL
