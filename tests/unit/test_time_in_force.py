"""T1 (docs/paper_live_gate.md): time-in-force is part of the order's identity, and a
protective stop outlives the session (INV-15)."""
from datetime import timedelta

import pytest

from packages.schemas.audit import ApprovedOrderSnapshot
from packages.schemas.core import (
    BrokerOrderRequest,
    OrderIntent,
    OrderState,
    OrderType,
    RiskApprovedOrder,
    TimeInForce,
    order_intent_hash,
)
from tests.unit.helpers import make_stop, submit_approved
from tests.unit.test_invariants import _paper_intent
from tests.unit.test_pretrade_audit import _approved


def test_a_day_stop_cannot_be_built_as_an_intent_or_a_broker_request():
    with pytest.raises(ValueError, match="GTC"):
        OrderIntent(**{**_paper_intent("tif-stop-000001").model_dump(),
                       "order_type": OrderType.STOP, "stop_price": 5.0})
    with pytest.raises(ValueError, match="GTC"):
        BrokerOrderRequest(client_order_id="tif-stop-000001", symbol="AAPL", side="SELL",
                           qty=1, order_type=OrderType.STOP, stop_price=5.0)
    BrokerOrderRequest(client_order_id="tif-stop-000001", symbol="AAPL", side="SELL", qty=1,
                       order_type=OrderType.STOP, stop_price=5.0, time_in_force=TimeInForce.GTC)


def test_time_in_force_is_in_the_hash_the_signature_and_the_snapshot_check(pipeline):
    approved = _approved(pipeline, "tif-hash-000001", qty=1.0)
    day = approved.intent
    gtc = day.model_copy(update={"time_in_force": TimeInForce.GTC})
    assert order_intent_hash(day) != order_intent_hash(gtc)
    with pytest.raises(ValueError, match="execution-relevant field changed"):
        RiskApprovedOrder(intent=gtc, approval=approved.approval)          # signature covers TIF
    snap = ApprovedOrderSnapshot.from_approved(approved)
    assert snap.matches_intent(day) and not snap.matches_intent(gtc)       # and so does the snapshot


def test_the_pipelines_protective_stop_reaches_the_broker_as_gtc(pipeline, clock):
    submit_approved(pipeline, _approved(pipeline, "tif-buy-0000001", qty=5.0))
    for f in pipeline.execution.broker.get_order_fills("tif-buy-0000001"):
        pipeline._record_fill(f.symbol, f.qty, f.price, f.fees, f.ts, note=f.client_order_id)
    assert pipeline.place_protective_stop("AAPL", 5.0, make_stop(entry=20.0, stop=1.0), clock.now())
    stop_cid = pipeline.open_stops["AAPL"][0]
    po = pipeline.execution.broker._orders[stop_cid]                  # noqa: SLF001
    assert po.request.time_in_force is TimeInForce.GTC

    clock.advance(3 * 86400)                                          # three sessions later
    assert pipeline.execution.broker.get_order_status(stop_cid).state is OrderState.ACKNOWLEDGED


def test_a_resting_day_order_expires_at_the_paper_broker_and_a_gtc_one_does_not(pipeline, clock):
    b = pipeline.execution.broker
    for cid, tif in (("tif-day-0000001", TimeInForce.DAY), ("tif-gtc-0000001", TimeInForce.GTC)):
        b.submit_order(BrokerOrderRequest(
            client_order_id=cid, symbol="AAPL", side="BUY", qty=1, order_type=OrderType.LIMIT,
            limit_price=1.0, time_in_force=tif))                      # far below market: rests
    assert b.get_order_status("tif-day-0000001").state is OrderState.ACKNOWLEDGED
    clock.advance(86400 + 60)
    assert b.get_order_status("tif-day-0000001").state is OrderState.EXPIRED
    assert b.get_order_status("tif-gtc-0000001").state is OrderState.ACKNOWLEDGED
