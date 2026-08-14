"""Unified Clock (MASTER SPEC §9).

Every event carries at minimum:
    event_time     — when the event happened in the market/world
    source_time    — timestamp claimed by the data source
    received_time  — when our system received it
    processed_time — when our system finished processing it

All internal storage is UTC.  UI layers may render JST / ET.
The four timestamps let us track latency and future leakage.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def ensure_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        raise ValueError("naive datetime forbidden; all timestamps must be timezone-aware UTC")
    return dt.astimezone(timezone.utc)


@dataclass(frozen=True)
class EventTimes:
    event_time: datetime
    source_time: datetime
    received_time: datetime
    processed_time: Optional[datetime] = None

    def __post_init__(self) -> None:
        for name in ("event_time", "source_time", "received_time"):
            object.__setattr__(self, name, ensure_utc(getattr(self, name)))
        if self.processed_time is not None:
            object.__setattr__(self, "processed_time", ensure_utc(self.processed_time))

    def with_processed(self, at: Optional[datetime] = None) -> "EventTimes":
        return EventTimes(
            event_time=self.event_time,
            source_time=self.source_time,
            received_time=self.received_time,
            processed_time=at or utcnow(),
        )

    @property
    def ingest_latency_sec(self) -> float:
        return (self.received_time - self.event_time).total_seconds()

    @property
    def is_future_leak(self) -> bool:
        """Data claiming to be from the future relative to receipt is suspect (§9, §10)."""
        return self.event_time > self.received_time


class Clock:
    """Injectable clock so replay/backtest can control time (§62)."""

    def now(self) -> datetime:
        return utcnow()


@dataclass
class FrozenClock(Clock):
    """Deterministic clock for tests, backtests and replay."""

    current: datetime = field(default_factory=utcnow)

    def now(self) -> datetime:
        return ensure_utc(self.current)

    def advance(self, seconds: float) -> None:
        from datetime import timedelta

        self.current = self.current + timedelta(seconds=seconds)
