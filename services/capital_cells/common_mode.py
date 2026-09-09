"""Common-Mode Failure Guard (docs/capital_cell_architecture.md §31,
§41 priority 14).

Detecting Dependency Concentration (too many cells relying on one market
data feed, or the whole book relying on one execution broker) must never,
by itself, trigger an automatic switch that then places LIVE orders through
a different broker. This module keeps detection and authorization
separate on purpose: `detect_dependency_concentration` only reports a
finding; nothing in this module (or anywhere else) wires that finding
directly into a broker switch.

The two dependency kinds get different treatment, matching the doc exactly:

- **Market Data Feed Failover** can be automatic — but only when backed by
  a pre-tested, passed `FailoverProcedure`.
- **Execution Broker Failover** requires all three of: Human Approval,
  Reconciliation confirmation, and a pre-tested Procedure. Missing any one
  raises `BrokerFailoverForbiddenError` rather than returning a falsy
  value, so a caller cannot silently proceed on an unchecked return —
  same "raise, don't return False" contract as
  `packages.common.ledger.Ledger.rebase_high_water_mark` requiring a named
  human approver, and `services.supervisor.recovery.ResumeForbiddenError`
  blocking auto-resume after a major fault (§69). Broker change is exactly
  that kind of major operation (§31).
"""
from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import datetime
from typing import Optional


class DependencyKind(str, enum.Enum):
    MARKET_DATA_FEED = "MARKET_DATA_FEED"
    EXECUTION_BROKER = "EXECUTION_BROKER"


class BrokerFailoverForbiddenError(RuntimeError):
    """Execution Broker Failover attempted without Human Approval +
    Reconciliation confirmation + a pre-tested Procedure (§31)."""


@dataclass(frozen=True)
class DependencyConcentration:
    """A detected finding only — carries no authority to act on itself."""

    kind: DependencyKind
    dependency_id: str
    affected_cell_ids: tuple[str, ...]
    concentration_ratio: float
    detected_at: datetime


def detect_dependency_concentration(
        kind: DependencyKind, cell_dependencies: dict[str, str], detected_at: datetime,
        threshold: float = 0.5) -> list[DependencyConcentration]:
    """`cell_dependencies`: cell_id -> the single dependency_id (feed or
    broker) it currently relies on. Flags any dependency whose share of the
    given cells meets or exceeds `threshold`."""
    if not cell_dependencies:
        return []
    if not (0.0 < threshold <= 1.0):
        raise ValueError(f"threshold must be in (0,1], got {threshold}")

    by_dependency: dict[str, list[str]] = {}
    for cell_id, dep_id in cell_dependencies.items():
        by_dependency.setdefault(dep_id, []).append(cell_id)

    total = len(cell_dependencies)
    findings = []
    for dep_id, cells in sorted(by_dependency.items()):
        ratio = len(cells) / total
        if ratio >= threshold:
            findings.append(DependencyConcentration(
                kind=kind, dependency_id=dep_id, affected_cell_ids=tuple(sorted(cells)),
                concentration_ratio=ratio, detected_at=detected_at))
    return findings


@dataclass(frozen=True)
class FailoverProcedure:
    """Evidence a failover path was actually pre-tested (§31) — required
    input to both authorization paths below, automatic or human-approved."""

    procedure_id: str
    tested_at: datetime
    passed: bool


@dataclass(frozen=True)
class FailoverApproval:
    """All the evidence §31 requires for an Execution Broker Failover."""

    approved_by: str
    approved_at: datetime
    reconciliation_confirmed: bool
    procedure: FailoverProcedure


class CommonModeFailoverGuard:
    def authorize_market_data_failover(self, concentration: DependencyConcentration,
                                       procedure: FailoverProcedure) -> bool:
        """Market Data Feed Failover may be automatic — but ONLY when
        `procedure` is a passed pre-test. Returns a plain bool (not an
        exception): declining to fail over automatically is a normal,
        expected outcome here, not a forbidden operation."""
        if concentration.kind is not DependencyKind.MARKET_DATA_FEED:
            raise ValueError(
                f"authorize_market_data_failover called with {concentration.kind.value} "
                f"concentration — use authorize_execution_broker_failover for that")
        return procedure.passed

    def authorize_execution_broker_failover(
            self, concentration: DependencyConcentration,
            approval: Optional[FailoverApproval]) -> None:
        """Raises `BrokerFailoverForbiddenError` unless ALL of Human
        Approval, Reconciliation confirmation and a passed pre-tested
        Procedure are present. Returning normally IS the authorization —
        there is no boolean to ignore."""
        if concentration.kind is not DependencyKind.EXECUTION_BROKER:
            raise ValueError(
                f"authorize_execution_broker_failover called with "
                f"{concentration.kind.value} concentration — use "
                f"authorize_market_data_failover for that")
        if approval is None:
            raise BrokerFailoverForbiddenError(
                "Execution Broker Failover requires Human Approval (§31) — none supplied")
        if not approval.approved_by.strip():
            raise BrokerFailoverForbiddenError(
                "Execution Broker Failover requires a named human approver (§31)")
        if not approval.procedure.passed:
            raise BrokerFailoverForbiddenError(
                f"failover procedure {approval.procedure.procedure_id!r} did not pass "
                f"pre-testing (§31)")
        if not approval.reconciliation_confirmed:
            raise BrokerFailoverForbiddenError(
                "Execution Broker Failover requires Reconciliation confirmation (§31)")
