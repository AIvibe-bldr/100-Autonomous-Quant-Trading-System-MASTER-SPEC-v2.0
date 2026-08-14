"""Tests for corporate actions (§15), event calendar (§16), forecast tracker
(§32), human intent/override (§76-79), experiment registry (§98) and exit
optimizer (§52)."""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from packages.common.experiments import (
    Experiment,
    ExperimentDecision,
    ExperimentRegistry,
)
from packages.common.ledger import Ledger
from packages.schemas.core import Action, ProposalSource, ScenarioCase, StopType
from services.decision.forecast import Forecast, ForecastTracker
from services.decision.human_intent import (
    HumanIntent,
    HumanIntentAnalyzer,
    HumanVsAiAnalytics,
    IntentAction,
    OverrideRecord,
)
from services.market_data.corporate_actions import (
    CorporateAction,
    CorporateActionEngine,
    CorporateActionType,
    adjust_bars_for_split,
)
from services.market_data.event_calendar import EventCalendar, EventType, RiskEvent
from services.market_data.service import MarketDataService, MockProvider
from services.pdca.exit_optimizer import (
    ExitOptimizer,
    ExitProposalStage,
    StagePromotionError,
)
from services.quant.scanner import QuantScanner
from tests.conftest import SESSION_TIME


# --- Corporate actions (§15) ----------------------------------------------

def _ledger_with_position() -> Ledger:
    ledger = Ledger(initial_cash=1000.0)
    ledger.record_fill("AAPL", side_qty=10, price=50.0, fees=0.0, at=SESSION_TIME)
    return ledger


def test_stock_split_preserves_value():
    ledger = _ledger_with_position()
    eng = CorporateActionEngine(ledger)
    eng.apply(CorporateAction(CorporateActionType.STOCK_SPLIT, "AAPL",
                              effective_date=date(2026, 8, 11), ratio=4.0), SESSION_TIME)
    lot = ledger.positions["AAPL"]
    assert lot.qty == pytest.approx(40)
    assert lot.avg_cost == pytest.approx(12.5)
    assert ledger.equity({"AAPL": 12.5}) == pytest.approx(1000.0)  # value unchanged


def test_dividend_credits_cash():
    ledger = _ledger_with_position()
    CorporateActionEngine(ledger).apply(
        CorporateAction(CorporateActionType.DIVIDEND, "AAPL",
                        effective_date=date(2026, 8, 11), cash_per_share=1.5), SESSION_TIME)
    assert ledger.cash == pytest.approx(500 + 15)


def test_cash_merger_closes_position():
    ledger = _ledger_with_position()
    CorporateActionEngine(ledger).apply(
        CorporateAction(CorporateActionType.MERGER, "AAPL",
                        effective_date=date(2026, 8, 11), cash_per_share=60.0), SESSION_TIME)
    assert ledger.position_qty("AAPL") == 0.0
    assert ledger.realized_pnl == pytest.approx(100.0)


def test_symbol_change_moves_position():
    ledger = _ledger_with_position()
    CorporateActionEngine(ledger).apply(
        CorporateAction(CorporateActionType.SYMBOL_CHANGE, "AAPL",
                        effective_date=date(2026, 8, 11), new_symbol="AAPL2"), SESSION_TIME)
    assert ledger.position_qty("AAPL") == 0.0
    assert ledger.position_qty("AAPL2") == pytest.approx(10)


def test_backtest_split_adjustment():
    md = MarketDataService(MockProvider())
    bars = [s.bar for s in md.bars("AAPL", SESSION_TIME, 10, received_at=SESSION_TIME)]
    action = CorporateAction(CorporateActionType.STOCK_SPLIT, "AAPL",
                             effective_date=bars[5].ts.date(), ratio=2.0)
    adjusted = adjust_bars_for_split(bars, action)
    assert adjusted[0].close == pytest.approx(bars[0].close / 2)
    assert adjusted[0].volume == bars[0].volume * 2
    assert adjusted[6].close == pytest.approx(bars[6].close)  # post-split untouched


# --- Event calendar (§16) --------------------------------------------------

def test_event_calendar_feeds_gap_risk():
    cal = EventCalendar()
    cal.add(RiskEvent(EventType.EARNINGS, on=date(2026, 8, 13), symbol="NVDA"))
    cal.add(RiskEvent(EventType.FOMC, on=date(2026, 8, 20)))
    assert cal.has_event_before("NVDA", date(2026, 8, 11), horizon_days=5)
    assert not cal.has_event_before("AAPL", date(2026, 8, 11), horizon_days=5)
    # market-wide FOMC hits every symbol within horizon
    assert cal.has_event_before("AAPL", date(2026, 8, 18), horizon_days=5)


# --- Forecast tracker (§32) -----------------------------------------------

def _forecast(horizon: str = "1w") -> Forecast:
    return Forecast(symbol="AAPL", made_at=SESSION_TIME, horizon=horizon,
                    price_at_forecast=100.0,
                    bear=ScenarioCase(description="d", target_price=90, probability=0.2),
                    base=ScenarioCase(description="b", target_price=102, probability=0.5),
                    bull=ScenarioCase(description="u", target_price=115, probability=0.3))


