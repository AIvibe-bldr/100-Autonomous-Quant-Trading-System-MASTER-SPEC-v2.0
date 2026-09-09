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
    NettingEngine,
    TransferPriceMethod,
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

class TestNettingEngine:
    def _engine(self, price: float = 100.0) -> NettingEngine:
        return NettingEngine(transfer_price_fn=lambda symbol: price)

    def test_full_offset_example_from_spec(self):
        # §6: Cell A BUY 100, Cell B SELL 80 -> Broker Net Order BUY 20
        intents = [_intent("i1", "A", "NVDA", Action.BUY, 100),
                   _intent("i2", "B", "NVDA", Action.SELL, 80)]
        result = self._engine().net(intents)
        r = result.by_symbol["NVDA"]
        assert r.net_broker_side is Action.BUY
        assert r.net_broker_qty == pytest.approx(20)
        assert r.internal_cross_qty == pytest.approx(80)
        assert result.gross_buy_flow == pytest.approx(100)
        assert result.gross_sell_flow == pytest.approx(80)
        assert result.net_broker_flow == pytest.approx(20)

    def test_partial_cross_example_from_spec(self):
        # §7: Cell A BUY 100, Cell B SELL 60 (cell B holds >=60) -> Broker BUY 40
        intents = [_intent("i1", "A", "NVDA", Action.BUY, 100),
                   _intent("i2", "B", "NVDA", Action.SELL, 60)]
        result = self._engine().net(intents)
        r = result.by_symbol["NVDA"]
        assert r.net_broker_side is Action.BUY
        assert r.net_broker_qty == pytest.approx(40)
        assert r.internal_cross_qty == pytest.approx(60)
        assert len(r.crosses) == 1
        assert r.crosses[0].buy_cell_id == "A"
        assert r.crosses[0].sell_cell_id == "B"

    def test_fully_crossed_produces_no_broker_order(self):
        intents = [_intent("i1", "A", "NVDA", Action.BUY, 50),
                   _intent("i2", "B", "NVDA", Action.SELL, 50)]
        result = self._engine().net(intents)
        r = result.by_symbol["NVDA"]
        assert r.net_broker_side is None
        assert r.net_broker_qty == 0.0
        assert result.broker_orders() == []

    def test_gross_intent_is_not_lost_even_when_fully_netted(self):
        intents = [_intent("i1", "A", "NVDA", Action.BUY, 50),
                   _intent("i2", "B", "NVDA", Action.SELL, 50)]
        result = self._engine().net(intents)
        r = result.by_symbol["NVDA"]
        assert r.gross_buy_qty == 50 and r.gross_sell_qty == 50
        assert len(r.contributing_intents) == 2

    def test_net_broker_order_is_deterministic_regardless_of_input_order(self):
        intents_a = [_intent("i1", "A", "NVDA", Action.BUY, 100),
                     _intent("i2", "B", "NVDA", Action.SELL, 60),
                     _intent("i3", "C", "NVDA", Action.SELL, 30)]
        intents_b = list(reversed(intents_a))
        result_a = self._engine().net(intents_a)
        result_b = self._engine().net(intents_b)
        ra, rb = result_a.by_symbol["NVDA"], result_b.by_symbol["NVDA"]
        assert ra.net_broker_side == rb.net_broker_side
        assert ra.net_broker_qty == rb.net_broker_qty
        assert ra.crosses == rb.crosses

    def test_no_intents_for_symbol_means_no_cross(self):
        intents = [_intent("i1", "A", "NVDA", Action.BUY, 10)]
        result = self._engine().net(intents)
        r = result.by_symbol["NVDA"]
        assert r.crosses == ()
        assert r.net_broker_qty == pytest.approx(10)

    def test_internal_cross_uses_versioned_transfer_price(self):
        intents = [_intent("i1", "A", "NVDA", Action.BUY, 10),
                   _intent("i2", "B", "NVDA", Action.SELL, 10)]
        result = self._engine(price=123.45).net(intents)
        cross = result.by_symbol["NVDA"].crosses[0]
        assert cross.transfer_price == pytest.approx(123.45)
        assert cross.transfer_price_method is TransferPriceMethod.ARRIVAL_MID_V1

    def test_apply_crosses_never_touches_master_ledger(self):
        """§39: internal_cross_does_not_create_fake_master_trade /
        ..._fake_tax_event. Structural check: the Master Ledger object is
        never even passed to apply_crosses_to_cells, so it cannot record a
        fake trade or fee no matter what the netting result contains."""
        master = Ledger(initial_cash=10_000.0)
        cell_a, cell_b = CellLedger("A"), CellLedger("B")
        cell_a.allocate_cash(10_000.0, at=AT)
        cell_b.allocate_cash(10_000.0, at=AT)
        cell_b.record_broker_fill("NVDA", 100, price=50.0, fees=0.0, at=AT)  # so B can sell

        intents = [_intent("i1", "A", "NVDA", Action.BUY, 60),
                   _intent("i2", "B", "NVDA", Action.SELL, 60)]
        result = self._engine(price=52.0).net(intents)
        assert result.by_symbol["NVDA"].net_broker_qty == 0.0  # fully internal

        master_entries_before = len(master.entries)
        apply_crosses_to_cells({"A": cell_a, "B": cell_b}, result, at=AT)

        assert len(master.entries) == master_entries_before  # untouched
        assert master.position_qty("NVDA") == 0.0             # no fake master position
        assert cell_a.position_qty("NVDA") == pytest.approx(60)
        assert cell_b.position_qty("NVDA") == pytest.approx(40)
        # virtual P&L landed on the cells, never on the real ledger
        assert cell_b.virtual_realized_pnl == pytest.approx((52.0 - 50.0) * 60)
        assert master.realized_pnl == 0.0
        assert master.fees_paid == 0.0  # no fake fee / tax event either


