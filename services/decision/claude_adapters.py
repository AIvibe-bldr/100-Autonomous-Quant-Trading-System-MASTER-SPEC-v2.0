"""Real Claude-backed Decision AI / Skeptic AI / Audit AI (MASTER SPEC §27-31,
ADDENDUM A3).

Each adapter implements the same Protocol as its Mock counterpart
(`MockDecisionModel`, `MockSkepticModel`, `MockAuditModel`), so it drops
into `TradingPipeline` / `IndependentAuditor` without touching pipeline
code. None of these adapters import `packages.broker_adapters` — Broker
credentials never reach an AI component (§74).

Structured outputs (`client.messages.parse(output_format=...)`) constrain
every response to a pydantic schema and validate it client-side, so a
model reply that doesn't fit the contract raises instead of silently
returning malformed data — the same "Malformed Output: Reject" contract
the Mock adapters already honor (§28, INV-14, INV-20).

Bookkeeping fields the model has no business generating — decision_version,
proposal_symbol, model_family, decision_id, audited_at — are filled in by
this code after parsing, never trusted from model output.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict

from packages.common.llm_client import (
    DEFAULT_MODEL_CONFIG,
    AgentModel,
    AgentRole,
    LLMCallError,
    LLMModelConfig,
    Provider,
    get_client,
    resolve_decision_agent,
)
from packages.common.environment import Environment
from packages.schemas.audit import AuditOutput, AuditVerdict, DetectedConflict
from packages.schemas.core import DecisionOutput, OrderIntent, SizedProposal, SkepticOutput
from packages.schemas.monitor import MonitorRecommendation
from services.decision.audit import AuditContext, IndependentAuditor
from services.decision.models import DECISION_VERSION, DecisionContext, UntrustedText
from services.decision.monitor import MonitorContext
from services.decision.prompts import (
    DECISION_SYSTEM_PROMPT as _DECISION_SYSTEM_PROMPT,
    build_decision_prompt as _build_decision_prompt,
)

DECISION_ADAPTER_VERSION = "claude-adapter-1.0.0"


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _SkepticJudgment(_StrictModel):
    """What we ask the LLM for — no proposal_symbol/model_family (§29: those
    are known to the caller, never trusted from model output)."""

    objections: list[str]
    severity: float
    recommends_veto: bool


class _AuditJudgment(_StrictModel):
    """Semantic-consistency judgment only — decision_id/client_order_id/
    model/model_family/audited_at are filled in by the caller (A3-3)."""

    verdict: AuditVerdict
    reasons: list[str]
    detected_conflicts: list[DetectedConflict] = []
    severity: float


class _MonitorJudgment(_StrictModel):
    """Anomaly narrative + recommendation only — at/services_reviewed/model/
    model_family are filled in by the caller (§66), never trusted from
    model output."""

    findings: list[str]
    severity: float
    recommendation: MonitorRecommendation


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

_SKEPTIC_SYSTEM_PROMPT = """\
You are the Skeptic AI. A separate Decision AI has reached an investment \
judgment; your job is to refute that JUDGMENT — not to check the paperwork.

Interrogate specifically:
- Should this really be a BUY, or is NO_TRADE/WAIT the more rational stance?
- What material risk has the Decision AI overlooked?
- Is there confirmation bias — evidence selected to fit a conclusion?
- Is the news interpretation wrong, stale, or over-read?
- Is the institutional-flow reading wrong?
- Does the thesis contradict the current market regime?
- Is the historical analogue actually comparable?
- Is the bull case overweighted, or the bear case underweighted?

Do NOT spend your effort on order mechanics — quantity digits, symbol match, \
side match, stop presence. A separate Pre-Trade Audit AI checks those \
immediately before submission; duplicating it here wastes the second opinion.

