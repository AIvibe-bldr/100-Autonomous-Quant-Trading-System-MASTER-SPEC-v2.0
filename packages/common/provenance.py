"""Decision Provenance (MASTER SPEC §75).

Every decision is stored with everything needed to reproduce it exactly:
inputs, news refs, data timestamps, model + prompt versions, outputs, the
skeptic's view, the risk verdict, stop plan, size, order, fills and result.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from packages.common.clock import utcnow
from packages.common.environment import Environment


@dataclass
class ProvenanceRecord:
    decision_id: str
    environment: Environment
    created_at: datetime = field(default_factory=utcnow)
    input_features: dict[str, Any] = field(default_factory=dict)
    news_refs: list[dict[str, str]] = field(default_factory=list)
    data_timestamps: dict[str, str] = field(default_factory=dict)
    model: str = ""
    model_version: str = ""
    prompt_version: str = ""
    output: Optional[dict[str, Any]] = None
    skeptic_output: Optional[dict[str, Any]] = None
    risk_decision: Optional[dict[str, Any]] = None
    stop_plan: Optional[dict[str, Any]] = None
    position_size: Optional[dict[str, Any]] = None
    order_ref: Optional[str] = None
    fill_refs: list[str] = field(default_factory=list)
    result: Optional[dict[str, Any]] = None

    @property
    def is_audit_complete(self) -> bool:
        """An executed decision must carry the full chain (§75, §105 audit completeness)."""
        if self.order_ref is None:
            return self.output is not None and self.risk_decision is not None
        return all([self.output is not None, self.risk_decision is not None,
                    self.stop_plan is not None, self.position_size is not None])


class ProvenanceStore:
    def __init__(self, environment: Environment) -> None:
        self.environment = environment
        self._records: dict[str, ProvenanceRecord] = {}

    def open(self, decision_id: str) -> ProvenanceRecord:
        rec = ProvenanceRecord(decision_id=decision_id, environment=self.environment)
        self._records[decision_id] = rec
        return rec

    def get(self, decision_id: str) -> ProvenanceRecord:
        return self._records[decision_id]

    def all(self) -> list[ProvenanceRecord]:
        return list(self._records.values())

    def audit_completeness(self) -> float:
        recs = [r for r in self._records.values() if r.order_ref is not None]
        if not recs:
            return 1.0
        return sum(1 for r in recs if r.is_audit_complete) / len(recs)

    def export_json(self, path: Path) -> None:
        def default(o: Any) -> Any:
            if isinstance(o, datetime):
                return o.isoformat()
            if isinstance(o, Environment):
                return o.value
            return str(o)

        payload = [vars(r) for r in self._records.values()]
        path.write_text(json.dumps(payload, default=default, indent=2))
