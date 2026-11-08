"""Durable incident log (MASTER SPEC §69, §71): the memory that makes "no auto-resume after a
major fault" true across process restarts.

Append-only JSON lines. Two kinds of event: a FAULT (with its postmortem skeleton) and a RESOLUTION
(named operator, reason, time). A major fault stays open until a human records a resolution; a
runner that finds an open major fault refuses to start. Fail closed: a log that exists but cannot be
read counts as an open major incident — an unreadable record must never read as "all clear"."""
from __future__ import annotations

import json
import os
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from services.supervisor.recovery import FaultSeverity, Incident

DEFAULT_PATH = "var/incidents.jsonl"
ENV_VAR = "QUANT_INCIDENT_LOG"
UNREADABLE_ID = "LOG-UNREADABLE"


class IncidentLog:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    @classmethod
    def from_env(cls) -> "IncidentLog":
        return cls(os.environ.get(ENV_VAR) or DEFAULT_PATH)

    # -- writing ---------------------------------------------------------------
    def _append(self, event: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a") as f:
            f.write(json.dumps(event, default=str) + "\n")
            f.flush()
            os.fsync(f.fileno())

    def record_fault(self, incident: Incident, postmortem: dict[str, Any]) -> None:
        self._append({"event": "fault", "incident_id": incident.incident_id,
                      "severity": incident.severity.value, "service": incident.service,
                      "description": incident.description,
                      "detected_at": incident.detected_at.isoformat(), "postmortem": postmortem})

    def record_resolution(self, incident_id: str, operator: str, reason: str, at: datetime,
                          human_approved: bool) -> None:
        operator, reason = (operator or "").strip(), (reason or "").strip()
        if not operator or not reason:
            raise ValueError("resolving an incident needs a named operator and a reason")
        if incident_id not in {i["incident_id"] for i in self.incidents()}:
            raise KeyError(f"unknown incident {incident_id}")
        self._append({"event": "resolved", "incident_id": incident_id, "operator": operator,
                      "reason": reason, "at": at.isoformat(), "human_approved": human_approved})

    # -- reading ---------------------------------------------------------------
    def _events(self) -> list[dict[str, Any]]:
        try:
            lines = self.path.read_text().splitlines()
        except FileNotFoundError:
            return []
        out = []
        for line in lines:
            if line.strip():
                out.append(json.loads(line))          # a corrupt line raises -> handled by callers
        return out

    def incidents(self) -> list[dict[str, Any]]:
        """Every incident with `resolved` and `resolution` filled in from the events."""
        try:
            events = self._events()
        except (OSError, ValueError):
            return [{"incident_id": UNREADABLE_ID, "severity": FaultSeverity.MAJOR.value,
                     "service": "incident-log", "resolved": False, "resolution": None,
                     "description": f"incident log {self.path} is unreadable — failing closed"}]
        by_id: dict[str, dict[str, Any]] = {}
        for e in events:
            if e.get("event") == "fault":
                by_id[e["incident_id"]] = {**e, "resolved": False, "resolution": None}
            elif e.get("event") == "resolved" and e.get("incident_id") in by_id:
                by_id[e["incident_id"]].update(resolved=True, resolution={
                    k: e[k] for k in ("operator", "reason", "at", "human_approved")})
        return list(by_id.values())

    def open_major(self) -> list[dict[str, Any]]:
        return [i for i in self.incidents()
                if i["severity"] == FaultSeverity.MAJOR.value and not i["resolved"]]

    def next_seq(self) -> int:
        """Number of incidents recorded so far (the next RecoveryManager starts after it)."""
        try:
            return sum(1 for e in self._events() if e.get("event") == "fault")
        except (OSError, ValueError):
            return 0
