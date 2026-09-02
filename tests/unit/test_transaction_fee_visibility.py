"""Transaction fees in the cost breakdown (§80-83).

Trading/broker fees are deducted from cash the moment a fill lands
(Ledger._append), so they are already inside `trading_pnl` before the cost
engine ever sees them. These tests cover the follow-up: fees must ALSO be
visible in the §80-83 cost breakdown (by_category / trading_fees_total), but
must never be subtracted a second time from operating_costs/project_net_pnl.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest
from fastapi.testclient import TestClient

from apps.api.main import create_app
from packages.common.clock import FrozenClock
from services.cost_manager.engine import CostCategory, CostEntry, OperatingCostEngine
from services.market_data.universe import UniverseManager, UniverseSymbol
from tests.conftest import SYMBOLS, build_pipeline

SESSION_TIME = datetime(2026, 8, 11, 15, 0, tzinfo=timezone.utc)


def _universe() -> UniverseManager:
    um = UniverseManager()
    for s in SYMBOLS:
        um.add(UniverseSymbol(symbol=s, listed_from=date(2015, 1, 1)))
    return um


# --- OperatingCostEngine: no double count ------------------------------------

def test_transaction_fees_excluded_from_operating_total():
    eng = OperatingCostEngine()
    eng.record(CostEntry(at=SESSION_TIME, category=CostCategory.AI, amount=30.0))
    eng.record_transaction_fee(at=SESSION_TIME, amount=5.0, note="fill-1")
    assert eng.total() == 30.0            # fee excluded — already in trading_pnl
    assert eng.trading_fees_total() == 5.0
    assert eng.by_category()[CostCategory.TRANSACTION_FEE] == 5.0


def test_two_pnl_does_not_double_count_fees():
    eng = OperatingCostEngine()
    eng.record(CostEntry(at=SESSION_TIME, category=CostCategory.SERVER, amount=20.0))
    eng.record_transaction_fee(at=SESSION_TIME, amount=3.5)
    # trading_pnl already reflects the fee (it came out of the ledger's cash)
    two = eng.two_pnl(trading_pnl=100.0)
    assert two.operating_costs == 20.0                 # not 23.5
    assert two.project_net_pnl == 80.0                 # 100 - 20, fee not subtracted again


def test_zero_or_negative_fee_is_not_recorded():
    eng = OperatingCostEngine()
    eng.record_transaction_fee(at=SESSION_TIME, amount=0.0)
    assert eng.entries == []


def test_broker_fee_category_also_excluded_from_total():
    """Both fee categories in the spec's enum are already-in-trading_pnl."""
    eng = OperatingCostEngine()
    eng.record(CostEntry(at=SESSION_TIME, category=CostCategory.BROKER_FEE, amount=2.0))
    eng.record(CostEntry(at=SESSION_TIME, category=CostCategory.AI, amount=1.0))
    assert eng.total() == 1.0
    assert eng.trading_fees_total() == 2.0


# --- pipeline wiring: fees recorded exactly once, matching the ledger --------

def test_pipeline_records_every_fill_fee_into_the_cost_engine(clock, universe):
    cost_engine = OperatingCostEngine()
    pipe = build_pipeline(clock, universe, cost_engine=cost_engine)
    result = pipe.run_session(clock.now())
    if result.orders_filled == 0:
        pytest.skip("no fill this session")
    assert cost_engine.trading_fees_total() == pytest.approx(pipe.ledger.fees_paid, abs=1e-9)


def test_pipeline_without_a_cost_engine_still_works(clock, universe):
    """cost_engine is optional — attaching one must not be required to trade."""
    pipe = build_pipeline(clock, universe)
    assert pipe.cost_engine is None
    result = pipe.run_session(clock.now())
    assert result is not None


# --- status API: visible, and not double-counted in the response -------------

def test_portfolio_endpoint_exposes_fee_breakdown_without_double_counting():
    clock = FrozenClock(current=SESSION_TIME)
    universe = _universe()
    cost_engine = OperatingCostEngine()
    cost_engine.record(CostEntry(at=SESSION_TIME, category=CostCategory.AI, amount=5.0))
    pipeline = build_pipeline(clock, universe, cost_engine=cost_engine)
    pipeline.run_session(clock.now())

    client = TestClient(create_app(pipeline, cost_engine=cost_engine))
    body = client.get("/portfolio").json()

    assert body["operating_costs"] == pytest.approx(5.0)
    assert body["transaction_fees_total"] == pytest.approx(pipeline.ledger.fees_paid, abs=1e-9)
    assert body["cost_breakdown"]["AI"] == pytest.approx(5.0)
    if pipeline.ledger.fees_paid > 0:
        assert body["cost_breakdown"]["TRANSACTION_FEE"] == pytest.approx(
            pipeline.ledger.fees_paid, abs=1e-9)
    # the whole point: operating_costs must not include the fee
    assert body["operating_costs"] != pytest.approx(
        5.0 + pipeline.ledger.fees_paid, abs=1e-9) or pipeline.ledger.fees_paid == 0
    assert body["project_net_pnl"] == pytest.approx(
        body["trading_pnl"] - body["operating_costs"])


def test_api_shares_the_pipelines_cost_engine_when_none_is_passed():
    """If the caller didn't wire a shared cost_engine, create_app must attach
    its own to the pipeline — otherwise fees the pipeline records would never
    reach the instance the API reads from."""
    clock = FrozenClock(current=SESSION_TIME)
    pipeline = build_pipeline(clock, _universe())
    assert pipeline.cost_engine is None

    client = TestClient(create_app(pipeline))   # no cost_engine passed
    assert pipeline.cost_engine is not None     # create_app attached one

    pipeline.run_session(clock.now())
    body = client.get("/portfolio").json()
    assert body["transaction_fees_total"] == pytest.approx(pipeline.ledger.fees_paid, abs=1e-9)


def test_api_uses_the_pipelines_own_cost_engine_over_a_different_one_passed_in():
    """The pipeline's cost_engine (the one it actually writes fees into) must
    win — a second, unrelated instance passed to create_app would silently
    show zero fees forever."""
    clock = FrozenClock(current=SESSION_TIME)
    pipelines_engine = OperatingCostEngine()
    pipeline = build_pipeline(clock, _universe(), cost_engine=pipelines_engine)
    pipeline.run_session(clock.now())

    unrelated_engine = OperatingCostEngine()
    client = TestClient(create_app(pipeline, cost_engine=unrelated_engine))
    body = client.get("/portfolio").json()
    assert body["transaction_fees_total"] == pytest.approx(pipeline.ledger.fees_paid, abs=1e-9)
