"""Tests for packages/common/durable_pipeline_state.py and Ledger.restore()."""
from __future__ import annotations

import tempfile
from datetime import datetime, timezone

import pytest

from packages.broker_adapters.paper import PaperBroker
from packages.common.calendar import TradingCalendar
from packages.common.clock import FrozenClock
from packages.common.durable_pipeline_state import DurablePipelineState
from packages.common.environment import Environment
from packages.common.ledger import EntryKind, Ledger, LedgerEntry, PositionLot
from packages.common.risk_config import RiskConfig
from packages.schemas.core import BrokerPosition
from services.market_data.service import MarketDataService, MockProvider
from services.pdca.decision_quality import DecisionKind, DecisionQualityEngine, DecisionSnapshot
from services.reconciliation.engine import ReconciliationEngine
from services.risk.master_controller import MasterRiskController

AT = datetime(2026, 8, 11, tzinfo=timezone.utc)


@pytest.fixture
def db_path():
    with tempfile.NamedTemporaryFile(suffix=".db") as f:
        yield f.name


# --- Ledger.restore() ---------------------------------------------------------

def test_restore_reproduces_an_identical_ledger_after_real_trading():
    original = Ledger(initial_cash=1000.0)
    original.record_fill("AAPL", 2.0, 150.0, fees=1.0, at=AT, note="buy")
    original.record_fill("AAPL", -1.0, 160.0, fees=0.5, at=AT, note="sell half")
    original.deposit(50.0, at=AT, note="extra funding")

    restored = Ledger.restore(
        initial_cash=original.initial_cash, cash=original.cash,
        positions=original.positions, realized_pnl=original.realized_pnl,
        fees_paid=original.fees_paid, high_water_mark=original.high_water_mark,
        entries=original.entries, currency=original.currency)

    assert restored.cash == original.cash
    assert restored.positions == original.positions
    assert restored.realized_pnl == original.realized_pnl
    assert restored.fees_paid == original.fees_paid
    assert restored.high_water_mark == original.high_water_mark
    assert restored.entries == original.entries
    prices = {"AAPL": 160.0}
    assert restored.equity(prices) == original.equity(prices)


def test_restore_does_not_re_deposit_initial_cash():
    """__init__ always appends an "initial funding" entry — restore() must
    not add a SECOND one on top of the persisted entries."""
    original = Ledger(initial_cash=1000.0)
    restored = Ledger.restore(
        initial_cash=original.initial_cash, cash=original.cash,
        positions=original.positions, realized_pnl=original.realized_pnl,
        fees_paid=original.fees_paid, high_water_mark=original.high_water_mark,
        entries=original.entries, currency=original.currency)
    assert len(restored.entries) == 1
    assert restored.cash == 1000.0


# --- DurablePipelineState ------------------------------------------------------

def _decision_snapshot(decision_id: str) -> DecisionSnapshot:
    return DecisionSnapshot(decision_id=decision_id, symbol="AAPL", ts=AT,
                            reference_price=150.0, decision=DecisionKind.BUY,
                            confidence=0.7, expected_horizon="1w",
                            expected_return_range=(-0.05, 0.10),
                            alpha_scores={"fundamental_inflection": 1.0})


def test_has_saved_state_is_false_before_the_first_save(db_path):
    store = DurablePipelineState(db_path, Environment.PAPER)
    assert not store.has_saved_state()


def test_a_full_round_trip_reproduces_ledger_positions_snapshots_and_equity(db_path):
    ledger = Ledger(initial_cash=1000.0)
    ledger.record_fill("AAPL", 2.0, 150.0, fees=1.0, at=AT, note="buy")
    dq = DecisionQualityEngine()
    dq.record(_decision_snapshot("d1"))
    dq.record(_decision_snapshot("d2"))
    equity_series = [{"at": AT.isoformat(), "equity": 1049.0}]

    store = DurablePipelineState(db_path, Environment.PAPER)
    store.save(ledger, dq, equity_series, last_session_at=AT)

    assert store.has_saved_state()
    restored_ledger = store.load_ledger()
    assert restored_ledger.cash == ledger.cash
    assert restored_ledger.positions == ledger.positions
    assert restored_ledger.entries == ledger.entries

    restored_snapshots = {s.decision_id: s for s in store.load_decision_snapshots()}
    assert set(restored_snapshots) == {"d1", "d2"}
    assert restored_snapshots["d1"] == dq.snapshot("d1")

    assert store.load_equity_series() == equity_series
    assert store.load_last_session_at() == AT


