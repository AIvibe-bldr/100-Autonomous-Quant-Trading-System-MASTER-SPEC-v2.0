"""SAFETY_AUDIT F6 (rejection circuit breaker) and F10 (alert sink)."""
from datetime import datetime, timedelta, timezone

import pytest

from packages.common.alerts import AlertSeverity
from packages.common.calendar import TradingCalendar
from packages.common.clock import FrozenClock
from packages.common.risk_config import RiskConfig
from services.risk.circuit_breaker import RejectionCircuitBreaker
from services.risk.master_controller import MasterRiskController, RiskState


class _Sink:
    def __init__(self):
        self.sent = []

    def send(self, severity, message):
        self.sent.append((severity, message))


def _clock():
    return FrozenClock(current=datetime(2026, 8, 10, 15, 0, tzinfo=timezone.utc))


def test_breaker_trips_on_nth_rejection_inside_window_only():
    clock = _clock()
    cb = RejectionCircuitBreaker(max_rejections=3, window_sec=600, clock=clock)
    assert not cb.record_rejection() and not cb.record_rejection()
    clock.advance(601)                      # the first two age out of the window
    assert not cb.record_rejection() and not cb.record_rejection()
    assert cb.record_rejection()            # 3 inside the window → trip


def test_breaker_rejects_nonsense_parameters():
    with pytest.raises(ValueError):
        RejectionCircuitBreaker(max_rejections=0, window_sec=60, clock=_clock())
    with pytest.raises(ValueError):
        RejectionCircuitBreaker(max_rejections=3, window_sec=0, clock=_clock())


def test_risk_config_carries_breaker_thresholds_and_validates_them():
    cfg = RiskConfig()
    assert cfg.circuit_breaker_max_rejections >= 1 and cfg.circuit_breaker_window_sec > 0
    with pytest.raises(ValueError):
        RiskConfig(circuit_breaker_max_rejections=0)


@pytest.mark.parametrize("state", [RiskState.HALT_NEW_ENTRIES, RiskState.SAFE_EXIT,
                                   RiskState.FULL_BROKER_DISCONNECT])
def test_entering_a_stop_state_alerts_once_with_the_reason(state):
    sink = _Sink()
    rc = MasterRiskController(config=RiskConfig(), calendar=TradingCalendar(), clock=_clock(),
                              alert_sink=sink)
    rc.set_state(state, reason="because")
    rc.set_state(state, reason="because again")        # no state change → no repeat
    assert len(sink.sent) == 1
    sev, msg = sink.sent[0]
    assert sev is AlertSeverity.CRITICAL and state.value in msg and "because" in msg


def test_returning_to_normal_does_not_page():
    sink = _Sink()
    rc = MasterRiskController(config=RiskConfig(), calendar=TradingCalendar(), clock=_clock(),
                              alert_sink=sink)
    rc.set_state(RiskState.NORMAL)
    assert sink.sent == []


def test_default_has_a_no_op_sink():
    rc = MasterRiskController(config=RiskConfig(), calendar=TradingCalendar(), clock=_clock())
    rc.set_state(RiskState.HALT_NEW_ENTRIES, reason="x")      # must not raise


def test_repeated_broker_rejections_halt_entries_and_alert_end_to_end():
    from datetime import date, time

    from packages.schemas.core import BrokerAck
    from services.market_data.universe import UniverseManager, UniverseSymbol
    from tests.conftest import SYMBOLS, build_pipeline

    clock = FrozenClock(current=datetime.combine(date(2026, 8, 10), time(15, 0), tzinfo=timezone.utc))
    universe = UniverseManager()
    for s in SYMBOLS:
        universe.add(UniverseSymbol(symbol=s, listed_from=date(2015, 1, 1)))
    p = build_pipeline(clock, universe)
    sink = _Sink()
    p.risk_controller.alert_sink = sink
    p.execution.broker.submit_order = lambda req: BrokerAck(
        client_order_id=req.client_order_id, broker_order_id="", accepted=False,
        reason="account blocked")
    p.run_session(clock.now())
    assert p.risk_controller.state is RiskState.HALT_NEW_ENTRIES
    assert len(sink.sent) == 1 and "circuit breaker" in sink.sent[0][1]
