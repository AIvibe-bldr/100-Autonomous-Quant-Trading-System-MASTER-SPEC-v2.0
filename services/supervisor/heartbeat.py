"""Heartbeat registry (MASTER SPEC §66-68).

Every service reports status / last_success / latency / error_rate /
queue_depth / data_age.  The supervisor is a deterministic monitor; an
Opus-class Monitor AI is only consulted on anomaly (§66) and is out of V1
scope.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import datetime, timedelta

from packages.common.clock import Clock, ensure_utc


class ServiceStatus(str, enum.Enum):
    OK = "OK"
    WARNING = "WARNING"
    ERROR = "ERROR"


@dataclass
class Heartbeat:
    service: str
    at: datetime
    status: ServiceStatus
    last_success: datetime
    latency_ms: float = 0.0
    error_rate: float = 0.0
    queue_depth: int = 0
    data_age_sec: float = 0.0


class HeartbeatRegistry:
    def __init__(self, clock: Clock, stale_after: timedelta = timedelta(minutes=5)) -> None:
        self._clock = clock
        self._stale_after = stale_after
        self._latest: dict[str, Heartbeat] = {}

    def report(self, hb: Heartbeat) -> None:
        self._latest[hb.service] = hb

    def status_of(self, service: str) -> ServiceStatus:
        hb = self._latest.get(service)
        if hb is None:
            return ServiceStatus.ERROR
        if self._clock.now() - ensure_utc(hb.at) > self._stale_after:
            return ServiceStatus.ERROR  # silent service = failed service
        return hb.status

    def overall(self) -> ServiceStatus:
        statuses = [self.status_of(s) for s in self._latest] or [ServiceStatus.ERROR]
        if ServiceStatus.ERROR in statuses:
            return ServiceStatus.ERROR
        if ServiceStatus.WARNING in statuses:
            return ServiceStatus.WARNING
        return ServiceStatus.OK

    def snapshot(self) -> dict[str, ServiceStatus]:
        return {s: self.status_of(s) for s in sorted(self._latest)}
