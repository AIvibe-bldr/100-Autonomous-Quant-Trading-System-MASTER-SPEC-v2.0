"""Trading Calendar (MASTER SPEC §13).

V1 covers the US Regular Session only.  Open/close times must come from a
calendar *provider* (broker / exchange feed) — never hardcoded into trading
logic.  `StaticCalendarProvider` ships holiday *data* (data, not logic) and is
the stand-in until a broker adapter supplies the real calendar.

Handles: holidays, early closes, DST (via zoneinfo), halts, market-wide
circuit breaker, session status.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Optional, Protocol
from zoneinfo import ZoneInfo

from packages.common.clock import ensure_utc

ET = ZoneInfo("America/New_York")


class SessionStatus(str, enum.Enum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"
    HALTED = "HALTED"                # market-wide circuit breaker (§13)


@dataclass(frozen=True)
class SessionHours:
    open_et: time
    close_et: time


REGULAR = SessionHours(time(9, 30), time(16, 0))
EARLY_CLOSE = SessionHours(time(9, 30), time(13, 0))


class CalendarProvider(Protocol):
    """Source of market days — implemented by broker adapters (§13: ハードコード禁止)."""

    def holidays(self, year: int) -> set[date]: ...
    def early_closes(self, year: int) -> set[date]: ...


class StaticCalendarProvider:
    """NYSE holiday data for 2025-2026.  This is calendar *data* shipped with
    the repo; swap for a broker-fed provider before LIVE (§13)."""

    _HOLIDAYS = {
        2025: {date(2025, 1, 1), date(2025, 1, 20), date(2025, 2, 17), date(2025, 4, 18),
               date(2025, 5, 26), date(2025, 6, 19), date(2025, 7, 4), date(2025, 9, 1),
               date(2025, 11, 27), date(2025, 12, 25)},
        2026: {date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16), date(2026, 4, 3),
               date(2026, 5, 25), date(2026, 6, 19), date(2026, 7, 3), date(2026, 9, 7),
               date(2026, 11, 26), date(2026, 12, 25)},
    }
    _EARLY = {
        2025: {date(2025, 7, 3), date(2025, 11, 28), date(2025, 12, 24)},
        2026: {date(2026, 11, 27), date(2026, 12, 24)},
    }

    def holidays(self, year: int) -> set[date]:
        if year not in self._HOLIDAYS:
            raise ValueError(f"no calendar data for {year}; refusing to guess (§13)")
        return self._HOLIDAYS[year]

    def early_closes(self, year: int) -> set[date]:
        return self._EARLY.get(year, set())


@dataclass
class TradingCalendar:
    provider: CalendarProvider = field(default_factory=StaticCalendarProvider)
    circuit_breaker_active: bool = False

    def is_trading_day(self, d: date) -> bool:
        return d.weekday() < 5 and d not in self.provider.holidays(d.year)

    def session_hours(self, d: date) -> Optional[SessionHours]:
        if not self.is_trading_day(d):
            return None
        return EARLY_CLOSE if d in self.provider.early_closes(d.year) else REGULAR

    def status(self, at: datetime) -> SessionStatus:
        if self.circuit_breaker_active:
            return SessionStatus.HALTED
        at_et = ensure_utc(at).astimezone(ET)  # DST handled by zoneinfo
        hours = self.session_hours(at_et.date())
        if hours is None:
            return SessionStatus.CLOSED
        now_t = at_et.time()
        if hours.open_et <= now_t < hours.close_et:
            return SessionStatus.OPEN
        return SessionStatus.CLOSED

    def is_open(self, at: datetime) -> bool:
        return self.status(at) is SessionStatus.OPEN

    def next_trading_day(self, d: date) -> date:
        nxt = d + timedelta(days=1)
        while not self.is_trading_day(nxt):
            nxt += timedelta(days=1)
        return nxt
