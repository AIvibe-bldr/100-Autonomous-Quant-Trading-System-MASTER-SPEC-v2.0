"""Attribution, exit review and daily review as session observers (§50-55, §63-64)."""
from datetime import date, datetime, timedelta, timezone

import pytest

from packages.common.clock import FrozenClock
from packages.schemas.core import Action
from services.market_data.universe import UniverseManager, UniverseSymbol
from services.pdca.observer import DecisionEvent, FillEvent, SessionEndEvent
from services.pdca.performance_observers import (
    AttributionObserver,
    DailyReviewObserver,
    ExitReviewObserver,
)
from services.pdca.post_trade import PostTradeTracker
from tests.conftest import SYMBOLS, build_pipeline

T = datetime(2026, 8, 11, 15, 0, tzinfo=timezone.utc)


def fill(side, qty, price, ts, ref=None, fees=0.0, sym="AAA"):
    return FillEvent(symbol=sym, side=side, qty=qty, price=price, fees=fees, ts=ts,
                     client_order_id="c1", reference_price=ref)


class _Result:
    data_warnings: list = []
    orders_filled = 0
    risk_passed = 0
    decision_candidates = 0
    skeptic_vetoes = 0
    traded = False
    no_trade_reasons = ["nothing to do"]


def end(ts, price_on, equity=1000.0):
    return SessionEndEvent(as_of=ts, result=_Result(), price_on=price_on, equity=equity)


# ---- attribution ----

def test_a_closed_trade_is_split_into_beta_alpha_slippage_and_fees():
    obs = AttributionObserver(benchmark="SPY")
    obs.on_fill(fill(Action.BUY, 10, 100.0, T, ref=99.5, fees=0.10))                  # paid 0.5 over plan
    obs.on_fill(fill(Action.SELL, 10, 110.0, T + timedelta(days=3), ref=111.0, fees=0.10))  # 1.0 under trigger
    bm = {T: 400.0, T + timedelta(days=3): 404.0}                                      # benchmark +1%
    obs.on_session_end(end(T + timedelta(days=3), lambda s, at: bm.get(at)))
    s = obs.summary()
    assert s["closed_trades"] == 1 and s["unattributed"] == 0
    assert s["market_beta_pnl"] == pytest.approx(10.0)                                 # 1000 notional * 1%
    assert s["alpha_pnl"] == pytest.approx(90.0)                                       # 100 gross - 10
    assert s["slippage"] == pytest.approx(5.0 + 10.0) and s["fees"] == pytest.approx(0.20)
    assert s["total_pnl"] == pytest.approx(100.0 - 15.0 - 0.20)
    assert "beta = 1.0" in s["assumption"]


def test_partial_exits_share_entry_costs_pro_rata_and_unknown_entries_are_ignored():
    obs = AttributionObserver()
    obs.on_fill(fill(Action.SELL, 5, 50.0, T, sym="ZZZ"))                              # never saw the entry
    obs.on_fill(fill(Action.BUY, 10, 100.0, T, fees=1.0))
    obs.on_fill(fill(Action.SELL, 4, 101.0, T + timedelta(days=1)))
    obs.on_fill(fill(Action.SELL, 6, 102.0, T + timedelta(days=2)))
    obs.on_session_end(end(T + timedelta(days=2), lambda s, at: 100.0))
    assert [round(a.fees, 6) for a in obs.attributions] == [0.4, 0.6]


def test_without_a_benchmark_price_the_trade_is_counted_unattributed_not_guessed():
    obs = AttributionObserver()
    obs.on_fill(fill(Action.BUY, 1, 100.0, T))
    obs.on_fill(fill(Action.SELL, 1, 99.0, T + timedelta(days=1)))
    obs.on_session_end(end(T + timedelta(days=1), lambda s, at: None))
    assert obs.summary()["closed_trades"] == 0 and obs.summary()["unattributed"] == 1


# ---- exit review ----

def _decision_event(action="NO_TRADE", price=100.0):
    from tests.unit.helpers import make_decision
    from packages.schemas.core import DecisionAction
    d = make_decision("AAA").model_copy(update={"action": DecisionAction(action)})
    return DecisionEvent(decision_id="d", symbol="AAA", as_of=T, decision=d, price=price, context=None)