You have no broker access. You cannot place or block orders: a deterministic \
Risk Controller decides, using your judgment as one input.
"""

_AUDIT_SYSTEM_PROMPT = """\
You are the Pre-Trade Audit AI — a different agent from the Skeptic AI, \
running at a different moment with a different question. The investment \
judgment has already been debated and settled; you do not re-litigate it.

Your question is narrow: does the ORDER about to be sent to the broker \
faithfully express that settled judgment? Check:
- Symbol match between decision and order
- Side match (a BUY decision must not become a SELL order)
- Quantity plausibility — especially digit errors (3 vs 300)
- Price plausibility
- Stop present, and on the correct side for a long position
- Stop width consistent with the stated holding horizon
- Consistency with the stated thesis and confidence
- Event risk and stale signals
- Contradiction with the current position

You are NOT the final safety authority: leverage, cash, settled funds, \
exposure, duplicate orders, and short-selling limits are enforced separately \
by a deterministic Master Risk Controller you cannot see or override. If \
nothing is wrong, say so plainly rather than inventing a concern.
"""

_MONITOR_SYSTEM_PROMPT = """\
You are the Monitor AI. You are consulted only when a deterministic \
supervisor has already detected an anomaly — an unhealthy service, an \
elevated drawdown, a non-NORMAL risk state, or a recent near-miss. Your job \
is to read that evidence and produce a short, concrete assessment plus a \
recommendation.

You have NO broker access, cannot place or cancel any order, cannot open, \
close, or resize any position, cannot change any Risk Rule or threshold, and \
cannot change any system configuration. Your `recommendation` is a request \
to a human or to a separate deterministic component that DOES hold that \
authority — it is not an instruction that executes itself:
- CONTINUE_MONITORING: nothing actionable, keep watching
- NOTIFY: worth a human's attention, not urgent
- QUEUE_HUMAN_REVIEW: a human should look at this before more decisions ride on it
- ESCALATE_SAFE_EXIT: recommend the system move toward SAFE_EXIT — you cannot \
trigger this yourself, only ask for it

