"""Human intent through the live pipeline (§76-79, INV-8): same gates as an AI candidate, no bypass."""
from datetime import date, datetime, timedelta, timezone

import pytest

from packages.common.clock import FrozenClock
from packages.common.llm_client import LLMCallError
from services.decision.human_intent import IntentAction
from services.decision.intent_inbox import IntentInbox, IntentStatus
from services.market_data.universe import UniverseManager, UniverseSymbol
from services.pdca.forecast_observer import ForecastObserver
from services.pdca.override_observer import HumanOverrideObserver
from services.risk.master_controller import RiskState
from tests.conftest import SYMBOLS, build_pipeline

T = datetime(2026, 8, 11, 15, 0, tzinfo=timezone.utc)


def _setup(tmp_path, observers=()):
    clock = FrozenClock(current=T)
    universe = UniverseManager()
    for s in SYMBOLS:
        universe.add(UniverseSymbol(symbol=s, listed_from=date(2015, 1, 1)))
    box = IntentInbox(tmp_path / "intents.jsonl")
    p = build_pipeline(clock, universe, human_inbox=box, observers=list(observers))
    return p, box, clock


def _confirmed(p, box, symbol="NVDA"):
    iid = box.submit(symbol, IntentAction.WANT_BUY, "I like the setup", "alice", T)
    assert p.analyze_human_intents(T) == 1
    box.confirm(iid, "alice", T)
    return iid


def test_analysis_is_attached_before_anything_can_be_confirmed_and_says_what_it_is_not(tmp_path):
    p, box, _ = _setup(tmp_path)
    iid = box.submit("NVDA", IntentAction.WANT_BUY, "why", "alice", T)
    assert box.get(iid).status is IntentStatus.SUBMITTED
    assert p.analyze_human_intents(T) == 1 and p.analyze_human_intents(T) == 0       # once
    r = box.get(iid)
    a = r.analysis
    assert r.status is IntentStatus.ANALYZED and a["symbol"] == "NVDA" and a["current_price"] > 0
    assert set(a["forecast"]) == {"bear", "base", "bull"} and "NOT a model forecast" in " ".join(a["notes"])
    assert not p.ledger.positions                                                    # analysis places nothing


def test_a_confirmed_buy_goes_through_audit_and_risk_and_is_closed_with_its_outcome(tmp_path):
    p, box, clock = _setup(tmp_path)
    iid = _confirmed(p, box)
    result = p.run_session(clock.now())
    rec = box.get(iid)
    assert rec.status is IntentStatus.CONSUMED and rec.outcome == result.human_intents[iid]
    prov = p.provenance.get(f"NVDA-{T.date()}")
    assert prov.model == "human-intent" and prov.model_version == iid
    assert prov.skeptic_output["model_family"] == "human-intent"                    # the Skeptic is not consulted
    assert rec.outcome.startswith("ORDER_PLACED") and prov.order_ref in rec.outcome
    # not a shortcut: the order carries an Audit result and a signed Risk approval like any other
    trail = p.audit_log.get(prov.order_ref)
    assert trail.audit_result and trail.audit_result["verdict"] == "PASS"
    assert trail.risk_result["passed"] and trail.approved_snapshot_hash and trail.broker_submitted
    assert any(getattr(d, "client_order_id", None) == prov.order_ref for d in p.risk_controller.decisions_log)
    assert "NVDA" in {f.symbol for f in result.fills}


def test_a_human_cannot_bypass_the_master_risk_controller(tmp_path):
    p, box, clock = _setup(tmp_path)
    iid = _confirmed(p, box)
    p.risk_controller.set_state(RiskState.HALT_NEW_ENTRIES, reason="test")
    result = p.run_session(clock.now())
    assert box.get(iid).outcome.startswith("NOT_TRADED") and not p.ledger.positions.get("NVDA")
    assert "NVDA" not in {f.symbol for f in result.fills}


def test_an_intent_that_is_not_confirmed_never_trades(tmp_path):
    p, box, clock = _setup(tmp_path)
    iid = box.submit("NVDA", IntentAction.WANT_BUY, "why", "alice", T)
    p.analyze_human_intents(T)                                                        # ANALYZED, not CONFIRMED
    result = p.run_session(clock.now())
    assert box.get(iid).status is IntentStatus.ANALYZED and result.human_intents == {}
    assert "human-intent" not in {r.model for r in p.provenance.all()}


def test_a_name_the_scanner_will_not_pass_is_refused_with_a_reason(tmp_path):
    p, box, clock = _setup(tmp_path)
    iid = _confirmed(p, box, "NVDA")
    real = p.scanner.scan
    p.scanner.scan = lambda symbols, at: [] if symbols == ["NVDA"] else real(symbols, at)
    result = p.run_session(clock.now())
    assert box.get(iid).status is IntentStatus.REJECTED and "filters" in box.get(iid).outcome
    assert any("human intent refused" in r for r in result.no_trade_reasons)


