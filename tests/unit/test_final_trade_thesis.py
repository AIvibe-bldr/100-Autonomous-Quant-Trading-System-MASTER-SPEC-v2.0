"""Final Trade Thesis tests (MASTER SPEC §27).

§27's fixed pipeline reads Decision -> Skeptic -> Final Trade Thesis -> Loss
Control -> ... . These tests cover the thesis object itself (schema
invariants, the disagreement-score formula) and its wiring into the pipeline
(built once per surviving candidate, and its skeptic_id reaching the
Approved Order Snapshot so the audit trail can name which Skeptic agent
signed off — a field that previously existed on the snapshot but was never
populated).
"""
from __future__ import annotations

import pytest

from packages.schemas.core import FinalTradeThesis, ProposalSource, TradeProposal
from services.decision.thesis import build_final_trade_thesis, compute_disagreement_score
from tests.conftest import SESSION_TIME
from tests.unit.helpers import make_decision


def _skeptic(severity: float = 0.3, recommends_veto: bool = False):
    from packages.schemas.core import SkepticOutput

    return SkepticOutput(proposal_symbol="AAPL", objections=["a concern"],
                         severity=severity, recommends_veto=recommends_veto,
                         model_family="test-family")


def _proposal(confidence: float = 0.7, severity: float = 0.3) -> TradeProposal:
    decision = make_decision("AAPL").model_copy(update={"confidence": confidence})
    return TradeProposal(symbol="AAPL", side=decision.action.to_order_side(),
                         source=ProposalSource.AI, decision=decision,
                         skeptic=_skeptic(severity=severity), created_at=SESSION_TIME)


# --- disagreement score -----------------------------------------------------

def test_disagreement_score_increases_with_decision_confidence():
    """The same skeptic severity should read as MORE disagreement against a
    confident decision than against a hedged one — a confident Decision AI
    facing real objections is the more informative signal."""
    low_conf = compute_disagreement_score(
        make_decision("AAPL").model_copy(update={"confidence": 0.2}), _skeptic(severity=0.6))
    high_conf = compute_disagreement_score(
        make_decision("AAPL").model_copy(update={"confidence": 0.95}), _skeptic(severity=0.6))
    assert high_conf > low_conf


def test_disagreement_score_zero_when_skeptic_has_no_concern():
    score = compute_disagreement_score(make_decision("AAPL"), _skeptic(severity=0.0))
    assert score == 0.0


def test_disagreement_score_bounded_to_unit_interval():
    score = compute_disagreement_score(
        make_decision("AAPL").model_copy(update={"confidence": 1.0}), _skeptic(severity=1.0))
    assert 0.0 <= score <= 1.0


# --- schema invariants -------------------------------------------------------

def test_final_trade_thesis_requires_decision_and_skeptic_ids():
    proposal = _proposal()
    with pytest.raises(Exception):
        FinalTradeThesis(decision_id="", skeptic_id="skeptic:x", disagreement_score=0.1,
                         proposal=proposal, created_at=SESSION_TIME)
    with pytest.raises(Exception):
        FinalTradeThesis(decision_id="d1", skeptic_id="", disagreement_score=0.1,
                         proposal=proposal, created_at=SESSION_TIME)


def test_final_trade_thesis_rejects_proposal_without_skeptic():
    """A Final Trade Thesis cannot exist for a proposal the Skeptic never
    reviewed — that would defeat the whole point of the stage."""
    decision = make_decision("AAPL")
    unreviewed = TradeProposal(symbol="AAPL", side=decision.action.to_order_side(),
                               source=ProposalSource.AI, decision=decision,
                               skeptic=None, created_at=SESSION_TIME)
    with pytest.raises(Exception, match="Skeptic"):
        FinalTradeThesis(decision_id="d1", skeptic_id="skeptic:x", disagreement_score=0.1,
                         proposal=unreviewed, created_at=SESSION_TIME)


def test_build_final_trade_thesis_rejects_proposal_without_skeptic():
    decision = make_decision("AAPL")
    unreviewed = TradeProposal(symbol="AAPL", side=decision.action.to_order_side(),
                               source=ProposalSource.AI, decision=decision,
                               skeptic=None, created_at=SESSION_TIME)
    with pytest.raises(ValueError, match="Skeptic"):
        build_final_trade_thesis(unreviewed, decision_id="d1", skeptic_id="skeptic:x")


