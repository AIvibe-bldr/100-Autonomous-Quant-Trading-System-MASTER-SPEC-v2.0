"""Durable paper account: a restored account behaves exactly like one that never stopped."""
import json
import sqlite3
from datetime import date, datetime, timedelta, timezone

import pytest

from packages.common.clock import FrozenClock
from packages.common.durable_store import DurableCheckpointStore, DurableOrderStore
from packages.common.environment import Environment
from services.checkpoint import (
    CheckpointError, Checkpointer, capture, restore, restore_latest,
)
from services.market_data.universe import UniverseManager, UniverseSymbol
from services.risk.master_controller import RiskState
from tests.conftest import SYMBOLS, build_pipeline

START = datetime(2026, 8, 11, 15, 0, tzinfo=timezone.utc)


def make(tmp_path, name, clock):
    universe = UniverseManager()
    for s in SYMBOLS:
        universe.add(UniverseSymbol(symbol=s, listed_from=date(2015, 1, 1)))
    orders = DurableOrderStore(str(tmp_path / f"{name}-orders.db"), Environment.PAPER)
    p = build_pipeline(clock, universe, initial_cash=670.0, order_store=orders)
    store = DurableCheckpointStore(str(tmp_path / f"{name}-ckpt.db"), Environment.PAPER)
    p.checkpointer = Checkpointer(p, store)
    return p, store


def day(p, clock, n=1):
    for _ in range(n):
        p.checkpointer(f"session_start:{clock.now().date()}")
        p.run_session(clock.now())
        p.execution.broker.settle()
        p.checkpointer(f"session_end:{clock.now().date()}")
        clock.advance(86400)


def fingerprint(p):
    """Everything about the account that matters, with the random ids left out."""
    b = p.execution.broker
    st = b.export_state()
    return {
        "cash": round(p.ledger.cash, 9), "realized": round(p.ledger.realized_pnl, 9),
        "fees": round(p.ledger.fees_paid, 9), "hwm": round(p.ledger.high_water_mark, 9),
        "positions": {s: (round(l.qty, 9), round(l.avg_cost, 9)) for s, l in p.ledger.positions.items()},
        "broker_cash": (round(st["settled_cash"], 9), round(st["unsettled_cash"], 9)),
        "broker_positions": sorted((q["symbol"], round(q["qty"], 9), round(q["avg_cost"], 9)) for q in st["positions"]),
        "fills": [(f["symbol"], f["side"], round(f["qty"], 9), round(f["price"], 9)) for f in st["fills"]],
        "orders": sorted((o["request"]["symbol"], o["request"]["order_type"], o["state"],
                          round(o["filled_qty"], 9)) for o in st["orders"]),
        "stops": {s: (round(v[1].stop_price, 9), round(v[2], 9), round(v[3], 9)) for s, v in p.open_stops.items()},
        "sizing": p.sizing.export_state(), "risk_state": p.risk_controller.state.value,
    }


def test_a_restored_account_ends_exactly_where_an_uninterrupted_one_does(tmp_path):
    clock_a = FrozenClock(current=START)
    a, _ = make(tmp_path, "a", clock_a)
    day(a, clock_a, 16)

    clock_b = FrozenClock(current=START)
    b1, store_b = make(tmp_path, "b", clock_b)
    day(b1, clock_b, 8)
    assert b1.open_stops and b1.ledger.positions                                  # there is something to lose
    # the process dies here. A brand-new one starts on the same files.
    b2, store_b2 = make(tmp_path, "b", clock_b)
    report = restore_latest(b2, store_b2)
    assert report.reason.startswith("session_end:") and report.lost_orders == [] and report.interrupted_session is None
    assert fingerprint(b2) == fingerprint(b1)                                      # restored == what was saved
    day(b2, clock_b, 8)
    assert fingerprint(b2) == fingerprint(a)                                       # and it carries on identically


