"""Same-Symbol Multi-Cell Stop Management tests (docs/capital_cell_architecture.md
§14-16, §41 priority 8)."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from packages.schemas.capital_cell import CellStopPlan, StopScope
from services.capital_cells.ledger import CellLedger
from services.capital_cells.stops import (
    CellStopRegistry,
    DuplicateSellReservationError,
    DuplicateStopIdError,
    OversellError,
    SellReservationLedger,
    UnknownStopError,
)

AT = datetime(2026, 8, 10, 15, 0, tzinfo=timezone.utc)


def _cell_with(symbol_qty: dict[str, float], cell_id: str, cash: float = 1_000_000.0) -> CellLedger:
    cell = CellLedger(cell_id)
    cell.allocate_cash(cash, at=AT)
    for symbol, qty in symbol_qty.items():
        cell.record_broker_fill(symbol, qty, price=100.0, fees=0.0, at=AT)
    return cell


def _registry(master_positions: dict[str, float]) -> CellStopRegistry:
    ledger = SellReservationLedger()
    for symbol, qty in master_positions.items():
        ledger.sync_master_long_qty(symbol, qty)
    return CellStopRegistry(sell_reservations=ledger)


class TestStopScope:
    def test_cell_scope_exits_only_the_owning_cell(self):
        cells = {
            "A": _cell_with({"NVDA": 40}, "A"),
            "B": _cell_with({"NVDA": 60}, "B"),
        }
        registry = _registry({"NVDA": 100})
        registry.register(CellStopPlan(stop_id="s1", cell_id="A", symbol="NVDA",
                                       stop_price=170.0, scope=StopScope.CELL,
                                       reason="momentum broke down"))
        intents = registry.trigger("s1", cells, at=AT)
        assert [(i.cell_id, i.symbol, i.qty) for i in intents] == [("A", "NVDA", 40.0)]
        # §14: B's virtual position must be completely untouched
        assert cells["B"].position_qty("NVDA") == 60.0

    def test_symbol_scope_exits_every_cell_holding_it_regardless_of_thesis(self):
        cells = {
            "A": _cell_with({"NVDA": 40}, "A"),   # momentum thesis
            "B": _cell_with({"NVDA": 60}, "B"),   # unrelated news thesis
            "C": _cell_with({"MSFT": 10}, "C"),   # doesn't hold NVDA at all
        }
        registry = _registry({"NVDA": 100, "MSFT": 10})
        registry.register(CellStopPlan(stop_id="s1", cell_id="A", symbol="NVDA",
                                       stop_price=170.0, scope=StopScope.SYMBOL,
                                       reason="fraud disclosed"))
        intents = registry.trigger("s1", cells, at=AT)
        by_cell = {i.cell_id: i.qty for i in intents}
        assert by_cell == {"A": 40.0, "B": 60.0}
        assert "C" not in by_cell   # never held NVDA, nothing to exit

    def test_portfolio_scope_exits_every_cell_every_symbol(self):
        cells = {
            "A": _cell_with({"NVDA": 40, "MSFT": 5}, "A"),
            "B": _cell_with({"NVDA": 60}, "B"),
        }
        registry = _registry({"NVDA": 100, "MSFT": 5})
        registry.register(CellStopPlan(stop_id="s1", cell_id="A", symbol="NVDA",
                                       stop_price=170.0, scope=StopScope.PORTFOLIO,
                                       reason="market-wide emergency"))
        intents = registry.trigger("s1", cells, at=AT)
        by_cell = {(i.cell_id, i.symbol): i.qty for i in intents}
        assert by_cell == {("A", "NVDA"): 40.0, ("A", "MSFT"): 5.0, ("B", "NVDA"): 60.0}

    def test_cell_scope_never_produces_an_intent_for_a_different_cell(self):
        cells = {"A": _cell_with({"NVDA": 40}, "A"), "B": _cell_with({"NVDA": 60}, "B")}
        registry = _registry({"NVDA": 100})
        registry.register(CellStopPlan(stop_id="s1", cell_id="A", symbol="NVDA",
                                       stop_price=170.0, scope=StopScope.CELL, reason="x"))
        intents = registry.trigger("s1", cells, at=AT)
        assert all(i.cell_id == "A" for i in intents)


class TestOversellPrevention:
    def test_overlapping_triggers_do_not_double_reserve_the_same_cell(self):
        """§16: a SYMBOL-scope trigger arriving before an earlier CELL-scope
        exit for the same cell has settled must not claim the same shares
        twice."""
        cells = {"A": _cell_with({"NVDA": 40}, "A"), "B": _cell_with({"NVDA": 60}, "B")}
        registry = _registry({"NVDA": 100})
        registry.register(CellStopPlan(stop_id="cell-stop", cell_id="A", symbol="NVDA",
                                       stop_price=170.0, scope=StopScope.CELL, reason="x"))
        registry.register(CellStopPlan(stop_id="symbol-stop", cell_id="B", symbol="NVDA",
                                       stop_price=1.0, scope=StopScope.SYMBOL, reason="fraud"))

        first = registry.trigger("cell-stop", cells, at=AT)   # A's stop fires first
        assert [(i.cell_id, i.qty) for i in first] == [("A", 40.0)]

        # A's exit has NOT been applied to the ledger yet (still in flight
        # through netting/execution) -> cells["A"].position_qty is still 40.
        second = registry.trigger("symbol-stop", cells, at=AT)
        by_cell = {i.cell_id: i.qty for i in second}
        assert by_cell.get("A", 0.0) == 0.0   # already fully reserved, not re-claimed
        assert by_cell["B"] == 60.0

        total_reserved = registry.sell_reservations.reserved_qty("NVDA")
        assert total_reserved == pytest.approx(100.0)   # never exceeds master position

    def test_oversell_raises_when_demand_exceeds_master_position(self):
        """Simulates a corrupted/unreconciled state: cell ledgers together
        hold more than the synced master position. This must be refused,
        not silently allowed to oversell."""
        cells = {"A": _cell_with({"NVDA": 40}, "A"), "B": _cell_with({"NVDA": 60}, "B")}
        registry = _registry({"NVDA": 90})  # master only shows 90, cells sum to 100
        registry.register(CellStopPlan(stop_id="s1", cell_id="A", symbol="NVDA",
                                       stop_price=170.0, scope=StopScope.SYMBOL, reason="x"))
        with pytest.raises(OversellError):
            registry.trigger("s1", cells, at=AT)

    def test_unsynced_master_position_fails_closed(self):
        """No sync_master_long_qty call for this symbol -> available capacity
        defaults to 0 -> every reservation is refused. Mirrors
        CapitalReservationLedger requiring `total_cash` up front: forgetting
        to sync must block trades, never silently allow them."""
        ledger = SellReservationLedger()   # nothing synced
        with pytest.raises(OversellError):
            ledger.reserve_sell("r1", "A", "NVDA", 10.0)

    def test_release_frees_capacity_for_a_later_trigger(self):
        cells = {"A": _cell_with({"NVDA": 40}, "A")}
        registry = _registry({"NVDA": 40})
        registry.register(CellStopPlan(stop_id="s1", cell_id="A", symbol="NVDA",
                                       stop_price=170.0, scope=StopScope.CELL, reason="x"))
        intents = registry.trigger("s1", cells, at=AT)
        reservation_id = intents[0].intent_id
        registry.sell_reservations.release(reservation_id)
        assert registry.sell_reservations.reserved_qty("NVDA") == 0.0

    def test_duplicate_reservation_id_rejected(self):
        ledger = SellReservationLedger()
        ledger.sync_master_long_qty("NVDA", 100.0)
        ledger.reserve_sell("r1", "A", "NVDA", 10.0)
        with pytest.raises(DuplicateSellReservationError):
            ledger.reserve_sell("r1", "B", "NVDA", 5.0)


class TestCellStopRegistryLifecycle:
    def test_duplicate_stop_id_rejected(self):
        registry = _registry({"NVDA": 100})
        plan = CellStopPlan(stop_id="s1", cell_id="A", symbol="NVDA", stop_price=170.0,
                            scope=StopScope.CELL, reason="x")
        registry.register(plan)
        with pytest.raises(DuplicateStopIdError):
            registry.register(plan)

    def test_unknown_stop_id_raises_on_trigger(self):
        registry = _registry({"NVDA": 100})
        with pytest.raises(UnknownStopError):
            registry.trigger("nope", {}, at=AT)

    def test_cancel_removes_a_stop(self):
        registry = _registry({"NVDA": 100})
        registry.register(CellStopPlan(stop_id="s1", cell_id="A", symbol="NVDA",
                                       stop_price=170.0, scope=StopScope.CELL, reason="x"))
        registry.cancel("s1")
        with pytest.raises(UnknownStopError):
            registry.get("s1")

    def test_stops_for_cell_filters_correctly(self):
        registry = _registry({"NVDA": 100, "MSFT": 10})
        registry.register(CellStopPlan(stop_id="s1", cell_id="A", symbol="NVDA",
                                       stop_price=170.0, scope=StopScope.CELL, reason="x"))
        registry.register(CellStopPlan(stop_id="s2", cell_id="B", symbol="MSFT",
                                       stop_price=300.0, scope=StopScope.CELL, reason="y"))
        assert [s.stop_id for s in registry.stops_for_cell("A")] == ["s1"]

    def test_trigger_skips_cells_with_nothing_left_to_exit(self):
        cells = {"A": _cell_with({}, "A")}   # A holds nothing
        registry = _registry({"NVDA": 0})
        registry.register(CellStopPlan(stop_id="s1", cell_id="A", symbol="NVDA",
                                       stop_price=170.0, scope=StopScope.CELL, reason="x"))
        assert registry.trigger("s1", cells, at=AT) == []
