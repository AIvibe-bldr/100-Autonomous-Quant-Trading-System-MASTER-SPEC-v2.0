"""A later, milder automatic trigger must not relax a stricter MASTER STOP state
(found while adding the F6 slippage breaker). Only an explicit NORMAL resets."""
from datetime import datetime, timezone

import pytest

from packages.common.alerts import AlertSeverity
from packages.common.calendar import TradingCalendar
from packages.common.clock import FrozenClock
from packages.common.risk_config import RiskConfig
from services.risk.master_controller import MasterRiskController, RiskState

N, H, S, D = (RiskState.NORMAL, RiskState.HALT_NEW_ENTRIES, RiskState.SAFE_EXIT,
              RiskState.FULL_BROKER_DISCONNECT)


class _Sink:
    def __init__(self):
        self.sent = []

    def send(self, severity, message):
        self.sent.append((severity, message))


def _rc(sink=None):
    kw = {"alert_sink": sink} if sink else {}
    return MasterRiskController(
        config=RiskConfig(), calendar=TradingCalendar(),
        clock=FrozenClock(current=datetime(2026, 8, 10, 15, 0, tzinfo=timezone.utc)), **kw)


@pytest.mark.parametrize("current,requested,expected", [
    (D, H, D), (D, S, D), (S, H, S),                 # milder trigger: ignored
    (N, H, H), (H, S, S), (S, D, D), (H, D, D),      # escalation: applied
    (H, H, H),                                       # same state: no-op
    (D, N, N), (H, N, N), (S, N, N),                 # explicit reset still works
])
def test_set_state_only_escalates_unless_resetting_to_normal(current, requested, expected):
    rc = _rc()
    rc.state = current
    rc.set_state(requested, reason="test")
    assert rc.state is expected


def test_an_ignored_downgrade_does_not_page_as_a_new_halt():
    sink = _Sink()
    rc = _rc(sink)
    rc.set_state(D, reason="broker down")
    rc.set_state(H, reason="reconciliation mismatch")
    assert rc.state is D
    assert [s for s, _ in sink.sent] == [AlertSeverity.CRITICAL]      # only the disconnect
