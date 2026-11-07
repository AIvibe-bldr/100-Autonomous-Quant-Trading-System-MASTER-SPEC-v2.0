"""Broker interface contract (docs/paper_live_gate.md T2-T4): fills are fetched per order,
a broker that watches its own resting stops needs no simulator hook, and an account the
broker has blocked halts entries."""
import pytest

from packages.broker_adapters.base import BrokerAdapter
from packages.broker_adapters.paper import PaperBroker
from packages.schemas.core import Action, BrokerAccountStatus, Quote
from services.risk.master_controller import RiskState
from tests.conftest import SESSION_TIME
from tests.unit.helpers import make_stop, submit_approved
from tests.unit.test_pretrade_audit import _approved


class SelfWatchingBroker(PaperBroker):
    """Behaves like a real broker: it evaluates resting orders on its own (whenever it is
    asked), and exposes NO simulator hook (uses the interface default, which is [])."""
    poll_resting_orders = BrokerAdapter.poll_resting_orders      # the real-broker default

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.price = 20.0
        self.blocked = False

    def get_order_status(self, client_order_id):
        for po in list(self._orders.values()):
            if po.state.value in ("ACKNOWLEDGED", "PARTIALLY_FILLED"):
                self._execute(po)
        return super().get_order_status(client_order_id)

    def get_account_status(self):
        return BrokerAccountStatus(trading_blocked=self.blocked, reason="account restricted")


def _swap_in(pipeline, clock):
    b = SelfWatchingBroker(
        quote_fn=lambda s: Quote(symbol=s, ts=clock.now(), bid=b.price - 0.05, ask=b.price + 0.05,
                                 bid_size=100, ask_size=100),
        clock=clock, initial_cash=1000.0)
    pipeline.execution.broker = b
    pipeline.reconciliation.broker = b
    return b


def _buy_and_book(pipeline, b):
    submit_approved(pipeline, _approved(pipeline, "buy-contract-001", qty=5.0))
    for f in b.get_order_fills("buy-contract-001"):
        pipeline._record_fill(f.symbol, f.qty, f.price, f.fees, f.ts, note=f.client_order_id)


def test_get_order_fills_returns_only_that_orders_fills(pipeline, clock):
    b = _swap_in(pipeline, clock)
    submit_approved(pipeline, _approved(pipeline, "ord-a-0000001", qty=2.0))
    submit_approved(pipeline, _approved(pipeline, "ord-b-0000001", qty=3.0))
    assert [f.qty for f in b.get_order_fills("ord-a-0000001")] == [2.0]
    assert [f.qty for f in b.get_order_fills("ord-b-0000001")] == [3.0]
    assert b.get_order_fills("unknown") == []


def test_stop_filled_by_a_self_watching_broker_reaches_the_ledger_once(pipeline, clock):
    b = _swap_in(pipeline, clock)
    _buy_and_book(pipeline, b)
    assert pipeline.place_protective_stop("AAPL", 5.0, make_stop(entry=20.0, stop=18.0),
                                          clock.now())
    assert pipeline.manage_open_positions(clock.now())[0] == 0       # price above the stop: rests
    b.price = 17.0                                                   # the broker triggers the stop
    triggered, _ = pipeline.manage_open_positions(clock.now())
    assert triggered == 1
    assert "AAPL" not in pipeline.ledger.positions or pipeline.ledger.positions["AAPL"].qty == 0
    assert pipeline.manage_open_positions(clock.now())[0] == 0       # never reported twice
    assert pipeline.reconciliation.reconcile().consistent


def test_each_fill_is_returned_by_a_sync_only_once(pipeline, clock):
    b = _swap_in(pipeline, clock)
    _buy_and_book(pipeline, b)
    pipeline.place_protective_stop("AAPL", 5.0, make_stop(entry=20.0, stop=18.0), clock.now())
    b.price = 17.0
    first = pipeline.execution.sync_open_orders()
    again = pipeline.execution.sync_open_orders()
    assert [f.side for f in first] == [Action.SELL] and again == []


def test_a_broker_blocked_account_is_a_reconciliation_mismatch_and_halts_entries(pipeline, clock):
    b = _swap_in(pipeline, clock)
    assert pipeline.reconciliation.reconcile().consistent
    b.blocked = True
    report = pipeline.reconciliation.reconcile()
    assert [m.kind for m in report.mismatches] == ["account_trading_blocked"]
    assert pipeline.risk_controller.state is RiskState.HALT_NEW_ENTRIES
