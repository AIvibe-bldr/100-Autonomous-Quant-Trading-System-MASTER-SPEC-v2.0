"""Point-in-Time store contract (both stores) and the observer that records decision inputs."""
from datetime import date, datetime, timedelta, timezone

import pytest

from packages.common.clock import FrozenClock
from packages.common.durable_store import DurablePitStore
from packages.common.environment import Environment
from packages.common.pit_store import PointInTimeStore
from services.market_data.universe import UniverseManager, UniverseSymbol
from services.pdca.pit_observer import PitObserver
from tests.conftest import SYMBOLS, build_pipeline

T = datetime(2026, 8, 11, 15, 0, tzinfo=timezone.utc)


@pytest.fixture(params=["memory", "durable"])
def store(request, tmp_path):
    if request.param == "memory":
        return PointInTimeStore()
    return DurablePitStore(str(tmp_path / "pit.db"), Environment.PAPER)


def test_a_later_revision_is_invisible_to_an_earlier_as_of(store):
    store.put("eps:AAPL", 1.00, T, T, "sec")
    store.put("eps:AAPL", 1.20, T, T + timedelta(days=30), "sec")           # restated a month later
    assert store.get_as_of("eps:AAPL", T + timedelta(days=1)).value == 1.00
    assert store.get_as_of("eps:AAPL", T + timedelta(days=30)).value == 1.20
    assert store.get_as_of("eps:AAPL", T - timedelta(seconds=1)) is None
    assert store.get_as_of("eps:MSFT", T) is None


def test_history_is_ordered_by_receipt_and_the_later_put_wins_a_tie(store):
    store.put("k", "first", T, T)
    store.put("k", "second", T, T)                                          # same receipt instant
    store.put("k", "early", T, T - timedelta(hours=1))
    assert [r.value for r in store.history("k")] == ["early", "first", "second"]
    assert store.get_as_of("k", T).value == "second"


def test_durable_store_survives_reopen_and_namespaces_environments(tmp_path):
    path = str(tmp_path / "pit.db")
    DurablePitStore(path, Environment.PAPER).put("k", {"a": [1, 2]}, T, T, "src")
    again = DurablePitStore(path, Environment.PAPER).get_as_of("k", T)
    assert again.value == {"a": [1, 2]} and again.source == "src" and again.received_time == T
    assert DurablePitStore(path, Environment.LIVE).get_as_of("k", T) is None


def test_observer_records_inputs_and_outputs_stamped_with_receipt_time(tmp_path):
    pit = DurablePitStore(str(tmp_path / "pit.db"), Environment.PAPER)
    clock = FrozenClock(current=T)
    universe = UniverseManager()
    for s in SYMBOLS:
        universe.add(UniverseSymbol(symbol=s, listed_from=date(2015, 1, 1)))
    obs = PitObserver(pit)
    p = build_pipeline(clock, universe, observers=[obs])
    result = p.run_session(T)
    assert obs.summary()["decisions_recorded"] == result.candidates > 0
    sym = next(iter(p.provenance.all())).output["symbol"]
    inp, out = pit.get_as_of(f"decision_input:{sym}", T), pit.get_as_of(f"decision_output:{sym}", T)
    assert inp.value["quote"]["bid"] > 0 and inp.value["last_bar_ts"] and "news" in inp.value
    assert out.value["symbol"] == sym and out.value["action"]
    assert pit.get_as_of(f"decision_input:{sym}", T - timedelta(seconds=1)) is None   # not knowable earlier
