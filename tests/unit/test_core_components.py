from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from packages.common.calendar import SessionStatus, TradingCalendar
from packages.common.clock import EventTimes
from packages.common.ledger import Ledger, minimum_operable_equity
from packages.common.pit_store import PointInTimeStore
from packages.common.settlement import SettlementManager
from packages.schemas.core import Bar, Quote
from services.data_validation.integrity import DataHealth, DataIntegrityEngine, Severity
from services.market_data.service import MarketDataService, MockProvider
from services.market_data.universe import UniverseManager, UniverseSymbol
from tests.conftest import SESSION_TIME


# --- Unified Clock (§9) ---------------------------------------------------

def test_naive_datetime_rejected():
    with pytest.raises(ValueError, match="naive"):
        EventTimes(event_time=datetime(2026, 1, 1),
                   source_time=SESSION_TIME, received_time=SESSION_TIME)


def test_future_leak_detection():
    t = EventTimes(event_time=SESSION_TIME + timedelta(hours=1),
                   source_time=SESSION_TIME, received_time=SESSION_TIME)
    assert t.is_future_leak


# --- Trading Calendar (§13) ----------------------------------------------

def test_calendar_open_regular_session():
    cal = TradingCalendar()
    assert cal.status(SESSION_TIME) is SessionStatus.OPEN  # Tue 11:00 ET


def test_calendar_closed_on_holiday_and_weekend():
    cal = TradingCalendar()
    july4 = datetime(2025, 7, 4, 15, 0, tzinfo=timezone.utc)
    assert cal.status(july4) is SessionStatus.CLOSED
    saturday = datetime(2026, 8, 15, 15, 0, tzinfo=timezone.utc)
    assert cal.status(saturday) is SessionStatus.CLOSED


def test_calendar_early_close():
    cal = TradingCalendar()
    # Dec 24 2025, 13:30 ET = 18:30 UTC → after 13:00 early close
    assert cal.status(datetime(2025, 12, 24, 18, 30, tzinfo=timezone.utc)) is SessionStatus.CLOSED
    assert cal.status(datetime(2025, 12, 24, 15, 30, tzinfo=timezone.utc)) is SessionStatus.OPEN


def test_calendar_circuit_breaker():
    cal = TradingCalendar()
    cal.circuit_breaker_active = True
    assert cal.status(SESSION_TIME) is SessionStatus.HALTED


def test_calendar_refuses_unknown_year():
    cal = TradingCalendar()
    with pytest.raises(ValueError, match="refusing to guess"):
        cal.is_trading_day(date(2030, 1, 2))


# --- Ledger & HWM (§3, §5) ------------------------------------------------

def test_ledger_equity_and_hwm():
    ledger = Ledger(initial_cash=1000.0)
    ledger.record_fill("AAPL", side_qty=10, price=50.0, fees=1.0, at=SESSION_TIME)
    assert ledger.cash == pytest.approx(1000 - 500 - 1)
    snap = ledger.snapshot({"AAPL": 60.0})
    assert snap.equity == pytest.approx(499 + 600)
    assert snap.high_water_mark == pytest.approx(1099)
    snap2 = ledger.snapshot({"AAPL": 40.0})
    assert snap2.drawdown == pytest.approx(1 - 899 / 1099)


def test_challenge_does_not_end_at_zero_cash():
    """§4: Cash=0 with stock held is NOT the end."""
    ledger = Ledger(initial_cash=500.0)
    ledger.record_fill("AAPL", side_qty=10, price=50.0, fees=0.0, at=SESSION_TIME)
    assert ledger.cash == pytest.approx(0.0)
    assert ledger.equity({"AAPL": 50.0}) == pytest.approx(500.0)
    floor = minimum_operable_equity(min_order_value=1.0, typical_fee=0.05,
                                    fractional_shares=True, min_share_price=3.0)
    assert ledger.equity({"AAPL": 50.0}) > floor  # challenge continues


