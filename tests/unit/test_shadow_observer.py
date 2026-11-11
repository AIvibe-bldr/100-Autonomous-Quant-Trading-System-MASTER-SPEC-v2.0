"""Shadow portfolios, ablation and alpha shadow-running as an observer (§56-58)."""
from datetime import date, datetime, timedelta, timezone

import pytest

from packages.common.clock import FrozenClock
from packages.schemas.core import Bar
from services.market_data.universe import UniverseManager, UniverseSymbol
from services.pdca.observer import DecisionEvent, SessionEndEvent
from services.pdca.shadow import ShadowVariant
from services.pdca.shadow_observer import ShadowObserver
from tests.conftest import SYMBOLS, build_pipeline

T = datetime(2026, 8, 11, 15, 0, tzinfo=timezone.utc)


class Scan:
    def __init__(self, symbol, last_close, score, bars=()):
        self.symbol, self.last_close, self.score, self.bars = symbol, last_close, score, tuple(bars)


class Ctx:
    def __init__(self, scan):
        self.scan = scan


class _R:
    data_warnings = []


def decision(symbol, last_close, score, bars=()):
    from tests.unit.helpers import make_decision
    return DecisionEvent(decision_id="d", symbol=symbol, as_of=T, decision=make_decision(symbol),
                         price=last_close, context=Ctx(Scan(symbol, last_close, score, bars)))


def end(ts, prices, equity):
    return SessionEndEvent(as_of=ts, result=_R(), price_on=lambda s, at: prices.get(s), equity=equity)


def test_variants_are_marked_one_session_later_against_the_same_candidates():
    obs = ShadowObserver(1000.0, alphas=[], picks=2)
    for sym, px, score in (("A", 100.0, 3.0), ("B", 50.0, 2.0), ("C", 10.0, 1.0)):
        obs.on_decision(decision(sym, px, score))
    obs.on_session_end(end(T, {"SPY": 400.0}, 1000.0))                    # first session: only seeds
    assert obs.summary()["sessions"] == 0
    day2 = T + timedelta(days=1)
    obs.on_session_end(end(day2, {"A": 110.0, "B": 45.0, "C": 99.0, "SPY": 404.0}, 1010.0))
    r = {x["variant"]: x["total_return"] for x in obs.summary()["ranking"]}
    assert r["QUANT_ONLY"] == pytest.approx(((0.10) + (-0.10)) / 2)       # top 2 by score: A, B (C ignored)
    assert r["BENCHMARK"] == pytest.approx(0.01) and r["FULL"] == pytest.approx(0.01)


def test_the_ai_stack_contribution_is_full_minus_quant_only_and_is_caveated():
    obs = ShadowObserver(1000.0, alphas=[], picks=1)
    obs.on_decision(decision("A", 100.0, 1.0))
    obs.on_session_end(end(T, {"SPY": 400.0}, 1000.0))
    obs.on_session_end(end(T + timedelta(days=1), {"A": 102.0, "SPY": 400.0}, 1050.0))
    s = obs.summary()
    assert s["ai_stack_contribution"] == pytest.approx(0.05 - 0.02)
    assert any("not like-for-like" in c for c in s["caveats"]) and any("LLM re-run" in c for c in s["caveats"])


def test_an_unpriceable_pick_is_skipped_and_a_session_with_no_priceable_names_records_nothing_for_it():
    obs = ShadowObserver(1000.0, alphas=[], picks=2)
    obs.on_decision(decision("A", 100.0, 2.0))
    obs.on_decision(decision("B", 50.0, 1.0))
    obs.on_session_end(end(T, {"SPY": 400.0}, 1000.0))
    obs.on_session_end(end(T + timedelta(days=1), {"A": 101.0, "SPY": 400.0}, 1000.0))     # B has no price
    q = next(x for x in obs.summary()["ranking"] if x["variant"] == "QUANT_ONLY")
    assert q["total_return"] == pytest.approx(0.01)
    obs.on_decision(decision("A", 101.0, 2.0))
    obs.on_session_end(end(T + timedelta(days=2), {"SPY": 400.0}, 1000.0))                  # nothing priceable
    q2 = next(x for x in obs.summary()["ranking"] if x["variant"] == "QUANT_ONLY")
    assert q2["total_return"] == pytest.approx(0.01)                                       # unchanged, not zeroed


def _trend_bars(up: bool):
    px = 100.0
    out = []
    for i in range(40):
        px *= 1.01 if up else 0.995
        out.append(Bar(symbol="X", ts=T - timedelta(days=40 - i), open=px, high=px, low=px, close=px, volume=1000))
    return out


def test_alpha_candidates_shadow_trade_only_where_their_signal_fires():
    obs = ShadowObserver(1000.0, picks=1)                                   # default alphas
    obs.on_decision(decision("UP", 100.0, 2.0, _trend_bars(True)))          # momentum fires (> +5% in 20d)
    obs.on_decision(decision("FLAT", 100.0, 1.0, _trend_bars(False)))
    obs.on_session_end(end(T, {"SPY": 400.0}, 1000.0))
    obs.on_session_end(end(T + timedelta(days=1), {"UP": 103.0, "FLAT": 90.0, "SPY": 400.0}, 1000.0))
    a = obs.summary()["alpha_shadow"]
    assert a["momentum"]["mean_return"] == pytest.approx(0.03)               # UP only
    assert "consider_promotion" not in obs.summary()
    assert obs.champions.champion is None                                    # nobody was promoted


def test_runs_in_the_pipeline_without_changing_trading():
    def build(obs):
        clock = FrozenClock(current=T)
        universe = UniverseManager()
        for s in SYMBOLS:
            universe.add(UniverseSymbol(symbol=s, listed_from=date(2015, 1, 1)))
        return build_pipeline(clock, universe, observers=obs), clock
    plain, c0 = build([])
    shadow = ShadowObserver(1000.0)
    p, c1 = build([shadow])
    for _ in range(8):
        a, b = p.run_session(c1.now()), plain.run_session(c0.now())
        assert (a.orders_filled, a.stops_triggered) == (b.orders_filled, b.stops_triggered)
        assert a.data_warnings == []
        for pp, cc in ((p, c1), (plain, c0)):
            cc.advance(86400)
            pp.execution.broker.settle()
    s = shadow.summary()
    assert s["sessions"] >= 5 and {x["variant"] for x in s["ranking"]} == {"FULL", "QUANT_ONLY", "BENCHMARK"}
    assert set(s["alpha_shadow"]) <= {"momentum", "mean_reversion"}
