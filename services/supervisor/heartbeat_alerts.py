"""Heartbeat alerting (SAFETY_AUDIT F10): tell a human when a service's heartbeat
goes ERROR (missing, stale or self-reported). Edge-triggered: one CRITICAL alert
when a service ENTERS ERROR, silence while it stays there, and a WARNING-level
"recovered" note when it leaves, so a flapping or long outage cannot spam."""
from __future__ import annotations

from packages.common.alerts import AlertSeverity, AlertSink, notify
from services.supervisor.heartbeat import HeartbeatRegistry, ServiceStatus


class HeartbeatAlerter:
    def __init__(self, registry: HeartbeatRegistry, sink: AlertSink) -> None:
        self._registry = registry
        self._sink = sink
        self._in_error: set[str] = set()

    def check(self) -> list[str]:
        """Evaluate once; returns the services newly in ERROR (for the caller's log)."""
        newly: list[str] = []
        current = {s for s, st in self._registry.snapshot().items() if st is ServiceStatus.ERROR}
        for service in sorted(current - self._in_error):
            newly.append(service)
            notify(self._sink, AlertSeverity.CRITICAL,
                   f"service heartbeat ERROR: {service} (missing, stale or failing)")
        for service in sorted(self._in_error - current):
            notify(self._sink, AlertSeverity.WARNING, f"service heartbeat recovered: {service}")
        self._in_error = current
        return newly
