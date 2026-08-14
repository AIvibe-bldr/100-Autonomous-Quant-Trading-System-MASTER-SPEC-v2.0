"""Point-in-Time Store (MASTER SPEC §10).

Backtests may only see data that was knowable at the as-of moment.  Every
record is stored with the time we *received* it; queries filter on that, so a
later revision of a fundamental value never leaks into the past.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

from packages.common.clock import ensure_utc


@dataclass(frozen=True)
class PitRecord:
    key: str
    value: Any
    event_time: datetime
    received_time: datetime
    source: str


class PointInTimeStore:
    def __init__(self) -> None:
        self._records: dict[str, list[PitRecord]] = {}

    def put(self, key: str, value: Any, event_time: datetime, received_time: datetime,
            source: str = "unknown") -> None:
        rec = PitRecord(key=key, value=value, event_time=ensure_utc(event_time),
                        received_time=ensure_utc(received_time), source=source)
        self._records.setdefault(key, []).append(rec)
        self._records[key].sort(key=lambda r: r.received_time)

    def get_as_of(self, key: str, as_of: datetime) -> Optional[PitRecord]:
        """Latest record that had been RECEIVED by `as_of` — revisions received
        later are invisible, preventing future leakage (§10)."""
        as_of = ensure_utc(as_of)
        best: Optional[PitRecord] = None
        for rec in self._records.get(key, []):
            if rec.received_time <= as_of:
                best = rec
            else:
                break
        return best

    def history(self, key: str) -> list[PitRecord]:
        return list(self._records.get(key, []))
