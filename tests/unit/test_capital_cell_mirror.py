"""Capital-cell mirror, phase A: AI/HUMAN cells shadow the real books, observe-only."""
from datetime import date, datetime, timedelta, timezone

import pytest

from packages.common.clock import FrozenClock
from packages.schemas.core import Action
from services.capital_cells.mirror import CELL_AI, CELL_HUMAN, CapitalCellMirrorObserver
from services.decision.human_intent import IntentAction
from services.decision.intent_inbox import IntentInbox
from services.market_data.universe import UniverseManager, UniverseSymbol
from services.pdca.observer import FillEvent, SessionEndEvent
from services.risk.master_controller import RiskState
from tests.conftest import SYMBOLS, build_pipeline

T = datetime(2026, 8, 11, 15, 0, tzinfo=timezone.utc)


def fill(side, qty, price, sym="AAA", cid="c1", fees=0.0, source=None, ts=T):
    return FillEvent(symbol=sym, side=side, qty=qty, price=price, fees=fees, ts=ts, client_order_id=cid,
                     reference_price=None, source=source)


class _R:
    data_warnings = []


def end(cash, positions, prices=None, ts=T):
    return SessionEndEvent(as_of=ts, result=_R(), price_on=lambda s, at: (prices or {}).get(s), equity=0.0,
                           ledger_cash=cash, ledger_positions=positions)


# ---- the mirror on its own ----

def test_buys_draw_cash_from_the_shared_pool_into_the_originating_cell():
    m = CapitalCellMirrorObserver(1000.0)
    m.on_fill(fill(Action.BUY, 2, 100.0, "AAA", "o1", fees=0.5, source="AI"))
    m.on_fill(fill(Action.BUY, 1, 50.0, "BBB", "o2", fees=0.0, source="HUMAN"))
    ai, human = m.cells[CELL_AI], m.cells[CELL_HUMAN]
    assert ai.position_qty("AAA") == 2 and human.position_qty("BBB") == 1
    assert ai.cash == pytest.approx(0.0) and human.cash == pytest.approx(0.0)           # drew exactly what it spent
    assert m.summary()["unallocated_cash"] == pytest.approx(1000.0 - 200.5 - 50.0)
    m.on_session_end(end(cash=1000.0 - 250.5, positions={"AAA": 2.0, "BBB": 1.0}))
    assert m.summary()["reconciliation"]["latest"]["consistent"]


def test_a_stop_sale_goes_to_the_cells_holding_the_symbol_pro_rata_with_fees_split_exactly():
    m = CapitalCellMirrorObserver(1000.0)
    m.on_fill(fill(Action.BUY, 3, 10.0, "AAA", "o1", source="AI"))
    m.on_fill(fill(Action.BUY, 1, 10.0, "AAA", "o2", source="HUMAN"))
    m.on_fill(fill(Action.SELL, 4, 12.0, "AAA", "stop1", fees=0.4))                        # protective stop: no source
    ai, human = m.cells[CELL_AI], m.cells[CELL_HUMAN]
    assert ai.position_qty("AAA") == 0 and human.position_qty("AAA") == 0
    assert ai.virtual_realized_pnl == pytest.approx(3 * 2.0) and human.virtual_realized_pnl == pytest.approx(2.0)
    assert ai.cash + human.cash == pytest.approx(4 * 12.0 - 0.4)                           # proceeds stay in the cells
    m.on_session_end(end(cash=1000.0 - 40.0 + 48.0 - 0.4, positions={}))
    assert m.summary()["reconciliation"]["latest"]["consistent"] and m.errors == []


def test_a_buy_with_no_stated_originator_is_counted_as_defaulted_and_later_fills_follow_the_same_cell():
    m = CapitalCellMirrorObserver(1000.0)
    m.on_fill(fill(Action.BUY, 1, 10.0, cid="resting"))
    m.on_fill(fill(Action.BUY, 1, 10.0, cid="resting", ts=T + timedelta(days=1)))
    assert m.defaulted_to_ai == 1 and m.cells[CELL_AI].position_qty("AAA") == 2


def test_a_mirror_that_disagrees_with_the_master_is_reported_not_acted_on():
    m = CapitalCellMirrorObserver(1000.0)
    m.on_fill(fill(Action.BUY, 2, 100.0, source="AI"))
    m.on_session_end(end(cash=800.0, positions={"AAA": 3.0}))                                # the master holds 3, not 2
    rec = m.summary()["reconciliation"]
    assert rec["sessions_inconsistent"] == 1 and not rec["latest"]["consistent"]
    assert [x["kind"] for x in rec["latest"]["mismatches"]] == ["position"]