def test_skipped_decisions_are_tracked_as_counterfactuals_and_graded_when_a_week_has_passed():
    tr = PostTradeTracker()
    obs = ExitReviewObserver(tr)
    obs.on_decision(_decision_event("NO_TRADE", 100.0))
    obs.on_decision(_decision_event("BUY", 100.0))                                     # BUYs are not skipped
    obs.on_session_end(end(T + timedelta(days=8), lambda s, at: 110.0))
    cf = obs.summary()["counterfactual"]
    assert cf["tracked"] == 1 and cf["with_1w_outcome"] == 1
    assert cf["mean_1w_return_of_skipped"] == pytest.approx(0.10)


def test_stops_that_keep_bouncing_file_one_research_proposal_never_a_live_change():
    tr = PostTradeTracker()
    obs = ExitReviewObserver(tr)
    for i in range(25):
        tr.track(f"S{i}", 100.0, T, kind="STOP")
    obs.on_session_end(end(T + timedelta(days=8), lambda s, at: 105.0))                # all recovered 5%
    obs.on_session_end(end(T + timedelta(days=9), lambda s, at: 105.0))
    s = obs.summary()
    assert s["stops"]["early_rate"] == 1.0
    assert [p["name"] for p in s["proposals"]] == ["review-early-stops"]               # once, not twice
    assert s["proposals"][0]["stage"] == "RESEARCH"


def test_few_graded_stops_file_no_proposal():
    tr = PostTradeTracker()
    obs = ExitReviewObserver(tr)
    for i in range(5):
        tr.track(f"S{i}", 100.0, T, kind="STOP")
    obs.on_session_end(end(T + timedelta(days=8), lambda s, at: 105.0))
    assert obs.summary()["proposals"] == []


def test_an_unavailable_price_leaves_the_outcome_for_a_later_session():
    tr = PostTradeTracker()
    obs = ExitReviewObserver(tr)
    obs.on_decision(_decision_event("WAIT", 100.0))
    obs.on_session_end(end(T + timedelta(days=8), lambda s, at: None))
    assert obs.summary()["counterfactual"]["with_1w_outcome"] == 0
    obs.on_session_end(end(T + timedelta(days=9), lambda s, at: 90.0))
    assert obs.summary()["counterfactual"]["with_1w_outcome"] == 1


# ---- daily review ----

def test_daily_review_grades_and_explains_each_session():
    obs = DailyReviewObserver(initial_equity=1000.0)
    obs.on_session_end(end(T, lambda s, at: None, equity=1012.0))
    s = obs.summary()
    assert s["pnl"] == pytest.approx(12.0) and s["grades"]["pnl"] == "A" and "overall" in s["grades"]
    assert any(l.startswith("WHY PROFIT") for l in s["narrative"])
    obs.on_session_end(end(T + timedelta(days=1), lambda s, at: None, equity=1002.0))
    assert obs.summary()["pnl"] == pytest.approx(-10.0)                                # vs the previous session


# ---- through the real pipeline ----

def _pipeline(observers=()):
    clock = FrozenClock(current=T)
    universe = UniverseManager()
    for s in SYMBOLS:
        universe.add(UniverseSymbol(symbol=s, listed_from=date(2015, 1, 1)))
    return build_pipeline(clock, universe, observers=list(observers)), clock


def test_stop_outs_flow_from_the_pipeline_into_attribution_and_the_review_without_changing_trading():
    plain, pclock = _pipeline()
    p, clock = _pipeline()
    attr, daily = AttributionObserver(), DailyReviewObserver(1000.0)
    review = ExitReviewObserver(p.post_trade)
    p.observers.extend([attr, review, daily])
    for _ in range(15):                                   # the mock market stops a few trades out
        got, ref = p.run_session(clock.now()), plain.run_session(pclock.now())
        assert (got.scanned, got.orders_filled, got.stops_triggered) == (
            ref.scanned, ref.orders_filled, ref.stops_triggered)
        assert got.data_warnings == []
        for pp, cc in ((p, clock), (plain, pclock)):
            cc.advance(86400)
            pp.execution.broker.settle()
    s = attr.summary()
    assert s["closed_trades"] >= 1 and s["unattributed"] == 0
    assert s["total_pnl"] == pytest.approx(s["market_beta_pnl"] + s["alpha_pnl"] - s["slippage"] - s["fees"])
    assert p.ledger.cash == pytest.approx(plain.ledger.cash)                          # same books
    assert {k: v.qty for k, v in p.ledger.positions.items()} == {k: v.qty for k, v in plain.ledger.positions.items()}
    assert daily.summary()["grades"]["overall"] in "ABCDF"
    assert review.summary()["counterfactual"]["tracked"] > 0
