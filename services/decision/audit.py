"""Independent Audit AI (MASTER SPEC ADDENDUM A3).

Semantic pre-trade audit: does the order actually mean what the decision
said?  This is NOT the final barrier — the deterministic Master Risk
Controller remains the last gate (A3-4).  The auditor holds no broker access
and runs, by design, on a different model family than the Decision AI
(A3-5); V1 ships a deterministic mock behind the same protocol.

Checks (A3-3): symbol match, side match, quantity plausibility, stop
existence & direction, horizon vs stop-width consistency, thesis/risk
consistency, stale signal, implausible magnitudes, portfolio conflicts.

Conditional audit (A3-6): cheap deterministic triggers decide when audit is
mandatory; if the auditor is unavailable for a mandatory audit, the trade is
blocked (INV-19).  In V1 paper mode every order is audited to collect data.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Optional, Protocol

from packages.common.clock import ensure_utc
from packages.schemas.audit import (
    AuditOutput,
    AuditVerdict,
    MalformedAuditError,
    validate_audit_output,
)
from packages.schemas.core import DecisionOutput, OrderIntent, SizedProposal

AUDIT_MODEL_VERSION = "1.0.0"

# horizon string → typical max sane stop distance (A3-2: 1-3 days with -35% stop
# is suspicious)
_HORIZON_MAX_STOP = {"1d": 0.06, "1w": 0.12, "1m": 0.20, "3m": 0.30, "6m": 0.40}


@dataclass
class AuditContext:
    now: datetime
    signal_age: timedelta = timedelta(0)
    stale_after: timedelta = timedelta(minutes=30)
    portfolio_notes: dict[str, Any] = field(default_factory=dict)


class AuditUnavailableError(RuntimeError):
    """The audit model cannot be reached — mandatory audits must block (INV-19)."""


class AuditModel(Protocol):
    name: str
    model_family: str

    def audit(self, decision: DecisionOutput, sized: SizedProposal,
              intent: OrderIntent, context: AuditContext) -> dict[str, Any]:
        """Returns raw JSON; caller validates via validate_audit_output (INV-20)."""
        ...


class MockAuditModel:
    """Deterministic semantic auditor. Deliberately a different 'family' from
    MockDecisionModel (A3-5)."""

    name = "mock-audit"
    model_family = "mock-audit-family"

    def audit(self, decision: DecisionOutput, sized: SizedProposal,
              intent: OrderIntent, context: AuditContext) -> dict[str, Any]:
        conflicts: list[dict[str, Any]] = []

        def conflict(check: str, detail: str, severity: float) -> None:
            conflicts.append({"check": check, "detail": detail, "severity": severity})

        # 1. symbol consistency
        if decision.symbol != intent.symbol or sized.proposal.symbol != intent.symbol:
            conflict("symbol_match",
                     f"decision {decision.symbol} vs order {intent.symbol}", 1.0)

        # 2. side consistency (INV-16: Decision BUY / Order SELL → REJECT)
        if decision.action != intent.side:
            conflict("side_match",
                     f"decision {decision.action.value} vs order {intent.side.value}", 1.0)

        # 3. quantity plausibility vs the sized proposal
        if abs(intent.qty - sized.qty) > max(1e-6, sized.qty * 0.001):
            conflict("quantity_consistency",
                     f"sized {sized.qty} vs order {intent.qty}", 1.0)
        if intent.qty > sized.qty * 10:
            conflict("quantity_magnitude",
                     f"order qty {intent.qty} is 10x the sized qty — digit error?", 1.0)

        # 4. stop existence & direction
        stop = sized.stop_plan
        if stop is None:
            conflict("stop_exists", "no stop plan attached", 1.0)
        else:
            if stop.stop_price >= stop.entry_price:
                conflict("stop_direction", "stop above entry for a long", 1.0)
            # 5. horizon vs stop width (A3-2)
            max_stop = _HORIZON_MAX_STOP.get(decision.expected_horizon, 0.40)
            width = stop.stop_distance / stop.entry_price
            if width > max_stop:
                conflict("horizon_stop_consistency",
                         f"horizon {decision.expected_horizon} with {width:.0%} stop", 0.6)

        # 6. confidence vs size ("high risk so small" but size is huge)
        if decision.confidence < 0.4 and sized.notional > 0:
            if sized.risk_amount > 0 and sized.calibrated_confidence < 0.4:
                conflict("confidence_size_consistency",
                         f"low confidence {decision.confidence:.2f} — verify small size",
                         0.4)

        # 7. stale signal (A3-3)
        if context.signal_age > context.stale_after:
            conflict("stale_signal",
                     f"signal age {context.signal_age} exceeds {context.stale_after}", 0.8)

        # 8. thesis presence — an order without a stated thesis is unauditable
        if not decision.key_evidence:
            conflict("thesis_consistency", "no key evidence recorded", 0.5)

        max_sev = max((c["severity"] for c in conflicts), default=0.0)
        verdict = ("REJECT" if max_sev >= 0.8
                   else "REVIEW" if max_sev >= 0.5 else "PASS")
        return {
            "decision_id": sized.proposal.proposal_id,
            "client_order_id": intent.client_order_id,
            "verdict": verdict,
            "reasons": tuple(c["detail"] for c in conflicts) or ("no conflicts detected",),
            "detected_conflicts": tuple(conflicts),
            "severity": max_sev,
            "model": self.name,
            "model_family": self.model_family,
            "audited_at": context.now.isoformat(),
        }


# ---------------------------------------------------------------------------
# Conditional audit triggers (A3-6)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AuditTriggerContext:
    notional_vs_typical: float = 1.0      # this order / typical order size
    volatility: float = 0.0
    event_within_horizon: bool = False
    model_disagreement: float = 0.0
    new_alpha: bool = False
    first_live_use: bool = False
    rapid_price_move: bool = False
    complex_stop: bool = False
    low_liquidity: bool = False
    risk_score: float = 0.0
    data_uncertain: bool = False


def audit_required(t: AuditTriggerContext, high_vol_threshold: float = 0.04,
                   disagreement_threshold: float = 0.5) -> bool:
    """A3-6: mandatory-audit conditions. V1 paper audits everything anyway."""
    return any([
        t.notional_vs_typical > 2.0,
        t.volatility > high_vol_threshold,
        t.event_within_horizon,
        t.model_disagreement > disagreement_threshold,
        t.new_alpha,
        t.first_live_use,
        t.rapid_price_move,
        t.complex_stop,
        t.low_liquidity,
        t.risk_score > 0.7,
        t.data_uncertain,
    ])


@dataclass
class IndependentAuditor:
    """Wraps an AuditModel with validation, unavailability policy and logging
    hooks.  audit_all=True in V1 paper mode (A3-6)."""

    model: Optional[AuditModel]
    audit_all: bool = True

    def audit(self, decision: DecisionOutput, sized: SizedProposal, intent: OrderIntent,
              context: AuditContext,
              triggers: AuditTriggerContext = AuditTriggerContext()) -> AuditOutput:
        mandatory = self.audit_all or audit_required(triggers)
        now = ensure_utc(context.now)

        if self.model is None:
            if mandatory:
                # INV-19: no auditor + mandatory audit = no trade
                raise AuditUnavailableError(
                    "audit model unavailable — high-risk trade blocked (A3-6)")
            return AuditOutput(decision_id=sized.proposal.proposal_id,
                               client_order_id=intent.client_order_id,
                               verdict=AuditVerdict.PASS,
                               reasons=("audit skipped: not required and model absent",),
                               severity=0.0, model="none", model_family="none",
                               forced=False, audited_at=now)

        try:
            raw = self.model.audit(decision, sized, intent, context)
        except Exception as e:  # model call failed entirely
            if mandatory:
                raise AuditUnavailableError(str(e)) from e
            raise
        try:
            out = validate_audit_output(raw)
        except MalformedAuditError:
            # INV-20: malformed audit output is a REJECT, never a silent pass
            return AuditOutput(decision_id=sized.proposal.proposal_id,
                               client_order_id=intent.client_order_id,
                               verdict=AuditVerdict.REJECT,
                               reasons=("malformed audit output rejected (INV-20)",),
                               severity=1.0, model=getattr(self.model, "name", "?"),
                               model_family=getattr(self.model, "model_family", "?"),
                               forced=mandatory, audited_at=now)
        return out.model_copy(update={"forced": mandatory})
