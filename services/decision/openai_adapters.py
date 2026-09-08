"""Real OpenAI-backed Decision AI (MASTER SPEC §25, §27-28).

Decision AI is specified as GPT-5.6 Sol / OpenAI (§25). `claude_adapters.py`
carries a Claude fallback used automatically until an OpenAI credential is
configured (`packages.common.llm_client.resolve_decision_agent`); this module
is the real thing — same `DecisionModel` Protocol, same shared prompt
(`services/decision/prompts.py`, so the two providers are asked the identical
question), same "Malformed Output: Reject" contract (§28, INV-14), same "no
broker access, ever" property (this module never imports
`packages.broker_adapters`). It just talks to the OpenAI structured-outputs
API instead of Anthropic's.

No network access is exercised in tests: a fake client is injected via
dependency injection, mirroring claude_adapters.py.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from packages.common.llm_client import (
    OPENAI_DECISION_MODEL,
    AgentModel,
    AgentRole,
    LLMCallError,
    Provider,
    get_client,
)
from packages.schemas.core import DecisionOutput
from services.decision.models import DECISION_VERSION, DecisionContext
from services.decision.prompts import DECISION_SYSTEM_PROMPT, build_decision_prompt

OPENAI_DECISION_ADAPTER_VERSION = "openai-adapter-1.0.0"


class OpenAIAPIError(LLMCallError):
    """Wraps any failure talking to the OpenAI API (network, refusal,
    malformed structured output) into one exception type callers can catch."""


@dataclass
class OpenAIDecisionModel:
    """Decision AI Protocol implementation (§27-28) backed by the OpenAI API.

    Uses the Structured Outputs `beta.chat.completions.parse` call, which
    constrains the response to a pydantic schema and validates it
    client-side — the same "Malformed Output: Reject" contract the Claude
    adapter and the Mock adapter both honor (§28, INV-14, INV-20)."""

    agent: AgentModel = field(default_factory=lambda: AgentModel(
        role=AgentRole.DECISION, provider=Provider.OPENAI,
        model=OPENAI_DECISION_MODEL, max_tokens=2048))
    client: Optional[Any] = None
    name: str = field(init=False)
    version: str = field(init=False)
    model_id: str = field(init=False)

    def __post_init__(self) -> None:
        self.model_id = self.agent.model
        self.name = self.agent.agent_id
        self.version = OPENAI_DECISION_ADAPTER_VERSION

    def _client(self) -> Any:
        return self.client or get_client(Provider.OPENAI)

    def decide(self, context: DecisionContext) -> dict[str, Any]:
        try:
            completion = self._client().beta.chat.completions.parse(
                model=self.agent.model,
                max_tokens=self.agent.max_tokens,
                messages=[
                    {"role": "system", "content": DECISION_SYSTEM_PROMPT},
                    {"role": "user", "content": build_decision_prompt(context)},
                ],
                response_format=DecisionOutput,
            )
            message = completion.choices[0].message
        except Exception as e:  # network / SDK-level validation
            raise OpenAIAPIError(
                f"decision call failed for {context.scan.symbol}: {e}") from e
        if getattr(message, "refusal", None):
            raise OpenAIAPIError(
                f"decision call for {context.scan.symbol} was refused: {message.refusal}")
        parsed = message.parsed
        if parsed is None:
            raise OpenAIAPIError(
                f"decision call for {context.scan.symbol} returned no parsed output")
        data = parsed.model_dump(mode="json")
        # decision_version is bookkeeping, not something to trust from the model
        data["decision_version"] = f"{self.model_id}:{DECISION_VERSION}"
        return data
