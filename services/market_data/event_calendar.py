"""Event Calendar (MASTER SPEC §16).

Tracks known-in-advance risk events: earnings, economic releases, FOMC,
company events, FDA dates, hearings, expiration events.  Feeds overnight gap
risk (§40): `has_event_before(symbol, horizon)` is what
`services/risk/gap_risk.gap_risk_score` consumes.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional


class EventType(str, enum.Enum):
    EARNINGS = "EARNINGS"
    ECONOMIC_RELEASE = "ECONOMIC_RELEASE"
    FOMC = "FOMC"
    COMPANY_EVENT = "COMPANY_EVENT"
    FDA_DATE = "FDA_DATE"
    HEARING = "HEARING"
    EXPIRATION = "EXPIRATION"


# market-wide events apply to every symbol
_MARKET_WIDE = {EventType.ECONOMIC_RELEASE, EventType.FOMC, EventType.EXPIRATION}


@dataclass(frozen=True)
class RiskEvent:
    event_type: EventType
    on: date
    symbol: Optional[str] = None       # None = market-wide
    description: str = ""

    @property
    def market_wide(self) -> bool:
        return self.symbol is None or self.event_type in _MARKET_WIDE


class EventCalendar:
    def __init__(self) -> None:
        self._events: list[RiskEvent] = []

    def add(self, event: RiskEvent) -> None:
        self._events.append(event)

    def events_between(self, start: date, end: date,
                       symbol: Optional[str] = None) -> list[RiskEvent]:
        out = []
        for e in self._events:
            if not (start <= e.on <= end):
                continue
            if e.market_wide or symbol is None or e.symbol == symbol:
                out.append(e)
        return sorted(out, key=lambda e: e.on)

    def has_event_before(self, symbol: str, as_of: date, horizon_days: int) -> bool:
        """Gap-risk input (§16 → §40): any event for this symbol (or market-
        wide) inside the holding horizon."""
        return bool(self.events_between(as_of, as_of + timedelta(days=horizon_days),
                                        symbol=symbol))
