"""Composition root for the PAPER pipeline: the one place that wires the engines together.

It used to live in tests/conftest.py, which meant a long-running process would have had to
import from the test tree. The wiring is unchanged; `tests.conftest.build_pipeline` now just
delegates here."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from packages.broker_adapters.paper import PaperBroker
from packages.common.calendar import TradingCalendar
from packages.common.clock import Clock
from packages.common.environment import Environment
from packages.common.ledger import Ledger
from packages.common.provenance import ProvenanceStore
from packages.common.risk_config import RiskConfig
from services.capital_allocation.engine import CapitalAllocationEngine
from services.cost_manager.engine import OperatingCostEngine
from services.data_validation.integrity import DataIntegrityEngine
from services.decision.audit import IndependentAuditor, MockAuditModel
from services.decision.models import CalibrationTracker, MockDecisionModel, MockSkepticModel
from services.execution.engine import ExecutionEngine
from services.execution.state_machine import OrderStateMachine
from services.loss_control.engine import LossControlEngine
from services.market_data.service import MarketDataService, MockProvider
from services.market_data.universe import UniverseManager
from services.pipeline import TradingPipeline
from services.position_sizing.engine import PositionSizingEngine
from services.quant.scanner import QuantScanner
from services.risk.master_controller import MasterRiskController

DEFAULT_SYMBOLS = ["AAPL", "MSFT", "NVDA", "AMD", "AVGO", "TSLA", "AMZN", "GOOG", "META", "NFLX",
           "PLTR", "CRWD", "PANW", "SNOW", "DDOG", "MRNA", "PFE", "XOM", "CVX", "JPM"]

DEFAULT_THEMES = {
    "NVDA": ["AI", "Semiconductor"], "AMD": ["AI", "Semiconductor"],
    "AVGO": ["AI", "Semiconductor"], "AAPL": ["Consumer"], "MSFT": ["AI", "SaaS"],
    "TSLA": ["EV"], "AMZN": ["Consumer"], "GOOG": ["AI"], "META": ["AI"],
    "NFLX": ["Consumer"], "PLTR": ["AI"], "CRWD": ["SaaS"], "PANW": ["SaaS"],
    "SNOW": ["SaaS"], "DDOG": ["SaaS"], "MRNA": ["Biotech"], "PFE": ["Healthcare"],
    "XOM": ["Energy"], "CVX": ["Energy"], "JPM": ["Finance"],
}


def build_paper_pipeline(clock: Clock, universe: UniverseManager,
                   initial_cash: float = 1000.0,
                   config: RiskConfig | None = None,
                   decision_model=None, skeptic_model=None,
                   auditor: IndependentAuditor | None = None,
                   cost_engine: OperatingCostEngine | None = None,
                   wall_clock=None, audit_store=None, provider=None,
                   kill_switch=None, news_source=None,
                   event_source=None, order_store=None, observers=None,
                         flow_filings_source=None) -> TradingPipeline:
    """decision_model / skeptic_model / auditor default to the deterministic
    Mocks (keeps tests network-free and reproducible); pass real Claude
    adapters (services.decision.claude_adapters.build_llm_stack) to run the
    pipeline against the actual API — see scripts/run_llm_paper_demo.py."""
    env = Environment.PAPER
    cfg = config or RiskConfig()
    md = MarketDataService(provider or MockProvider())
    ledger = Ledger(initial_cash=initial_cash)
    broker = PaperBroker(quote_fn=lambda s: md.quote(s, clock.now()), clock=clock,
                         initial_cash=initial_cash)
    calendar = TradingCalendar()
    risk = MasterRiskController(config=cfg, calendar=calendar, clock=clock)
    sm = OrderStateMachine(clock=clock)
    execution = ExecutionEngine(broker=broker, risk_controller=risk, state_machine=sm,
                                clock=clock, environment=env, kill_switch=kill_switch,
                                store=order_store)
    return TradingPipeline(
        environment=env, clock=clock, market_data=md, universe=universe,
        scanner=QuantScanner(md, min_dollar_volume=1_000_000),
        integrity=DataIntegrityEngine(),
        decision_model=decision_model or MockDecisionModel(),
        skeptic=skeptic_model or MockSkepticModel(),
        calibration=CalibrationTracker(),
        loss_control=LossControlEngine(),
        sizing=PositionSizingEngine(config=cfg),
        allocation=CapitalAllocationEngine(config=cfg),
        risk_controller=risk, execution=execution, ledger=ledger,
        provenance=ProvenanceStore(env), wall_clock=wall_clock or Clock(),
        audit_store=audit_store,
        # V1 paper mode: audit every order to collect data (A3-6)
        auditor=auditor or IndependentAuditor(model=MockAuditModel(), environment=env,
                                              audit_all=True),
        cost_engine=cost_engine, symbol_themes=DEFAULT_THEMES, news_source=news_source,
        event_source=event_source, observers=list(observers or []),
        flow_filings_source=flow_filings_source)


@dataclass
class RealDataSources:
    """Market data, news and event sources for a run on real data. Orders still go to the
    PaperBroker; these are read-only data feeds."""
    provider: Any
    news_source: Optional[Any] = None
    event_source: Optional[Any] = None
    flow_filings_source: Optional[Any] = None          # SEC 13D/13G, when EDGAR is configured
    notices: list[str] = field(default_factory=list)       # e.g. "EDGAR disabled: ..."


def build_real_data_sources() -> RealDataSources:
    """Alpaca market data, news and corporate actions (needs APCA_API_KEY_ID /
    APCA_API_SECRET_KEY — raises MarketDataUnavailable without them: no data, no session),
    plus SEC EDGAR 8-K / Form 4 news and earnings estimates when QUANT_SEC_USER_AGENT is set."""
    from services.market_data.alpaca_events import AlpacaCorporateActionsSource
    from services.market_data.alpaca_provider import AlpacaMarketDataProvider
    from services.market_data.edgar_client import EdgarClient, EdgarUnavailable
    from services.market_data.event_source import EventsUnavailable
    from services.news.alpaca_news import AlpacaNewsSource
    from services.news.source import NewsUnavailable

    out = RealDataSources(provider=AlpacaMarketDataProvider.from_env())
    try:
        out.event_source = AlpacaCorporateActionsSource.from_env()
    except EventsUnavailable:
        out.event_source = None
    try:
        out.news_source = AlpacaNewsSource.from_env()          # same Alpaca credentials
    except NewsUnavailable:
        out.news_source = None                                  # Decision is told "news not available"
    try:
        edgar = EdgarClient.from_env()
    except EdgarUnavailable as e:
        out.notices.append(f"EDGAR disabled: {e}")
        return out
    from services.market_data.edgar_earnings import EdgarEarningsEstimator
    from services.market_data.event_composite import CompositeEventSource
    from services.news.composite import CompositeNewsSource
    from services.news.edgar_insider import EdgarInsiderNewsSource
    from services.news.edgar_news import EdgarFilingsNewsSource
    out.news_source = CompositeNewsSource(
        [x for x in (out.news_source, EdgarFilingsNewsSource(edgar),
                     EdgarInsiderNewsSource(edgar)) if x is not None])
    out.event_source = CompositeEventSource(
        [x for x in (out.event_source, EdgarEarningsEstimator(edgar)) if x is not None])
    from services.institutional.sources import EdgarOwnershipFilingsSource
    out.flow_filings_source = EdgarOwnershipFilingsSource(edgar)
    return out


def attach_learning_observers(pipeline: TradingPipeline, initial_cash: float, pit_store=None) -> list:
    """Attach the read-only learning observers (forecast grading, P&L attribution, exit/counterfactual
    review, daily review, shadow portfolios + alpha shadow-running, and Point-in-Time recording when a store is given). Returns them."""
    from services.pdca.forecast_observer import ForecastObserver
    from services.pdca.performance_observers import (
        AttributionObserver,
        DailyReviewObserver,
        ExitReviewObserver,
    )
    from services.pdca.pit_observer import PitObserver
    from services.pdca.shadow_observer import ShadowObserver

    observers = [ForecastObserver(), AttributionObserver(), ExitReviewObserver(pipeline.post_trade),
                 DailyReviewObserver(initial_cash), ShadowObserver(initial_cash)]
    if pit_store is not None:
        observers.append(PitObserver(pit_store))
    pipeline.observers.extend(observers)
    return observers