# ---------------------------------------------------------------------------
# 7. Fill Allocation Engine
# ---------------------------------------------------------------------------

class TestFillAllocationEngine:
    def test_pro_rata_allocation_sums_exactly_to_fill_qty(self):
        residual = [_intent("i1", "A", "NVDA", Action.BUY, 30),
                    _intent("i2", "B", "NVDA", Action.BUY, 10)]
        allocations = FillAllocationEngine().allocate(
            residual, fill_qty=40.0, fill_price=55.0, fill_fees=1.0)
        assert sum(a.qty for a in allocations) == pytest.approx(40.0)
        assert sum(a.fees for a in allocations) == pytest.approx(1.0)

    def test_partial_broker_fill_still_sums_exactly(self):
        residual = [_intent("i1", "A", "NVDA", Action.BUY, 30),
                    _intent("i2", "B", "NVDA", Action.BUY, 10),
                    _intent("i3", "C", "NVDA", Action.BUY, 7)]
        # only 11 of the 47 requested actually filled (partial broker fill)
        allocations = FillAllocationEngine().allocate(
            residual, fill_qty=11.0, fill_price=55.0, fill_fees=0.55)
        assert sum(a.qty for a in allocations) == pytest.approx(11.0)
        assert sum(a.fees for a in allocations) == pytest.approx(0.55)

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
        result = netting.net(intents)
        apply_crosses_to_cells(cells, result, at=AT)

        r = result.by_symbol["NVDA"]
        assert r.net_broker_side is Action.BUY
        assert r.net_broker_qty == pytest.approx(40)  # 70 gross buy - 30 crossed

        # residual buy demand after C's 30 was crossed away: A keeps 20, B keeps 20
        residual = [_intent("i1", "A", "NVDA", Action.BUY, 20),
                    _intent("i2", "B", "NVDA", Action.BUY, 20)]
        allocations = FillAllocationEngine().allocate(residual, fill_qty=40.0,
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