Be concrete: cite the specific services, the specific drawdown number, the \
specific risk state. Do not speculate about causes you have no evidence for.
"""


def _build_skeptic_prompt(decision: DecisionOutput, ctx: DecisionContext) -> str:
    s = ctx.scan
    return (
        f"Decision AI proposal for {decision.symbol}:\n"
        f"action={decision.action.value} confidence={decision.confidence}\n"
        f"bull={decision.bull_case.model_dump(mode='json')}\n"
        f"base={decision.base_case.model_dump(mode='json')}\n"
        f"bear={decision.bear_case.model_dump(mode='json')}\n"
        f"key_evidence={decision.key_evidence}\n"
        f"counter_evidence={decision.counter_evidence}\n"
        f"risk_factors={decision.risk_factors}\n"
        f"expected_horizon={decision.expected_horizon}\n"
        f"Market regime: {ctx.regime}\n"
        f"Quant score={s.score:.4f} volatility={s.volatility:.4f} "
        f"momentum_20d={s.momentum_20d:.4f}\n\n"
        "Critique this proposal. List concrete objections (empty list only if "
        "you truly find none), an overall severity in [0,1] (0=no concern, "
        "1=fatal flaw), and whether you recommend a veto."
    )


def _build_audit_prompt(decision: DecisionOutput, sized: SizedProposal,
                        intent: OrderIntent, context: AuditContext) -> str:
    stop = sized.stop_plan
    return (
        f"Decision: symbol={decision.symbol} action={decision.action.value} "
        f"confidence={decision.confidence} horizon={decision.expected_horizon}\n"
        f"thesis={decision.base_case.description}\n"
        f"Sized proposal: symbol={sized.proposal.symbol} qty={sized.qty} "
        f"entry={stop.entry_price} stop={stop.stop_price} "
        f"stop_type={stop.stop_type.value} gap_risk={stop.gap_risk_score:.2f}\n"
        f"Order intent about to be sent: symbol={intent.symbol} "
        f"side={intent.side.value} qty={intent.qty} "
        f"order_type={intent.order_type.value}\n"
        f"Signal age: {context.signal_age}\n\n"
        "Check: does the order's symbol/side/quantity genuinely match the "
        "decision and sized proposal? Is a stop present and on the correct "
        "side for a long position? Is the stop width sane for the stated "
        "horizon? Report a verdict (PASS/REJECT/REVIEW) with concrete "
        "detected_conflicts (empty list if none) and an overall severity."
    )


def _build_monitor_prompt(ctx: MonitorContext) -> str:
    hb = ", ".join(f"{s}={st.value}" for s, st in sorted(ctx.heartbeats.items())) or "(none reporting)"
    positions = ", ".join(f"{s}={q:g}" for s, q in sorted(ctx.open_positions.items())) or "(none)"
    misses = "\n".join(f"- {m}" for m in ctx.recent_near_misses) or "(none)"
    return (
        f"Service heartbeats: {hb}\n"
        f"Risk state: {ctx.risk_state}\n"
        f"Drawdown from high-water mark: {ctx.drawdown:.2%}\n"
        f"Open positions: {positions}\n"
        f"Recent near-misses:\n{misses}\n\n"
        "Assess this anomaly and produce findings + severity [0,1] + a recommendation."
    )


# ---------------------------------------------------------------------------
# Adapters
# ---------------------------------------------------------------------------

class ClaudeAPIError(LLMCallError):
    """Wraps any failure talking to the Anthropic API (network, refusal,
    malformed structured output) into one exception type callers can catch."""


@dataclass
class ClaudeDecisionModel:
    """Decision AI Protocol implementation (§27-28) backed by the Claude API."""

    agent: AgentModel = field(default_factory=lambda: DEFAULT_MODEL_CONFIG.decision)
    client: Optional[Any] = None
    name: str = field(init=False)
    version: str = field(init=False)
    model_id: str = field(init=False)

    def __post_init__(self) -> None:
        self.model_id = self.agent.model
        self.name = self.agent.agent_id
        self.version = DECISION_ADAPTER_VERSION

    def _client(self) -> Any:
        return self.client or get_client()

    def decide(self, context: DecisionContext) -> dict[str, Any]:
        try:
            response = self._client().messages.parse(
                model=self.agent.model,
                max_tokens=self.agent.max_tokens,
                system=_DECISION_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": _build_decision_prompt(context)}],
                output_format=DecisionOutput,
            )
            parsed = response.parsed_output
        except Exception as e:  # network / refusal / SDK-level validation
            raise ClaudeAPIError(f"decision call failed for {context.scan.symbol}: {e}") from e
        if parsed is None:
            raise ClaudeAPIError(
                f"decision call for {context.scan.symbol} returned no parsed output "
                f"(stop_reason={getattr(response, 'stop_reason', '?')})")
        data = parsed.model_dump(mode="json")
        # decision_version is bookkeeping, not something to trust from the model
        data["decision_version"] = f"{self.model_id}:{DECISION_VERSION}"
        return data


@dataclass
class ClaudeSkepticModel:
    """Skeptic AI Protocol implementation (§29), deliberately a different
    model from Decision AI (A3-5) to avoid correlated blind spots.

    Fails safe: if the Skeptic cannot be consulted, that is treated as a
    veto rather than a silent pass — an unreachable second opinion must not
    quietly downgrade to "no objections" (mirrors INV-19's audit-unavailable
    policy for the same reason).
    """

    agent: AgentModel = field(default_factory=lambda: DEFAULT_MODEL_CONFIG.skeptic)
    client: Optional[Any] = None
    name: str = field(init=False)
    model_family: str = field(init=False)
    model_id: str = field(init=False)

    def __post_init__(self) -> None:
        self.model_id = self.agent.model
        # agent_id keeps Skeptic distinct from Pre-Trade Audit even when both
        # resolve to the same Claude Opus model (§25)
        self.name = self.agent.agent_id
        self.model_family = f"{self.agent.provider.value}:{self.agent.model}"

    def _client(self) -> Any:
        return self.client or get_client()

    def critique(self, decision: DecisionOutput, context: DecisionContext) -> SkepticOutput:
        try:
            response = self._client().messages.parse(
                model=self.agent.model,
                max_tokens=self.agent.max_tokens,
                system=_SKEPTIC_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": _build_skeptic_prompt(decision, context)}],
                output_format=_SkepticJudgment,
            )
            judgment = response.parsed_output
            if judgment is None:
                raise ClaudeAPIError(
                    f"skeptic call for {decision.symbol} returned no parsed output "
                    f"(stop_reason={getattr(response, 'stop_reason', '?')})")
        except Exception as e:
            return SkepticOutput(
                proposal_symbol=decision.symbol,
                objections=[f"Skeptic AI unavailable — treating as veto: {e}"],
                severity=1.0, recommends_veto=True, model_family=self.model_family)
        return SkepticOutput(
            proposal_symbol=decision.symbol,
            objections=judgment.objections or ["no objections raised"],
            severity=max(0.0, min(1.0, judgment.severity)),
            recommends_veto=judgment.recommends_veto,
            model_family=self.model_family)


@dataclass
class ClaudeAuditModel:
    """Audit AI Protocol implementation (ADDENDUM A3) — deliberately the
    fastest/cheapest tier, since this is a narrow structural check, not deep
    reasoning, and it runs on every order the pipeline attempts (A3-6: V1
    paper audits everything)."""

    agent: AgentModel = field(default_factory=lambda: DEFAULT_MODEL_CONFIG.audit)
    client: Optional[Any] = None
    name: str = field(init=False)
    model_family: str = field(init=False)
    model_id: str = field(init=False)

    def __post_init__(self) -> None:
        self.model_id = self.agent.model
        self.name = self.agent.agent_id
        self.model_family = f"{self.agent.provider.value}:{self.agent.model}"

    def _client(self) -> Any:
        return self.client or get_client()

    def audit(self, decision: DecisionOutput, sized: SizedProposal, intent: OrderIntent,
              context: AuditContext) -> dict[str, Any]:
        # let exceptions propagate — IndependentAuditor wraps them as
        # AuditUnavailableError for mandatory audits (INV-19)
        response = self._client().messages.parse(
            model=self.agent.model,
            max_tokens=self.agent.max_tokens,
            system=_AUDIT_SYSTEM_PROMPT,
            messages=[{"role": "user",
                      "content": _build_audit_prompt(decision, sized, intent, context)}],
            output_format=_AuditJudgment,
        )
        judgment = response.parsed_output
        if judgment is None:
            raise ClaudeAPIError(
                f"audit call for {intent.client_order_id} returned no parsed output "
                f"(stop_reason={getattr(response, 'stop_reason', '?')})")
        return {
            "decision_id": sized.proposal.proposal_id,
            "client_order_id": intent.client_order_id,
            "verdict": judgment.verdict.value,
            "reasons": tuple(judgment.reasons) or ("no conflicts detected",),
            "detected_conflicts": tuple(c.model_dump(mode="json")
                                        for c in judgment.detected_conflicts),
            "severity": max(0.0, min(1.0, judgment.severity)),
            "model": self.name,
            "model_family": self.model_family,
            "audited_at": context.now.isoformat(),
        }


@dataclass
class ClaudeMonitorModel:
    """Monitor AI Protocol implementation (§66). Consulted only on anomaly
    (see `services.decision.monitor.anomaly_present`) — never on every tick —
    and holds no broker/position/risk-config/system-config access; its output
    is narrative plus a recommendation a human or a separate deterministic
    component acts on."""

    agent: AgentModel = field(default_factory=lambda: DEFAULT_MODEL_CONFIG.monitor)
    client: Optional[Any] = None
    name: str = field(init=False)
    model_family: str = field(init=False)
    model_id: str = field(init=False)

    def __post_init__(self) -> None:
        self.model_id = self.agent.model
        self.name = self.agent.agent_id
        self.model_family = f"{self.agent.provider.value}:{self.agent.model}"

    def _client(self) -> Any:
        return self.client or get_client()

    def review(self, context: MonitorContext) -> dict[str, Any]:
        try:
            response = self._client().messages.parse(
                model=self.agent.model,
                max_tokens=self.agent.max_tokens,
                system=_MONITOR_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": _build_monitor_prompt(context)}],
                output_format=_MonitorJudgment,
            )
            judgment = response.parsed_output
        except Exception as e:
            raise ClaudeAPIError(f"monitor call failed: {e}") from e
        if judgment is None:
            raise ClaudeAPIError(
                f"monitor call returned no parsed output "
                f"(stop_reason={getattr(response, 'stop_reason', '?')})")
        return {
            "at": context.now.isoformat(),
            "services_reviewed": tuple(sorted(context.heartbeats)),
            "findings": tuple(judgment.findings) or ("no findings reported",),
            "severity": max(0.0, min(1.0, judgment.severity)),
            "recommendation": judgment.recommendation.value,
            "model": self.name,
            "model_family": self.model_family,
        }


def build_llm_stack(config: Optional[LLMModelConfig] = None,
                    client: Optional[Any] = None,
                    environment: Environment = Environment.PAPER) -> tuple[
        Any, ClaudeSkepticModel, IndependentAuditor]:
    """Wire the three agents. Skeptic and Pre-Trade Audit are separate agents
    with separate prompts, call sites and agent ids even when they resolve to
    the same model (§25). The auditor is fail-closed in LIVE (§9).

    Decision AI is specified as GPT-5.6 Sol / OpenAI (§25): if an OpenAI
    credential is configured, `resolve_decision_agent` picks it and the
    returned decision model is `OpenAIDecisionModel`; otherwise it falls back
    to `cfg.decision` (Claude) so the system still runs end-to-end. `client`,
    when given, is used only for the Anthropic-backed roles (Skeptic, Audit,
    and Decision when it falls back to Claude) — a resolved OpenAI decision
    always gets its own OpenAI client, since the two providers' SDKs are not
    interchangeable.

    Raises LLMUnavailableError immediately if credentials aren't configured
    for whichever provider ends up in use, rather than failing mid-session."""
    cfg = config or DEFAULT_MODEL_CONFIG
    anthropic_client = client or get_client(Provider.ANTHROPIC)

    decision_agent = resolve_decision_agent(cfg)
    decision: Any
    if decision_agent.provider is Provider.OPENAI:
        from services.decision.openai_adapters import OpenAIDecisionModel

        decision = OpenAIDecisionModel(agent=decision_agent, client=get_client(Provider.OPENAI))
    else:
        decision = ClaudeDecisionModel(agent=decision_agent, client=anthropic_client)

    skeptic = ClaudeSkepticModel(agent=cfg.skeptic, client=anthropic_client)
    audit_model = ClaudeAuditModel(agent=cfg.audit, client=anthropic_client)
    auditor = IndependentAuditor(model=audit_model, audit_all=True,
                                 environment=environment)
    return decision, skeptic, auditor


def build_monitor(config: Optional[LLMModelConfig] = None,
                  client: Optional[Any] = None) -> "MonitorSupervisor":
    """Separate from `build_llm_stack` because the Monitor is consulted on a
    different trigger (anomaly, §66) than the per-candidate pipeline — wiring
    it alongside Decision/Skeptic/Audit would imply it runs every scan, which
    it must not."""
    from services.decision.monitor import MonitorSupervisor

    cfg = config or DEFAULT_MODEL_CONFIG
    monitor_model = ClaudeMonitorModel(agent=cfg.monitor, client=client or get_client())
    return MonitorSupervisor(model=monitor_model)
