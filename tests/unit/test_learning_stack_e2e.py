"""Everything wired together: runner + all learning observers + session records, over a week."""
import json
from datetime import date, datetime, timezone

from packages.common.calendar import TradingCalendar
from packages.common.clock import FrozenClock
from packages.common.durable_store import DurablePitStore, DurableSessionStore
from packages.common.environment import Environment
from services.composition import attach_learning_observers
from services.decision.human_intent import IntentAction
from services.decision.intent_inbox import IntentInbox
from services.market_data.universe import UniverseManager, UniverseSymbol
from services.supervisor.heartbeat import HeartbeatRegistry
from services.supervisor.runner import RunnerConfig, SessionRunner
from services.supervisor.session_record import SessionRecorder
from tests.conftest import SYMBOLS, build_pipeline

START = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)
END = datetime(2026, 8, 19, 0, 0, tzinfo=timezone.utc)


def _week(tmp_path, observers: bool, inbox=None):
    clock = FrozenClock(current=START)
    universe = UniverseManager()
    for s in SYMBOLS:
        universe.add(UniverseSymbol(symbol=s, listed_from=date(2015, 1, 1)))
    p = build_pipeline(clock, universe, initial_cash=670.0, human_inbox=inbox)
    store = DurableSessionStore(str(tmp_path / f"s{observers}.db"), Environment.PAPER)
    if observers:
        attach_learning_observers(p, 670.0, pit_store=DurablePitStore(str(tmp_path / "pit.db"), Environment.PAPER))
    recorder = SessionRecorder(p, store, "mock-ai")
    sink = type("S", (), {"send": lambda self, sev, msg: None})()

    def after(at, result):
        recorder(at, result)
        p.execution.broker.settle()
    runner = SessionRunner(p, TradingCalendar(), clock, HeartbeatRegistry(clock), sink, RunnerConfig(),
                           sleep=lambda s: clock.advance(s), after_session=after,
                           market_open_hook=p.analyze_human_intents)
    assert runner.run_forever(lambda: clock.now() >= END) is None
    return p, store


def test_a_week_with_every_observer_records_valid_summaries_and_changes_no_trade(tmp_path):
    plain_p, plain_store = _week(tmp_path, observers=False)
    p, store = _week(tmp_path, observers=True)
    got, ref = store.all(), plain_store.all()
    assert len(got) == len(ref) == 6                                                # Aug 11-14, 17, 18
    for a, b in zip(got, ref):                                                      # identical trading
        assert (a.equity, a.cash, a.orders_filled, a.stops_triggered, a.risk_state) == (
            b.equity, b.cash, b.orders_filled, b.stops_triggered, b.risk_state)
        assert a.data_warnings == 0
    detail = json.loads(got[-1].detail_json)
    assert set(detail["observers"]) == {"forecast", "attribution", "exit_review", "daily_review",
                                        "shadow", "human_vs_ai", "capital_cells", "pit"}
    assert detail["observers"]["forecast"]["recorded"] > 0
    assert detail["observers"]["shadow"]["sessions"] >= 4
    assert detail["observers"]["daily_review"]["grades"]["overall"] in "ABCDF"
    assert detail["observers"]["pit"]["decisions_recorded"] > 0
    cells = detail["observers"]["capital_cells"]
    assert cells["errors"] == [] and cells["reconciliation"]["sessions_checked"] == 6
    assert cells["reconciliation"]["sessions_inconsistent"] == 0                 # the cells add up to the books
    assert "disagreement_multipliers" in detail


def test_an_unconfirmed_human_intent_is_analysed_by_the_runner_then_lapses_without_trading(tmp_path):
    box = IntentInbox(tmp_path / "intents.jsonl")
    iid = box.submit("NVDA", IntentAction.WANT_BUY, "thesis", "alice", START)
    p, store = _week(tmp_path, observers=True, inbox=box)       # analysed by the runner while the market is open
    rec = box.get(iid)
    assert rec.analysis and rec.analysis["symbol"] == "NVDA"     # analysis attached; no one confirmed it ...
    assert rec.status.value == "EXPIRED"                         # ... so it lapsed after 7 days, never traded
    assert "human-intent" not in {r.model for r in p.provenance.all()}
