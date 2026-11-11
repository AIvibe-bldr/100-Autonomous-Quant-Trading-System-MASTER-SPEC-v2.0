"""Combine several event sources (same contract as services/news/composite.py)."""
from __future__ import annotations

from datetime import date

from services.market_data.event_calendar import RiskEvent
from services.market_data.event_source import EventSource, EventsUnavailable


class CompositeEventSource:
    def __init__(self, sources: list[EventSource]) -> None:
        self._sources = sources
        self.last_errors: list[str] = []

    def fetch(self, symbols: list[str], start: date, end: date) -> list[RiskEvent]:
        self.last_errors = []
        events: list[RiskEvent] = []
        for src in self._sources:
            try:
                events.extend(src.fetch(symbols, start, end))
            except EventsUnavailable as e:
                self.last_errors.append(str(e))
        if len(self.last_errors) == len(self._sources):
            raise EventsUnavailable("; ".join(self.last_errors) or "no event sources")
        return sorted(events, key=lambda e: (e.on, e.symbol or ""))
