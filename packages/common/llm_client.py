"""LLM provider abstraction + per-agent model assignment (§25 of the role spec).

Two things live here:

1. **Provider abstraction.** Each AI role names a provider (`anthropic` /
   `openai`) and a model. Adapters ask this module for a client rather than
   importing an SDK directly, so swapping Decision AI to a GPT-class model is
   a config change plus one adapter, not a pipeline change.

2. **Per-agent assignment.** Roles are separate agents even when they share a
   model — separate prompt, separate call site, separate audit id (§25). The
   assignment table below is the single place that says who runs on what.

| Role              | Provider (spec)   | Why                                        |
|-------------------|-------------------|--------------------------------------------|
| Decision AI       | GPT-5.6 Sol       | investment judgment                         |
| Skeptic AI        | Claude Opus       | refutes the *judgment*                      |
| Pre-Trade Audit   | Claude Opus       | refutes the *order* — different agent/id    |
| Monitor AI        | Claude Opus       | anomaly analysis only                       |
| Judge AI          | GPT-5.6 Sol       | recommends, never promotes                  |
| Builder / Research| Claude Fable 5    | alpha & feature generation                  |

Decision AI and Judge AI are specified as GPT-5.6 Sol on OpenAI. For Decision
AI, `resolve_decision_agent()` is the real seam: it picks OpenAI when
`OPENAI_API_KEY` is configured (`services/decision/openai_adapters.py`) and
falls back to a Claude model of equivalent tier otherwise, so the system
still runs end-to-end without one; `DECISION_PROVIDER_TARGET` records the
intended provider so the substitution is visible rather than silent. Judge AI
has a slot here (`LLMModelConfig.judge`) but no adapter calls it yet —
`AlphaJudge` (services/alpha_factory/factory.py) is fully deterministic
Python today (backtest / walk-forward / Monte Carlo gates), consistent with
§14's "the Judge recommends mechanically" but short of the GPT-5.6 Sol
assignment named in the spec. No module here imports
`packages.broker_adapters` — no AI role ever receives broker credentials
(§26-10).
"""
from __future__ import annotations

import enum
import os
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any


class LLMUnavailableError(RuntimeError):
    """No credentials for the requested provider, or its SDK is missing.

    Callers already treat an unavailable model as a hard stop for mandatory
    checks (INV-19, §9) — this exception is what triggers that path.
    """


class LLMCallError(RuntimeError):
    """A call to a provider's API failed (network, refusal, malformed
    structured output). Provider-specific adapters raise a subclass of this
    (`ClaudeAPIError`, `OpenAIAPIError`) so callers that don't care which
    provider is behind a given role can catch one type."""


class Provider(str, enum.Enum):
    ANTHROPIC = "anthropic"
    OPENAI = "openai"


class AgentRole(str, enum.Enum):
    """Every role is a distinct agent with its own prompt and call site,
    even when two roles resolve to the same underlying model (§25)."""

    DECISION = "DECISION"
    SKEPTIC = "SKEPTIC"
    PRE_TRADE_AUDIT = "PRE_TRADE_AUDIT"
    MONITOR = "MONITOR"
    JUDGE = "JUDGE"
    BUILDER = "BUILDER"


# Provider the spec asks for on the judgment roles; recorded so the current
# Claude substitution is explicit rather than an undocumented default.
DECISION_PROVIDER_TARGET = Provider.OPENAI
OPENAI_DECISION_MODEL = os.environ.get("QUANT_OPENAI_DECISION_MODEL", "gpt-5.6-sol")


@dataclass(frozen=True)
class AgentModel:
    role: AgentRole
    provider: Provider
    model: str
    max_tokens: int = 2048

    @property
    def agent_id(self) -> str:
        """Stable identifier that distinguishes two agents sharing a model —
        e.g. Skeptic and Pre-Trade Audit both on Claude Opus (§25)."""
        return f"{self.role.value.lower()}:{self.provider.value}:{self.model}"


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


