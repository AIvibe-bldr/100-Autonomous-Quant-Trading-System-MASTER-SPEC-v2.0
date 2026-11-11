"""Event source boundary (MASTER SPEC §16): where known-in-advance events come
from. A source only fetches. Failure is `EventsUnavailable`, never an empty
list that would be read as "no events"."""
from __future__ import annotations

from datetime import date
from typing import Protocol

from services.market_data.event_calendar import RiskEvent


class EventsUnavailable(RuntimeError):
    """The events could not be obtained (HTTP failure, missing credentials)."""


class EventSource(Protocol):
    def fetch(self, symbols: list[str], start: date, end: date) -> list[RiskEvent]:
        """Events dated in [start, end] for `symbols`."""
        ...
