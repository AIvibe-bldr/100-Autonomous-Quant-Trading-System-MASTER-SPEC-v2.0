"""Executable Invariants (MASTER SPEC §102).

The safety principles are automated tests, not prose.  IDs map to
docs/invariants.md.
"""
from __future__ import annotations

import ast
from datetime import date, timedelta
from pathlib import Path

import pytest

from packages.broker_adapters.base import DuplicateClientOrderIdError
from packages.common.clock import FrozenClock
from packages.common.environment import (
    Environment,
    EnvironmentMismatchError,
    require_same_environment,
)
from packages.common.ledger import Ledger
from packages.common.risk_config import RiskConfig
from packages.schemas.core import (
    Action,
    OrderIntent,
    OrderType,
    RiskApproval,
    RiskRejection,
)
from services.decision.models import MalformedDecisionError, validate_decision
from services.execution.engine import UnauthorizedOrderError, make_client_order_id
from services.position_sizing.engine import PositionSizingEngine
from services.risk.master_controller import ThrottleLevel, throttle_level
from tests.conftest import SESSION_TIME, build_pipeline

REPO = Path(__file__).resolve().parents[2]


def _paper_intent(client_order_id: str, symbol: str = "AAPL", qty: float = 1.0,
                  side: Action = Action.BUY, protective: bool = False) -> OrderIntent:
    return OrderIntent(client_order_id=client_order_id, proposal_id="p1", symbol=symbol,
                      side=side, qty=qty, order_type=OrderType.MARKET,
                      environment=Environment.PAPER, is_protective_exit=protective,
                      created_at=SESSION_TIME)


def _default_view(pipeline, **overrides):
    from services.data_validation.integrity import DataHealth
    from services.risk.master_controller import PortfolioRiskView

    base = dict(equity=1000.0, settled_cash=1000.0, total_exposure_notional=0.0,
                position_notional={}, position_qty={}, theme_exposure={}, symbol_themes={},
                drawdown=0.0, stop_plan_exists=True, gap_risk_score=0.1,
                adv_shares=1_000_000, correlation_to_book=0.0, reconciliation_ok=True,
                data_health=DataHealth.OK, broker_connected=True)
    base.update(overrides)
    return PortfolioRiskView(**base)


# INV-1: leverage <= 1.0 — an order pushing exposure past equity is rejected
def test_leverage_never_exceeds_one(pipeline):
    view = _default_view(pipeline, total_exposure_notional=900.0)
    intent = _paper_intent("inv1-order-0001", qty=10)  # 10sh × $20 entry → breach
    verdict = pipeline.risk_controller.review(intent, view, entry_price=20.0)
    assert isinstance(verdict, RiskRejection)
    assert any("leverage" in r or "cash" in r or "exposure" in r for r in verdict.reasons)


# INV-2: margin liabilities are structurally impossible
def test_no_margin_liabilities():
    ledger = Ledger(initial_cash=100.0)
    with pytest.raises(ValueError, match="margin"):
        ledger.record_fill("AAPL", side_qty=10, price=20.0, fees=0.0, at=SESSION_TIME)


# INV-3: no short selling
def test_no_short_selling():
    ledger = Ledger(initial_cash=1000.0)
    with pytest.raises(ValueError, match="short"):
        ledger.record_fill("AAPL", side_qty=-5, price=20.0, fees=0.0, at=SESSION_TIME)


# INV-4: every entry has a stop plan
def test_every_entry_has_stop_plan(pipeline):
    view = _default_view(pipeline, stop_plan_exists=False)
    verdict = pipeline.risk_controller.review(_paper_intent("inv4-order-0001"), view,
                                              entry_price=20.0)
    assert isinstance(verdict, RiskRejection)
    assert any("stop" in r for r in verdict.reasons)


# INV-5: order value <= settled cash only (unsettled funds unusable §14)
def test_order_value_within_settled_cash(pipeline):
    view = _default_view(pipeline, settled_cash=50.0)
    verdict = pipeline.risk_controller.review(_paper_intent("inv5-order-0001", qty=10),
                                              view, entry_price=20.0)
    assert isinstance(verdict, RiskRejection)
    assert any("cash_settled" in r for r in verdict.reasons)


# INV-6: AI layer cannot reach the broker — no decision module imports broker_adapters,
# and unsigned orders cannot execute
def test_ai_cannot_reach_broker_imports():
    decision_dir = REPO / "services" / "decision"
    for py in decision_dir.rglob("*.py"):
        tree = ast.parse(py.read_text())
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for n in names:
                assert "broker" not in n.lower(), f"{py} imports broker: {n}"


def test_ai_cannot_reach_broker_signature(pipeline):
    from packages.schemas.core import RiskApprovedOrder, RiskCheck

    intent = _paper_intent("inv6-order-0001")
    forged = RiskApproval(approval_id="forged", client_order_id=intent.client_order_id,
                          symbol=intent.symbol, side=intent.side, qty=intent.qty,
                          checks=(RiskCheck(name="fake", passed=True),),
                          risk_state="NORMAL", approved_at=SESSION_TIME,
                          signature="00" * 32)
    with pytest.raises(UnauthorizedOrderError):
        pipeline.execution.submit(RiskApprovedOrder(intent=intent, approval=forged))