def test_an_unreadable_inbox_acts_on_nothing_and_does_not_stop_the_ai_session(tmp_path):
    p, box, clock = _setup(tmp_path)
    box.path.write_text("{garbage\n")
    plain, _, _ = _setup(tmp_path / "x")
    ref = plain.run_session(clock.now())
    result = p.run_session(clock.now())
    assert any("inbox unreadable" in w for w in result.data_warnings)
    assert (result.scanned, result.orders_filled) == (ref.scanned, ref.orders_filled)
    assert p.analyze_human_intents(T) == 0


def test_without_an_inbox_nothing_changes(tmp_path):
    p, _, clock = _setup(tmp_path)
    p.human_inbox = None
    plain, _, c2 = _setup(tmp_path / "y")
    a, b = p.run_session(clock.now()), plain.run_session(c2.now())
    assert (a.scanned, a.orders_filled, a.human_intents) == (b.scanned, b.orders_filled, {})


def test_the_ai_failing_to_answer_does_not_block_the_humans_choice_and_is_recorded_as_unknown(tmp_path):
    ov = HumanOverrideObserver()
    p, box, clock = _setup(tmp_path, observers=[ov])
    iid = _confirmed(p, box)

    def down(ctx):
        raise LLMCallError("provider down")
    p.decision_model.decide = down
    p.run_session(clock.now())
    assert box.get(iid).status is IntentStatus.CONSUMED
    assert p._human_ai_view.get("NVDA") is None
    assert len(ov._pending) == 1 and ov._pending[0].ai_action is None


def test_learning_loops_grade_the_ai_not_the_humans_template_forecast(tmp_path):
    fo = ForecastObserver()
    p, box, clock = _setup(tmp_path, observers=[fo])
    plain_fo = ForecastObserver()
    plain, _, c2 = _setup(tmp_path / "z", observers=[plain_fo])
    _confirmed(p, box, "NVDA")
    p.run_session(clock.now())
    plain.run_session(c2.now())
    # the human's symbol adds no forecast of its own (the AI one is for the same symbol at most once)
    assert fo.summary()["recorded"] <= plain_fo.summary()["recorded"] + 1
    assert all(f.model != "human-1.0" for f in fo.tracker.forecasts)


def test_override_analytics_compare_the_human_with_the_ai_after_a_week(tmp_path):
    ov = HumanOverrideObserver()
    p, box, clock = _setup(tmp_path, observers=[ov])
    _confirmed(p, box)
    p.run_session(clock.now())
    for _ in range(9):
        clock.advance(86400)
        p.run_session(clock.now())
    s = ov.summary()
    assert s["overrides"] == 1 and s["pending"] == 0 and s["resolved"] in (0, 1)
    assert "not money" in s["unit"]


def test_allocated_candidates_beyond_the_session_limit_are_reported_not_dropped_silently(tmp_path):
    p, _, clock = _setup(tmp_path)
    result = p.run_session(clock.now())
    cut = [r for r in result.no_trade_reasons if "allocated but not ordered" in r]
    assert result.allocated > p.max_new_positions and len(cut) == result.allocated - p.max_new_positions


def test_the_runner_attaches_analyses_only_while_the_market_is_open(tmp_path):
    from packages.common.calendar import TradingCalendar
    from services.supervisor.heartbeat import HeartbeatRegistry
    from services.supervisor.runner import RunnerConfig, SessionRunner
    p, box, clock = _setup(tmp_path)
    calls = []
    sink = type("S", (), {"send": lambda self, sev, msg: None})()

    def hook(now):
        calls.append(now)
        return p.analyze_human_intents(now)
    runner = SessionRunner(p, TradingCalendar(), clock, HeartbeatRegistry(clock), sink, RunnerConfig(),
                           sleep=lambda s: None, market_open_hook=hook)
    iid = box.submit("NVDA", IntentAction.WANT_BUY, "why", "alice", T)
    clock.current = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)                # 08:00 ET: closed
    runner.tick()
    assert calls == [] and box.get(iid).status is IntentStatus.SUBMITTED
    clock.current = datetime(2026, 8, 11, 13, 40, tzinfo=timezone.utc)               # 09:40 ET: open, before the session
    runner.tick()
    assert len(calls) == 1 and box.get(iid).status is IntentStatus.ANALYZED


def test_a_failing_market_open_hook_never_stops_the_runner(tmp_path):
    from packages.common.calendar import TradingCalendar
    from services.supervisor.heartbeat import HeartbeatRegistry
    from services.supervisor.runner import RunnerConfig, SessionRunner
    p, _, clock = _setup(tmp_path)
    clock.current = datetime(2026, 8, 11, 13, 40, tzinfo=timezone.utc)

    def boom(now):
        raise RuntimeError("hook bug")
    sink = type("S", (), {"send": lambda self, sev, msg: None})()
    runner = SessionRunner(p, TradingCalendar(), clock, HeartbeatRegistry(clock), sink, RunnerConfig(),
                           sleep=lambda s: None, market_open_hook=boom)
    runner.tick()
    assert runner.failed is None
