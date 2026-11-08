from __future__ import annotations

from datetime import datetime, timezone

import pytest

from packages.common.calendar import TradingCalendar
from packages.common.clock import Clock, FrozenClock
from packages.common.environment import Environment
from packages.common.ledger import Ledger
from packages.common.provenance import ProvenanceStore
from packages.common.risk_config import RiskConfig
from packages.broker_adapters.paper import PaperBroker
from services.capital_allocation.engine import CapitalAllocationEngine
from services.cost_manager.engine import OperatingCostEngine
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

from services.composition import DEFAULT_SYMBOLS as SYMBOLS  # noqa: E402  (tests import these names)
from services.composition import DEFAULT_THEMES as THEMES  # noqa: E402,F401
from services.composition import build_paper_pipeline  # noqa: E402


def build_pipeline(clock: FrozenClock, universe: UniverseManager, **kwargs) -> TradingPipeline:
    """Test entry point; the wiring itself lives in services/composition.py."""
    return build_paper_pipeline(clock, universe, **kwargs)


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


@pytest.fixture
def pipeline(clock: FrozenClock, universe: UniverseManager) -> TradingPipeline:
    return build_pipeline(clock, universe)
