"""Tests for Phase 8-12: disagreement, recovery, PDCA, shadow, cost, alpha
factory judging."""
from __future__ import annotations

from datetime import date

import pytest

from packages.common.clock import FrozenClock
from services.alpha_factory.factory import (
    AlphaJudge,
    AlphaStage,
    ChampionChallenger,
    MomentumAlpha,
)
from services.cost_manager.engine import (
    CostCategory,
    CostEntry,
    DataRoiEngine,
    DataRoiVerdict,
    OperatingCostEngine,
)
from services.decision.disagreement import ModelDisagreementEngine
from services.market_data.service import MarketDataService, MockProvider
from services.pdca.attribution import (
    ProfitSource,
    TradeRecord,
    attribute,
    profit_quality,
)
from services.pdca.review import DailyReview
from services.pdca.shadow import AblationEngine, ShadowPortfolioManager, ShadowVariant
from services.supervisor.recovery import (
    FaultSeverity,
    RecoveryManager,
    ResumeForbiddenError,
)
from tests.conftest import SESSION_TIME


# --- Disagreement (§30) ---------------------------------------------------

def test_unanimous_vs_split():
    eng = ModelDisagreementEngine()
    unanimous = eng.measure("AAPL", {"quant": 1.0, "news": 1.0, "decision": 1.0})
    assert unanimous.disagreement == pytest.approx(0.0)
    assert unanimous.risk_multiplier == pytest.approx(1.0)
    split = eng.measure("AAPL", {"quant": 1.0, "news": -1.0})
    assert split.disagreement == pytest.approx(1.0)
    assert split.risk_multiplier == pytest.approx(0.5)


# --- Recovery (§69) -------------------------------------------------------

def test_major_fault_cannot_auto_resume():
    rm = RecoveryManager(clock=FrozenClock(current=SESSION_TIME))
    inc = rm.report_fault("execution", "UNKNOWN order unresolved", FaultSeverity.MAJOR)
    assert not rm.can_auto_resume(inc)
    with pytest.raises(ResumeForbiddenError):
        rm.resume(inc)
    rm.resume(inc, human_approved=True)
    assert inc.resolved and inc.human_approved_resume


def test_minor_fault_auto_resumes_and_postmortem_shape():
    rm = RecoveryManager(clock=FrozenClock(current=SESSION_TIME))
    inc = rm.report_fault("market_data", "one stale quote", FaultSeverity.MINOR)
    rm.resume(inc)
    assert inc.resolved
    pm = rm.postmortem(inc)
    assert set(pm) >= {"timeline", "root_cause", "damage", "corrective_action",
                       "regression_test"}


# --- Attribution & profit quality (§54-55) --------------------------------

def test_attribution_decomposes():
    t = TradeRecord(symbol="AAPL", qty=10, entry_price=100, exit_price=110,
                    market_return_during_hold=0.05, slippage_cost=2.0, fees=1.0)
    a = attribute(t)
    assert a.total_pnl == pytest.approx(100 - 3)
    assert a.market_beta_pnl == pytest.approx(50)
    assert a.alpha_pnl == pytest.approx(50)


def test_profit_quality_detects_outlier():
    trades = [TradeRecord("A", 1, 100, 101, 0.0, 0, 0) for _ in range(5)]
    big = TradeRecord("B", 1, 100, 200, 0.0, 0, 0)
    atts = [attribute(t) for t in trades + [big]]
    q = profit_quality(atts)
    assert q.source is ProfitSource.LUCKY_OUTLIER


def test_profit_quality_market_lift():
    t = TradeRecord("A", 10, 100, 105, market_return_during_hold=0.06,
                    slippage_cost=0, fees=0)
    q = profit_quality([attribute(t)])
    assert q.source is ProfitSource.MARKET_LIFT


# --- Daily review (§63-64) ------------------------------------------------