# INV-7: duplicate client order IDs impossible
def test_duplicate_client_order_id_rejected(pipeline):
    view = _default_view(pipeline)
    intent = _paper_intent("inv7-order-0001", qty=1)
    verdict = pipeline.risk_controller.review(intent, view, entry_price=20.0)
    assert isinstance(verdict, RiskApproval)
    from packages.schemas.core import RiskApprovedOrder

    order = RiskApprovedOrder(intent=intent, approval=verdict)
    pipeline.execution.submit(order)
    with pytest.raises(DuplicateClientOrderIdError):
        pipeline.execution.submit(order)


# INV-8: human override cannot bypass risk — overrides flow through the same review
def test_human_override_cannot_bypass_risk(pipeline):
    view = _default_view(pipeline, settled_cash=0.0)  # human wants to buy with no cash
    verdict = pipeline.risk_controller.review(_paper_intent("inv8-order-0001"), view,
                                              entry_price=20.0)
    assert isinstance(verdict, RiskRejection)  # no code path mints approval without review


# INV-9: environment separation
def test_environment_separation():
    assert Environment.LIVE is not Environment.PAPER
    with pytest.raises(EnvironmentMismatchError):
        require_same_environment(Environment.LIVE, Environment.PAPER)


def test_environment_mismatch_order_rejected(pipeline):
    view = _default_view(pipeline)
    intent = OrderIntent(client_order_id="inv9-order-0001", proposal_id="p1", symbol="AAPL",
                        side=Action.BUY, qty=1.0, order_type=OrderType.MARKET,
                        environment=Environment.LIVE, created_at=SESSION_TIME)
    verdict = pipeline.risk_controller.review(intent, view, entry_price=20.0)
    if isinstance(verdict, RiskApproval):
        from packages.schemas.core import RiskApprovedOrder

        with pytest.raises(UnauthorizedOrderError, match="environment"):
            pipeline.execution.submit(RiskApprovedOrder(intent=intent, approval=verdict))


# INV-10: no martingale — risk budget cannot grow after a loss
def test_no_martingale_sizing():
    engine = PositionSizingEngine(config=RiskConfig())
    engine.record_trade_result(realized_pnl=-50.0, risk_amount=5.0)
    from packages.schemas.core import Quote
    from services.position_sizing.engine import PortfolioContext
    from tests.unit.helpers import make_proposal, make_stop

    proposal = make_proposal("AAPL")
    stop = make_stop(entry=100.0, stop=98.0)
    quote = Quote(symbol="AAPL", ts=SESSION_TIME, bid=99.9, ask=100.1, bid_size=500,
                  ask_size=500)
    ctx = PortfolioContext(equity=10_000.0, settled_cash=10_000.0, existing_exposure={},
                           theme_exposure={}, symbol_themes={})
    sized = engine.size(proposal, stop, quote, ctx, calibrated_confidence=1.0)
    # 1% of 10k = 100 risk budget, but last loss risked only 5.0 → capped at 5.0
    assert sized.risk_amount <= 5.0 + 1e-6


# INV-11: total exposure <= 100%
def test_total_exposure_cap():
    from packages.common.risk_config import RiskConfig as RC

    with pytest.raises(ValueError):
        RC(max_total_exposure_pct=1.5)
    with pytest.raises(ValueError):
        RC(max_leverage=2.0)


# INV-12: MASTER STOP still allows protective exits (§43)
def test_master_stop_allows_protective_exit(pipeline):
    from services.risk.master_controller import RiskState

    pipeline.risk_controller.set_state(RiskState.HALT_NEW_ENTRIES)
    view = _default_view(pipeline, position_qty={"AAPL": 5.0})
    entry = pipeline.risk_controller.review(_paper_intent("inv12-entry-001"), view,
                                            entry_price=20.0)
    assert isinstance(entry, RiskRejection)
    exit_intent = _paper_intent("inv12-exit-0001", qty=5, side=Action.SELL, protective=True)
    exit_verdict = pipeline.risk_controller.review(exit_intent, view, entry_price=20.0)
    assert isinstance(exit_verdict, RiskApproval)


# INV-13: risk config is immutable at runtime
def test_risk_config_immutable_at_runtime():
    cfg = RiskConfig()
    with pytest.raises(Exception):
        cfg.max_leverage = 5.0  # type: ignore[misc]


# INV-14: malformed AI output rejected (§28)
def test_malformed_decision_rejected():
    with pytest.raises(MalformedDecisionError):
        validate_decision({"symbol": "AAPL", "action": "YOLO_BUY_EVERYTHING"})
    with pytest.raises(MalformedDecisionError):
        validate_decision({"symbol": "AAPL", "action": "BUY", "confidence": 3.5})


# INV-15 / §39: drawdown throttling ladder
def test_drawdown_throttle_ladder():
    cfg = RiskConfig()
    assert throttle_level(0.05, cfg) is ThrottleLevel.NORMAL
    assert throttle_level(0.15, cfg) is ThrottleLevel.REDUCED_SIZE
    assert throttle_level(0.25, cfg) is ThrottleLevel.NO_NEW_ENTRY
    assert throttle_level(0.35, cfg) is ThrottleLevel.REVIEW_SAFE_MODE
