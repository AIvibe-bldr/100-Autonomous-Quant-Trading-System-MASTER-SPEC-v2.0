from __future__ import annotations

from datetime import datetime, timezone

import pytest

from packages.common.calendar import TradingCalendar
from packages.common.clock import FrozenClock
from packages.common.environment import Environment
from packages.common.ledger import Ledger
from packages.common.provenance import ProvenanceStore
from packages.common.risk_config import RiskConfig
from packages.broker_adapters.paper import PaperBroker
from services.capital_allocation.engine import CapitalAllocationEngine
from services.data_validation.integrity import DataIntegrityEngine
from services.decision.audit import IndependentAuditor, MockAuditModel
from services.decision.models import CalibrationTracker, MockDecisionModel, MockSkepticModel
from services.execution.engine import ExecutionEngine
from services.execution.state_machine import OrderStateMachine
from services.loss_control.engine import LossControlEngine
from services.market_data.service import MarketDataService, MockProvider
from services.market_data.universe import UniverseManager, UniverseSymbol
from services.pipeline import TradingPipeline
from services.position_sizing.engine import PositionSizingEngine
from services.quant.scanner import QuantScanner
from services.risk.master_controller import MasterRiskController

# a Tuesday with US market open (14:30 ET-open in UTC is 13:30 EDT... use 15:00 UTC = 11:00 ET)
SESSION_TIME = datetime(2026, 8, 11, 15, 0, tzinfo=timezone.utc)

SYMBOLS = ["AAPL", "MSFT", "NVDA", "AMD", "AVGO", "TSLA", "AMZN", "GOOG", "META", "NFLX",
           "PLTR", "CRWD", "PANW", "SNOW", "DDOG", "MRNA", "PFE", "XOM", "CVX", "JPM"]

THEMES = {
    "NVDA": ["AI", "Semiconductor"], "AMD": ["AI", "Semiconductor"],
    "AVGO": ["AI", "Semiconductor"], "AAPL": ["Consumer"], "MSFT": ["AI", "SaaS"],
    "TSLA": ["EV"], "AMZN": ["Consumer"], "GOOG": ["AI"], "META": ["AI"],
    "NFLX": ["Consumer"], "PLTR": ["AI"], "CRWD": ["SaaS"], "PANW": ["SaaS"],
    "SNOW": ["SaaS"], "DDOG": ["SaaS"], "MRNA": ["Biotech"], "PFE": ["Healthcare"],
    "XOM": ["Energy"], "CVX": ["Energy"], "JPM": ["Finance"],
}


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(current=SESSION_TIME)


@pytest.fixture
def universe() -> UniverseManager:
    um = UniverseManager()
    from datetime import date
    for s in SYMBOLS:
        um.add(UniverseSymbol(symbol=s, listed_from=date(2015, 1, 1)))
    # a delisted name: visible historically, absent today (§12)
    um.add(UniverseSymbol(symbol="DEADCO", listed_from=date(2015, 1, 1),
                          delisted_at=date(2024, 1, 1)))
    return um


def build_pipeline(clock: FrozenClock, universe: UniverseManager,
                   initial_cash: float = 1000.0,
                   config: RiskConfig | None = None) -> TradingPipeline:
    env = Environment.PAPER
    cfg = config or RiskConfig()
    md = MarketDataService(MockProvider())
    ledger = Ledger(initial_cash=initial_cash)
    broker = PaperBroker(quote_fn=lambda s: md.quote(s, clock.now()), clock=clock,
                         initial_cash=initial_cash)
    calendar = TradingCalendar()
    risk = MasterRiskController(config=cfg, calendar=calendar, clock=clock)
    sm = OrderStateMachine(clock=clock)
    execution = ExecutionEngine(broker=broker, risk_controller=risk, state_machine=sm,
                                clock=clock, environment=env)
    return TradingPipeline(
        environment=env, clock=clock, market_data=md, universe=universe,
        scanner=QuantScanner(md, min_dollar_volume=1_000_000),
        integrity=DataIntegrityEngine(),
        decision_model=MockDecisionModel(), skeptic=MockSkepticModel(),
        calibration=CalibrationTracker(),
        loss_control=LossControlEngine(),
        sizing=PositionSizingEngine(config=cfg),
        allocation=CapitalAllocationEngine(config=cfg),
        risk_controller=risk, execution=execution, ledger=ledger,
        provenance=ProvenanceStore(env),
        # V1 paper mode: audit every order to collect data (A3-6)
        auditor=IndependentAuditor(model=MockAuditModel(), audit_all=True),
        symbol_themes=THEMES)


@pytest.fixture
def pipeline(clock: FrozenClock, universe: UniverseManager) -> TradingPipeline:
    return build_pipeline(clock, universe)
