"""Final Trade Thesis (MASTER SPEC §27).

The fixed pipeline is Decision AI -> Skeptic AI -> **Final Trade Thesis** ->
Loss Control -> Position Sizing -> ... . This module builds that thesis: it
does not re-judge the trade (that already happened), it records who judged it
and how much the two agents actually disagreed, so that fact survives into
the audit trail and the Approved Order Snapshot rather than being implicit in
two separately-logged blobs.
"""
from __future__ import annotations

from packages.schemas.core import DecisionOutput, FinalTradeThesis, SkepticOutput, TradeProposal


def compute_disagreement_score(decision: DecisionOutput, skeptic: SkepticOutput) -> float:
    """0 = the Skeptic raised nothing of note; 1 = the Skeptic's reservations
    were as strong as they can be short of an outright veto, against a
    Decision AI that was fully confident.

    Severity alone conflates "mild concern about a decision that was already
    hedged" with "mild concern about a decision the model was sure of" — the
    second is the more informative kind of disagreement, so severity is
    weighted by the decision's own confidence rather than used on its own.
    A trade reaches this stage only when the Skeptic did NOT veto, so
    severity here is always sub-veto by construction.
    """
    return round(max(0.0, min(1.0, skeptic.severity * (0.5 + 0.5 * decision.confidence))), 6)


def build_final_trade_thesis(proposal: TradeProposal, decision_id: str,
                             skeptic_id: str) -> FinalTradeThesis:
    """`decision_id` is the provenance record id (which scan/session produced
    this decision); `skeptic_id` is the reviewing agent's identity (e.g.
    `skeptic:anthropic:claude-opus-5`), not a per-call instance id — the
    point is knowing *which agent* signed off, matching the Pre-Trade Audit
    AI's own separate `agent_id` (§25)."""
    if proposal.skeptic is None:
        raise ValueError("cannot build a Final Trade Thesis without a Skeptic critique")
    score = compute_disagreement_score(proposal.decision, proposal.skeptic)
    return FinalTradeThesis(decision_id=decision_id, skeptic_id=skeptic_id,
                            disagreement_score=score, proposal=proposal,
                            created_at=proposal.created_at)
