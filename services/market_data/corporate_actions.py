"""Corporate Action Engine (MASTER SPEC §15).

Handles: stock split, reverse split, dividend, secondary offering, rights,
tender offer, merger, acquisition, delisting, symbol change.  Actions are
applied to portfolio accounting (Ledger) and must equally be applied to
backtest data (the price-adjustment helpers here).
"""
from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional

from packages.common.ledger import EntryKind, Ledger, LedgerEntry
from packages.schemas.core import Bar


class CorporateActionType(str, enum.Enum):
    STOCK_SPLIT = "STOCK_SPLIT"
    REVERSE_SPLIT = "REVERSE_SPLIT"
    DIVIDEND = "DIVIDEND"
    SECONDARY_OFFERING = "SECONDARY_OFFERING"
    RIGHTS = "RIGHTS"
    TENDER_OFFER = "TENDER_OFFER"
    MERGER = "MERGER"
    ACQUISITION = "ACQUISITION"
    DELISTING = "DELISTING"
    SYMBOL_CHANGE = "SYMBOL_CHANGE"


@dataclass(frozen=True)
class CorporateAction:
    action_type: CorporateActionType
    symbol: str
    effective_date: date
    ratio: float = 1.0                 # split: new shares per old share
    cash_per_share: float = 0.0        # dividend / cash merger consideration
    new_symbol: Optional[str] = None   # symbol change / acquisition


class CorporateActionEngine:
    """Applies actions to the live ledger and adjusts historical bars for
    backtests so pre-action prices remain comparable (§15)."""

    def __init__(self, ledger: Ledger) -> None:
        self.ledger = ledger
        self.applied: list[CorporateAction] = []

    def apply(self, action: CorporateAction, at: datetime) -> None:
        qty = self.ledger.position_qty(action.symbol)
        t = action.action_type

        if t in (CorporateActionType.STOCK_SPLIT, CorporateActionType.REVERSE_SPLIT):
            if action.ratio <= 0:
                raise ValueError("split ratio must be positive")
            if qty > 0:
                lot = self.ledger.positions[action.symbol]
                new_qty = lot.qty * action.ratio
                new_cost = lot.avg_cost / action.ratio
                # rewrite position via internal structure and log a zero-cash entry
                self.ledger._positions[action.symbol].qty = new_qty          # noqa: SLF001
                self.ledger._positions[action.symbol].avg_cost = new_cost    # noqa: SLF001
                self.ledger.entries.append(LedgerEntry(
                    at=at, kind=EntryKind.CORPORATE_ACTION, amount=0.0,
                    symbol=action.symbol, qty=new_qty - qty,
                    note=f"{t.value} ratio {action.ratio}"))

        elif t is CorporateActionType.DIVIDEND:
            if qty > 0 and action.cash_per_share > 0:
                self.ledger._append(LedgerEntry(                              # noqa: SLF001
                    at=at, kind=EntryKind.DIVIDEND, amount=qty * action.cash_per_share,
                    symbol=action.symbol, note="cash dividend"))

        elif t in (CorporateActionType.MERGER, CorporateActionType.ACQUISITION,
                   CorporateActionType.TENDER_OFFER):
            if qty > 0 and action.cash_per_share > 0:
                # cash-out at the deal price
                self.ledger.record_fill(action.symbol, side_qty=-qty,
                                        price=action.cash_per_share, fees=0.0, at=at,
                                        note=f"{t.value} cash-out")

        elif t is CorporateActionType.SYMBOL_CHANGE:
            if qty > 0 and action.new_symbol:
                lot = self.ledger.positions[action.symbol]
                self.ledger._positions.pop(action.symbol)                    # noqa: SLF001
                lot.symbol = action.new_symbol
                self.ledger._positions[action.new_symbol] = lot              # noqa: SLF001
                self.ledger.entries.append(LedgerEntry(
                    at=at, kind=EntryKind.CORPORATE_ACTION, amount=0.0,
                    symbol=action.new_symbol, note=f"symbol change from {action.symbol}"))

        elif t is CorporateActionType.DELISTING:
            # delisting with no consideration: position marks to zero via prices;
            # universe manager marks the symbol delisted (§12)
            self.ledger.entries.append(LedgerEntry(
                at=at, kind=EntryKind.CORPORATE_ACTION, amount=0.0,
                symbol=action.symbol, note="delisted"))

        self.applied.append(action)


def adjust_bars_for_split(bars: list[Bar], action: CorporateAction) -> list[Bar]:
    """Backtest adjustment (§15): divide pre-split prices, multiply volume,
    so history is continuous."""
    if action.action_type not in (CorporateActionType.STOCK_SPLIT,
                                  CorporateActionType.REVERSE_SPLIT):
        return bars
    out: list[Bar] = []
    for b in bars:
        if b.ts.date() < action.effective_date:
            out.append(Bar(symbol=b.symbol, ts=b.ts,
                           open=b.open / action.ratio, high=b.high / action.ratio,
                           low=b.low / action.ratio, close=b.close / action.ratio,
                           volume=int(b.volume * action.ratio),
                           vwap=(b.vwap / action.ratio) if b.vwap else None,
                           source=b.source, data_version=b.data_version + "+split"))
        else:
            out.append(b)
    return out
