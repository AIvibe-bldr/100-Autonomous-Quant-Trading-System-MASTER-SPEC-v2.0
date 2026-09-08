"""Pre-Trade Audit Log & Near-Miss learning (MASTER SPEC ADDENDUM A5, A6).

Every order attempt is logged end-to-end — decision, audit verdict, risk
verdict, approved snapshot hash, broker submission/ack, fills — so "why did
this order reach the broker?" is fully reconstructable (A5).

Rejections are not garbage: every audit/risk/execution rejection is a
prevented near-miss, aggregated for the safety panel (A6: 今月防止した誤発注)
and fed back into the improvement loop alongside real incidents (§71).
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from packages.common.clock import ensure_utc


class NearMissKind(str, enum.Enum):
    WRONG_SIDE = "WRONG_SIDE"
    QUANTITY_ERROR = "QUANTITY_ERROR"
    DUPLICATE = "DUPLICATE"
    STALE_ORDER = "STALE_ORDER"
    SYMBOL_MISMATCH = "SYMBOL_MISMATCH"
    NO_STOP = "NO_STOP"
    HASH_MISMATCH = "HASH_MISMATCH"
    OTHER = "OTHER"


class Stage(str, enum.Enum):
    AUDIT = "AUDIT"
    RISK = "RISK"
    EXECUTION = "EXECUTION"


_CONFLICT_TO_KIND = {
    "side_match": NearMissKind.WRONG_SIDE,
    "quantity_consistency": NearMissKind.QUANTITY_ERROR,
    "quantity_magnitude": NearMissKind.QUANTITY_ERROR,
    "symbol_match": NearMissKind.SYMBOL_MISMATCH,
    "stop_exists": NearMissKind.NO_STOP,
    "stale_signal": NearMissKind.STALE_ORDER,
}


@dataclass
class NearMiss:
    at: datetime
    kind: NearMissKind
    stage: Stage
    decision_id: str = ""
    client_order_id: str = ""
    detail: str = ""


@dataclass
class HumanReviewItem:
    """§8: an audit REVIEW verdict parks the order here instead of sending it.

    Nothing in this module can approve an item — resolution is a human action
    recorded via `resolve_human_review`, and a resolved item still has to go
    back through Audit + Risk as a new order intent (§12).
    """

    at: datetime
    client_order_id: str
    decision_id: str
    reasons: list[str] = field(default_factory=list)
    severity: float = 0.0
    resolved_by: str = ""
    resolution: str = ""      # "" while pending


@dataclass
class PreTradeRecord:
    """A5: one row per order attempt, filled in as the pipeline progresses."""

    client_order_id: str
    decision_id: str
    created_at: datetime
    decision_summary: dict[str, Any] = field(default_factory=dict)
    audit_result: Optional[dict[str, Any]] = None
    risk_result: Optional[dict[str, Any]] = None
    approved_snapshot_hash: str = ""
    broker_submitted: bool = False
    broker_ack: Optional[dict[str, Any]] = None
    fills: list[str] = field(default_factory=list)
    final_state: str = ""
    # protective exits carry no Decision to compare against, so semantic audit
    # does not apply (they still pass the deterministic risk controller)
    protective_exit: bool = False


class PreTradeAuditLog:
    def __init__(self) -> None:
        self.records: dict[str, PreTradeRecord] = {}
        self.near_misses: list[NearMiss] = []
        self.human_review_queue: list[HumanReviewItem] = []

    # -- A5 -----------------------------------------------------------------
    def open(self, client_order_id: str, decision_id: str, at: datetime,
             decision_summary: dict[str, Any]) -> PreTradeRecord:
        rec = PreTradeRecord(client_order_id=client_order_id, decision_id=decision_id,
                             created_at=ensure_utc(at), decision_summary=decision_summary)
        self.records[client_order_id] = rec
        return rec

    def get(self, client_order_id: str) -> PreTradeRecord:
        return self.records[client_order_id]

    def is_fully_traceable(self, client_order_id: str) -> bool:
        """A5: an order that reached the broker must carry the full chain."""
        r = self.records[client_order_id]
        if not r.broker_submitted:
            return True
        audited = r.audit_result is not None or r.protective_exit
        return all([audited, r.risk_result is not None,
                    r.approved_snapshot_hash != "", r.final_state != ""])

    # -- A6 -----------------------------------------------------------------
    def record_near_miss(self, stage: Stage, kind: NearMissKind, at: datetime,
                         decision_id: str = "", client_order_id: str = "",
                         detail: str = "") -> None:
        self.near_misses.append(NearMiss(at=ensure_utc(at), kind=kind, stage=stage,
                                         decision_id=decision_id,
                                         client_order_id=client_order_id, detail=detail))

    # -- §8: REVIEW verdict --------------------------------------------------
    def queue_human_review(self, client_order_id: str, decision_id: str, at: datetime,
                           reasons: list[str], severity: float) -> HumanReviewItem:
        """Park an order for human review. It is NOT sent to the broker."""
        item = HumanReviewItem(at=ensure_utc(at), client_order_id=client_order_id,
                               decision_id=decision_id, reasons=list(reasons),
                               severity=severity)
        self.human_review_queue.append(item)
        return item

    def resolve_human_review(self, client_order_id: str, resolved_by: str,
                             resolution: str) -> None:
        """Record a human's decision. Resolving does NOT release the order —
        acting on it requires a fresh order intent through Audit + Risk (§12)."""
        if not resolved_by:
            raise PermissionError("human review requires a named reviewer (§8)")
        for item in self.human_review_queue:
            if item.client_order_id == client_order_id and not item.resolution:
                item.resolved_by = resolved_by
                item.resolution = resolution
                return
        raise KeyError(f"no pending review for {client_order_id}")

    def pending_human_reviews(self) -> list[HumanReviewItem]:
        return [i for i in self.human_review_queue if not i.resolution]

    def record_audit_rejection(self, audit_result: dict[str, Any], at: datetime,
                               decision_id: str, client_order_id: str) -> None:
        """Map audit conflicts to near-miss kinds (A6)."""
        conflicts = audit_result.get("detected_conflicts", [])
        kinds = {_CONFLICT_TO_KIND.get(c.get("check", ""), NearMissKind.OTHER)
                 for c in conflicts} or {NearMissKind.OTHER}
        for kind in kinds:
            self.record_near_miss(Stage.AUDIT, kind, at, decision_id, client_order_id,
                                  detail="; ".join(audit_result.get("reasons", [])))

    def monthly_safety_summary(self, year: int, month: int) -> dict[str, Any]:
        """A7 発注安全性 panel data."""
        def in_month(at: datetime) -> bool:
            return (at.year, at.month) == (year, month)

        month_records = [r for r in self.records.values() if in_month(r.created_at)]
        audits = [r for r in month_records if r.audit_result is not None]
        passes = [r for r in audits if r.audit_result.get("verdict") == "PASS"]
        rejects = [r for r in audits if r.audit_result.get("verdict") == "REJECT"]
        misses = [n for n in self.near_misses if in_month(n.at)]
        by_kind: dict[str, int] = {}
        for n in misses:
            by_kind[n.kind.value] = by_kind.get(n.kind.value, 0) + 1
        return {
            "month": f"{year:04d}-{month:02d}",
            "pre_trade_audits": len(audits),
            "audit_pass": len(passes),
            "audit_reject": len(rejects),
            "prevented_potential_errors": len(misses),
            "prevented_by_kind": by_kind,
            "critical_execution_errors": sum(
                1 for r in month_records if r.final_state == "UNKNOWN"),
        }