@dataclass(frozen=True)
class LLMModelConfig:
    """Per-role assignment. Every value is env-overridable so the Operating
    Cost / Data ROI engines (§80-82) can tune the mix without a code change."""

    decision: AgentModel = field(default_factory=lambda: AgentModel(
        role=AgentRole.DECISION, provider=Provider.ANTHROPIC,
        model=_env("QUANT_DECISION_MODEL", "claude-opus-5"), max_tokens=2048))
    skeptic: AgentModel = field(default_factory=lambda: AgentModel(
        role=AgentRole.SKEPTIC, provider=Provider.ANTHROPIC,
        model=_env("QUANT_SKEPTIC_MODEL", "claude-opus-5"), max_tokens=1024))
    audit: AgentModel = field(default_factory=lambda: AgentModel(
        role=AgentRole.PRE_TRADE_AUDIT, provider=Provider.ANTHROPIC,
        model=_env("QUANT_AUDIT_MODEL", "claude-opus-5"), max_tokens=1024))
    monitor: AgentModel = field(default_factory=lambda: AgentModel(
        role=AgentRole.MONITOR, provider=Provider.ANTHROPIC,
        model=_env("QUANT_MONITOR_MODEL", "claude-opus-5"), max_tokens=2048))
    judge: AgentModel = field(default_factory=lambda: AgentModel(
        role=AgentRole.JUDGE, provider=Provider.ANTHROPIC,
        model=_env("QUANT_JUDGE_MODEL", "claude-opus-5"), max_tokens=2048))
    builder: AgentModel = field(default_factory=lambda: AgentModel(
        role=AgentRole.BUILDER, provider=Provider.ANTHROPIC,
        model=_env("QUANT_BUILDER_MODEL", "claude-fable-5"), max_tokens=4096))

    def for_role(self, role: AgentRole) -> AgentModel:
        return {
            AgentRole.DECISION: self.decision,
            AgentRole.SKEPTIC: self.skeptic,
            AgentRole.PRE_TRADE_AUDIT: self.audit,
            AgentRole.MONITOR: self.monitor,
            AgentRole.JUDGE: self.judge,
            AgentRole.BUILDER: self.builder,
        }[role]

    def distinct_agents(self) -> set[str]:
        """Skeptic and Pre-Trade Audit must remain separate agent ids even on
        the same model — used by the invariant test for §25."""
        return {m.agent_id for m in
                (self.decision, self.skeptic, self.audit, self.monitor,
                 self.judge, self.builder)}


DEFAULT_MODEL_CONFIG = LLMModelConfig()


def credentials_available(provider: Provider = Provider.ANTHROPIC) -> bool:
    """Best-effort pre-check so callers can pick Mocks instead of building a
    client that will immediately fail. The SDK remains the authority on
    whether a credential actually works."""
    if provider is Provider.OPENAI:
        return bool(os.environ.get("OPENAI_API_KEY"))
    return bool(
        os.environ.get("MY_ANTHROPIC_API_KEY")
        or os.environ.get("ANTHROPIC_AUTH_TOKEN")
        or os.environ.get("ANTHROPIC_PROFILE")
        or os.path.exists(os.path.expanduser("~/.config/anthropic"))
    )


@lru_cache(maxsize=4)
def get_client(provider: Provider = Provider.ANTHROPIC) -> Any:
    """Lazily construct one shared client per provider. Tests inject fakes
    into the adapters directly and never call this."""
    if provider is Provider.OPENAI:
        try:
            import openai
        except ImportError as e:
            raise LLMUnavailableError(
                "openai package not installed — run `pip install openai`") from e
        if not credentials_available(Provider.OPENAI):
            raise LLMUnavailableError("no OPENAI_API_KEY configured")
        return openai.OpenAI()

    try:
        import anthropic
    except ImportError as e:
        raise LLMUnavailableError(
            "anthropic package not installed — run `pip install anthropic`") from e
    if not credentials_available(Provider.ANTHROPIC):
        raise LLMUnavailableError(
            "no Anthropic credentials found — set MY_ANTHROPIC_API_KEY or run "
            "`ant auth login`")
    # anthropic.Anthropic() only reads the SDK's own ANTHROPIC_API_KEY env var,
    # not our MY_ANTHROPIC_API_KEY — pass it explicitly when set, otherwise
    # let the SDK fall back to ANTHROPIC_AUTH_TOKEN / ANTHROPIC_PROFILE / CLI auth.
    anthropic_api_key = os.environ.get("MY_ANTHROPIC_API_KEY")
    if anthropic_api_key:
        return anthropic.Anthropic(api_key=anthropic_api_key)
    return anthropic.Anthropic()


def resolve_decision_agent(config: "LLMModelConfig | None" = None) -> AgentModel:
    """§25: Decision AI is specified as GPT-5.6 Sol on OpenAI. If an OpenAI
    credential is actually configured, use it; otherwise fall back to the
    Claude model `config.decision` already names, so the system still runs
    end-to-end rather than raising for a provider nobody has set up yet.
    The substitution is a return value here, not a silent default, so
    callers (and their logs) can see which provider actually served the
    role for a given run."""
    cfg = config or DEFAULT_MODEL_CONFIG
    if credentials_available(Provider.OPENAI):
        return AgentModel(role=AgentRole.DECISION, provider=Provider.OPENAI,
                          model=OPENAI_DECISION_MODEL, max_tokens=cfg.decision.max_tokens)
    return cfg.decision
