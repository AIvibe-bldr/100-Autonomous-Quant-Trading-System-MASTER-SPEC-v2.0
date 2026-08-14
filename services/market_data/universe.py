"""Universe Manager (MASTER SPEC §12).

Keeps delisted companies so historical scans never suffer survivorship bias:
`symbols_as_of(date)` returns what was actually tradeable then, including
names that have since disappeared.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Optional


@dataclass(frozen=True)
class UniverseSymbol:
    symbol: str
    listed_from: date
    delisted_at: Optional[date] = None
    exchange: str = "NASDAQ"

    def active_on(self, d: date) -> bool:
        if d < self.listed_from:
            return False
        return self.delisted_at is None or d < self.delisted_at


class UniverseManager:
    def __init__(self) -> None:
        self._symbols: dict[str, UniverseSymbol] = {}

    def add(self, sym: UniverseSymbol) -> None:
        self._symbols[sym.symbol] = sym

    def symbols_as_of(self, d: date) -> list[str]:
        """Point-in-time universe: includes later-delisted names (§12)."""
        return sorted(s.symbol for s in self._symbols.values() if s.active_on(d))

    def current_symbols(self, today: date) -> list[str]:
        return self.symbols_as_of(today)

    def is_tradeable(self, symbol: str, d: date) -> bool:
        s = self._symbols.get(symbol)
        return s is not None and s.active_on(d)

    def all_ever(self) -> list[str]:
        return sorted(self._symbols)
