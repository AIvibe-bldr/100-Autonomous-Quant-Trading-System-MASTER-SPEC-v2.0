"""SAFETY_AUDIT F6 (abnormal slippage) and F10 (heartbeat ERROR alert)."""
from datetime import datetime, timedelta, timezone

import pytest

from packages.common.alerts import AlertSeverity
from packages.common.clock import FrozenClock
from packages.common.risk_config import RiskConfig
from packages.schemas.core import Action, BrokerFill
from services.risk.circuit_breaker import SlippageCircuitBreaker, adverse_slippage_bps
from services.risk.master_controller import RiskState
from services.supervisor.heartbeat import Heartbeat, HeartbeatRegistry, ServiceStatus
from services.supervisor.heartbeat_alerts import HeartbeatAlerter
from services.market_data.universe import UniverseManager, UniverseSymbol
from tests.conftest import SYMBOLS, build_pipeline
from datetime import date

T0 = datetime(2026, 11, 9, 15, 0, tzinfo=timezone.utc)


class Sink:
    def __init__(self):
        self.sent = []

    def send(self, severity, message):
        self.sent.append((severity, message))


def test_adverse_slippage_is_side_aware():
    assert adverse_slippage_bps(Action.BUY, 100.0, 101.0) == pytest.approx(100.0)
    assert adverse_slippage_bps(Action.BUY, 100.0, 99.0) < 0          # better than reference
    assert adverse_slippage_bps(Action.SELL, 100.0, 99.0) == pytest.approx(100.0)


def test_slippage_breaker_counts_inside_the_window_only():
    clock = FrozenClock(current=T0)
    b = SlippageCircuitBreaker(2, 60.0, clock)
    assert b.record_abnormal_fill() is False
    clock.current = T0 + timedelta(seconds=120)                         # first one aged out
    assert b.record_abnormal_fill() is False
    assert b.record_abnormal_fill() is True


@pytest.mark.parametrize("bad", [dict(max_slippage_bps=0.0), dict(circuit_breaker_max_slippage_fills=0)])
def test_config_rejects_a_disabled_slippage_breaker(bad):
    with pytest.raises(ValueError):
        RiskConfig(**bad)


def _build(**kw):
    clock = FrozenClock(current=datetime(2026, 8, 10, 15, 0, tzinfo=timezone.utc))
    universe = UniverseManager()
    for s in SYMBOLS:
        universe.add(UniverseSymbol(symbol=s, listed_from=date(2015, 1, 1)))
    return build_pipeline(clock, universe, **kw), clock


def _engine(sink):
    p, _ = _build(config=RiskConfig(max_slippage_bps=50.0, circuit_breaker_max_slippage_fills=2))
    p.risk_controller.alert_sink = sink
    return p.execution


def _fill(i, side=Action.BUY, price=102.0):
    return BrokerFill(broker_fill_id=f"f{i}", client_order_id=f"c{i}", symbol="AAA", side=side,
                      qty=1, price=price, fees=0, ts=T0)


def test_repeated_abnormal_slippage_halts_new_entries_and_alerts():
    sink = Sink()
    ex = _engine(sink)
    for i in (1, 2):
        ex._reference_prices[f"c{i}"] = 100.0
        ex._check_slippage(_fill(i))
    assert ex.risk_controller.state is RiskState.HALT_NEW_ENTRIES
    kinds = [s for s, _ in sink.sent]
    assert kinds.count(AlertSeverity.WARNING) == 2 and AlertSeverity.CRITICAL in kinds


def test_one_abnormal_fill_warns_but_does_not_halt_and_normal_or_unreferenced_fills_are_ignored():
    sink = Sink()
    ex = _engine(sink)
    ex._reference_prices["c1"] = 100.0
    ex._check_slippage(_fill(1))                                        # +200 bps: abnormal
    ex._reference_prices["c2"] = 100.0
    ex._check_slippage(_fill(2, price=100.2))                           # +20 bps: normal
    ex._check_slippage(_fill(3))                                        # no reference: ignored
    assert ex.risk_controller.state is RiskState.NORMAL
    assert len(sink.sent) == 1


def test_a_fill_is_counted_once_even_if_polled_again():
    ex = _engine(Sink())
    ex._reference_prices["c1"] = 100.0
    for _ in range(3):
        ex._check_slippage(_fill(1))
    assert ex.risk_controller.state is RiskState.NORMAL                 # 1 event, threshold 2


def test_slippage_never_downgrades_a_worse_state():
    ex = _engine(Sink())
    ex.risk_controller.set_state(RiskState.SAFE_EXIT)
    for i in (1, 2):
        ex._reference_prices[f"c{i}"] = 100.0
        ex._check_slippage(_fill(i))
    assert ex.risk_controller.state is RiskState.SAFE_EXIT


def test_pipeline_passes_the_planned_entry_price_as_the_reference():
    p, clock = _build()
    seen = []
    real = p.execution.submit
    p.execution.submit = lambda order, snapshot=None, **kw: (seen.append(kw), real(order, snapshot=snapshot, **kw))[1]
    p.run_session(clock.now())
    buys = [kw for kw in seen if "reference_price" in kw]
    assert buys and all(kw["reference_price"] > 0 for kw in buys)


def _hb(clock, name, status=ServiceStatus.OK, at=None):
    return Heartbeat(service=name, at=at or clock.now(), status=status, last_success=clock.now())


def test_heartbeat_alert_fires_once_on_entering_error_and_notes_recovery():
    clock = FrozenClock(current=T0)
    reg, sink = HeartbeatRegistry(clock), Sink()
    alerter = HeartbeatAlerter(reg, sink)
    reg.report(_hb(clock, "market-data"))
    assert alerter.check() == [] and sink.sent == []
    reg.report(_hb(clock, "market-data", ServiceStatus.ERROR))
    assert alerter.check() == ["market-data"]
    assert alerter.check() == []                                        # still down: silent
    assert [s for s, _ in sink.sent] == [AlertSeverity.CRITICAL]
    reg.report(_hb(clock, "market-data"))
    alerter.check()
    assert [s for s, _ in sink.sent] == [AlertSeverity.CRITICAL, AlertSeverity.WARNING]


def test_a_silent_service_is_an_error_and_an_empty_registry_is_not_alerted():
    clock = FrozenClock(current=T0)
    reg, sink = HeartbeatRegistry(clock), Sink()
    alerter = HeartbeatAlerter(reg, sink)
    assert alerter.check() == []                                        # nothing reporting yet
    reg.report(_hb(clock, "execution"))
    clock.current = T0 + timedelta(minutes=10)                          # stale
    assert alerter.check() == ["execution"]


def test_pipeline_checks_heartbeats_at_session_start():
    p, clock = _build()
    reg, sink = HeartbeatRegistry(clock), Sink()
    reg.report(_hb(clock, "market-data", ServiceStatus.ERROR))
    p.heartbeat_alerter = HeartbeatAlerter(reg, sink)
    p.run_session(clock.now())
    assert [s for s, _ in sink.sent] == [AlertSeverity.CRITICAL]