def test_realized_pnl():
    ledger = Ledger(initial_cash=1000.0)
    ledger.record_fill("AAPL", side_qty=10, price=50.0, fees=0.0, at=SESSION_TIME)
    ledger.record_fill("AAPL", side_qty=-10, price=55.0, fees=0.0, at=SESSION_TIME)
    assert ledger.realized_pnl == pytest.approx(50.0)
    assert ledger.position_qty("AAPL") == 0.0


# --- Settlement (§14) -----------------------------------------------------

def test_settlement_t_plus_1():
    sm = SettlementManager(settled_cash=1000.0)
    sm.on_sell(proceeds=500.0, at=SESSION_TIME)
    assert sm.buying_power == 1000.0  # proceeds not spendable yet
    sm.process_settlements(SESSION_TIME + timedelta(days=1, minutes=1))
    assert sm.buying_power == 1500.0


def test_unsettled_funds_cannot_be_spent():
    sm = SettlementManager(settled_cash=100.0)
    sm.on_sell(proceeds=500.0, at=SESSION_TIME)
    with pytest.raises(ValueError, match="settled"):
        sm.on_buy(cost=300.0, at=SESSION_TIME)


# --- Point-in-Time Store (§10) -------------------------------------------

def test_pit_no_future_leakage():
    store = PointInTimeStore()
    t0 = SESSION_TIME
    store.put("AAPL:eps", 1.00, event_time=t0, received_time=t0)
    # revision arrives two days later
    store.put("AAPL:eps", 1.25, event_time=t0, received_time=t0 + timedelta(days=2))
    as_of_next_day = store.get_as_of("AAPL:eps", t0 + timedelta(days=1))
    assert as_of_next_day is not None and as_of_next_day.value == 1.00  # revision invisible
    later = store.get_as_of("AAPL:eps", t0 + timedelta(days=3))
    assert later is not None and later.value == 1.25


# --- Universe (§12) -------------------------------------------------------

def test_universe_survivorship_bias():
    um = UniverseManager()
    um.add(UniverseSymbol("LIVECO", listed_from=date(2015, 1, 1)))
    um.add(UniverseSymbol("DEADCO", listed_from=date(2015, 1, 1),
                          delisted_at=date(2024, 1, 1)))
    assert "DEADCO" in um.symbols_as_of(date(2023, 6, 1))   # visible historically
    assert "DEADCO" not in um.symbols_as_of(date(2026, 6, 1))  # gone today


# --- Data Integrity (§11) -------------------------------------------------

def test_bid_gt_ask_is_critical():
    eng = DataIntegrityEngine()
    q = Quote(symbol="AAPL", ts=SESSION_TIME, bid=101.0, ask=100.0, bid_size=1, ask_size=1)
    issues = eng.validate_quote(q, SESSION_TIME)
    assert any(i.check == "bid_gt_ask" and i.severity is Severity.CRITICAL for i in issues)
    assert eng.health is DataHealth.HALT_ENTRIES


def test_stale_feed_detected():
    eng = DataIntegrityEngine()
    q = Quote(symbol="AAPL", ts=SESSION_TIME - timedelta(hours=2), bid=99.0, ask=100.0,
              bid_size=1, ask_size=1)
    eng.validate_quote(q, SESSION_TIME)
    assert eng.health is DataHealth.HALT_ENTRIES


def test_extreme_price_detected():
    eng = DataIntegrityEngine()
    bars = [Bar(symbol="X", ts=SESSION_TIME - timedelta(days=1), open=10, high=11, low=9,
                close=10, volume=1000),
            Bar(symbol="X", ts=SESSION_TIME, open=10, high=30, low=9, close=30, volume=1000)]
    eng.validate_bars("X", bars)
    assert eng.health is DataHealth.HALT_ENTRIES


def test_mock_provider_is_deterministic():
    md = MarketDataService(MockProvider())
    a = md.bars("AAPL", SESSION_TIME, 10, received_at=SESSION_TIME)
    b = md.bars("AAPL", SESSION_TIME, 10, received_at=SESSION_TIME)
    assert [x.bar.close for x in a] == [x.bar.close for x in b]
