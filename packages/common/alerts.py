"""Alert sink (SAFETY_AUDIT F10): the one way the system tells a human that
something needs attention. The interface is deliberately a single method; the
real destination (Slack, e-mail, pager) is a deployment-time choice, so the
default does nothing and `LoggingAlertSink` is the minimal visible one.
A sink must never raise into trading logic — callers guard with `notify`."""
from __future__ import annotations

import enum
import logging
from typing import Protocol


class AlertSeverity(str, enum.Enum):
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


class AlertSink(Protocol):
    def send(self, severity: AlertSeverity, message: str) -> None: ...


class NullAlertSink:
    def send(self, severity: AlertSeverity, message: str) -> None:
        return None


class LoggingAlertSink:
    def __init__(self, logger: logging.Logger | None = None) -> None:
        self._log = logger or logging.getLogger("quant.alerts")

    def send(self, severity: AlertSeverity, message: str) -> None:
        level = logging.CRITICAL if severity is AlertSeverity.CRITICAL else logging.WARNING
        self._log.log(level, "ALERT[%s] %s", severity.value, message)


def notify(sink: AlertSink, severity: AlertSeverity, message: str) -> None:
    """Fire an alert without ever letting a broken sink interfere with the
    safety path that raised it: a failure is logged, not propagated."""
    try:
        sink.send(severity, message)
    except Exception:   # noqa: BLE001 — alerting must not break the stop it reports
        logging.getLogger("quant.alerts").exception("alert sink failed: %s", message)
