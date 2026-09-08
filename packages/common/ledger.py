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
        """Validate BEFORE mutating.

        This used to append the entry and add the cash first, then raise if the
        result was negative — so the guard enforcing "no margin borrowing"
        (INV-2) left the ledger holding exactly the negative cash it exists to
        forbid, plus an entry for a trade that never happened. A rejected
        write must leave no trace.
        """
        candidate_cash = self._cash + entry.amount
        if candidate_cash < -1e-9:
            raise ValueError(
                "ledger cash would go negative — margin borrowing is forbidden (§2, INV-2)"
            )
        self.entries.append(entry)
        self._cash = candidate_cash
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

        # --- validate the WHOLE transaction before touching any state ---
        # The position used to be written first and the cash checked second,
        # so a rejected fill left a position nobody had paid for. Worse, the
        # fee was a separate append: the trade leg could commit and the fee
        # leg then raise, debiting cash without recording the fee.
        remove_position = False
        if side_qty > 0:
            new_qty = (lot.qty if lot else 0.0) + side_qty
            new_cost = ((lot.qty * lot.avg_cost if lot else 0.0) + side_qty * price) / new_qty
            new_lot: Optional[PositionLot] = PositionLot(symbol, new_qty, new_cost)
        else:
            sell_qty = -side_qty
            if lot is None or lot.qty + 1e-9 < sell_qty:
                raise ValueError(f"short selling forbidden (§2, INV-3): {symbol} qty={lot.qty if lot else 0} sell={sell_qty}")
            realized = (price - lot.avg_cost) * sell_qty
            remaining = lot.qty - sell_qty
            remove_position = remaining <= 1e-9
            new_lot = None if remove_position else PositionLot(symbol, remaining, lot.avg_cost)

        trade_amount = -side_qty * price
        if self._cash + trade_amount - (fees or 0.0) < -1e-9:
            raise ValueError(
                "ledger cash would go negative — margin borrowing is forbidden (§2, INV-2)"
            )

        # --- commit: every leg above is now known to succeed ---
        if remove_position:
            del self._positions[symbol]
        else:
            assert new_lot is not None
            self._positions[symbol] = new_lot
        self._append(LedgerEntry(at=at, kind=EntryKind.TRADE, amount=trade_amount,
                                 symbol=symbol, qty=side_qty, price=price,
                                 realized_pnl=realized, note=note))
        if fees:
            self._append(LedgerEntry(at=at, kind=EntryKind.FEE, amount=-fees, symbol=symbol,
                                     note=f"fees for {note or symbol}"))

    def rebase_high_water_mark(self, approved_by: str, at: Optional[datetime] = None) -> float:
        """Reset the drawdown baseline to current equity — HUMAN ONLY (§69).

        The throttle ladder (§39) is derived from drawdown, and the high-water
        mark only ever rises. Once NO_NEW_ENTRY engages and the protective
        stops liquidate the book, equity equals cash and stops moving — so the
        drawdown that caused the lockout can never shrink, because shrinking it
        requires trading and trading is what is blocked. That is a permanent,
        silent halt that looks exactly like "no candidates today".

        §5 says crossing a drawdown level must never permanently end the
        challenge by itself, and §69 says resuming is a human's call. This is
        that call, made explicit: it requires a named approver and leaves a
        ledger entry, so a resumption is always attributable.
        """
        from packages.common.clock import utcnow

        if not approved_by.strip():
            raise ValueError("rebasing the high-water mark requires a named human approver (§69)")
        equity_before = self.high_water_mark
        # equity is cash + marks; without marks we can only rebase to cash, so
        # callers pass marked equity via snapshot() first. Use cash as the floor.
        self.high_water_mark = max(self._cash, 0.0)
        self.entries.append(LedgerEntry(
            at=ensure_utc(at) if at else utcnow(), kind=EntryKind.CASH, amount=0.0,
            note=f"high-water mark rebased {equity_before:.2f} → {self.high_water_mark:.2f} "
                 f"by {approved_by} (§69)"))
        return self.high_water_mark

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
