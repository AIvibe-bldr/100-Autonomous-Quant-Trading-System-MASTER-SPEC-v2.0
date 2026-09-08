"""Tests for the real OpenAI-backed Decision AI adapter (MASTER SPEC §25).

No network access and no API key required: a FakeOpenAIClient shaped like
`openai.OpenAI().beta.chat.completions.parse(...)` is injected directly
(dependency injection in the adapter constructor), mirroring
test_claude_adapters.py. Also covers `resolve_decision_agent` (the seam that
picks OpenAI when a credential is configured and falls back to Claude
otherwise) and `build_llm_stack`'s provider-based wiring.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

import pytest

from packages.common.llm_client import (
    DEFAULT_MODEL_CONFIG,
    AgentModel,
    AgentRole,
    LLMCallError,
    LLMModelConfig,
    Provider,
    resolve_decision_agent,
)
from services.decision.models import DecisionContext
from services.decision.openai_adapters import OpenAIAPIError, OpenAIDecisionModel
from services.quant.scanner import ScanResult

SESSION_TIME = datetime(2026, 8, 11, 15, 0, tzinfo=timezone.utc)


# --- fake openai client -----------------------------------------------------

@dataclass
class _FakeMessage:
    parsed: Any = None
    refusal: Optional[str] = None


@dataclass
class _FakeChoice:
    message: _FakeMessage


@dataclass
class _FakeCompletion:
    choices: list


@dataclass
class _FakeParse:
    next_completion: Any = None
    next_exception: Optional[Exception] = None
    last_kwargs: dict = field(default_factory=dict)

    def __call__(self, **kwargs):
        self.last_kwargs = kwargs
        if self.next_exception is not None:
            raise self.next_exception
        return self.next_completion


@dataclass
class _FakeChatCompletions:
    parse: _FakeParse = field(default_factory=_FakeParse)


@dataclass
class _FakeChat:
    completions: _FakeChatCompletions = field(default_factory=_FakeChatCompletions)


@dataclass
class _FakeBeta:
    chat: _FakeChat = field(default_factory=_FakeChat)


@dataclass
class FakeOpenAIClient:
    beta: _FakeBeta = field(default_factory=_FakeBeta)


def _scan(symbol: str = "AAPL") -> ScanResult:
    from packages.schemas.core import Bar

    bars = tuple(Bar(symbol=symbol, ts=SESSION_TIME, open=100, high=101, low=99,
                     close=100, volume=1_000_000) for _ in range(21))
    return ScanResult(symbol=symbol, last_close=100.0, momentum_20d=0.05,
                      dollar_volume=5_000_000, volatility=0.02, score=1.5, bars=bars)


def _ctx() -> DecisionContext:
    return DecisionContext(scan=_scan(), regime="BULL", portfolio_summary={"cash": 1000.0})


def _decision_payload(symbol: str = "AAPL") -> dict:
    return {
        "symbol": symbol, "action": "BUY", "confidence": 0.7, "expected_horizon": "1w",
        "expected_return_range": (-0.05, 0.10),
        "bull_case": {"description": "b", "target_price": 110, "probability": 0.3},
        "base_case": {"description": "n", "target_price": 102, "probability": 0.5},
        "bear_case": {"description": "d", "target_price": 94, "probability": 0.2},
        "key_evidence": ["e1"], "counter_evidence": ["c1"], "risk_factors": ["r1"],
        "invalidation_conditions": ["i1"], "unknowns": ["u1"], "decision_version": "ignored",
    }


class _FakeParsedDecision:
    """Stands in for the pydantic model OpenAI's SDK would return already
    parsed — `.model_dump(mode='json')` is all the adapter calls on it."""

    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def model_dump(self, mode: str = "python") -> dict:
        return dict(self._payload)


# --- OpenAIDecisionModel ------------------------------------------------------

def test_decision_parses_structured_output():
    client = FakeOpenAIClient()
    client.beta.chat.completions.parse.next_completion = _FakeCompletion(
        choices=[_FakeChoice(message=_FakeMessage(parsed=_FakeParsedDecision(
            _decision_payload("AAPL"))))])
    model = OpenAIDecisionModel(
        agent=AgentModel(role=AgentRole.DECISION, provider=Provider.OPENAI,
                         model="gpt-5.6-sol"),
        client=client)

    raw = model.decide(_ctx())
    assert raw["symbol"] == "AAPL"
    # decision_version is bookkeeping the adapter overwrites, never trusted
    # from the model's own output (§28) — same contract as the Claude adapter
    assert raw["decision_version"].startswith("gpt-5.6-sol:")
    assert client.beta.chat.completions.parse.last_kwargs["model"] == "gpt-5.6-sol"
    assert "AAPL" in client.beta.chat.completions.parse.last_kwargs["messages"][1]["content"]


def test_decision_and_claude_adapter_share_the_identical_system_prompt():
    """§25: swapping the provider must not change the question being asked."""
    from services.decision.claude_adapters import _DECISION_SYSTEM_PROMPT
    from services.decision.prompts import DECISION_SYSTEM_PROMPT

    assert _DECISION_SYSTEM_PROMPT is DECISION_SYSTEM_PROMPT


def test_decision_refusal_raises():
    client = FakeOpenAIClient()
    client.beta.chat.completions.parse.next_completion = _FakeCompletion(
        choices=[_FakeChoice(message=_FakeMessage(parsed=None, refusal="policy"))])
    model = OpenAIDecisionModel(client=client)
    with pytest.raises(OpenAIAPIError, match="refused"):
        model.decide(_ctx())


def test_decision_none_output_raises():
    client = FakeOpenAIClient()
    client.beta.chat.completions.parse.next_completion = _FakeCompletion(
        choices=[_FakeChoice(message=_FakeMessage(parsed=None))])
    model = OpenAIDecisionModel(client=client)
    with pytest.raises(OpenAIAPIError, match="no parsed output"):
        model.decide(_ctx())


def test_decision_api_failure_raises_openai_api_error():
    client = FakeOpenAIClient()
    client.beta.chat.completions.parse.next_exception = ConnectionError("network down")
    model = OpenAIDecisionModel(client=client)
    with pytest.raises(OpenAIAPIError, match="decision call failed"):
        model.decide(_ctx())


def test_openai_api_error_is_an_llm_call_error():
    """Callers that don't care which provider is behind Decision AI can catch
    the shared base type."""
    assert issubclass(OpenAIAPIError, LLMCallError)


# --- resolve_decision_agent (provider seam) -----------------------------------

def test_resolve_decision_agent_falls_back_to_claude_without_openai_creds(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    agent = resolve_decision_agent(DEFAULT_MODEL_CONFIG)
    assert agent.provider is Provider.ANTHROPIC
    assert agent == DEFAULT_MODEL_CONFIG.decision


def test_resolve_decision_agent_prefers_openai_when_configured(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-real")
    agent = resolve_decision_agent(DEFAULT_MODEL_CONFIG)
    assert agent.provider is Provider.OPENAI
    assert agent.role is AgentRole.DECISION


def test_resolve_decision_agent_never_widens_max_tokens_silently(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-real")
    cfg = LLMModelConfig()
    agent = resolve_decision_agent(cfg)
    assert agent.max_tokens == cfg.decision.max_tokens


# --- build_llm_stack wiring ---------------------------------------------------

def test_build_llm_stack_picks_openai_decision_when_available(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-real")
    from services.decision import claude_adapters as ca

    fake_anthropic = object()
    fake_openai = FakeOpenAIClient()
    monkeypatch.setattr(ca, "get_client",
                        lambda provider=Provider.ANTHROPIC: (
                            fake_openai if provider is Provider.OPENAI else fake_anthropic))

    decision, skeptic, auditor = ca.build_llm_stack()
    assert isinstance(decision, OpenAIDecisionModel)
    assert decision.agent.provider is Provider.OPENAI


def test_build_llm_stack_falls_back_to_claude_decision_without_openai_creds(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    from services.decision import claude_adapters as ca

    decision, skeptic, auditor = ca.build_llm_stack(client=object())
    assert isinstance(decision, ca.ClaudeDecisionModel)
    assert decision.agent.provider is Provider.ANTHROPIC