def test_yesterdays_flow_observation_does_not_tilt_todays_decision(tmp_path):
    """Regression: the flow engine kept the latest observation per feature forever."""
    from services.institutional.engine import FlowFeature, FlowObservation
    clock = FrozenClock(current=START)
    p, _ = make(tmp_path, "f", clock)
    p.flow_engine.ingest(FlowObservation("PLTR", FlowFeature.VOLUME_ANOMALY, 1.0, START - timedelta(days=9), "own-bars"))
    assert p.flow_engine.signal("PLTR") is not None
    p.run_session(clock.now())
    assert p.flow_engine.signal("PLTR") is None or p.flow_engine.signal("PLTR").score != 1.0   # the old spike is gone


# ---- the store ----

def test_store_keeps_the_newest_n_falls_back_past_a_damaged_row_and_can_be_opened_read_only(tmp_path):
    path = str(tmp_path / "c.db")
    st = DurableCheckpointStore(path, Environment.PAPER, keep=3)
    for i in range(6):
        st.save({"n": i}, f"r{i}", START + timedelta(minutes=i))
    assert st.info()["count"] == 3 and st.latest_valid().state == {"n": 5}
    con = sqlite3.connect(path)
    con.execute("UPDATE paper_checkpoints SET state_json = '{\"n\": 999}' WHERE id = (SELECT MAX(id) FROM paper_checkpoints)")
    con.commit()
    assert st.latest_valid().state == {"n": 4} and st.skipped_corrupt == 1             # the damaged newest is skipped
    ro = DurableCheckpointStore(path, Environment.PAPER, read_only=True)
    with pytest.raises(PermissionError):
        ro.save({}, "x", START)
    assert ro.info()["reason"] == "r5"
    con.execute("UPDATE paper_checkpoints SET checksum = 'bad'")
    con.commit()
    assert st.latest_valid() is None and st.skipped_corrupt == 3


def test_a_value_that_is_not_json_is_refused_at_save_time(tmp_path):
    st = DurableCheckpointStore(str(tmp_path / "c.db"), Environment.PAPER)
    with pytest.raises(TypeError):
        st.save({"when": START}, "x", START)                                            # never stringified into mush


def test_environments_do_not_share_snapshots(tmp_path):
    DurableCheckpointStore(str(tmp_path / "c.db"), Environment.PAPER).save({"a": 1}, "x", START)
    assert DurableCheckpointStore(str(tmp_path / "c.db"), Environment.LIVE).latest_valid() is None


# ---- restoring ----

def test_nothing_to_restore_is_an_error_not_a_fresh_account(tmp_path):
    clock = FrozenClock(current=START)
    p, store = make(tmp_path, "n", clock)
    with pytest.raises(CheckpointError, match="no valid checkpoint"):
        restore_latest(p, store)


def test_a_tampered_ledger_is_refused_and_nothing_trades(tmp_path):
    clock = FrozenClock(current=START)
    p, store = make(tmp_path, "t", clock)
    day(p, clock, 4)
    state = store.latest_valid().state
    sym = next(iter(state["ledger"]["positions"]))
    state["ledger"]["positions"][sym]["qty"] += 1.0                                      # a position the entries do not explain
    store.save(state, "tampered", clock.now())
    q, store2 = make(tmp_path, "t", clock)
    with pytest.raises(CheckpointError, match="damaged"):
        restore_latest(q, store2)


def test_a_position_without_a_live_protective_stop_is_refused(tmp_path):
    clock = FrozenClock(current=START)
    p, store = make(tmp_path, "u", clock)
    day(p, clock, 4)
    state = store.latest_valid().state
    sym = next(iter(state["registry"]["open_stops"]))
    del state["registry"]["open_stops"][sym]
    store.save(state, "no-stop", clock.now())
    q, store2 = make(tmp_path, "u", clock)
    with pytest.raises(CheckpointError, match=f"without a live protective stop.*{sym}"):
        restore_latest(q, store2)


def test_a_broker_that_disagrees_with_the_ledger_is_refused(tmp_path):
    clock = FrozenClock(current=START)
    p, store = make(tmp_path, "m", clock)
    day(p, clock, 4)
    state = store.latest_valid().state
    state["broker"]["settled_cash"] += 50.0
    store.save(state, "drift", clock.now())
    q, store2 = make(tmp_path, "m", clock)
    with pytest.raises(CheckpointError, match="does not reconcile"):
        restore_latest(q, store2)


