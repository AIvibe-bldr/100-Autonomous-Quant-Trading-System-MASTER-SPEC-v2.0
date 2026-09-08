"""Experiment Registry (MASTER SPEC §98).

Every system change is an experiment: hypothesis, change, baseline,
challenger, period, data, result, decision, rollback.  Nothing is promoted
to LIVE without a registered experiment and a human-approved decision
(§106).
"""
from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional


class ExperimentDecision(str, enum.Enum):
    PENDING = "PENDING"
    ADOPT = "ADOPT"
    REJECT = "REJECT"
    ROLLED_BACK = "ROLLED_BACK"


@dataclass
class Experiment:
    hypothesis: str
    change: str
    baseline: str
    challenger: str
    period_start: datetime
    period_end: Optional[datetime] = None
    data_refs: dict[str, Any] = field(default_factory=dict)
    result: dict[str, Any] = field(default_factory=dict)
    decision: ExperimentDecision = ExperimentDecision.PENDING
    decided_by: str = ""
    rollback_plan: str = ""
    experiment_id: str = field(default_factory=lambda: str(uuid.uuid4()))


class ExperimentRegistry:
    def __init__(self) -> None:
        self._experiments: dict[str, Experiment] = {}

    def register(self, exp: Experiment) -> str:
        if not exp.rollback_plan:
            raise ValueError("every experiment needs a rollback plan before it starts (§98)")
        self._experiments[exp.experiment_id] = exp
        return exp.experiment_id

    def get(self, experiment_id: str) -> Experiment:
        return self._experiments[experiment_id]

    def record_result(self, experiment_id: str, result: dict[str, Any],
                      period_end: datetime) -> None:
        exp = self._experiments[experiment_id]
        exp.result = result
        exp.period_end = period_end

    def decide(self, experiment_id: str, decision: ExperimentDecision,
               decided_by: str) -> None:
        """§106: adoption into LIVE requires a human decision-maker on record."""
        if decision is ExperimentDecision.ADOPT and not decided_by:
            raise PermissionError("ADOPT requires a named human approver (§106)")
        exp = self._experiments[experiment_id]
        if not exp.result and decision is ExperimentDecision.ADOPT:
            raise ValueError("cannot adopt an experiment with no recorded result")
        exp.decision = decision
        exp.decided_by = decided_by

    def pending(self) -> list[Experiment]:
        return [e for e in self._experiments.values()
                if e.decision is ExperimentDecision.PENDING]

    def adopted(self) -> list[Experiment]:
        return [e for e in self._experiments.values()
                if e.decision is ExperimentDecision.ADOPT]
