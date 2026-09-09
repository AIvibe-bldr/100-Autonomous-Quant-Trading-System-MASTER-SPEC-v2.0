"""Capital Cell foundation tests (docs/capital_cell_architecture.md §41
priority 1-7). Covers the subset of the doc's §39 invariants that apply to
this scope:

    cell_position >= 0
    cell_sell_qty <= cell_position
    sum(cell_positions_by_symbol) == broker_position_after_reconciliation
    sum(cell_cash) + master_unallocated_cash + adjustments == master_cash
    no_cell_can_spend_reserved_capital_of_another_cell
    net_broker_order == deterministic_net(gross_cell_intents)
    internal_cross_does_not_create_fake_master_trade
    internal_cross_does_not_create_fake_tax_event
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from packages.common.calendar import TradingCalendar
from packages.common.clock import FrozenClock
from packages.common.ledger import Ledger
from packages.common.risk_config import RiskConfig
from packages.schemas.capital_cell import CapitalCell, CellOrderIntent, CellStatus
from packages.schemas.core import Action
from services.capital_cells.fill_allocation import (
    FillAllocationEngine,
    apply_allocations_to_cells,
)
from services.capital_cells.ledger import CellLedger, CellOverdrawError, CellShortSellError
from services.capital_cells.netting import (
    DuplicateIntentIdError,
    NettingEngine,
    TransferPriceMethod,
    UnknownCellError,
    apply_crosses_to_cells,
)
from services.capital_cells.reconciliation import CellReconciliationEngine
from services.capital_cells.reservation import (
    CapitalReservationLedger,
    DuplicateReservationError,
    InsufficientCapitalError,
)
from services.risk.master_controller import MasterRiskController, RiskState

AT = datetime(2026, 8, 10, 15, 0, tzinfo=timezone.utc)


def _risk_controller() -> MasterRiskController:
    return MasterRiskController(config=RiskConfig(), calendar=TradingCalendar(),
                                clock=FrozenClock(current=AT))


def _intent(intent_id: str, cell_id: str, symbol: str, side: Action, qty: float) -> CellOrderIntent:
    return CellOrderIntent(intent_id=intent_id, cell_id=cell_id, symbol=symbol,
                           side=side, qty=qty, created_at=AT)


# ---------------------------------------------------------------------------
# 1. Cell Schema
# ---------------------------------------------------------------------------

class TestCellSchema:
    def test_capital_cell_has_no_target_progress_field(self):
        cell = CapitalCell(cell_id="c1", name="Momentum A", alpha_hypothesis="20d momentum",
                           status=CellStatus.ACTIVE)
        assert not hasattr(cell, "target_progress")
        assert not hasattr(cell, "hundred_x_goal")

    def test_cell_order_intent_rejects_unknown_fields(self):
        with pytest.raises(ValidationError):
            CellOrderIntent(intent_id="i1", cell_id="c1", symbol="NVDA", side=Action.BUY,
                            qty=10, created_at=AT, target_progress=0.5)  # type: ignore[call-arg]

    def test_cell_order_intent_qty_must_be_positive(self):
        with pytest.raises(ValidationError):
            CellOrderIntent(intent_id="i1", cell_id="c1", symbol="NVDA", side=Action.BUY,
                            qty=0, created_at=AT)


# ---------------------------------------------------------------------------
# 2 & 5. Virtual Position/Cash Ledger + Cell SELL / No Short
# ---------------------------------------------------------------------------

class TestCellLedger:
    def test_allocate_and_buy(self):
        cell = CellLedger("c1")
        cell.allocate_cash(1000.0, at=AT)
        cell.record_broker_fill("NVDA", 10, price=50.0, fees=1.0, at=AT)
        assert cell.position_qty("NVDA") == 10
        assert cell.cash == pytest.approx(1000.0 - 500.0 - 1.0)

    def test_cell_position_never_negative_after_full_sell(self):
        cell = CellLedger("c1")
        cell.allocate_cash(1000.0, at=AT)
        cell.record_broker_fill("NVDA", 10, price=50.0, fees=0.0, at=AT)
        cell.record_broker_fill("NVDA", -10, price=55.0, fees=0.0, at=AT)
        assert cell.position_qty("NVDA") == 0
        assert "NVDA" not in cell.positions

    def test_cell_sell_qty_cannot_exceed_cell_position(self):
        cell = CellLedger("c1")
        cell.allocate_cash(1000.0, at=AT)
        cell.record_broker_fill("NVDA", 10, price=50.0, fees=0.0, at=AT)
        with pytest.raises(CellShortSellError):
            cell.record_broker_fill("NVDA", -11, price=55.0, fees=0.0, at=AT)

    def test_cell_cannot_short_from_zero(self):
        cell = CellLedger("c1")
        cell.allocate_cash(1000.0, at=AT)
        with pytest.raises(CellShortSellError):
            cell.record_broker_fill("NVDA", -1, price=50.0, fees=0.0, at=AT)

    def test_cell_cannot_overdraw_virtual_cash(self):
        cell = CellLedger("c1")
        cell.allocate_cash(100.0, at=AT)
        with pytest.raises(CellOverdrawError):
            cell.record_broker_fill("NVDA", 10, price=50.0, fees=0.0, at=AT)  # needs $500

    def test_internal_cross_never_charges_fees(self):
        cell = CellLedger("c1")
        cell.allocate_cash(1000.0, at=AT)
        cell.record_internal_cross("NVDA", 10, price=50.0, at=AT)
        assert cell.cash == pytest.approx(1000.0 - 500.0)  # no fee leg at all
        assert cell.virtual_realized_pnl == 0.0


# ---------------------------------------------------------------------------
# 3. Master Reconciliation Invariant
# ---------------------------------------------------------------------------

class TestCellReconciliation:
    def _build(self):
        master = Ledger(initial_cash=1000.0)
        cell_a, cell_b = CellLedger("a"), CellLedger("b")
        cell_a.allocate_cash(600.0, at=AT)
        cell_b.allocate_cash(400.0, at=AT)
        return master, {"a": cell_a, "b": cell_b}

    def test_consistent_when_cash_and_positions_match(self):
        master, cells = self._build()
        engine = CellReconciliationEngine(master_ledger=master, cells=cells)
        report = engine.reconcile()
        assert report.consistent, report.mismatches

    def test_cash_mismatch_detected_and_halts_new_entries(self):
        master, cells = self._build()
        cells["a"].allocate_cash(50.0, at=AT)  # cells now sum to 1050, master still 1000
        risk = _risk_controller()
        engine = CellReconciliationEngine(master_ledger=master, cells=cells,
                                          risk_controller=risk)
        report = engine.reconcile()
        assert not report.consistent
        assert any(m.kind == "cash" for m in report.mismatches)
        assert risk.state is RiskState.HALT_NEW_ENTRIES

    def test_position_mismatch_detected(self):
        master, cells = self._build()
        master.record_fill("NVDA", 10, price=50.0, fees=0.0, at=AT)
        # cells never recorded the matching position -> sum(cells) != master
        engine = CellReconciliationEngine(master_ledger=master, cells=cells)
        report = engine.reconcile()
        assert not report.consistent
        assert any(m.kind == "position" for m in report.mismatches)

    def test_position_reconciles_when_cells_sum_to_master(self):
        master, cells = self._build()
        master.record_fill("NVDA", 10, price=50.0, fees=0.0, at=AT)
        cells["a"].record_broker_fill("NVDA", 6, price=50.0, fees=0.0, at=AT)
        cells["b"].record_broker_fill("NVDA", 4, price=50.0, fees=0.0, at=AT)
        engine = CellReconciliationEngine(master_ledger=master, cells=cells)
        report = engine.reconcile()
        assert report.consistent, report.mismatches

    def test_unallocated_master_cash_is_part_of_the_equation(self):
        master = Ledger(initial_cash=1000.0)
        cells = {"a": CellLedger("a")}
        cells["a"].allocate_cash(600.0, at=AT)  # only 600 of 1000 handed out
        engine = CellReconciliationEngine(master_ledger=master, cells=cells,
                                          unallocated_master_cash=400.0)
        report = engine.reconcile()
        assert report.consistent, report.mismatches


# ---------------------------------------------------------------------------
# 4. Capital Reservation Ledger
# ---------------------------------------------------------------------------

class TestCapitalReservation:
    def test_reserve_reduces_available_cash(self):
        ledger = CapitalReservationLedger(total_cash=1000.0)
        ledger.reserve("r1", "cell-a", 300.0)
        assert ledger.available_cash == pytest.approx(700.0)
        assert ledger.reserved_cash == pytest.approx(300.0)

    def test_no_cell_can_spend_reserved_capital_of_another_cell(self):
        ledger = CapitalReservationLedger(total_cash=1000.0)
        ledger.reserve("r1", "cell-a", 700.0)
        with pytest.raises(InsufficientCapitalError):
            ledger.reserve("r2", "cell-b", 400.0)  # only 300 left
        assert ledger.cell_reserved_capital("cell-a") == 700.0
        assert ledger.cell_reserved_capital("cell-b") == 0.0

    def test_release_frees_capital_for_other_cells(self):
        ledger = CapitalReservationLedger(total_cash=1000.0)
        ledger.reserve("r1", "cell-a", 700.0)
        ledger.release("r1")
        ledger.reserve("r2", "cell-b", 700.0)  # would have failed before release
        assert ledger.cell_reserved_capital("cell-b") == 700.0

    def test_duplicate_reservation_id_rejected(self):
        ledger = CapitalReservationLedger(total_cash=1000.0)
        ledger.reserve("r1", "cell-a", 100.0)
        with pytest.raises(DuplicateReservationError):
            ledger.reserve("r1", "cell-b", 50.0)

    def test_sync_total_cash_tracks_master_truth(self):
        ledger = CapitalReservationLedger(total_cash=1000.0)
        ledger.reserve("r1", "cell-a", 900.0)
        ledger.sync_total_cash(950.0)  # e.g. a fee was paid at the broker
        assert ledger.available_cash == pytest.approx(50.0)


# ---------------------------------------------------------------------------
# 6. Internal Netting / Collision Engine
# ---------------------------------------------------------------------------

def _cells(holdings: dict[str, float], cash: float = 100_000.0,
           price: float = 40.0) -> dict[str, CellLedger]:
    """Cells with `cash` virtual cash each, pre-loaded with the given NVDA
    holdings so a SELL intent is backed by a real virtual long (§7)."""
    cells: dict[str, CellLedger] = {}
    for cell_id, qty in holdings.items():
        cell = CellLedger(cell_id)
        cell.allocate_cash(cash, at=AT)
        if qty:
            cell.record_broker_fill("NVDA", qty, price=price, fees=0.0, at=AT)
        cells[cell_id] = cell
    return cells


class TestNettingEngine:
    def _engine(self, price: float = 100.0) -> NettingEngine:
        return NettingEngine(transfer_price_fn=lambda symbol: price)

    def test_full_offset_example_from_spec(self):
        # §6: Cell A BUY 100, Cell B SELL 80 -> Broker Net Order BUY 20
        cells = _cells({"A": 0, "B": 80})
        intents = [_intent("i1", "A", "NVDA", Action.BUY, 100),
                   _intent("i2", "B", "NVDA", Action.SELL, 80)]
        result = self._engine().net(intents, cells)
        r = result.by_symbol["NVDA"]
        assert r.net_broker_side is Action.BUY
        assert r.net_broker_qty == pytest.approx(20)
        assert r.internal_cross_qty == pytest.approx(80)
        assert result.gross_buy_flow == pytest.approx(100)
        assert result.gross_sell_flow == pytest.approx(80)
        assert result.net_broker_flow == pytest.approx(20)

    def test_partial_cross_example_from_spec(self):
        # §7: Cell A BUY 100, Cell B SELL 60 (cell B holds >=60) -> Broker BUY 40
        cells = _cells({"A": 0, "B": 60})
        intents = [_intent("i1", "A", "NVDA", Action.BUY, 100),
                   _intent("i2", "B", "NVDA", Action.SELL, 60)]
        result = self._engine().net(intents, cells)
        r = result.by_symbol["NVDA"]
        assert r.net_broker_side is Action.BUY
        assert r.net_broker_qty == pytest.approx(40)
        assert r.internal_cross_qty == pytest.approx(60)
        assert len(r.crosses) == 1
        assert r.crosses[0].buy_cell_id == "A"
        assert r.crosses[0].sell_cell_id == "B"

    def test_cross_requires_the_selling_cell_to_actually_hold_the_shares(self):
        """§7: B's SELL may only cross if B really holds those shares. A plan
        that would manufacture a cell-level short must never be produced."""
        cells = _cells({"A": 0, "B": 40})   # B holds 40 but wants to sell 60
        intents = [_intent("i1", "A", "NVDA", Action.BUY, 100),
                   _intent("i2", "B", "NVDA", Action.SELL, 60)]
        with pytest.raises(CellShortSellError):
            self._engine().net(intents, cells)

    def test_split_sell_intents_are_checked_in_aggregate(self):
        """Two 30-share SELLs each look legal against a 50-share holding;
        their sum does not (§2)."""
        cells = _cells({"A": 0, "B": 50})
        intents = [_intent("i1", "A", "NVDA", Action.BUY, 100),
                   _intent("i2", "B", "NVDA", Action.SELL, 30),
                   _intent("i3", "B", "NVDA", Action.SELL, 30)]
        with pytest.raises(CellShortSellError):
            self._engine().net(intents, cells)

    def test_duplicate_intent_ids_are_rejected(self):
        """§13: unique intent ids. With duplicates the sort falls back on
        input order and netting stops being deterministic."""
        cells = _cells({"A": 0, "B": 0, "C": 10})
        intents = [_intent("same", "A", "NVDA", Action.BUY, 10),
                   _intent("same", "B", "NVDA", Action.BUY, 10),
                   _intent("i3", "C", "NVDA", Action.SELL, 10)]
        with pytest.raises(DuplicateIntentIdError):
            self._engine().net(intents, cells)

    def test_unknown_cell_is_rejected(self):
        intents = [_intent("i1", "GHOST", "NVDA", Action.BUY, 10)]
        with pytest.raises(UnknownCellError):
            self._engine().net(intents, _cells({"A": 0}))

    def test_fully_crossed_produces_no_broker_order(self):
        cells = _cells({"A": 0, "B": 50})
        intents = [_intent("i1", "A", "NVDA", Action.BUY, 50),
                   _intent("i2", "B", "NVDA", Action.SELL, 50)]
        result = self._engine().net(intents, cells)
        r = result.by_symbol["NVDA"]
        assert r.net_broker_side is None
        assert r.net_broker_qty == 0.0
        assert r.residual_intents == ()
        assert result.broker_orders() == []

    def test_gross_intent_is_not_lost_even_when_fully_netted(self):
        cells = _cells({"A": 0, "B": 50})
        intents = [_intent("i1", "A", "NVDA", Action.BUY, 50),
                   _intent("i2", "B", "NVDA", Action.SELL, 50)]
        result = self._engine().net(intents, cells)
        r = result.by_symbol["NVDA"]
        assert r.gross_buy_qty == 50 and r.gross_sell_qty == 50
        assert len(r.contributing_intents) == 2

    def test_residual_intents_say_which_cells_the_broker_order_owes(self):
        """Fill Allocation must be driven from the netting result, not from
        the caller re-deriving who was left unfilled."""
        cells = _cells({"A": 0, "B": 0, "C": 30})
        intents = [_intent("i1", "A", "NVDA", Action.BUY, 50),
                   _intent("i2", "B", "NVDA", Action.BUY, 20),
                   _intent("i3", "C", "NVDA", Action.SELL, 30)]
        r = self._engine().net(intents, cells).by_symbol["NVDA"]
        assert r.net_broker_qty == pytest.approx(40)
        residual = {i.cell_id: i.qty for i in r.residual_intents}
        assert residual == {"A": pytest.approx(20), "B": pytest.approx(20)}
        assert sum(i.qty for i in r.residual_intents) == pytest.approx(r.net_broker_qty)

    def test_residual_intents_on_the_sell_side_when_net_is_a_sell(self):
        cells = _cells({"A": 0, "B": 70})
        intents = [_intent("i1", "A", "NVDA", Action.BUY, 20),
                   _intent("i2", "B", "NVDA", Action.SELL, 70)]
        r = self._engine().net(intents, cells).by_symbol["NVDA"]
        assert r.net_broker_side is Action.SELL
        assert r.net_broker_qty == pytest.approx(50)
        assert [(i.cell_id, i.qty) for i in r.residual_intents] == [("B", pytest.approx(50))]

    def test_net_broker_order_is_deterministic_regardless_of_input_order(self):
        cells = _cells({"A": 0, "B": 60, "C": 30})
        intents_a = [_intent("i1", "A", "NVDA", Action.BUY, 100),
                     _intent("i2", "B", "NVDA", Action.SELL, 60),
                     _intent("i3", "C", "NVDA", Action.SELL, 30)]
        intents_b = list(reversed(intents_a))
        ra = self._engine().net(intents_a, cells).by_symbol["NVDA"]
        rb = self._engine().net(intents_b, cells).by_symbol["NVDA"]
        assert ra.net_broker_side == rb.net_broker_side
        assert ra.net_broker_qty == rb.net_broker_qty
        assert ra.crosses == rb.crosses
        assert ra.residual_intents == rb.residual_intents

    def test_no_intents_for_symbol_means_no_cross(self):
        intents = [_intent("i1", "A", "NVDA", Action.BUY, 10)]
        result = self._engine().net(intents, _cells({"A": 0}))
        r = result.by_symbol["NVDA"]
        assert r.crosses == ()
        assert r.net_broker_qty == pytest.approx(10)

    def test_internal_cross_uses_versioned_transfer_price(self):
        cells = _cells({"A": 0, "B": 10})
        intents = [_intent("i1", "A", "NVDA", Action.BUY, 10),
                   _intent("i2", "B", "NVDA", Action.SELL, 10)]
        result = self._engine(price=123.45).net(intents, cells)
        cross = result.by_symbol["NVDA"].crosses[0]
        assert cross.transfer_price == pytest.approx(123.45)
        assert cross.transfer_price_method is TransferPriceMethod.ARRIVAL_MID_V1

    def test_apply_crosses_never_touches_master_ledger(self):
        """§39: internal_cross_does_not_create_fake_master_trade /
        ..._fake_tax_event. Structural check: the Master Ledger object is
        never even passed to apply_crosses_to_cells, so it cannot record a
        fake trade or fee no matter what the netting result contains."""
        master = Ledger(initial_cash=10_000.0)
        cells = _cells({"A": 0, "B": 100}, cash=10_000.0, price=50.0)
        cell_a, cell_b = cells["A"], cells["B"]

        intents = [_intent("i1", "A", "NVDA", Action.BUY, 60),
                   _intent("i2", "B", "NVDA", Action.SELL, 60)]
        result = self._engine(price=52.0).net(intents, cells)
        assert result.by_symbol["NVDA"].net_broker_qty == 0.0  # fully internal

        master_entries_before = len(master.entries)
        apply_crosses_to_cells(cells, result, at=AT)

        assert len(master.entries) == master_entries_before  # untouched
        assert master.position_qty("NVDA") == 0.0             # no fake master position
        assert cell_a.position_qty("NVDA") == pytest.approx(60)
        assert cell_b.position_qty("NVDA") == pytest.approx(40)
        # virtual P&L landed on the cells, never on the real ledger
        assert cell_b.virtual_realized_pnl == pytest.approx((52.0 - 50.0) * 60)
        assert master.realized_pnl == 0.0
        assert master.fees_paid == 0.0  # no fake fee / tax event either

    def test_apply_crosses_is_all_or_nothing(self):
        """A rejected write must leave no trace (same rule as Ledger._append).
        A partial apply would itself break the §4 reconciliation invariant it
        is supposed to protect."""
        cells = _cells({"A": 0, "B": 40, "C": 30}, price=50.0)
        intents = [_intent("i1", "A", "NVDA", Action.BUY, 70),
                   _intent("i2", "B", "NVDA", Action.SELL, 40),
                   _intent("i3", "C", "NVDA", Action.SELL, 30)]
        result = self._engine(price=52.0).net(intents, cells)

        # C's holding vanishes between planning and applying (e.g. an earlier
        # protective stop already sold it) -> the whole apply must be refused.
        cells["C"].record_broker_fill("NVDA", -30, price=51.0, fees=0.0, at=AT)
        before = {cid: (c.cash, c.position_qty("NVDA")) for cid, c in cells.items()}
        with pytest.raises(CellShortSellError):
            apply_crosses_to_cells(cells, result, at=AT)
        after = {cid: (c.cash, c.position_qty("NVDA")) for cid, c in cells.items()}
        assert before == after

    def test_apply_crosses_refuses_when_a_buying_cell_lacks_virtual_cash(self):
        cells = _cells({"A": 0, "B": 100}, price=50.0)
        cells["A"] = CellLedger("A")
        cells["A"].allocate_cash(100.0, at=AT)   # nowhere near 60 * 52
        intents = [_intent("i1", "A", "NVDA", Action.BUY, 60),
                   _intent("i2", "B", "NVDA", Action.SELL, 60)]
        result = self._engine(price=52.0).net(intents, cells)
        before = {cid: (c.cash, c.position_qty("NVDA")) for cid, c in cells.items()}
        with pytest.raises(CellOverdrawError):
            apply_crosses_to_cells(cells, result, at=AT)
        assert {cid: (c.cash, c.position_qty("NVDA")) for cid, c in cells.items()} == before

    def test_a_cell_funded_by_its_own_sale_can_still_cross(self):
        """Sell legs are applied before buy legs, so a cell whose purchase is
        funded by its own simultaneous sale is not spuriously rejected."""
        cells = _cells({"A": 0, "B": 0}, cash=0.0)
        cells["B"].allocate_cash(5_000.0, at=AT)
        cells["B"].record_broker_fill("MSFT", 100, price=50.0, fees=0.0, at=AT)
        cells["A"].allocate_cash(5_000.0, at=AT)
        cells["A"].record_broker_fill("NVDA", 100, price=50.0, fees=0.0, at=AT)
        # A sells NVDA (raising cash) and buys MSFT in the same batch
        intents = [_intent("i1", "A", "MSFT", Action.BUY, 100),
                   _intent("i2", "B", "MSFT", Action.SELL, 100),
                   _intent("i3", "A", "NVDA", Action.SELL, 100),
                   _intent("i4", "B", "NVDA", Action.BUY, 100)]
        result = self._engine(price=50.0).net(intents, cells)
        apply_crosses_to_cells(cells, result, at=AT)
        assert cells["A"].position_qty("MSFT") == pytest.approx(100)
        assert cells["A"].position_qty("NVDA") == 0
        assert cells["B"].position_qty("NVDA") == pytest.approx(100)


# ---------------------------------------------------------------------------
# 7. Fill Allocation Engine
# ---------------------------------------------------------------------------

class TestFillAllocationEngine:
    def test_pro_rata_allocation_sums_exactly_to_fill_qty(self):
        residual = [_intent("i1", "A", "NVDA", Action.BUY, 30),
                    _intent("i2", "B", "NVDA", Action.BUY, 10)]
        allocations = FillAllocationEngine().allocate(
            residual, fill_qty=40.0, fill_price=55.0, fill_fees=1.0)
        assert abs(sum(a.qty for a in allocations) - 40.0) < 1e-9
        assert abs(sum(a.fees for a in allocations) - 1.0) < 1e-9

    def test_partial_broker_fill_still_sums_exactly(self):
        """Quantities that do NOT divide evenly: naive per-cell rounding
        leaves ~1e-6 of drift, which becomes SUM(cell positions) != master
        position at reconciliation. The residual must land somewhere."""
        residual = [_intent("i1", "A", "NVDA", Action.BUY, 30),
                    _intent("i2", "B", "NVDA", Action.BUY, 10),
                    _intent("i3", "C", "NVDA", Action.BUY, 7)]
        # only 11 of the 47 requested actually filled (partial broker fill)
        allocations = FillAllocationEngine().allocate(
            residual, fill_qty=11.0, fill_price=55.0, fill_fees=0.55)
        assert abs(sum(a.qty for a in allocations) - 11.0) < 1e-9
        assert abs(sum(a.fees for a in allocations) - 0.55) < 1e-9

    def test_uneven_allocation_still_reconciles_against_master(self):
        """The end consequence of the exact-sum property, asserted where it
        actually bites (§4)."""
        master = Ledger(initial_cash=100_000.0)
        cells = _cells({"A": 0, "B": 0, "C": 0}, cash=10_000.0)
        residual = [_intent("i1", "A", "NVDA", Action.BUY, 30),
                    _intent("i2", "B", "NVDA", Action.BUY, 10),
                    _intent("i3", "C", "NVDA", Action.BUY, 7)]
        allocations = FillAllocationEngine().allocate(residual, 11.0, 55.0, 0.55)
        apply_allocations_to_cells(cells, allocations, at=AT)
        master.record_fill("NVDA", 11.0, price=55.0, fees=0.55, at=AT)
        report = CellReconciliationEngine(
            master_ledger=master, cells=cells,
            unallocated_master_cash=100_000.0 - 30_000.0).reconcile()
        assert report.consistent, report.mismatches

    def test_allocation_rejects_fill_larger_than_requested(self):
        residual = [_intent("i1", "A", "NVDA", Action.BUY, 10)]
        with pytest.raises(ValueError):
            FillAllocationEngine().allocate(residual, fill_qty=20.0, fill_price=55.0, fill_fees=0.0)

    def test_allocation_is_deterministic_regardless_of_input_order(self):
        residual_a = [_intent("i1", "A", "NVDA", Action.BUY, 30),
                      _intent("i2", "B", "NVDA", Action.BUY, 10),
                      _intent("i3", "C", "NVDA", Action.BUY, 7)]
        residual_b = list(reversed(residual_a))
        alloc_a = FillAllocationEngine().allocate(residual_a, 20.0, 55.0, 1.0)
        alloc_b = FillAllocationEngine().allocate(residual_b, 20.0, 55.0, 1.0)
        by_cell_a = {a.cell_id: (a.qty, a.fees) for a in alloc_a}
        by_cell_b = {a.cell_id: (a.qty, a.fees) for a in alloc_b}
        assert by_cell_a == by_cell_b

    def test_allocation_rejects_mixed_symbols_or_sides(self):
        engine = FillAllocationEngine()
        mixed_symbols = [_intent("i1", "A", "NVDA", Action.BUY, 10),
                         _intent("i2", "B", "MSFT", Action.BUY, 10)]
        with pytest.raises(ValueError):
            engine.allocate(mixed_symbols, 10.0, 55.0, 0.0)
        mixed_sides = [_intent("i1", "A", "NVDA", Action.BUY, 10),
                       _intent("i2", "B", "NVDA", Action.SELL, 10)]
        with pytest.raises(ValueError):
            engine.allocate(mixed_sides, 10.0, 55.0, 0.0)

    def test_apply_allocations_is_all_or_nothing(self):
        """A real fill that one cell cannot absorb must not leave the other
        cells half-updated — that would be the §4 reconciliation break."""
        cells = _cells({"A": 0, "B": 0}, cash=0.0)
        cells["A"].allocate_cash(10_000.0, at=AT)
        cells["B"].allocate_cash(100.0, at=AT)      # cannot afford its share
        residual = [_intent("i1", "A", "NVDA", Action.BUY, 20),
                    _intent("i2", "B", "NVDA", Action.BUY, 20)]
        allocations = FillAllocationEngine().allocate(residual, 40.0, 53.0, 2.0)
        before = {cid: (c.cash, c.position_qty("NVDA")) for cid, c in cells.items()}
        with pytest.raises(CellOverdrawError):
            apply_allocations_to_cells(cells, allocations, at=AT)
        assert {cid: (c.cash, c.position_qty("NVDA")) for cid, c in cells.items()} == before

    def test_apply_allocations_posts_real_fill_to_each_cell(self):
        cell_a, cell_b = CellLedger("A"), CellLedger("B")
        cell_a.allocate_cash(1000.0, at=AT)
        cell_b.allocate_cash(1000.0, at=AT)
        residual = [_intent("i1", "A", "NVDA", Action.BUY, 6),
                    _intent("i2", "B", "NVDA", Action.BUY, 4)]
        allocations = FillAllocationEngine().allocate(residual, 10.0, 50.0, 1.0)
        apply_allocations_to_cells({"A": cell_a, "B": cell_b}, allocations, at=AT)
        assert cell_a.position_qty("NVDA") == pytest.approx(6)
        assert cell_b.position_qty("NVDA") == pytest.approx(4)
        # fees were real and summed exactly (no invented/lost cents)
        total_fees_deducted = (1000.0 - cell_a.cash - 6 * 50.0) + (1000.0 - cell_b.cash - 4 * 50.0)
        assert total_fees_deducted == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# End-to-end: netting -> broker fill -> allocation -> reconciliation
# ---------------------------------------------------------------------------

class TestCapitalCellEndToEnd:
    def test_full_cycle_reconciles_against_master_ledger(self):
        master = Ledger(initial_cash=6000.0)
        cell_a, cell_b, cell_c = CellLedger("A"), CellLedger("B"), CellLedger("C")
        cell_a.allocate_cash(2700.0, at=AT)  # covers 30 crossed @52 + 20 broker @53
        cell_b.allocate_cash(1200.0, at=AT)  # covers 20 broker @53
        cell_c.allocate_cash(2000.0, at=AT)  # covers its pre-existing 30-share NVDA position
        cells = {"A": cell_a, "B": cell_b, "C": cell_c}
        # give C an existing NVDA position (and mirror it on the master ledger)
        # so it can be the seller in the netting step below without violating
        # cell-level no-short (§2).
        master.record_fill("NVDA", 30, price=50.0, fees=0.0, at=AT)
        cell_c.record_broker_fill("NVDA", 30, price=50.0, fees=0.0, at=AT)

        intents = [_intent("i1", "A", "NVDA", Action.BUY, 50),
                   _intent("i2", "B", "NVDA", Action.BUY, 20),
                   _intent("i3", "C", "NVDA", Action.SELL, 30)]
        netting = NettingEngine(transfer_price_fn=lambda s: 52.0)
        result = netting.net(intents, cells)
        apply_crosses_to_cells(cells, result, at=AT)

        r = result.by_symbol["NVDA"]
        assert r.net_broker_side is Action.BUY
        assert r.net_broker_qty == pytest.approx(40)  # 70 gross buy - 30 crossed

        # who the broker order still owes comes from the netting result itself,
        # never re-derived by the caller
        allocations = FillAllocationEngine().allocate(
            list(r.residual_intents), fill_qty=r.net_broker_qty,
            fill_price=53.0, fill_fees=2.0)
        apply_allocations_to_cells(cells, allocations, at=AT)
        master.record_fill("NVDA", 40, price=53.0, fees=2.0, at=AT)

        # $100 of master cash was never allocated to any cell (6000 - 2700 - 1200 - 2000)
        engine = CellReconciliationEngine(master_ledger=master, cells=cells,
                                          unallocated_master_cash=100.0)
        report = engine.reconcile()
        assert report.consistent, report.mismatches
        assert master.position_qty("NVDA") == pytest.approx(
            sum(c.position_qty("NVDA") for c in cells.values()))
