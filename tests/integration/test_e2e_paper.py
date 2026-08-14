"""End-to-end paper pipeline (MASTER SPEC §107 — V1 success definition).

市場データ取得 → 判断材料生成 → Loss Plan → Position Size → Risk審査 →
Paper注文 → 約定管理 → 追跡 → 完全なAudit
"""
from __future__ import annotations

import pytest

from packages.schemas.core import OrderState
from services.reconciliation.engine import ReconciliationEngine
from services.risk.master_controller import RiskState
from tests.conftest import SESSION_TIME, build_pipeline


def test_full_session_produces_trades_or_reasons(pipeline):
    result = pipeline.run_session(SESSION_TIME)
    assert result.scanned > 0
    # §93: either trades happened, or every non-trade has a reason
    assert result.traded or result.no_trade_reasons


def test_fills_update_ledger_and_reconcile(pipeline):
    result = pipeline.run_session(SESSION_TIME)
    if not result.traded:
        pytest.skip("mock market produced no trades this session")
    assert pipeline.ledger.positions  # ledger holds what was bought
    recon = ReconciliationEngine(broker=pipeline.execution.broker, ledger=pipeline.ledger,
                                 risk_controller=pipeline.risk_controller,
                                 cash_tolerance=1.0)
    report = recon.reconcile()
    assert report.consistent, [m.detail for m in report.mismatches]
    assert pipeline.risk_controller.state is RiskState.NORMAL


def test_audit_completeness(pipeline):
    """§105: every executed order carries full provenance."""
    pipeline.run_session(SESSION_TIME)
    assert pipeline.provenance.audit_completeness() == 1.0


def test_no_order_without_risk_approval(pipeline):
    pipeline.run_session(SESSION_TIME)
    sm = pipeline.execution.state_machine
    for rec in sm.orders_in_state(*OrderState):
        states = [t.to_state for t in rec.transitions]
        if OrderState.SUBMITTED in states:
            # RISK_APPROVED must precede SUBMITTED for every order (§7)
            assert states.index(OrderState.RISK_APPROVED) < states.index(OrderState.SUBMITTED)


def test_exposure_never_exceeds_equity(pipeline):
    pipeline.run_session(SESSION_TIME)
    prices = {s: pipeline.market_data.quote(s, SESSION_TIME).mid
              for s in pipeline.ledger.positions}
    snap = pipeline.ledger.snapshot(prices)
    assert snap.positions_value <= snap.equity + 1e-6  # leverage <= 1.0 end-to-end
    assert snap.cash >= -1e-9


def test_session_closed_blocks_entries(clock, universe):
    from datetime import datetime, timezone

    p = build_pipeline(clock, universe)
    weekend = datetime(2026, 8, 15, 15, 0, tzinfo=timezone.utc)  # Saturday
    clock.current = weekend
    result = p.run_session(weekend)
    assert not result.traded
    if result.risk_rejected:
        assert any("market_open" in r for r in result.no_trade_reasons)
