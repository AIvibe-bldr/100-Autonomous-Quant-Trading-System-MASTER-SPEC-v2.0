"""Pre-trade audit schemas (MASTER SPEC ADDENDUM A3-A4).

`AuditOutput` is the structured verdict of the Independent Audit AI — schema
validation failure is treated as REJECT (INV-20), mirroring the Decision AI
contract (§28).

`ApprovedOrderSnapshot` freezes the risk/audit-approved order.  Its SHA-256
hash covers every execution-relevant field; the Execution Engine only submits
orders whose intent re-hashes to an approved snapshot (INV-17), and builds
the broker request exclusively from snapshot fields so it cannot "helpfully"
alter quantity or price (INV-18, A4-2).
"""
from __future__ import annotations

import enum
import hashlib
import uuid
from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from packages.schemas.core import (
    Action,
    OrderIntent,
    OrderType,
    RiskApprovedOrder,
    canonical_order_payload,
)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# ---------------------------------------------------------------------------
# Audit verdict (A3-3)
# ---------------------------------------------------------------------------

class AuditVerdict(str, enum.Enum):
    PASS = "PASS"
    REJECT = "REJECT"
    REVIEW = "REVIEW"


class DetectedConflict(StrictModel):
    check: str
    detail: str
    severity: float = Field(ge=0.0, le=1.0)


AUDIT_SCHEMA_VERSION = "audit-1.1.0"


class AuditOutput(StrictModel):
    audit_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    decision_id: str
    client_order_id: str
    verdict: AuditVerdict
    reasons: tuple[str, ...]
    detected_conflicts: tuple[DetectedConflict, ...] = ()
    severity: float = Field(ge=0.0, le=1.0)
    # §7: structured findings so Near-Miss analysis can categorize what the
    # auditor actually caught, rather than parsing free text
    semantic_mismatches: tuple[str, ...] = ()   # decision vs order contradictions
    missing_information: tuple[str, ...] = ()   # what the auditor could not verify
    risk_concerns: tuple[str, ...] = ()         # event risk, stale signal, sizing
    audit_summary: str = ""
    audit_version: str = AUDIT_SCHEMA_VERSION
    model: str
    model_family: str
    forced: bool = False
    audited_at: datetime


class MalformedAuditError(RuntimeError):
    """Audit output failed schema validation → treated as REJECT (INV-20)."""


def validate_audit_output(raw: dict[str, Any]) -> AuditOutput:
    try:
        return AuditOutput.model_validate(raw)
    except ValidationError as e:
        raise MalformedAuditError(str(e)) from e


# ---------------------------------------------------------------------------
# Immutable approved order snapshot (A4)
# ---------------------------------------------------------------------------

# The canonical form lives in packages/schemas/core.py alongside OrderIntent so
# that the risk-approval signature and this snapshot hash cannot drift onto
# different field lists — a field covered by only one of them is a field the
# other silently leaves open.
_canonical = canonical_order_payload


class ApprovedOrderSnapshot(StrictModel):
    """A4: the exact order Risk + Audit approved, frozen and hashed."""

    snapshot_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    client_order_id: str
    order_intent_id: str = ""    # §11: the intent this snapshot froze
    symbol: str
    side: Action
    qty: float = Field(gt=0)
    order_type: OrderType
    limit_price: Optional[float] = None
    stop_price: Optional[float] = None
    take_profit: Optional[float] = None
    time_in_force: str = "DAY"
    strategy_id: str = ""
    decision_id: str = ""
    skeptic_id: str = ""         # §11: which Skeptic review this order carries
    risk_approval_id: str = ""
    audit_id: str = ""
    created_at: datetime
    hash: str

    @classmethod
    def compute_hash(cls, symbol: str, side: Action, qty: float, order_type: OrderType,
                     limit_price: Optional[float], stop_price: Optional[float],
                     take_profit: Optional[float], time_in_force: str,
                     client_order_id: str) -> str:
        return hashlib.sha256(_canonical(symbol, side, qty, order_type, limit_price,
                                         stop_price, take_profit, time_in_force,
                                         client_order_id).encode()).hexdigest()

    @classmethod
    def from_approved(cls, order: RiskApprovedOrder, decision_id: str = "",
                      audit_id: str = "", take_profit: Optional[float] = None,
                      strategy_id: str = "", skeptic_id: str = "",
                      at: Optional[datetime] = None) -> "ApprovedOrderSnapshot":
        i = order.intent
        h = cls.compute_hash(i.symbol, i.side, i.qty, i.order_type, i.limit_price,
                             i.stop_price, take_profit, "DAY", i.client_order_id)
        return cls(client_order_id=i.client_order_id, order_intent_id=i.proposal_id,
                   symbol=i.symbol, side=i.side,
                   qty=i.qty, order_type=i.order_type, limit_price=i.limit_price,
                   stop_price=i.stop_price, take_profit=take_profit,
                   strategy_id=strategy_id, decision_id=decision_id,
                   skeptic_id=skeptic_id,
                   risk_approval_id=order.approval.approval_id, audit_id=audit_id,
                   created_at=at or i.created_at, hash=h)

    def matches_intent(self, intent: OrderIntent) -> bool:
        """INV-17: any change to an execution-relevant field breaks the hash."""
        recomputed = self.compute_hash(intent.symbol, intent.side, intent.qty,
                                       intent.order_type, intent.limit_price,
                                       intent.stop_price, self.take_profit,
                                       self.time_in_force, intent.client_order_id)
        return recomputed == self.hash