def test_save_is_a_full_replace_not_an_append(db_path):
    """Calling save() twice must not double the positions/entries/
    snapshots/equity rows — each save() is the CURRENT full state, not an
    incremental delta."""
    ledger = Ledger(initial_cash=1000.0)
    dq = DecisionQualityEngine()
    dq.record(_decision_snapshot("d1"))
    store = DurablePipelineState(db_path, Environment.PAPER)

    store.save(ledger, dq, [{"at": AT.isoformat(), "equity": 1000.0}], last_session_at=AT)
    store.save(ledger, dq, [{"at": AT.isoformat(), "equity": 1000.0}], last_session_at=AT)

    assert len(store.load_decision_snapshots()) == 1
    assert len(store.load_equity_series()) == 1
    assert len(store.load_ledger().entries) == len(ledger.entries)


def test_paper_and_live_state_never_share_a_row_in_the_same_file(db_path):
    paper_ledger = Ledger(initial_cash=1000.0)
    live_ledger = Ledger(initial_cash=5000.0)
    paper_store = DurablePipelineState(db_path, Environment.PAPER)
    live_store = DurablePipelineState(db_path, Environment.LIVE)

    paper_store.save(paper_ledger, DecisionQualityEngine(), [], last_session_at=None)

    assert paper_store.has_saved_state()
    assert not live_store.has_saved_state()
    assert paper_store.load_ledger().initial_cash == 1000.0


def test_entries_are_restored_in_their_original_order(db_path):
    """entry_id is the primary key, not an ordering guarantee — a naive
    `ORDER BY entry_id` (a random uuid) would scramble replay order, which
    matters for anything that walks the entry log chronologically."""
    ledger = Ledger(initial_cash=1000.0)
    for i in range(5):
        ledger.deposit(1.0, at=AT, note=f"entry {i}")
    store = DurablePipelineState(db_path, Environment.PAPER)
    store.save(ledger, DecisionQualityEngine(), [], last_session_at=None)

    restored = store.load_ledger()
    assert [e.note for e in restored.entries] == [e.note for e in ledger.entries]


# --- PaperBroker.seed_state() + end-to-end reconciliation ---------------------

def test_a_freshly_seeded_broker_reconciles_clean_against_the_restored_ledger():
    """The real risk this whole restore path exists to avoid: PaperBroker
    tracks its own independent cash/positions (it stands in for a real
    broker, whose state ReconciliationEngine checks the ledger against). A
    Ledger restored from durable state with no matching broker seed would
    show the broker holding nothing while the ledger holds real positions
    — startup reconciliation would report a mismatch and HALT_NEW_ENTRIES
    on every single restart."""
    original_ledger = Ledger(initial_cash=1000.0)
    original_ledger.record_fill("AAPL", 2.0, 150.0, fees=1.0, at=AT, note="buy")

    restored_ledger = Ledger.restore(
        initial_cash=original_ledger.initial_cash, cash=original_ledger.cash,
        positions=original_ledger.positions, realized_pnl=original_ledger.realized_pnl,
        fees_paid=original_ledger.fees_paid, high_water_mark=original_ledger.high_water_mark,
        entries=original_ledger.entries, currency=original_ledger.currency)

    clock = FrozenClock(current=AT)
    md = MarketDataService(MockProvider())
    broker = PaperBroker(quote_fn=lambda s: md.quote(s, clock.now()), clock=clock,
                         initial_cash=500.0)  # deliberately wrong before seeding
    broker.seed_state(
        settled_cash=restored_ledger.cash, unsettled_cash=0.0,
        positions={s: BrokerPosition(symbol=s, qty=lot.qty, avg_cost=lot.avg_cost)
                  for s, lot in restored_ledger.positions.items()})

    risk = MasterRiskController(config=RiskConfig(), calendar=TradingCalendar(), clock=clock)
    recon = ReconciliationEngine(broker=broker, ledger=restored_ledger, risk_controller=risk)
    report = recon.reconcile()

    assert report.consistent, f"unexpected mismatches: {report.mismatches}"


def test_an_unseeded_broker_does_not_reconcile_against_a_restored_ledger_with_positions():
    """Negative control: confirms the test above is actually exercising
    seed_state(), not something else masking a real mismatch."""
    ledger = Ledger(initial_cash=1000.0)
    ledger.record_fill("AAPL", 2.0, 150.0, fees=1.0, at=AT, note="buy")

    clock = FrozenClock(current=AT)
    md = MarketDataService(MockProvider())
    broker = PaperBroker(quote_fn=lambda s: md.quote(s, clock.now()), clock=clock,
                         initial_cash=1000.0)  # never seeded

    risk = MasterRiskController(config=RiskConfig(), calendar=TradingCalendar(), clock=clock)
    recon = ReconciliationEngine(broker=broker, ledger=ledger, risk_controller=risk)
    report = recon.reconcile()

    assert not report.consistent