def test_final_trade_thesis_is_frozen():
    thesis = build_final_trade_thesis(_proposal(), decision_id="d1", skeptic_id="skeptic:x")
    with pytest.raises(Exception):
        thesis.disagreement_score = 0.9  # type: ignore[misc]


def test_build_final_trade_thesis_carries_the_skeptic_agent_identity():
    thesis = build_final_trade_thesis(_proposal(), decision_id="d1",
                                      skeptic_id="skeptic:anthropic:claude-opus-5")
    assert thesis.skeptic_id == "skeptic:anthropic:claude-opus-5"
    assert thesis.decision_id == "d1"
    assert thesis.proposal.decision.symbol == "AAPL"


# --- pipeline wiring ---------------------------------------------------------

def test_pipeline_builds_a_thesis_for_every_surviving_candidate(pipeline):
    result = pipeline.run_session(SESSION_TIME)
    if result.decision_candidates == 0:
        pytest.skip("no BUY candidate survived the skeptic this session")
    assert len(pipeline._final_theses) >= 1
    for decision_id, thesis in pipeline._final_theses.items():
        assert thesis.decision_id == decision_id
        assert thesis.skeptic_id                      # never empty
        assert 0.0 <= thesis.disagreement_score <= 1.0


def test_approved_order_snapshot_carries_skeptic_id():
    """Closes a traceability gap: `ApprovedOrderSnapshot.skeptic_id` existed
    on the schema but nothing populated it before the Final Trade Thesis
    wiring — an executed order's audit trail could not say which Skeptic
    agent had reviewed it. `from_approved` must round-trip the value."""
    from datetime import timezone

    from packages.common.environment import Environment
    from packages.schemas.audit import ApprovedOrderSnapshot
    from packages.schemas.core import (
        Action, OrderIntent, OrderType, RiskApproval, RiskApprovedOrder,
        order_intent_hash,
    )

    intent = OrderIntent(client_order_id="thesis-snap-0001", proposal_id="p1", symbol="AAPL",
                         side=Action.BUY, qty=1.0, order_type=OrderType.MARKET,
                         environment=Environment.PAPER, created_at=SESSION_TIME)
    approval = RiskApproval(approval_id="a1", client_order_id=intent.client_order_id,
                            symbol="AAPL", side=Action.BUY, qty=1.0,
                            intent_hash=order_intent_hash(intent), checks=(),
                            risk_state="NORMAL", approved_at=SESSION_TIME, signature="sig")
    approved = RiskApprovedOrder(intent=intent, approval=approval)
    snap = ApprovedOrderSnapshot.from_approved(approved, decision_id="d1",
                                               skeptic_id="skeptic:anthropic:claude-opus-5")
    assert snap.skeptic_id == "skeptic:anthropic:claude-opus-5"


def test_pipeline_populates_snapshot_skeptic_id_from_the_thesis(pipeline, monkeypatch):
    """End-to-end: whatever skeptic_id the thesis carried for a decision must
    be the one that reaches the snapshot execution actually submits."""
    from packages.schemas.audit import ApprovedOrderSnapshot

    seen: list[str] = []
    original = ApprovedOrderSnapshot.from_approved.__func__

    def _spy(cls, order, decision_id="", audit_id="", take_profit=None,
            strategy_id="", skeptic_id="", at=None):
        seen.append(skeptic_id)
        return original(cls, order, decision_id=decision_id, audit_id=audit_id,
                        take_profit=take_profit, strategy_id=strategy_id,
                        skeptic_id=skeptic_id, at=at)

    monkeypatch.setattr(ApprovedOrderSnapshot, "from_approved", classmethod(_spy))
    result = pipeline.run_session(SESSION_TIME)
    if result.orders_filled == 0:
        pytest.skip("no order reached execution this session")
    assert seen
    # a BUY entry carries the reviewing Skeptic's id; the automatic protective
    # stop placed right after it is not a Decision/Skeptic product and
    # legitimately carries none — so at least one call must be non-empty,
    # not every call
    assert any(sid for sid in seen)
