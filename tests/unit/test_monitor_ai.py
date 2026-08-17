"""Monitor AI tests (MASTER SPEC §66, role-assignment revision).

Covers: the deterministic anomaly trigger (Monitor is consulted only on
anomaly, not every tick), the Mock monitor's findings/severity/recommendation
mapping, fail-safe behavior when the model errors or returns malformed
output, the structural absence of any actionable field on `MonitorOutput`,
and the Claude-backed adapter's prompt construction / parsing / error paths
(no network — a fake client is injected, mirroring test_claude_adapters.py).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

import pytest

from packages.schemas.monitor import (
    MalformedMonitorError,
    MonitorOutput,
    MonitorRecommendation,
    validate_monitor_output,
)
from services.decision.monitor import (
    MockMonitorModel,
    MonitorContext,
    MonitorSupervisor,
    anomaly_present,
)
from services.supervisor.heartbeat import ServiceStatus

SESSION_TIME = datetime(2026, 8, 11, 15, 0, tzinfo=timezone.utc)


def _ctx(**overrides) -> MonitorContext:
    base = dict(now=SESSION_TIME, heartbeats={"scanner": ServiceStatus.OK,
                                              "execution": ServiceStatus.OK},
               risk_state="NORMAL", drawdown=0.02, open_positions={})
    base.update(overrides)
    return MonitorContext(**base)


# --- anomaly trigger (§66: consulted only on anomaly) -----------------------

def test_no_anomaly_when_everything_healthy():
    assert not anomaly_present(_ctx())


def test_anomaly_present_on_unhealthy_service():
    assert anomaly_present(_ctx(heartbeats={"scanner": ServiceStatus.ERROR}))


def test_anomaly_present_on_elevated_drawdown():
    assert anomaly_present(_ctx(drawdown=0.25))
    assert not anomaly_present(_ctx(drawdown=0.10))


def test_anomaly_present_on_non_normal_risk_state():
    assert anomaly_present(_ctx(risk_state="HALT_NEW_ENTRIES"))


def test_anomaly_present_on_near_miss():
    assert anomaly_present(_ctx(recent_near_misses=("wrong-side SELL blocked",)))


# --- Mock monitor -------------------------------------------------------------

def test_mock_monitor_clean_state_recommends_continue():
    out = MonitorSupervisor(model=MockMonitorModel()).review(_ctx())
    assert out.recommendation is MonitorRecommendation.CONTINUE_MONITORING
    assert out.severity == 0.0


def test_mock_monitor_flags_unhealthy_service():
    out = MonitorSupervisor(model=MockMonitorModel()).review(
        _ctx(heartbeats={"scanner": ServiceStatus.ERROR}))
    assert out.severity >= 0.9
    assert out.recommendation is MonitorRecommendation.ESCALATE_SAFE_EXIT
    assert any("scanner" in f for f in out.findings)


def test_mock_monitor_flags_severe_drawdown():
    out = MonitorSupervisor(model=MockMonitorModel()).review(_ctx(drawdown=0.32))
    assert out.severity == 1.0
    assert out.recommendation is MonitorRecommendation.ESCALATE_SAFE_EXIT


def test_mock_monitor_moderate_drawdown_queues_review():
    out = MonitorSupervisor(model=MockMonitorModel()).review(_ctx(drawdown=0.22))
    assert out.recommendation is MonitorRecommendation.QUEUE_HUMAN_REVIEW


def test_mock_monitor_near_miss_only_notifies():
    out = MonitorSupervisor(model=MockMonitorModel()).review(
        _ctx(recent_near_misses=("stale signal blocked",)))
    assert out.recommendation is MonitorRecommendation.NOTIFY


# --- fail-safe -----------------------------------------------------------------

class _ExplodingMonitorModel:
    name = "exploding-monitor"
    model_family = "exploding-family"

    def review(self, context):
        raise TimeoutError("monitor API timed out")


class _MalformedMonitorModel:
    name = "malformed-monitor"
    model_family = "malformed-family"

    def review(self, context):
        return {"findings": "not a list", "severity": 2.5}


def test_monitor_model_exception_degrades_to_queue_human_review():
    """Unlike the Pre-Trade Audit AI, the Monitor has no seat at the order
    gate — an unreachable Monitor cannot block a trade. It still must not
    vanish silently during a real anomaly, so it degrades to a finding."""
    out = MonitorSupervisor(model=_ExplodingMonitorModel()).review(_ctx(drawdown=0.25))
    assert out.recommendation is MonitorRecommendation.QUEUE_HUMAN_REVIEW
    assert "unavailable" in out.findings[0].lower()


def test_monitor_malformed_output_degrades_to_queue_human_review():
    out = MonitorSupervisor(model=_MalformedMonitorModel()).review(_ctx())
    assert out.recommendation is MonitorRecommendation.QUEUE_HUMAN_REVIEW
    assert "malformed" in out.findings[0].lower()


def test_validate_monitor_output_rejects_bad_severity():
    with pytest.raises(MalformedMonitorError):
        validate_monitor_output({
            "at": SESSION_TIME.isoformat(), "services_reviewed": (), "findings": (),
            "severity": 5.0, "recommendation": "NOTIFY",
            "model": "x", "model_family": "y"})


# --- structural: no actionable field exists at all ----------------------------

def test_monitor_output_has_no_broker_position_or_config_field():
    """Broker発注禁止/Position変更禁止/Risk Rule変更禁止/System設定変更禁止:
    the guarantee is structural — assert directly on the schema's field
    names rather than trusting that no code path happens to call one."""
    fields = set(MonitorOutput.model_fields)
    forbidden_substrings = ("broker", "order", "position", "risk_rule", "config",
                            "qty", "quantity", "symbol", "side")
    for f in fields:
        assert not any(s in f.lower() for s in forbidden_substrings), f


def test_monitor_model_protocol_has_no_broker_or_execution_handle():
    for model in (MockMonitorModel(), _ExplodingMonitorModel(), _MalformedMonitorModel()):
        assert not hasattr(model, "broker")
        assert not hasattr(model, "submit")
        assert not hasattr(model, "risk_controller")


# --- Claude adapter (fake client, no network) ---------------------------------

@dataclass
class _FakeResponse:
    parsed_output: Any
    stop_reason: str = "end_turn"


@dataclass
class _FakeMessages:
    next_response: Any = None
    next_exception: Optional[Exception] = None
    last_kwargs: dict = field(default_factory=dict)

    def parse(self, **kwargs):
        self.last_kwargs = kwargs
        if self.next_exception is not None:
            raise self.next_exception
        return self.next_response


@dataclass
class FakeAnthropicClient:
    messages: _FakeMessages = field(default_factory=_FakeMessages)


def test_claude_monitor_parses_structured_output():
    from packages.common.llm_client import AgentModel, AgentRole, Provider
    from services.decision.claude_adapters import ClaudeMonitorModel, _MonitorJudgment

    client = FakeAnthropicClient()
    client.messages.next_response = _FakeResponse(parsed_output=_MonitorJudgment(
        findings=["service X unhealthy"], severity=0.8,
        recommendation=MonitorRecommendation.ESCALATE_SAFE_EXIT))
    model = ClaudeMonitorModel(
        agent=AgentModel(role=AgentRole.MONITOR, provider=Provider.ANTHROPIC,
                         model="claude-opus-5"),
        client=client)

    raw = model.review(_ctx(heartbeats={"X": ServiceStatus.ERROR}))
    out = validate_monitor_output(raw)
    assert out.severity == 0.8
    assert out.recommendation is MonitorRecommendation.ESCALATE_SAFE_EXIT
    assert client.messages.last_kwargs["model"] == "claude-opus-5"
    assert "risk state" in client.messages.last_kwargs["messages"][0]["content"].lower()


def test_claude_monitor_none_output_raises():
    from services.decision.claude_adapters import ClaudeAPIError, ClaudeMonitorModel

    client = FakeAnthropicClient()
    client.messages.next_response = _FakeResponse(parsed_output=None, stop_reason="refusal")
    model = ClaudeMonitorModel(client=client)
    with pytest.raises(ClaudeAPIError, match="no parsed output"):
        model.review(_ctx())


def test_claude_monitor_api_failure_raises():
    from services.decision.claude_adapters import ClaudeAPIError, ClaudeMonitorModel

    client = FakeAnthropicClient()
    client.messages.next_exception = ConnectionError("network down")
    model = ClaudeMonitorModel(client=client)
    with pytest.raises(ClaudeAPIError, match="monitor call failed"):
        model.review(_ctx())


def test_claude_monitor_exception_still_degrades_via_supervisor():
    """The adapter raises (like the audit adapter); MonitorSupervisor is what
    turns that into a non-blocking QUEUE_HUMAN_REVIEW finding."""
    from services.decision.claude_adapters import ClaudeMonitorModel

    client = FakeAnthropicClient()
    client.messages.next_exception = RuntimeError("overloaded")
    model = ClaudeMonitorModel(client=client)
    out = MonitorSupervisor(model=model).review(_ctx(drawdown=0.25))
    assert out.recommendation is MonitorRecommendation.QUEUE_HUMAN_REVIEW


def test_monitor_agent_id_distinct_from_skeptic_and_audit():
    """Even though Monitor also defaults to Claude Opus (§25), it is its own
    agent id — sharing a model tier is not sharing an identity."""
    from packages.common.llm_client import DEFAULT_MODEL_CONFIG

    ids = {DEFAULT_MODEL_CONFIG.skeptic.agent_id, DEFAULT_MODEL_CONFIG.audit.agent_id,
           DEFAULT_MODEL_CONFIG.monitor.agent_id}
    assert len(ids) == 3
