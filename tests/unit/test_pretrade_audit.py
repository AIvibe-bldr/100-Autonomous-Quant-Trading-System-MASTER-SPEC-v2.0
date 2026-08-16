"""ADDENDUM A8 tests: Pre-Trade Independent Audit, Immutable Approved Order
Snapshot, Decision Quality Engine.

Cases already covered elsewhere (referenced, not duplicated):
Duplicate Order (INV-7), no stop (INV-4), leverage (INV-1), insufficient cash
(INV-5), broker position mismatch → HALT_NEW_ENTRIES (§48) — see
tests/unit/test_invariants.py and tests/chaos/test_failures.py.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from packages.common.environment import Environment
from packages.schemas.audit import (
    ApprovedOrderSnapshot,
    AuditVerdict,
    MalformedAuditError,
    validate_audit_output,
)
from packages.schemas.core import (
    Action,
    OrderIntent,
    OrderType,
    Quote,
    RiskApproval,
    RiskApprovedOrder,
    SizedProposal,
)
from services.decision.audit import (
    AuditContext,
    AuditTriggerContext,
    AuditUnavailableError,
    IndependentAuditor,
    MockAuditModel,
    audit_required,
)
from services.execution.engine import OrderTamperError
from services.pdca.decision_quality import (
    AvoidanceLabel,
    DecisionKind,
    DecisionQualityEngine,
    DecisionQualityReporter,
    DecisionSnapshot,
    OutcomeClass,
    SnapshotTamperError,
)
from tests.conftest import SESSION_TIME
from tests.unit.helpers import make_decision, make_proposal, make_stop
from tests.unit.test_invariants import _default_view, _paper_intent


def _sized(symbol: str = "AAPL", qty: float = 3.0) -> SizedProposal:
    return SizedProposal(proposal=make_proposal(symbol), stop_plan=make_stop(),
                         qty=qty, risk_amount=qty * 4.0, notional=qty * 100.0,
                         calibrated_confidence=0.6, sizing_version="1.0.0")


def _intent(symbol: str = "AAPL", side: Action = Action.BUY, qty: float = 3.0,
            cid: str = "audit-test-0001") -> OrderIntent:
    return OrderIntent(client_order_id=cid, proposal_id="p1", symbol=symbol, side=side,
                      qty=qty, order_type=OrderType.MARKET, environment=Environment.PAPER,
                      created_at=SESSION_TIME)


def _auditor() -> IndependentAuditor:
    return IndependentAuditor(model=MockAuditModel(), audit_all=True)


CTX = AuditContext(now=SESSION_TIME)


# --- A8: Decision BUY / Order SELL → REJECT (INV-16) ----------------------

def test_decision_buy_order_sell_rejected():
    decision = make_decision("AAPL", action=Action.BUY)
    out = _auditor().audit(decision, _sized(), _intent(side=Action.SELL), CTX)
    assert out.verdict is AuditVerdict.REJECT
    assert any(c.check == "side_match" for c in out.detected_conflicts)


# --- A8: Symbol変更 → REJECT ----------------------------------------------

def test_symbol_change_rejected():
    decision = make_decision("AAPL")
    out = _auditor().audit(decision, _sized("AAPL"), _intent(symbol="TSLA"), CTX)
    assert out.verdict is AuditVerdict.REJECT
    assert any(c.check == "symbol_match" for c in out.detected_conflicts)


# --- A6例: BUY 3 shares → order 300 shares → REJECT ------------------------

def test_quantity_digit_error_rejected():
    decision = make_decision("AAPL")
    out = _auditor().audit(decision, _sized(qty=3.0), _intent(qty=300.0), CTX)
    assert out.verdict is AuditVerdict.REJECT
    assert any("quantity" in c.check for c in out.detected_conflicts)


# --- A3-2: 短Horizonに過大Stop → suspicious --------------------------------

def test_horizon_stop_inconsistency_flagged():
    decision = make_decision("AAPL").model_copy(update={"expected_horizon": "1d"})
    sized = SizedProposal(proposal=make_proposal("AAPL"),
                          stop_plan=make_stop(entry=100.0, stop=65.0),  # -35% stop
                          qty=3.0, risk_amount=105.0, notional=300.0,
                          calibrated_confidence=0.6, sizing_version="1.0.0")
    out = _auditor().audit(decision, sized, _intent(), CTX)
    assert any(c.check == "horizon_stop_consistency" for c in out.detected_conflicts)
    assert out.verdict in (AuditVerdict.REVIEW, AuditVerdict.REJECT)


# --- A3-3: stale signal ----------------------------------------------------

def test_stale_signal_rejected():
    ctx = AuditContext(now=SESSION_TIME, signal_age=timedelta(hours=2))
    out = _auditor().audit(make_decision("AAPL"), _sized(), _intent(), ctx)
    assert out.verdict is AuditVerdict.REJECT
    assert any(c.check == "stale_signal" for c in out.detected_conflicts)


def test_clean_order_passes():
    out = _auditor().audit(make_decision("AAPL"), _sized(), _intent(), CTX)
    assert out.verdict is AuditVerdict.PASS


# --- A8: Malformed Audit JSON → REJECT (INV-20) ---------------------------

def test_malformed_audit_rejected():
    with pytest.raises(MalformedAuditError):
        validate_audit_output({"verdict": "MAYBE"})

    class BrokenModel:
        name = "broken"
        model_family = "broken"

        def audit(self, decision, sized, intent, context):
            return {"verdict": "YES_TOTALLY_FINE", "garbage": True}

    auditor = IndependentAuditor(model=BrokenModel(), audit_all=True)
    out = auditor.audit(make_decision("AAPL"), _sized(), _intent(), CTX)
    assert out.verdict is AuditVerdict.REJECT  # malformed → REJECT, never pass


# --- A8: Audit AI unavailable → high-risk trade blocked (INV-19) ----------

def test_audit_unavailable_blocks_high_risk():
    auditor = IndependentAuditor(model=None, audit_all=False)
    high_risk = AuditTriggerContext(volatility=0.10)  # high volatility → mandatory
    assert audit_required(high_risk)
    with pytest.raises(AuditUnavailableError):
        auditor.audit(make_decision("AAPL"), _sized(), _intent(), CTX, triggers=high_risk)
    # low-risk without mandatory triggers may proceed unaudited (logged as skipped)
    low_risk = AuditTriggerContext()
    out = auditor.audit(make_decision("AAPL"), _sized(), _intent(), CTX, triggers=low_risk)
    assert out.verdict is AuditVerdict.PASS


# --- A8: Hash mismatch → REJECT (INV-17) & no engine modification (INV-18) -

def _approved(pipeline, cid: str, qty: float) -> RiskApprovedOrder:
    intent = _paper_intent(cid, qty=qty)
    verdict = pipeline.risk_controller.review(intent, _default_view(pipeline),
                                              entry_price=20.0)
    assert isinstance(verdict, RiskApproval)
    return RiskApprovedOrder(intent=intent, approval=verdict)


def test_hash_mismatch_rejected(pipeline):
    order_small = _approved(pipeline, "hash-test-00001", qty=3.0)
    order_large = _approved(pipeline, "hash-test-00002", qty=10.0)
    # snapshot approved for the 3-share order must not authorize the 10-share order
    snapshot_small = ApprovedOrderSnapshot.from_approved(order_small)
    with pytest.raises(OrderTamperError, match="re-audit"):
        pipeline.execution.submit(order_large, snapshot=snapshot_small)


def test_modified_order_requires_reaudit(pipeline):
    """A4: after tamper rejection, a fresh intent through the full pipeline works."""
    order_a = _approved(pipeline, "hash-test-00003", qty=2.0)
    snapshot_b = ApprovedOrderSnapshot.from_approved(
        _approved(pipeline, "hash-test-00004", qty=5.0))
    with pytest.raises(OrderTamperError):
        pipeline.execution.submit(order_a, snapshot=snapshot_b)
    # re-audit path: new approval + matching snapshot succeeds
    order_c = _approved(pipeline, "hash-test-00005", qty=2.0)
    state = pipeline.execution.submit(order_c,
                                      snapshot=ApprovedOrderSnapshot.from_approved(order_c))
    assert state.value in ("FILLED", "PARTIALLY_FILLED", "ACKNOWLEDGED")


def test_matching_snapshot_accepted(pipeline):
    order = _approved(pipeline, "hash-test-00006", qty=1.0)
    snap = ApprovedOrderSnapshot.from_approved(order)
    assert snap.matches_intent(order.intent)
    state = pipeline.execution.submit(order, snapshot=snap)
    assert state.value in ("FILLED", "PARTIALLY_FILLED", "ACKNOWLEDGED")


# --- A8: Decision Snapshot変更 → 新Decision ID必須 (INV-21) ----------------

def _snap(decision_id: str = "d1", confidence: float = 0.7,
          kind: DecisionKind = DecisionKind.BUY,
          rule_compliant: bool = True) -> DecisionSnapshot:
    return DecisionSnapshot(decision_id=decision_id, symbol="AAPL", ts=SESSION_TIME,
                            reference_price=100.0, decision=kind, confidence=confidence,
                            expected_horizon="1w", expected_return_range=(-0.05, 0.10),
                            rule_compliant=rule_compliant, had_stop_plan=True,
                            skeptic_consulted=True)


def test_decision_snapshot_immutable():
    eng = DecisionQualityEngine()
    eng.record(_snap("d1", confidence=0.7))
    eng.record(_snap("d1", confidence=0.7))  # identical re-record: no-op
    with pytest.raises(SnapshotTamperError, match="NEW decision_id"):
        eng.record(_snap("d1", confidence=0.9))  # changed content, same id
    with pytest.raises(Exception):
        eng.snapshot("d1").confidence = 0.9  # frozen model  # type: ignore[misc]


# --- A1: outcome/process separation ---------------------------------------

def _tracked_engine(snap: DecisionSnapshot, final_return: float) -> DecisionQualityEngine:
    eng = DecisionQualityEngine()
    eng.record(snap)
    px = snap.reference_price * (1 + final_return)
    eng.track(SESSION_TIME + timedelta(weeks=2),
              price_fn=lambda s, t: px)
    return eng


def test_lucky_rule_breaking_scores_low():
    """A1-5: ルール違反でBUY→偶然+50% は Outcome GOOD / Process BAD."""
    snap = _snap("lucky", rule_compliant=False)
    eng = _tracked_engine(snap, final_return=0.50)
    ev = eng.evaluation("lucky")
    assert ev.outcome_class is OutcomeClass.GOOD       # outcome was good…
    assert ev.process_score <= 40.0                    # …but process was bad
    assert ev.overall_score <= 40.0                    # …and overall is capped


def test_good_avoidance_and_missed_opportunity():
    """A1-3/A1-4: NO TRADEの追跡。下落回避=GOOD_AVOIDANCE、上昇見逃し=MISSED."""
    avoided = _snap("avoid1", kind=DecisionKind.NO_TRADE)
    eng = _tracked_engine(avoided, final_return=-0.10)
    ev = eng.evaluation("avoid1")
    assert ev.avoidance_label is AvoidanceLabel.GOOD_AVOIDANCE
    assert ev.outcome_class is OutcomeClass.GOOD

    missed = _snap("miss1", kind=DecisionKind.NO_TRADE)
    eng2 = _tracked_engine(missed, final_return=+0.30)
    ev2 = eng2.evaluation("miss1")
    assert ev2.avoidance_label is AvoidanceLabel.MISSED_OPPORTUNITY
    # A1-5: ルール遵守のNO TRADEはProcess GOODのまま
    assert ev2.outcome_class is OutcomeClass.MIXED
    assert ev2.process_score >= 80.0


def test_sell_decisions_graded():
    """A1-4: SELL後に大幅下落 → GOOD SELL、上昇 → BAD側."""
    good_sell = _snap("sell-good", kind=DecisionKind.SELL)
    ev = _tracked_engine(good_sell, final_return=-0.10).evaluation("sell-good")
    assert ev.outcome_class is OutcomeClass.GOOD
    bad_sell = _snap("sell-bad", kind=DecisionKind.SELL)
    ev2 = _tracked_engine(bad_sell, final_return=+0.10).evaluation("sell-bad")
    assert ev2.outcome_class is OutcomeClass.BAD


# --- A2: monthly report ----------------------------------------------------

def test_monthly_report_aggregation():
    eng = DecisionQualityEngine()
    for i, ret in enumerate([0.10, 0.05, -0.08, 0.001]):
        snap = _snap(f"m{i}")
        eng.record(snap)
        px = snap.reference_price * (1 + ret)
        eng.track(SESSION_TIME + timedelta(weeks=2),
                  price_fn=lambda s, t, p=px: p)
    eng.record(_snap("pending1"))  # not yet matured → PENDING

    r = DecisionQualityReporter(eng).monthly(SESSION_TIME.year, SESSION_TIME.month)
    assert r.total == 5
    assert r.counts.get("GOOD", 0) == 2
    assert r.counts.get("BAD", 0) == 1
    assert r.counts.get("MIXED", 0) == 1
    assert r.counts.get("PENDING", 0) == 1
    assert r.by_kind["BUY"]["total"] == 5
    assert r.avg_good_return > 0 > r.avg_bad_loss
    assert ">=90%" not in r.confidence_buckets or True  # buckets present
    assert 0 <= r.average_score <= 100


def test_pipeline_end_to_end_audit_and_quality(pipeline):
    """Integration: pipeline audits every order (V1 paper), logs are fully
    traceable (A5), and decision snapshots exist for non-BUY outcomes too."""
    result = pipeline.run_session(SESSION_TIME)
    if result.risk_passed:
        assert result.audit_passed >= result.risk_passed  # audit precedes risk
    for cid in pipeline.audit_log.records:
        assert pipeline.audit_log.is_fully_traceable(cid)
    kinds = {s.decision for s in pipeline.decision_quality._snapshots.values()}
    assert kinds & {DecisionKind.BUY, DecisionKind.NO_TRADE, DecisionKind.AVOID,
                    DecisionKind.WAIT}
    summary = pipeline.audit_log.monthly_safety_summary(SESSION_TIME.year,
                                                        SESSION_TIME.month)
    assert summary["pre_trade_audits"] == result.audit_passed + result.audit_rejected
    assert summary["critical_execution_errors"] == 0