def test_a_halt_survives_a_restart_and_the_wrong_environment_or_schema_is_refused(tmp_path):
    clock = FrozenClock(current=START)
    p, store = make(tmp_path, "h", clock)
    day(p, clock, 2)
    p.risk_controller.set_state(RiskState.HALT_NEW_ENTRIES, reason="test")
    p.checkpointer("halted")
    q, store2 = make(tmp_path, "h", clock)
    restore_latest(q, store2)
    assert q.risk_controller.state is RiskState.HALT_NEW_ENTRIES
    rec = store2.latest_valid()
    bad_schema = type(rec)(id=rec.id, at=rec.at, reason=rec.reason, schema_version=99, state=rec.state)
    r, _ = make(tmp_path, "h2", clock)
    with pytest.raises(CheckpointError, match="schema"):
        restore(r, bad_schema)
    live_state = dict(rec.state, environment="LIVE")
    with pytest.raises(CheckpointError, match="LIVE"):
        restore(r, type(rec)(id=1, at=rec.at, reason="x", schema_version=1, state=live_state))


def test_fills_already_booked_are_not_reported_again_after_a_restore(tmp_path):
    clock = FrozenClock(current=START)
    p, store = make(tmp_path, "d", clock)
    day(p, clock, 10)
    assert p.execution.export_state()["seen_fill_ids"]
    q, store2 = make(tmp_path, "d", clock)
    restore_latest(q, store2)
    assert q.execution.sync_open_orders() == []                                         # nothing is double-booked


def test_only_a_simulated_broker_can_be_checkpointed(tmp_path):
    clock = FrozenClock(current=START)
    p, _ = make(tmp_path, "s", clock)
    p.execution.broker = object()
    with pytest.raises(CheckpointError, match="simulated broker"):
        capture(p)


# ---- crashing in the middle of a session ----

class Crash(BaseException):
    """Stands in for the process dying (not an Exception: nothing in the pipeline may swallow it)."""


def test_a_crash_between_a_fill_and_its_checkpoint_rewinds_that_order_and_leaves_a_consistent_account(tmp_path):
    clock = FrozenClock(current=START)
    p, store = make(tmp_path, "x", clock)
    day(p, clock, 3)
    saved = p.checkpointer
    seen = {"orders": 0}

    def dying(reason):
        if reason == "order":
            seen["orders"] += 1
            if seen["orders"] == 2:                      # the 2nd order of the session has traded but is not saved
                raise Crash()
        saved(reason)
    p.checkpointer = dying
    with pytest.raises(Crash):
        day(p, clock, 1)
    q, store2 = make(tmp_path, "x", clock)
    report = restore_latest(q, store2)
    assert report.reason == "order"
    # the trade in flight is TWO orders — the entry and its protective stop — and they rewind together, so
    # there is never a position without its stop
    assert len(report.lost_orders) == 2 and sum("stop" in o for o in report.lost_orders) == 1
    for lost in report.lost_orders:
        assert q.execution.store.get(lost).state.value == "REJECTED"                     # the store says so too
        assert q.execution.state_machine.get(lost).state.value == "REJECTED"
    assert set(q.ledger.positions) == set(q.open_stops)                                  # every position has its stop
    assert q.reconciliation.reconcile().consistent
    day(q, clock, 3)                                                                     # and trading simply continues
    assert q.reconciliation.reconcile().consistent and q.ledger.cash >= 0


def test_a_crash_before_the_first_order_of_a_session_is_reported_as_an_interrupted_session(tmp_path):
    clock = FrozenClock(current=START)
    p, store = make(tmp_path, "y", clock)
    day(p, clock, 3)
    p.checkpointer(f"session_start:{clock.now().date()}")                                # the session began ...
    q, store2 = make(tmp_path, "y", clock)                                               # ... and the process died
    report = restore_latest(q, store2)
    assert report.interrupted_session == clock.now().date() and report.lost_orders == []
