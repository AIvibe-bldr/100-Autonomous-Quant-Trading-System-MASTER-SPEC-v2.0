"""Common-Mode Failure Guard tests (docs/capital_cell_architecture.md §31,
§41 priority 14)."""
from __future__ import annotations

import dataclasses
from datetime import datetime, timezone

import pytest

from services.capital_cells.common_mode import (
    BrokerFailoverForbiddenError,
    CommonModeFailoverGuard,
    DependencyConcentration,
    DependencyKind,
    FailoverApproval,
    FailoverProcedure,
    detect_dependency_concentration,
)

AT = datetime(2026, 8, 10, 15, 0, tzinfo=timezone.utc)


class TestDetectDependencyConcentration:
    def test_flags_dependency_above_threshold(self):
        deps = {"A": "broker-x", "B": "broker-x", "C": "broker-y"}
        findings = detect_dependency_concentration(
            DependencyKind.EXECUTION_BROKER, deps, detected_at=AT, threshold=0.5)
        assert len(findings) == 1
        assert findings[0].dependency_id == "broker-x"
        assert findings[0].concentration_ratio == pytest.approx(2 / 3)
        assert findings[0].affected_cell_ids == ("A", "B")

    def test_below_threshold_not_flagged(self):
        deps = {"A": "broker-x", "B": "broker-y", "C": "broker-z"}
        findings = detect_dependency_concentration(
            DependencyKind.EXECUTION_BROKER, deps, detected_at=AT, threshold=0.5)
        assert findings == []

    def test_empty_dependencies_returns_no_findings(self):
        assert detect_dependency_concentration(
            DependencyKind.MARKET_DATA_FEED, {}, detected_at=AT) == []

    def test_invalid_threshold_rejected(self):
        with pytest.raises(ValueError):
            detect_dependency_concentration(
                DependencyKind.EXECUTION_BROKER, {"A": "x"}, detected_at=AT, threshold=0.0)
        with pytest.raises(ValueError):
            detect_dependency_concentration(
                DependencyKind.EXECUTION_BROKER, {"A": "x"}, detected_at=AT, threshold=1.5)

    def test_detection_alone_has_no_side_effects(self):
        """§31: detecting concentration must never itself change anything.
        Structural check: DependencyConcentration is a plain frozen
        dataclass with only report fields, nothing that can trigger an
        action."""
        deps = {"A": "broker-x", "B": "broker-x"}
        findings = detect_dependency_concentration(
            DependencyKind.EXECUTION_BROKER, deps, detected_at=AT, threshold=0.5)
        field_names = {f.name for f in dataclasses.fields(findings[0])}
        assert field_names == {"kind", "dependency_id", "affected_cell_ids",
                               "concentration_ratio", "detected_at"}


class TestMarketDataFeedFailover:
    def _concentration(self) -> DependencyConcentration:
        return DependencyConcentration(
            kind=DependencyKind.MARKET_DATA_FEED, dependency_id="polygon-primary",
            affected_cell_ids=("A", "B"), concentration_ratio=0.9, detected_at=AT)

    def test_passed_procedure_authorizes_automatic_failover(self):
        guard = CommonModeFailoverGuard()
        procedure = FailoverProcedure(procedure_id="mdf-1", tested_at=AT, passed=True)
        assert guard.authorize_market_data_failover(self._concentration(), procedure) is True

    def test_unpassed_procedure_does_not_authorize(self):
        guard = CommonModeFailoverGuard()
        procedure = FailoverProcedure(procedure_id="mdf-1", tested_at=AT, passed=False)
        assert guard.authorize_market_data_failover(self._concentration(), procedure) is False

    def test_wrong_dependency_kind_rejected(self):
        guard = CommonModeFailoverGuard()
        broker_concentration = DependencyConcentration(
            kind=DependencyKind.EXECUTION_BROKER, dependency_id="alpaca",
            affected_cell_ids=("A",), concentration_ratio=1.0, detected_at=AT)
        procedure = FailoverProcedure(procedure_id="mdf-1", tested_at=AT, passed=True)
        with pytest.raises(ValueError):
            guard.authorize_market_data_failover(broker_concentration, procedure)


class TestExecutionBrokerFailover:
    def _concentration(self) -> DependencyConcentration:
        return DependencyConcentration(
            kind=DependencyKind.EXECUTION_BROKER, dependency_id="alpaca-live",
            affected_cell_ids=("A", "B", "C"), concentration_ratio=1.0, detected_at=AT)

    def _passed_procedure(self) -> FailoverProcedure:
        return FailoverProcedure(procedure_id="broker-failover-1", tested_at=AT, passed=True)

    def test_full_approval_authorizes_without_raising(self):
        guard = CommonModeFailoverGuard()
        approval = FailoverApproval(approved_by="ops-lead", approved_at=AT,
                                    reconciliation_confirmed=True,
                                    procedure=self._passed_procedure())
        guard.authorize_execution_broker_failover(self._concentration(), approval)  # no raise

    def test_no_approval_forbidden(self):
        """§31: automatic broker switch to a different broker for LIVE
        orders is exactly what must never happen from detection alone."""
        guard = CommonModeFailoverGuard()
        with pytest.raises(BrokerFailoverForbiddenError):
            guard.authorize_execution_broker_failover(self._concentration(), None)

    def test_unnamed_approver_forbidden(self):
        guard = CommonModeFailoverGuard()
        approval = FailoverApproval(approved_by="   ", approved_at=AT,
                                    reconciliation_confirmed=True,
                                    procedure=self._passed_procedure())
        with pytest.raises(BrokerFailoverForbiddenError):
            guard.authorize_execution_broker_failover(self._concentration(), approval)

    def test_unpassed_procedure_forbidden_even_with_human_approval(self):
        guard = CommonModeFailoverGuard()
        approval = FailoverApproval(
            approved_by="ops-lead", approved_at=AT, reconciliation_confirmed=True,
            procedure=FailoverProcedure(procedure_id="p", tested_at=AT, passed=False))
        with pytest.raises(BrokerFailoverForbiddenError):
            guard.authorize_execution_broker_failover(self._concentration(), approval)

    def test_missing_reconciliation_confirmation_forbidden(self):
        guard = CommonModeFailoverGuard()
        approval = FailoverApproval(approved_by="ops-lead", approved_at=AT,
                                    reconciliation_confirmed=False,
                                    procedure=self._passed_procedure())
        with pytest.raises(BrokerFailoverForbiddenError):
            guard.authorize_execution_broker_failover(self._concentration(), approval)

    def test_wrong_dependency_kind_rejected(self):
        guard = CommonModeFailoverGuard()
        feed_concentration = DependencyConcentration(
            kind=DependencyKind.MARKET_DATA_FEED, dependency_id="polygon",
            affected_cell_ids=("A",), concentration_ratio=1.0, detected_at=AT)
        approval = FailoverApproval(approved_by="ops-lead", approved_at=AT,
                                    reconciliation_confirmed=True,
                                    procedure=self._passed_procedure())
        with pytest.raises(ValueError):
            guard.authorize_execution_broker_failover(feed_concentration, approval)