def test_review_risk_violation_dominates_grade():
    r = DailyReview(day=date(2026, 8, 11), pnl=100.0, equity=1100, start_equity=1000,
                    risk_violations=1)
    grades = r.compute_grades()
    assert grades["overall"] == "F"


def test_pace_is_informational_only():
    """§63: pace exists for display; nothing in RiskConfig consumes it."""
    r = DailyReview(day=date(2026, 8, 11), pnl=0.0, equity=1000, start_equity=1000,
                    day_index=10)
    pace = r.theoretical_pace_equity
    assert pace > 1000
    lines = r.narrative()
    assert any("informational only" in l for l in lines)


# --- Shadow & ablation (§56-57) -------------------------------------------

def test_shadow_ranking():
    mgr = ShadowPortfolioManager(initial_equity=1000,
                                 variants=[ShadowVariant.FULL, ShadowVariant.QUANT_ONLY])
    for _ in range(5):
        mgr.record_session({ShadowVariant.FULL: 0.01, ShadowVariant.QUANT_ONLY: -0.01})
    ranking = mgr.ranking()
    assert ranking[0][0] is ShadowVariant.FULL


def test_ablation_contribution_and_pruning():
    ab = AblationEngine()
    ab.record(set(), 0.10)
    ab.record({"news"}, 0.02)       # news helps a lot
    ab.record({"regime"}, 0.11)     # regime is removable
    assert ab.contribution("news") == pytest.approx(0.08)
    assert "regime" in ab.removable_candidates()
    assert "news" not in ab.removable_candidates()


# --- Cost & Two P&L (§80-83) ----------------------------------------------

def test_two_pnl_separation():
    eng = OperatingCostEngine()
    eng.record(CostEntry(at=SESSION_TIME, category=CostCategory.AI, amount=30.0))
    eng.record(CostEntry(at=SESSION_TIME, category=CostCategory.SERVER, amount=20.0))
    two = eng.two_pnl(trading_pnl=100.0)
    assert two.trading_pnl == 100.0
    assert two.project_net_pnl == 50.0  # §81: separate display, costs deducted


def test_data_roi_verdicts():
    roi = DataRoiEngine(min_observations=3)
    roi.record_session(0.01, 0.0)
    assert roi.evaluate(100, 10_000) is DataRoiVerdict.TESTING  # not enough data
    roi.record_session(0.01, 0.0)
    roi.record_session(0.01, 0.0)
    assert roi.evaluate(10, 10_000) is DataRoiVerdict.KEEP
    bad = DataRoiEngine(min_observations=3)
    for _ in range(3):
        bad.record_session(0.0, 0.01)
    assert bad.evaluate(100, 10_000) is DataRoiVerdict.CANCEL


# --- Alpha factory & champion/challenger (§23, §58) -----------------------

def test_judge_never_promotes_straight_to_live():
    md = MarketDataService(MockProvider())
    bars = [s.bar for s in md.bars("NVDA", SESSION_TIME, 250, received_at=SESSION_TIME)]
    verdict = AlphaJudge().judge(MomentumAlpha(), bars)
    assert verdict.stage in (AlphaStage.SHADOW, AlphaStage.REJECTED)  # never PROMOTED (§23)


def test_champion_challenger_promotion():
    cc = ChampionChallenger(min_shadow_sessions=3)
    for _ in range(3):
        cc.record_shadow("alpha_a", 0.01)
    promoted, _ = cc.consider_promotion("alpha_a")
    assert promoted and cc.champion == "alpha_a"
    # challenger must BEAT the champion
    for _ in range(3):
        cc.record_shadow("alpha_b", 0.005)
    promoted, why = cc.consider_promotion("alpha_b")
    assert not promoted and cc.champion == "alpha_a"
    for _ in range(3):
        cc.record_shadow("alpha_c", 0.05)
    promoted, _ = cc.consider_promotion("alpha_c")
    assert promoted and cc.champion == "alpha_c"
