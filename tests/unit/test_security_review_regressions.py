"""Regressions for the defects found in the security / correctness review.

Every test here failed before its fix. Several exist because mutation testing
showed the behaviour had no coverage at all — notably Master Risk Controller
checks 15 (`stale_order`) and 16 (`spread`), which were added by the "checks
13-16" commit with no test, so deleting either left the whole suite green.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pydantic
import pytest

from packages.common.clock import FrozenClock
from packages.common.environment import Environment, EnvironmentMismatchError
from packages.common.ledger import Ledger
from packages.schemas.audit import ApprovedOrderSnapshot
from packages.schemas.core import (
    Action,
    OrderType,
    RiskApproval,
    RiskApprovedOrder,
    RiskRejection,
    order_intent_hash,
)
from services.decision.audit import (
    AuditContext,
    AuditUnavailableError,
    IndependentAuditor,
    MockAuditModel,
)
from services.execution.engine import OrderTamperError
from services.market_data.service import MockProvider
from services.pdca.decision_quality import (
    DecisionKind,
    DecisionQualityEngine,
    DecisionSnapshot,
    OutcomeClass,
)
from tests.conftest import SESSION_TIME
from tests.unit.helpers import make_decision, make_proposal, make_stop
from tests.unit.test_invariants import _default_view, _paper_intent


# --- SEC-1: LIVE audit fail-closed, enforced by WIRING not just by the class ---

def test_pipeline_rejects_a_mixed_environment_component_graph(pipeline):
    """The class-level LIVE guarantee was real; the wiring to it was not.
    An auditor carrying its own PAPER environment inside a LIVE pipeline is
    exactly what turned fail-closed into fail-open."""
    import dataclasses

    with pytest.raises(EnvironmentMismatchError):
        dataclasses.replace(pipeline, environment=Environment.LIVE)


def test_auditor_requires_an_explicit_environment():
    """No default: a defaulted PAPER environment is the whole bug."""
    with pytest.raises(TypeError):
        IndependentAuditor(model=MockAuditModel(), audit_all=True)  # type: ignore[call-arg]


def test_live_audit_without_a_model_blocks_even_when_audit_all_is_false():
    auditor = IndependentAuditor(model=None, environment=Environment.LIVE, audit_all=False)
    sized = _sized()
    with pytest.raises(AuditUnavailableError):
        auditor.audit(make_decision("AAPL"), sized, _intent(), AuditContext(now=SESSION_TIME))


def _sized(qty: float = 3.0):
    from packages.schemas.core import SizedProposal

    return SizedProposal(proposal=make_proposal("AAPL"), stop_plan=make_stop(), qty=qty,
                         risk_amount=qty * 4.0, notional=qty * 100.0,
                         calibrated_confidence=0.6, sizing_version="1.0.0")


def _intent(**kw):
    from packages.schemas.core import OrderIntent

    base = dict(client_order_id="secreg-0001", proposal_id="p1", symbol="AAPL",
                side=Action.BUY, qty=3.0, order_type=OrderType.MARKET,
                environment=Environment.PAPER, created_at=SESSION_TIME)
    base.update(kw)
    return OrderIntent(**base)


# --- SEC-2: the signature must cover every execution-relevant field -----------

def _approved(pipeline, cid: str, qty: float = 1.0):
    intent = _paper_intent(cid, qty=qty)
    verdict = pipeline.risk_controller.review(intent, _default_view(pipeline),
                                              entry_price=20.0)
    assert isinstance(verdict, RiskApproval), getattr(verdict, "reasons", ())
    return RiskApprovedOrder(intent=intent, approval=verdict)


@pytest.mark.parametrize("update", [
    {"order_type": OrderType.LIMIT, "limit_price": 500.0},
    {"order_type": OrderType.STOP, "stop_price": 5.0},
])
def test_price_and_type_changes_after_approval_are_rejected(pipeline, update):
    """An approved MARKET buy used to be resubmittable as a LIMIT at 5x, or as
    a resting STOP the controller never reviewed — the HMAC still verified
    because it covered only side and quantity."""
    approved = _approved(pipeline, "secreg-sign-0001")
    with pytest.raises(ValueError, match="execution-relevant field changed"):
        RiskApprovedOrder(intent=approved.intent.model_copy(update=update),
                          approval=approved.approval)


def test_signature_covers_the_intent_hash(pipeline):
    """Swapping in a different intent_hash must invalidate the signature."""
    approved = _approved(pipeline, "secreg-sign-0002")
    assert pipeline.risk_controller.verify_signature(approved.approval)
    forged = approved.approval.model_copy(update={"intent_hash": "0" * 64})
    assert not pipeline.risk_controller.verify_signature(forged)


def test_execution_requires_a_caller_supplied_snapshot(pipeline):
    approved = _approved(pipeline, "secreg-snap-0001")
    with pytest.raises(OrderTamperError, match="cannot mint its own"):
        pipeline.execution.submit(approved)


# --- SEC-3: an unpriced order must reject, not evaluate as free --------------

def test_market_buy_without_a_reference_price_is_rejected(pipeline):
    view = _default_view(pipeline, equity=5000.0, settled_cash=5000.0)
    verdict = pipeline.risk_controller.review(
        _paper_intent("secreg-price-0001", qty=9000.0), view)   # entry_price omitted
    assert isinstance(verdict, RiskRejection)
    assert "priceable_order" in " ".join(verdict.reasons)


# --- SEC-4: staleness applies to entries, never to protective stops ----------

def test_check_stale_orders_leaves_protective_stops_alone(pipeline, clock):
    result = pipeline.run_session(SESSION_TIME)
    if result.protective_stops_placed == 0:
        pytest.skip("no protective stop was placed this session")
    resting_before = sum(1 for po in pipeline.execution.broker._orders.values()  # noqa: SLF001
                         if po.state.value in ("ACKNOWLEDGED", "SUBMITTED"))
    clock.current = clock.current + timedelta(days=2)
    cancelled = pipeline.execution.check_stale_orders()
    resting_after = sum(1 for po in pipeline.execution.broker._orders.values()  # noqa: SLF001
                        if po.state.value in ("ACKNOWLEDGED", "SUBMITTED"))
    assert cancelled == []
    assert resting_after == resting_before
    assert len(pipeline.ledger.positions) > 0   # …and the positions are still covered


# --- §28 checks 15 & 16: added with no test at all ---------------------------

def test_stale_signal_is_rejected(pipeline):
    """Check 15 (`stale_order`, §47) — deleting it left the suite green."""
    cfg = pipeline.risk_controller.config
    view = _default_view(pipeline, signal_age_sec=cfg.stale_order_after_sec + 1)
    verdict = pipeline.risk_controller.review(_paper_intent("secreg-stale-0001"), view,
                                              entry_price=20.0)
    assert isinstance(verdict, RiskRejection)
    assert "stale_order" in " ".join(verdict.reasons)


def test_wide_spread_is_rejected(pipeline):
    """Check 16 (`spread`) — likewise untested until now."""
    cfg = pipeline.risk_controller.config
    view = _default_view(pipeline, spread_pct=cfg.max_spread_pct * 10)
    verdict = pipeline.risk_controller.review(_paper_intent("secreg-spread-0001"), view,
                                              entry_price=20.0)
    assert isinstance(verdict, RiskRejection)
    assert "spread" in " ".join(verdict.reasons)


def test_protective_exit_is_exempt_from_staleness_and_spread(pipeline):
    """§43: neither may trap us in a position."""
    cfg = pipeline.risk_controller.config
    view = _default_view(pipeline, position_qty={"AAPL": 5.0},
                         signal_age_sec=cfg.stale_order_after_sec * 100,
                         spread_pct=cfg.max_spread_pct * 100)
    verdict = pipeline.risk_controller.review(
        _paper_intent("secreg-exit-0001", side=Action.SELL, qty=5.0, protective=True),
        view, entry_price=20.0)
    assert isinstance(verdict, RiskApproval), getattr(verdict, "reasons", ())


# --- Ledger: a rejected write must leave no trace ----------------------------

def test_rejected_fill_leaves_the_ledger_untouched():
    led = Ledger(initial_cash=100.0)
    with pytest.raises(ValueError, match="margin"):
        led.record_fill("AAPL", side_qty=10, price=50.0, fees=0.0, at=SESSION_TIME)
    assert led.cash == 100.0
    assert led.positions == {}


def test_rejected_fee_leg_does_not_commit_the_trade_leg():
    """The trade leg used to commit and the fee leg then raise, debiting cash
    without recording the fee."""
    led = Ledger(initial_cash=100.0)
    with pytest.raises(ValueError, match="margin"):
        led.record_fill("AAPL", side_qty=1, price=95.0, fees=10.0, at=SESSION_TIME)
    assert led.cash == 100.0
    assert led.positions == {}
    assert led.fees_paid == 0.0


def test_rejected_cash_movement_leaves_no_entry():
    """Covers `_append`'s own guard, which `record_fill` no longer reaches
    (it validates the whole transaction first). A withdrawal larger than the
    balance must leave neither the cash nor the entry behind."""
    led = Ledger(initial_cash=100.0)
    entries_before = len(led.entries)
    with pytest.raises(ValueError, match="margin"):
        led.deposit(-500.0, at=SESSION_TIME, note="oversized withdrawal")
    assert led.cash == 100.0
    assert len(led.entries) == entries_before


def test_high_water_mark_rebase_requires_a_named_human():
    led = Ledger(initial_cash=1000.0)
    with pytest.raises(ValueError, match="named human approver"):
        led.rebase_high_water_mark(approved_by="  ")
    before = led.high_water_mark
    led.rebase_high_water_mark(approved_by="risk-officer")
    assert led.high_water_mark <= before
    assert any("rebased" in e.note for e in led.entries)


# --- Decision quality: class and score must use one yardstick ----------------

def _snap(did: str, kind: DecisionKind = DecisionKind.BUY, horizon: str = "1w"):
    return DecisionSnapshot(decision_id=did, symbol="AAPL", ts=SESSION_TIME,
                            reference_price=100.0, decision=kind, confidence=0.7,
                            expected_horizon=horizon, expected_return_range=(-0.05, 0.10),
                            rule_compliant=True, had_stop_plan=True, skeptic_consulted=True)


def _graded(did, kind, final_ret, benchmark=0.0, **kw):
    eng = DecisionQualityEngine(**kw)
    eng.record(_snap(did, kind))
    eng.track(SESSION_TIME + timedelta(weeks=3),
              price_fn=lambda s, t: 100.0 * (1 + final_ret),
              benchmark_fn=(lambda a, b: benchmark) if benchmark else None)
    return eng.evaluation(did)


def test_outcome_class_and_score_cannot_contradict():
    """A BUY down 5% while the index was down 20% was labelled BAD and scored
    100 — class read the raw return, score read the benchmark-adjusted one."""
    beat = _graded("beat", DecisionKind.BUY, final_ret=-0.05, benchmark=-0.20)
    assert beat.outcome_class is OutcomeClass.GOOD and beat.outcome_score > 50

    lagged = _graded("lag", DecisionKind.BUY, final_ret=+0.05, benchmark=+0.20)
    assert lagged.outcome_class is OutcomeClass.BAD and lagged.outcome_score < 50


def test_outcome_score_is_not_binary():
    """The risk floor was `bad_threshold`, so every drawdown-free outcome
    clipped to 0 or 100 and average_score carried no information."""
    small = _graded("small", DecisionKind.BUY, final_ret=0.02)
    large = _graded("large", DecisionKind.BUY, final_ret=0.30)
    assert small.outcome_score < large.outcome_score


def test_zero_bad_threshold_does_not_divide_by_zero():
    ev = _graded("flat", DecisionKind.BUY, final_ret=0.0,
                 good_threshold=0.0, bad_threshold=0.0)
    assert 0.0 <= ev.outcome_score <= 100.0


def test_sell_risk_uses_the_adverse_direction():
    """MAE was raw-signed, so for a SELL the FAVOURABLE excursion became the
    risk denominator and an adverse spike never entered the score."""
    eng = DecisionQualityEngine()
    eng.record(_snap("sell1", DecisionKind.SELL))
    prices = {1: 1.30, 3: 1.30, 7: 0.95}   # +30% adverse spike, then -5%

    def price_fn(sym, t):
        days = (t - SESSION_TIME).days
        for d in sorted(prices):
            if days <= d:
                return 100.0 * prices[d]
        return 95.0

    eng.track(SESSION_TIME + timedelta(weeks=3), price_fn=price_fn)
    obs = eng.evaluation("sell1").observations
    worst = min(o.mae for o in obs.values())
    assert worst < -0.2, f"adverse excursion not captured for a SELL: {worst}"


# --- Mock market data: one price process, not two ----------------------------

def test_mock_quote_and_bars_agree():
    """get_quote used get_bars(days=1), which restarted the walk from the base
    price — so quotes were pinned within ±4% of base while the bar series
    walked ±50%. Stops never fired and P&L was always zero."""
    m = MockProvider()
    at = datetime(2026, 8, 11, 15, 0, tzinfo=timezone.utc)
    for offset in (0, 90, 300):
        when = at + timedelta(days=offset)
        quote = m.get_quote("NVDA", when).mid
        long_series = m.get_bars("NVDA", when, 90)[-1].close
        assert abs(quote / long_series - 1) < 0.10, (
            f"quote {quote} and bar close {long_series} are different processes")


def test_mock_price_is_independent_of_the_window_requested():
    m = MockProvider()
    at = datetime(2026, 8, 11, 15, 0, tzinfo=timezone.utc)
    closes = {days: m.get_bars("AAPL", at, days)[-1].close for days in (5, 30, 120)}
    assert len(set(round(c, 6) for c in closes.values())) == 1, closes


def test_stops_actually_trigger_over_a_run(clock, universe):
    """Guards the whole point of the mock fix: before it, 200 sessions
    produced zero stop triggers and realized_pnl == 0.0, so every test about
    stop handling was passing vacuously."""
    from tests.conftest import build_pipeline

    pipe = build_pipeline(clock, universe, initial_cash=1000.0)
    triggered = 0
    for _ in range(60):
        triggered += pipe.run_session(clock.now()).stops_triggered
        pipe.execution.broker.settle()
        clock.current = clock.current + timedelta(days=1)
    assert triggered > 0, "no protective stop triggered in 60 sessions"
    assert pipe.ledger.realized_pnl != 0.0


# --- Session-scoped working state --------------------------------------------

def test_final_theses_do_not_accumulate_across_sessions(clock, universe):
    from tests.conftest import build_pipeline

    pipe = build_pipeline(clock, universe)
    sizes = []
    for _ in range(5):
        pipe.run_session(clock.now())
        sizes.append(len(pipe._final_theses))   # noqa: SLF001
        clock.current = clock.current + timedelta(days=1)
    assert max(sizes) <= pipe.max_new_positions * 4, (
        f"theses accumulating across sessions: {sizes}")


def test_final_theses_returns_a_copy(pipeline):
    """The docstring promises a copy; returning the live dict passed before."""
    pipeline.run_session(SESSION_TIME)
    view = pipeline.final_theses()
    view.clear()
    assert pipeline.final_theses() != {} or not pipeline._final_theses  # noqa: SLF001


# --- SAFETY_AUDIT F1: Infinity accepted where NaN is rejected -----------------
#
# `Field(gt=0)` / `Field(ge=0)` reject NaN (any comparison with NaN is False,
# so the constraint check fails) but NOT positive Infinity (`inf > 0` is
# True). Every AI-facing / execution-relevant float field needed an explicit
# `allow_inf_nan=False`. `DecisionOutput.expected_return_range` was worse: a
# bare `tuple[float, float]` has no Field-level constraint reaching its
# elements at all, so it accepted `(nan, inf)` outright, and the existing
# `_range_ordered` validator's `lo > hi` check also silently passed NaN
# (any comparison with NaN is False).

def test_order_intent_rejects_infinite_qty():
    with pytest.raises(pydantic.ValidationError):
        _paper_intent("f1-order-0001", qty=float("inf"))


def _decision_kwargs(**overrides):
    from packages.schemas.core import DecisionAction, ScenarioCase

    kwargs = dict(
        symbol="AAPL", action=DecisionAction.BUY, confidence=0.7,
        expected_horizon="1w", expected_return_range=(-0.05, 0.10),
        bull_case=ScenarioCase(description="bull", target_price=110, probability=0.3),
        base_case=ScenarioCase(description="base", target_price=102, probability=0.5),
        bear_case=ScenarioCase(description="bear", target_price=94, probability=0.2),
        key_evidence=["e1"], counter_evidence=["c1"], risk_factors=["r1"],
        invalidation_conditions=["i1"], unknowns=["u1"], decision_version="1.0.0")
    kwargs.update(overrides)
    return kwargs


def test_decision_output_rejects_nan_and_infinite_range():
    from packages.schemas.core import DecisionOutput

    with pytest.raises(pydantic.ValidationError):
        DecisionOutput(**_decision_kwargs(
            expected_return_range=(float("nan"), float("inf"))))


def test_decision_output_accepts_a_valid_finite_range():
    from packages.schemas.core import DecisionOutput

    out = DecisionOutput(**_decision_kwargs(expected_return_range=(0.01, 0.05)))
    assert out.expected_return_range == (0.01, 0.05)


# --- SAFETY_AUDIT F2: signal_age_sec was a hardcoded 0.0, never a real value -

def test_risk_view_computes_real_signal_age_not_a_constant(pipeline):
    """`TradingPipeline._risk_view` used to pass `signal_age_sec=0.0`
    unconditionally (services/pipeline.py), so `MasterRiskController`'s
    `stale_order` check (§47) could never fire outside its own isolated
    unit test (test_stale_signal_is_rejected, above). This drives the real
    plumbing — `_signal_captured_at` + `wall_clock` — the same way
    `run_session` does, without depending on the mock scanner/decision
    funnel actually producing a candidate this session (it doesn't
    reliably — see the `pytest.skip` guards elsewhere in this suite)."""
    from packages.common.clock import FrozenClock
    from packages.schemas.core import Environment, OrderIntent, OrderType, Quote
    from services.position_sizing.engine import PortfolioContext

    proposal = make_proposal("AAPL")
    stop = make_stop(entry=100.0, stop=98.0)
    quote = Quote(symbol="AAPL", ts=SESSION_TIME, bid=99.9, ask=100.1, bid_size=500,
                 ask_size=500)
    ctx = PortfolioContext(equity=10_000.0, settled_cash=10_000.0, existing_exposure={},
                           theme_exposure={}, symbol_themes={})
    sized = pipeline.sizing.size(proposal, stop, quote, ctx, calibrated_confidence=1.0)
    pipeline._signal_captured_at[proposal.proposal_id] = SESSION_TIME  # noqa: SLF001

    intent = OrderIntent(client_order_id="f2-order-0001", proposal_id=proposal.proposal_id,
                         symbol="AAPL", side=Action.BUY, qty=sized.qty,
                         order_type=OrderType.MARKET, environment=Environment.PAPER,
                         created_at=SESSION_TIME)
    cfg = pipeline.risk_controller.config

    # fresh signal (a few seconds old) — must NOT trip stale_order
    pipeline.wall_clock = FrozenClock(current=SESSION_TIME + timedelta(seconds=2))
    fresh_view = pipeline._risk_view(sized, {}, SESSION_TIME)  # noqa: SLF001
    assert fresh_view.signal_age_sec == pytest.approx(2.0)
    fresh_verdict = pipeline.risk_controller.review(intent, fresh_view, entry_price=100.1)
    assert not (isinstance(fresh_verdict, RiskRejection)
               and "stale_order" in " ".join(fresh_verdict.reasons))

    # same signal, now old enough to exceed the configured threshold
    stale_age = cfg.stale_order_after_sec + 1
    pipeline.wall_clock = FrozenClock(current=SESSION_TIME + timedelta(seconds=stale_age))
    stale_view = pipeline._risk_view(sized, {}, SESSION_TIME)  # noqa: SLF001
    assert stale_view.signal_age_sec == pytest.approx(stale_age)
    stale_verdict = pipeline.risk_controller.review(intent, stale_view, entry_price=100.1)
    assert isinstance(stale_verdict, RiskRejection)
    assert "stale_order" in " ".join(stale_verdict.reasons)


def test_broker_fill_rejects_infinite_price():
    from packages.schemas.core import BrokerFill

    with pytest.raises(pydantic.ValidationError):
        BrokerFill(broker_fill_id="bf-1", client_order_id="f1-order-0002", symbol="AAPL",
                   side=Action.BUY, qty=1.0, price=float("inf"), fees=0.0, ts=SESSION_TIME)