def test_fills_the_cells_cannot_take_are_recorded_as_errors_never_raised():
    m = CapitalCellMirrorObserver(100.0)
    m.on_fill(fill(Action.SELL, 1, 10.0, sym="GHOST"))                                       # nobody holds it
    m.on_fill(fill(Action.BUY, 10, 100.0, source="AI"))                                      # 1000 > 100 in the pool
    assert len(m.errors) == 2 and "no cell holds GHOST" in m.errors[0] and "unallocated" in m.errors[1]


def test_unpriced_holdings_are_valued_at_cost_and_say_so():
    m = CapitalCellMirrorObserver(1000.0)
    m.on_fill(fill(Action.BUY, 2, 100.0, source="AI"))
    m.on_session_end(end(cash=800.0, positions={"AAA": 2.0}, prices={}))
    ai = m.summary()["reconciliation"]["latest"]["cells"][CELL_AI]
    assert ai["valued_at_cost"] == ["AAA"] and ai["marked_equity"] == pytest.approx(200.0)
    m.on_session_end(end(cash=800.0, positions={"AAA": 2.0}, prices={"AAA": 110.0}))
    assert m.summary()["reconciliation"]["latest"]["cells"][CELL_AI]["marked_equity"] == pytest.approx(220.0)


# ---- through the real pipeline ----

def _pipeline(observers, inbox=None):
    clock = FrozenClock(current=T)
    universe = UniverseManager()
    for s in SYMBOLS:
        universe.add(UniverseSymbol(symbol=s, listed_from=date(2015, 1, 1)))
    return build_pipeline(clock, universe, observers=list(observers), human_inbox=inbox), clock


def _run(p, clock, sessions):
    for _ in range(sessions):
        p.run_session(clock.now())
        clock.advance(86400)
        p.execution.broker.settle()


def test_over_many_sessions_with_ai_and_human_trades_the_cells_add_up_to_the_real_books(tmp_path):
    box = IntentInbox(tmp_path / "i.jsonl")
    m = CapitalCellMirrorObserver(1000.0)
    p, clock = _pipeline([m], inbox=box)
    iid = box.submit("NVDA", IntentAction.WANT_BUY, "thesis", "alice", T)
    p.analyze_human_intents(T)
    box.confirm(iid, "alice", T)
    _run(p, clock, 15)
    s = m.summary()
    assert s["errors"] == [] and s["defaulted_to_ai"] == 0
    assert s["reconciliation"]["sessions_checked"] == 15 and s["reconciliation"]["sessions_inconsistent"] == 0
    assert s["fills_attributed"][CELL_AI] > 0 and s["fills_attributed"][CELL_HUMAN] >= 1       # the human's buy
    ai, human = m.cells[CELL_AI], m.cells[CELL_HUMAN]
    # the virtual P&L split sums to the real realized P&L
    assert ai.virtual_realized_pnl + human.virtual_realized_pnl == pytest.approx(p.ledger.realized_pnl, abs=1e-6)
    assert ai.cash + human.cash + m._unallocated == pytest.approx(p.ledger.cash, abs=1e-6)
    for sym, lot in p.ledger.positions.items():
        assert ai.position_qty(sym) + human.position_qty(sym) == pytest.approx(lot.qty, abs=1e-6)


def test_the_mirror_changes_nothing_about_trading_and_a_mismatch_never_halts_entries():
    plain, c0 = _pipeline([])
    m = CapitalCellMirrorObserver(1000.0)
    p, c1 = _pipeline([m])
    for _ in range(8):
        a, b = p.run_session(c1.now()), plain.run_session(c0.now())
        assert (a.orders_filled, a.stops_triggered) == (b.orders_filled, b.stops_triggered)
        assert a.data_warnings == []
        for pp, cc in ((p, c1), (plain, c0)):
            cc.advance(86400)
            pp.execution.broker.settle()
        if _ == 3:                                         # break the mirror on purpose mid-run
            m.cells[CELL_AI]._cash += 5.0
    assert m.summary()["reconciliation"]["sessions_inconsistent"] >= 1
    assert p.risk_controller.state is RiskState.NORMAL and plain.risk_controller.state is RiskState.NORMAL
    assert p.ledger.cash == pytest.approx(plain.ledger.cash)
