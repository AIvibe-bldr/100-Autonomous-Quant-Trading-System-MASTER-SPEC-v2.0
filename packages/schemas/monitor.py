"""Monitor AI output schema (MASTER SPEC §66, role-assignment revision §25).

The Monitor AI is consulted only on anomaly (a heartbeat gone bad, a drawdown
breach, a reconciliation halt) and never receives a broker handle, a
position-mutation API, a risk-config write path, or a system-settings write
path. Structurally, its only possible output is advisory text plus a
recommended escalation level — there is no field here through which it could
place an order, resize a position, or change a rule.
"""
from __future__ import annotations

import enum
import uuid
from datetime import datetime

from pydantic import Field

from packages.schemas.core import StrictModel


class MonitorRecommendation(str, enum.Enum):
    """What the Monitor AI can ask a human or another system to do. It
    cannot execute any of these itself — Broker発注禁止/Position変更禁止/
    Risk Rule変更禁止/System設定変更禁止 (role-assignment revision)."""

    CONTINUE_MONITORING = "CONTINUE_MONITORING"
    NOTIFY = "NOTIFY"
    QUEUE_HUMAN_REVIEW = "QUEUE_HUMAN_REVIEW"
    ESCALATE_SAFE_EXIT = "ESCALATE_SAFE_EXIT"   # a recommendation, not an action


class MalformedMonitorError(RuntimeError):
    """Monitor output failed schema validation — treated as a REJECT-shaped
    finding by the caller, never as 'no anomaly' (mirrors INV-20)."""


class MonitorOutput(StrictModel):
    monitor_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    at: datetime
    services_reviewed: tuple[str, ...]
    findings: tuple[str, ...]
    severity: float = Field(ge=0.0, le=1.0)
    recommendation: MonitorRecommendation
    model: str
    model_family: str


def validate_monitor_output(raw: dict) -> MonitorOutput:
    from pydantic import ValidationError

    try:
        return MonitorOutput.model_validate(raw)
    except ValidationError as e:
        raise MalformedMonitorError(str(e)) from e
