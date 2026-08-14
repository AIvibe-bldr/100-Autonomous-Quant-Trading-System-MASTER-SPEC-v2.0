"""Accounting Ledger & Portfolio Equity (MASTER SPEC §3, §5, §85).

Portfolio Equity = Cash + Market Value of Positions − Liabilities.
A cash account can never hold margin liabilities: the ledger has no field to
record one, which structurally enforces `liabilities_from_margin == 0` (INV-2).

High-Water Mark (§5) is persisted on every equity snapshot; drawdown from the
peak feeds risk scaling, but crossing a drawdown level never permanently ends
the challenge by itself (§5) — that decision requires a human (§69).

The challenge does NOT end at Cash = 0 (§4): equity may be fully invested.
It ends only when Net Liquidation Value falls below the minimum operable
amount, derived from broker minimums / fees / fractional-share support.
"""
from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from packages.common.clock import ensure_utc


class EntryKind(str, enum.Enum):
    CASH = "CASH"
    TRADE = "TRADE"
    FEE = "FEE"
    DIVIDEND = "DIVIDEND"
    FX = "FX"
    CORPORATE_ACTION = "CORPORATE_ACTION"


@dataclass(frozen=True)
class LedgerEntry:
    at: datetime
    kind: EntryKind
    amount: float                      # signed cash delta
    symbol: Optional[str] = None
    qty: float = 0.0                   # signed share delta
    price: float = 0.0
    realized_pnl: float = 0.0
    note: str = ""
    entry_id: str = field(default_factory=lambda: str(uuid.uuid4()))


@dataclass
class PositionLot:
    symbol: str
    qty: float
    avg_cost: float


class Ledger:
    """Event-sourced cash & position ledger (§49, §85).

    All state is derived from the append-only entry log; `entries` can be
    replayed to reconstruct any historical state.
    """

    def __init__(self, initial_cash: float, currency: str = "USD") -> None:
        if initial_cash <= 0:
            raise ValueError("initial cash must be positive")
        self.currency = currency
        self.entries: list[LedgerEntry] = []
        self._cash = 0.0
        self._positions: dict[str, PositionLot] = {}
        self._realized_pnl = 0.0
        self._fees_paid = 0.0
        self.initial_cash = initial_cash
        self.high_water_mark = initial_cash
        self.deposit(initial_cash, note="initial funding")

    # -- mutations (append-only) -------------------------------------------
    def _append(self, entry: LedgerEntry) -> None:
        self.entries.append(entry)
        self._cash += entry.amount
        if self._cash < -1e-9:
            raise ValueError(
                "ledger cash would go negative — margin borrowing is forbidden (§2, INV-2)"
            )
        self._fees_paid += -entry.amount if entry.kind is EntryKind.FEE else 0.0
        self._realized_pnl += entry.realized_pnl

    def deposit(self, amount: float, at: Optional[datetime] = None, note: str = "") -> None:
        from packages.common.clock import utcnow

        self._append(LedgerEntry(at=ensure_utc(at) if at else utcnow(), kind=EntryKind.CASH,
                                 amount=amount, note=note))

    def record_fill(self, symbol: str, side_qty: float, price: float, fees: float,
                    at: datetime, note: str = "") -> None:
        """side_qty: positive = buy, negative = sell."""
        at = ensure_utc(at)
        realized = 0.0
        lot = self._positions.get(symbol)
        if side_qty > 0:
            new_qty = (lot.qty if lot else 0.0) + side_qty
            new_cost = ((lot.qty * lot.avg_cost if lot else 0.0) + side_qty * price) / new_qty
            self._positions[symbol] = PositionLot(symbol, new_qty, new_cost)
        else:
            sell_qty = -side_qty
            if lot is None or lot.qty + 1e-9 < sell_qty:
                raise ValueError(f"short selling forbidden (§2, INV-3): {symbol} qty={lot.qty if lot else 0} sell={sell_qty}")
            realized = (price - lot.avg_cost) * sell_qty
            remaining = lot.qty - sell_qty
            if remaining <= 1e-9:
                del self._positions[symbol]
            else:
                self._positions[symbol] = PositionLot(symbol, remaining, lot.avg_cost)
        self._append(LedgerEntry(at=at, kind=EntryKind.TRADE, amount=-side_qty * price,
                                 symbol=symbol, qty=side_qty, price=price,
                                 realized_pnl=realized, note=note))
        if fees:
            self._append(LedgerEntry(at=at, kind=EntryKind.FEE, amount=-fees, symbol=symbol,
                                     note=f"fees for {note or symbol}"))

    # -- views --------------------------------------------------------------
    @property
    def cash(self) -> float:
        return self._cash

    @property
    def realized_pnl(self) -> float:
        return self._realized_pnl

    @property
    def fees_paid(self) -> float:
        return self._fees_paid

    @property
    def positions(self) -> dict[str, PositionLot]:
        return dict(self._positions)

    def position_qty(self, symbol: str) -> float:
        lot = self._positions.get(symbol)
        return lot.qty if lot else 0.0

    def positions_value(self, prices: dict[str, float]) -> float:
        total = 0.0
        for sym, lot in self._positions.items():
            if sym not in prices:
                raise KeyError(f"no mark price for open position {sym}")
            total += lot.qty * prices[sym]
        return total

    def equity(self, prices: dict[str, float]) -> float:
        """Portfolio Equity / Net Liquidation Value (§3). Liabilities are
        structurally zero in a cash account."""
        return self._cash + self.positions_value(prices)

    def snapshot(self, prices: dict[str, float]) -> "EquitySnapshot":
        eq = self.equity(prices)
        if eq > self.high_water_mark:
            self.high_water_mark = eq
        dd = 0.0 if self.high_water_mark <= 0 else max(0.0, 1 - eq / self.high_water_mark)
        return EquitySnapshot(cash=self._cash, positions_value=eq - self._cash,
                              equity=eq, high_water_mark=self.high_water_mark, drawdown=dd)


@dataclass(frozen=True)
class EquitySnapshot:
    cash: float
    positions_value: float
    equity: float
    high_water_mark: float
    drawdown: float

    @property
    def liabilities(self) -> float:
        return 0.0  # cash account: no margin (INV-2)


def minimum_operable_equity(min_order_value: float, typical_fee: float,
                            fractional_shares: bool, min_share_price: float) -> float:
    """Challenge end threshold (§4): derived, not a hardcoded '10万円損したら終了'."""
    floor = min_order_value + typical_fee * 10
    if not fractional_shares:
        floor = max(floor, min_share_price + typical_fee * 10)
    return floor
