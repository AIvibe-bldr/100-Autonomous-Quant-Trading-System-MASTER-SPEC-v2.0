"""Recovery Manager & Incident Postmortem (MASTER SPEC §69, §71).

Minor faults may fall back safely and auto-resume; major faults must NOT
auto-resume — human review is required (§69).  Every major incident gets an
auto-generated postmortem skeleton (§71) whose regression test becomes part
of tests/failure/.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime

from packages.common.clock import Clock


class FaultSeverity(str, enum.Enum):
    MINOR = "MINOR"      # e.g. one stale quote, transient latency
    MAJOR = "MAJOR"      # e.g. broker mismatch, UNKNOWN order, data corruption


@dataclass
class Incident:
    incident_id: str
    severity: FaultSeverity
    service: str
    description: str
    detected_at: datetime
    timeline: list[str] = field(default_factory=list)
    root_cause: str = ""
    damage: str = ""
    why_control_failed: str = ""
    corrective_action: str = ""
    regression_test: str = ""
    resolved: bool = False
    human_approved_resume: bool = False


class ResumeForbiddenError(RuntimeError):
    """Auto-resume after a major fault is forbidden (§69)."""


class RecoveryManager:
    def __init__(self, clock: Clock) -> None:
        self._clock = clock
        self.incidents: list[Incident] = []
        self._seq = 0

    def report_fault(self, service: str, description: str,
                     severity: FaultSeverity) -> Incident:
        self._seq += 1
        inc = Incident(incident_id=f"INC-{self._seq:04d}", severity=severity,
                       service=service, description=description,
                       detected_at=self._clock.now())
        inc.timeline.append(f"{inc.detected_at.isoformat()} detected: {description}")
        self.incidents.append(inc)
        return inc

    def can_auto_resume(self, incident: Incident) -> bool:
        return incident.severity is FaultSeverity.MINOR

    def resume(self, incident: Incident, human_approved: bool = False) -> None:
        if incident.severity is FaultSeverity.MAJOR and not human_approved:
            raise ResumeForbiddenError(
                f"{incident.incident_id}: major fault requires human review (§69)")
        incident.resolved = True
        incident.human_approved_resume = human_approved
        incident.timeline.append(f"{self._clock.now().isoformat()} resumed "
                                 f"(human_approved={human_approved})")

    def postmortem(self, incident: Incident) -> dict[str, object]:
        """Auto-generated postmortem structure (§71)."""
        return {
            "incident_id": incident.incident_id,
            "timeline": list(incident.timeline),
            "root_cause": incident.root_cause or "TODO: root cause analysis",
            "detection": f"detected in service {incident.service}",
            "damage": incident.damage or "TODO: quantify damage",
            "why_control_failed": incident.why_control_failed or "TODO",
            "corrective_action": incident.corrective_action or "TODO",
            "regression_test": incident.regression_test or "TODO: add to tests/failure/",
        }

    @property
    def open_major_incidents(self) -> list[Incident]:
        return [i for i in self.incidents
                if i.severity is FaultSeverity.MAJOR and not i.resolved]
