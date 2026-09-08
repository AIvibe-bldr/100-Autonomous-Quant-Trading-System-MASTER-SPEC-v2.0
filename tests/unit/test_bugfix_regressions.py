"""Regression tests for bugs found in review.

Each test pins behaviour that was previously wrong:
1. protective stops were planned but never submitted (§33-34, INV-15)
2. resting stop orders never triggered after the session that placed them
3. re-polling a resting/partially filled order over-filled it
4. FeatureStore cascaded demotions when re-evaluated without new data (§59)
5. monthly() and trend() disagreed on GOOD% (A2)
6. ExitOptimizer.reject() discarded the reason
7. broker disconnect during exit sync crashed the session
8. NewsEngine deduped forever, so a repeat event could never be news again
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from packages.broker_adapters.paper import Fault
from packages.common.experiments import ExperimentRegistry
from packages.schemas.core import Action, OrderState, OrderType, StopType
from services.feature_manager.store import FeatureStatus, FeatureStore
from services.news.engine import NewsEngine, NewsItem, SourceTier
from services.pdca.decision_quality import (
    DecisionKind,
    DecisionQualityEngine,
    DecisionQualityReporter,
    DecisionSnapshot,
)
from services.pdca.exit_optimizer import ExitOptimizer, ExitProposalStage
from services.quant.replay import ReplayEngine
from services.risk.master_controller import RiskState
from tests.conftest import SESSION_TIME, build_pipeline
from tests.unit.helpers import make_stop


# --- 1 & 2: every filled entry gets a REAL protective stop that can trigger --

def test_overnight_requires_stop(pipeline):
    """INV-15: a filled entry must leave a resting protective stop order at the
    broker, not merely a stop plan on paper."""
    result = pipeline.run_session(SESSION_TIME)
    if not result.orders_filled:
        pytest.skip("no entries filled this session")
    assert result.protective_stops_placed == result.orders_filled
    for symbol in pipeline.ledger.positions:
        assert symbol in pipeline.open_stops, f"{symbol} held without a stop order"

    open_orders = pipeline.execution.broker.get_open_orders()
    stop_cids = {cid for cid, _, _, _ in pipeline.open_stops.values()}
    resting = {o.client_order_id for o in open_orders}
    assert stop_cids <= resting, "protective stops are not resting at the broker"


def test_protective_stop_is_sell_side_and_below_entry(pipeline):
    pipeline.run_session(SESSION_TIME)
    for symbol, (cid, stop, entry_px, _risk) in pipeline.open_stops.items():
        order = pipeline.execution._submitted[cid]           # noqa: SLF001
        assert order.intent.side is Action.SELL
        assert order.intent.order_type is OrderType.STOP
        assert order.intent.is_protective_exit is True
        assert order.intent.stop_price < entry_px


def test_resting_stop_triggers_when_price_falls(clock, universe):
    """A stop placed on day 1 must be able to fire on a later day."""
    p = build_pipeline(clock, universe)
    p.run_session(clock.now())
    if not p.open_stops:
        pytest.skip("no positions opened")
    symbol = next(iter(p.open_stops))
    _cid, stop, _entry, _risk = p.open_stops[symbol]

    # crash the market below every stop, then run the next session
    crashed = stop.stop_price * 0.5

    class CrashQuote:
        def __init__(self, inner):
            self.inner = inner

        def get_bars(self, sym, end, days):
            return self.inner.get_bars(sym, end, days)

        def get_quote(self, sym, at):
            q = self.inner.get_quote(sym, at)
            return q.model_copy(update={"bid": crashed, "ask": crashed * 1.001})

    p.market_data.provider = CrashQuote(p.market_data.provider)
    clock.advance(86_400)
    triggered, _ = p.manage_open_positions(clock.now())
    assert triggered >= 1
    assert p.ledger.position_qty(symbol) == pytest.approx(0.0)
    assert symbol not in p.open_stops
    # §37/INV-10: the realized loss must have fed the anti-martingale guard
    assert p.sizing._last_risk_amount is not None                   # noqa: SLF001


# --- 3: re-polling must not over-fill ---------------------------------------

def test_repolling_resting_order_does_not_overfill(pipeline):
    pipeline.execution.broker.fault = Fault.PARTIAL_FILL
    from tests.unit.test_pretrade_audit import _approved

    order = _approved(pipeline, "repoll-test-0001", qty=10.0)
    from packages.schemas.audit import ApprovedOrderSnapshot

    pipeline.execution.submit(order,
                              snapshot=ApprovedOrderSnapshot.from_approved(order))
    first = pipeline.execution.broker.get_order_status("repoll-test-0001")
    assert first.filled_qty == pytest.approx(5.0)

    pipeline.execution.broker.fault = Fault.NONE
    # re-polling twice must neither over-fill nor drive an illegal transition
    # (PARTIALLY_FILLED → REJECTED used to raise here)
    pipeline.execution.sync_open_orders()
    pipeline.execution.sync_open_orders()
    after = pipeline.execution.broker.get_order_status("repoll-test-0001")
    assert after.filled_qty <= 10.0 + 1e-9, "order over-filled beyond requested qty"
    assert after.state in (OrderState.PARTIALLY_FILLED, OrderState.FILLED)
    fills = [f for f in pipeline.execution.broker.get_fills(since=SESSION_TIME)
             if f.client_order_id == "repoll-test-0001"]
    assert sum(f.qty for f in fills) == pytest.approx(after.filled_qty)


def test_long_replay_keeps_ledger_and_broker_consistent(clock, universe):
    from services.reconciliation.engine import ReconciliationEngine

    p = build_pipeline(clock, universe)
    ReplayEngine(p).run(start=date(2026, 3, 2), sessions=40)
    recon = ReconciliationEngine(broker=p.execution.broker, ledger=p.ledger,
                                 risk_controller=p.risk_controller, cash_tolerance=1.0)
    report = recon.reconcile()
    assert report.consistent, [m.detail for m in report.mismatches]
    assert p.ledger.cash >= -1e-9
    for symbol in p.ledger.positions:
        assert symbol in p.open_stops, f"{symbol} unprotected after long replay"


# --- 4: feature drift must not cascade --------------------------------------

def test_drift_does_not_cascade_without_new_data():
    store = FeatureStore()
    store.register("f", "test", status=FeatureStatus.ACTIVE)
    for _ in range(10):
        store.record_contribution("f", -1.0)
    assert store.evaluate_drift("f") is FeatureStatus.REDUCED
    # repeated evaluation on the SAME evidence must not demote further (§59)
    assert store.evaluate_drift("f") is FeatureStatus.REDUCED
    assert store.evaluate_drift("f") is FeatureStatus.REDUCED
    # fresh evidence resumes the ladder
    for _ in range(10):
        store.record_contribution("f", -1.0)
    assert store.evaluate_drift("f") is FeatureStatus.SHADOW


# --- 5: GOOD% must be consistent between report and trend -------------------

def _graded_engine() -> DecisionQualityEngine:
    eng = DecisionQualityEngine()

    def snap(i: str) -> DecisionSnapshot:
        return DecisionSnapshot(decision_id=i, symbol="AAPL", ts=SESSION_TIME,
                                reference_price=100.0, decision=DecisionKind.BUY,
                                confidence=0.7, expected_horizon="1w",
                                expected_return_range=(-0.05, 0.10),
                                had_stop_plan=True, skeptic_consulted=True)

    for i, ret in enumerate([0.10, -0.08]):
        eng.record(snap(f"d{i}"))
        px = 100 * (1 + ret)
        eng.track(SESSION_TIME + timedelta(weeks=2), price_fn=lambda s, t, p=px: p)
    eng.record(snap("pending"))
    return eng


def test_monthly_and_trend_agree_on_good_pct():
    reporter = DecisionQualityReporter(_graded_engine())
    m = reporter.monthly(SESSION_TIME.year, SESSION_TIME.month)
    t = reporter.trend([(SESSION_TIME.year, SESSION_TIME.month)])[0]
    assert t["good_pct"] == pytest.approx(m.pct("GOOD"))
    # resolved-only view is exposed separately and excludes PENDING
    assert t["resolved_good_pct"] == pytest.approx(1 / 2)


# --- 6: reject reason must be kept ------------------------------------------

def test_exit_optimizer_keeps_reject_reason():
    reg = ExperimentRegistry()
    opt = ExitOptimizer(reg)
    p = opt.propose("tighter stop", StopType.ATR, "1x ATR", SESSION_TIME)
    opt.reject(p, "worse than champion in shadow")
    assert p.stage is ExitProposalStage.REJECTED
    assert p.reject_reason == "worse than champion in shadow"
    assert "worse than champion" in str(reg.get(p.experiment_id).result)


# --- 7: broker outage during exit sync is survivable ------------------------

def test_broker_disconnect_during_exit_sync_is_safe(pipeline):
    pipeline.run_session(SESSION_TIME)
    pipeline.execution.broker.fault = Fault.DISCONNECT
    triggered, synced = pipeline.manage_open_positions(SESSION_TIME)
    assert (triggered, synced) == (0, 0)
    assert pipeline.risk_controller.state is RiskState.FULL_BROKER_DISCONNECT


# --- 8: dedup expires after the novelty window ------------------------------

def test_news_dedup_expires_after_window():
    eng = NewsEngine(novelty_window=timedelta(days=3))

    def item(when):
        return NewsItem(title="FDA approves drug", text="", url="u", source="wire",
                        tier=SourceTier.RELIABLE_WIRE, published_at=when,
                        tickers=("MRNA",))

    assert len(eng.process([item(SESSION_TIME)])) == 1
    assert eng.process([item(SESSION_TIME + timedelta(hours=2))]) == []   # duplicate
    later = eng.process([item(SESSION_TIME + timedelta(days=30))])
    assert len(later) == 1, "same headline a month later is news again"
