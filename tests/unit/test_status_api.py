"""Status API tests for the Final Trade Thesis and Monitor AI panels.

Read-only contract (§39, §78): neither endpoint accepts a body or mutates
anything — both just project existing pipeline/risk-controller state.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest
from fastapi.testclient import TestClient

from apps.api.main import create_app
from packages.common.clock import FrozenClock
from services.decision.monitor import MockMonitorModel, MonitorSupervisor
from services.risk.master_controller import RiskState
from services.supervisor.heartbeat import Heartbeat, HeartbeatRegistry, ServiceStatus
from tests.conftest import SYMBOLS, build_pipeline
from services.market_data.universe import UniverseManager, UniverseSymbol

SESSION_TIME = datetime(2026, 8, 11, 15, 0, tzinfo=timezone.utc)


def _universe() -> UniverseManager:
    um = UniverseManager()
    for s in SYMBOLS:
        um.add(UniverseSymbol(symbol=s, listed_from=date(2015, 1, 1)))
    return um


@pytest.fixture
def api_pipeline():
    clock = FrozenClock(current=SESSION_TIME)
    pipeline = build_pipeline(clock, _universe())
    pipeline.run_session(clock.now())
    return pipeline


# --- /final-trade-theses ------------------------------------------------------

def test_final_trade_theses_endpoint_reflects_pipeline_state(api_pipeline):
    client = TestClient(create_app(api_pipeline))
    resp = client.get("/final-trade-theses")
    assert resp.status_code == 200
    body = resp.json()
    expected = api_pipeline.final_theses()
    assert len(body) == len(expected)
    if body:
        row = body[0]
        assert {"decision_id", "skeptic_id", "symbol", "action", "confidence",
                "disagreement_score", "skeptic_severity", "skeptic_objections",
                "created_at"} <= row.keys()
        thesis = expected[row["decision_id"]]
        assert row["skeptic_id"] == thesis.skeptic_id
        assert row["disagreement_score"] == thesis.disagreement_score
        assert 0.0 <= row["disagreement_score"] <= 1.0


def test_final_trade_theses_empty_when_no_session_ran():
    clock = FrozenClock(current=SESSION_TIME)
    pipeline = build_pipeline(clock, _universe())
    client = TestClient(create_app(pipeline))
    resp = client.get("/final-trade-theses")
    assert resp.status_code == 200
    assert resp.json() == []


def test_final_trade_theses_endpoint_is_read_only(api_pipeline):
    """No POST/PUT/DELETE exists — the dashboard can only observe."""
    client = TestClient(create_app(api_pipeline))
    for method in ("post", "put", "delete", "patch"):
        resp = getattr(client, method)("/final-trade-theses")
        assert resp.status_code == 405


# --- /monitor ------------------------------------------------------------------

def test_monitor_endpoint_not_consulted_when_healthy(api_pipeline):
    client = TestClient(create_app(api_pipeline))
    resp = client.get("/monitor")
    assert resp.status_code == 200
    body = resp.json()
    assert body["consulted"] is False
    assert body["recommendation"] == "CONTINUE_MONITORING"
    assert body["severity"] == 0.0


def test_monitor_endpoint_consulted_on_risk_state_anomaly(api_pipeline):
    api_pipeline.risk_controller.set_state(RiskState.HALT_NEW_ENTRIES, reason="test")
    client = TestClient(create_app(api_pipeline))
    resp = client.get("/monitor")
    body = resp.json()
    assert body["consulted"] is True
    assert body["recommendation"] in {"NOTIFY", "QUEUE_HUMAN_REVIEW", "ESCALATE_SAFE_EXIT"}
    assert any("HALT_NEW_ENTRIES" in f for f in body["findings"])


def test_monitor_endpoint_consulted_on_unhealthy_heartbeat(api_pipeline):
    clock = api_pipeline.clock
    hb = HeartbeatRegistry(clock=clock)
    hb.report(Heartbeat(service="scanner", at=clock.now(), status=ServiceStatus.ERROR,
                        last_success=clock.now()))
    client = TestClient(create_app(api_pipeline, heartbeats=hb))
    resp = client.get("/monitor")
    body = resp.json()
    assert body["consulted"] is True
    assert body["severity"] >= 0.9


def test_monitor_endpoint_uses_injected_supervisor(api_pipeline):
    api_pipeline.risk_controller.set_state(RiskState.HALT_NEW_ENTRIES, reason="test")
    calls: list[str] = []

    class _TrackingModel:
        name = "tracking-monitor"
        model_family = "tracking-family"

        def review(self, context):
            calls.append("called")
            return MockMonitorModel().review(context)

    client = TestClient(create_app(api_pipeline,
                                   monitor=MonitorSupervisor(model=_TrackingModel())))
    client.get("/monitor")
    assert calls == ["called"]


def test_monitor_endpoint_never_calls_model_when_no_anomaly(api_pipeline):
    """§66 cost contract: a quiet system must not spend a model call."""
    calls: list[str] = []

    class _ExplodingIfCalled:
        name = "should-not-be-called"
        model_family = "x"

        def review(self, context):
            calls.append("called")
            raise AssertionError("Monitor AI must not be consulted without an anomaly")

    client = TestClient(create_app(api_pipeline,
                                   monitor=MonitorSupervisor(model=_ExplodingIfCalled())))
    resp = client.get("/monitor")
    assert resp.status_code == 200
    assert calls == []


def test_monitor_endpoint_is_read_only(api_pipeline):
    client = TestClient(create_app(api_pipeline))
    for method in ("post", "put", "delete", "patch"):
        resp = getattr(client, method)("/monitor")
        assert resp.status_code == 405
