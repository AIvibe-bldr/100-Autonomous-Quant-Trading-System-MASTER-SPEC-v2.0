"""Monitor AI (MASTER SPEC §66, role-assignment revision §25).

A deterministic `HeartbeatRegistry` (services/supervisor/heartbeat.py) already
watches every service and can compute `overall()` without any AI. The Monitor
AI is not a replacement for that — it is consulted only when the deterministic
supervisor already sees an anomaly, to turn a pile of heartbeats/drawdown/
open-position numbers into a human-readable assessment and an escalation
recommendation. It holds no broker access, no position-mutation API, no
risk-config write path, and no system-settings write path (role-assignment
revision): whatever it says, the worst it can do is ask a human to look, or
recommend SAFE_EXIT to whichever component actually has that authority.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from packages.common.clock import ensure_utc
from packages.schemas.monitor import (
    MalformedMonitorError,
    MonitorOutput,
    MonitorRecommendation,
    validate_monitor_output,
)
from services.supervisor.heartbeat import ServiceStatus


@dataclass(frozen=True)
class MonitorContext:
    """Read-only snapshot the Monitor AI reasons over. Nothing in this object
    is a handle to anything — it is data, copied out of the live registries
    before the call, exactly like `AuditContext` and `DecisionContext`."""

    now: datetime
    heartbeats: dict[str, ServiceStatus]
    risk_state: str
    drawdown: float
    open_positions: dict[str, float]
    recent_near_misses: tuple[str, ...] = ()


def anomaly_present(ctx: MonitorContext, drawdown_review_at: float = 0.20) -> bool:
    """§66: the Monitor AI is consulted only on anomaly, not on every tick."""
    return (any(s is not ServiceStatus.OK for s in ctx.heartbeats.values())
            or ctx.drawdown >= drawdown_review_at
            or ctx.risk_state != "NORMAL"
            or bool(ctx.recent_near_misses))


class MonitorModel(Protocol):
    name: str
    model_family: str

    def review(self, context: MonitorContext) -> dict[str, Any]:
        """Returns raw JSON; caller MUST pass it through validate_monitor_output."""
        ...


class MockMonitorModel:
    """Deterministic stand-in: narrates the same signals `anomaly_present`
    already looked at, so V1 has a monitor without needing an LLM call."""

    name = "mock-monitor"
    model_family = "mock-monitor-family"

    def review(self, context: MonitorContext) -> dict[str, Any]:
        findings: list[str] = []
        severity = 0.0

        bad = {s: st for s, st in context.heartbeats.items() if st is not ServiceStatus.OK}
        if bad:
            findings.append(
                f"{len(bad)} service(s) unhealthy: "
                + ", ".join(f"{s}={st.value}" for s, st in sorted(bad.items())))
            severity = max(severity, 0.9 if any(
                st is ServiceStatus.ERROR for st in bad.values()) else 0.5)

        if context.drawdown >= 0.30:
            findings.append(f"drawdown {context.drawdown:.1%} at SAFE_MODE threshold")
            severity = max(severity, 1.0)
        elif context.drawdown >= 0.20:
            findings.append(f"drawdown {context.drawdown:.1%} elevated")
            severity = max(severity, 0.6)

        if context.risk_state != "NORMAL":
            findings.append(f"risk state is {context.risk_state}, not NORMAL")
            severity = max(severity, 0.7)

        for nm in context.recent_near_misses:
            findings.append(f"near-miss: {nm}")
            severity = max(severity, 0.4)

        if not findings:
            findings.append("no anomaly detected")
            recommendation = MonitorRecommendation.CONTINUE_MONITORING
        elif severity >= 0.9:
            recommendation = MonitorRecommendation.ESCALATE_SAFE_EXIT
        elif severity >= 0.5:
            recommendation = MonitorRecommendation.QUEUE_HUMAN_REVIEW
        else:
            recommendation = MonitorRecommendation.NOTIFY

        return {
            "at": context.now.isoformat(),
            "services_reviewed": tuple(sorted(context.heartbeats)),
            "findings": tuple(findings),
            "severity": severity,
            "recommendation": recommendation.value,
            "model": self.name,
            "model_family": self.model_family,
        }


@dataclass
class MonitorSupervisor:
    """Wraps a MonitorModel with validation and unavailability policy.

    Unlike the Pre-Trade Audit AI, an unavailable Monitor never blocks a
    trade — it has no seat at that gate. Unavailability during a real anomaly
    still needs to go somewhere, so it degrades to a QUEUE_HUMAN_REVIEW
    finding rather than silently vanishing."""

    model: MonitorModel

    def review(self, context: MonitorContext) -> MonitorOutput:
        now = ensure_utc(context.now)
        try:
            raw = self.model.review(context)
        except Exception as e:
            return MonitorOutput(
                at=now, services_reviewed=tuple(sorted(context.heartbeats)),
                findings=(f"Monitor AI unavailable during anomaly review: {e}",),
                severity=0.7, recommendation=MonitorRecommendation.QUEUE_HUMAN_REVIEW,
                model=getattr(self.model, "name", "?"),
                model_family=getattr(self.model, "model_family", "?"))
        try:
            return validate_monitor_output(raw)
        except MalformedMonitorError as e:
            return MonitorOutput(
                at=now, services_reviewed=tuple(sorted(context.heartbeats)),
                findings=(f"malformed monitor output rejected: {e}",),
                severity=0.6, recommendation=MonitorRecommendation.QUEUE_HUMAN_REVIEW,
                model=getattr(self.model, "name", "?"),
                model_family=getattr(self.model, "model_family", "?"))