def test_point_forecast_forbidden():
    with pytest.raises(ValueError, match="sum to 1.0"):
        Forecast(symbol="AAPL", made_at=SESSION_TIME, horizon="1w",
                 price_at_forecast=100.0,
                 bear=ScenarioCase(description="d", target_price=90, probability=0.0),
                 base=ScenarioCase(description="b", target_price=100, probability=0.0),
                 bull=ScenarioCase(description="u", target_price=110, probability=0.0))
    with pytest.raises(ValueError, match="horizon"):
        _forecast(horizon="2y")


def test_forecast_resolution_and_calibration():
    tracker = ForecastTracker()
    tracker.record(_forecast())
    resolved = tracker.resolve_due(SESSION_TIME + timedelta(weeks=1, hours=1),
                                   price_fn=lambda s, t: 101.0)
    assert resolved == 1
    assert tracker.forecasts[0].realized_case == "base"
    report = tracker.calibration_report()
    assert report["base"]["realized"] == 1.0


# --- Human intent (§76-79) -------------------------------------------------

def _scan_result():
    md = MarketDataService(MockProvider())
    scanner = QuantScanner(md, min_dollar_volume=0, basic_top_n=500, advanced_top_n=500)
    results = scanner.scan(["AAPL"], SESSION_TIME)
    if results:
        return results[0]
    # fall back: relax filters so AAPL always yields a ScanResult for the test
    scanner = QuantScanner(md, min_dollar_volume=0, min_price=0.0)
    all_bars = md.bars("AAPL", SESSION_TIME, 40, received_at=SESSION_TIME)
    from services.quant.scanner import ScanResult

    bars = [s.bar for s in all_bars]
    return ScanResult(symbol="AAPL", last_close=bars[-1].close, momentum_20d=0.01,
                      dollar_volume=1e7, volatility=0.02, score=0.5, bars=tuple(bars))


def test_intent_produces_analysis_not_order():
    """§76: 即注文禁止 — analyze() returns analysis, and the proposal it can
    produce still has to survive the full risk pipeline."""
    analyzer = HumanIntentAnalyzer()
    intent = HumanIntent(symbol="AAPL", action=IntentAction.WANT_BUY,
                         stated_reason="I like the product", at=SESSION_TIME)
    analysis = analyzer.analyze(intent, _scan_result(), regime="BULL",
                                news_headlines=[], existing_exposure_notional=0.0)
    assert set(analysis.forecast) == {"bear", "base", "bull"}      # §77 UI payload
    proposal = analyzer.to_proposal(intent, analysis, SESSION_TIME)
    assert proposal.source is ProposalSource.HUMAN
    assert proposal.side is Action.BUY
    # human proposals carry the same schema — no special execution path exists


def test_human_vs_ai_analytics():
    analytics = HumanVsAiAnalytics()
    for i in range(12):
        analytics.record_override(OverrideRecord(
            symbol="AAPL", at=SESSION_TIME, ai_action=Action.NO_TRADE,
            human_action=Action.BUY, ai_pnl=0.0, human_pnl=1.0))
    s = analytics.summary()
    assert s["human_value_added"] == pytest.approx(12.0)
    assert s["research_candidate"] is True  # §79: persistent edge → research


# --- Experiment registry (§98) --------------------------------------------

def test_experiment_requires_rollback_and_human_adoption():
    reg = ExperimentRegistry()
    with pytest.raises(ValueError, match="rollback"):
        reg.register(Experiment(hypothesis="h", change="c", baseline="b",
                                challenger="x", period_start=SESSION_TIME))
    eid = reg.register(Experiment(hypothesis="h", change="c", baseline="b",
                                  challenger="x", period_start=SESSION_TIME,
                                  rollback_plan="revert config"))
    with pytest.raises(ValueError, match="no recorded result"):
        reg.decide(eid, ExperimentDecision.ADOPT, decided_by="human")
    reg.record_result(eid, {"return": 0.05}, SESSION_TIME)
    with pytest.raises(PermissionError):
        reg.decide(eid, ExperimentDecision.ADOPT, decided_by="")
    reg.decide(eid, ExperimentDecision.ADOPT, decided_by="operator@example.com")
    assert reg.get(eid).decision is ExperimentDecision.ADOPT


# --- Exit optimizer (§52) --------------------------------------------------

def test_exit_optimizer_full_ladder_required():
    reg = ExperimentRegistry()
    opt = ExitOptimizer(reg)
    p = opt.propose("wider ATR stop", StopType.ATR, "3x ATR instead of 2x", SESSION_TIME)
    assert p.stage is ExitProposalStage.RESEARCH
    # cannot promote from research
    with pytest.raises(StagePromotionError):
        opt.promote(p, decided_by="human", result={}, at=SESSION_TIME)
    for key in ("backtest_return", "walkforward_return", "shadow_return", "judge_score"):
        opt.advance(p, key, 0.01)
    assert p.stage is ExitProposalStage.JUDGED
    # advancing beyond JUDGED must go through promote()
    with pytest.raises(StagePromotionError):
        opt.advance(p, "extra", 0.01)
    opt.promote(p, decided_by="operator", result={"improvement": 0.01}, at=SESSION_TIME)
    assert p.stage is ExitProposalStage.PROMOTED
    assert reg.get(p.experiment_id).decision is ExperimentDecision.ADOPT
