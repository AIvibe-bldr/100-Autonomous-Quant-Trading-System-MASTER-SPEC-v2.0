from __future__ import annotations

import pytest

from packages.common.clock import FrozenClock
from packages.schemas.core import OrderState as S
from services.execution.state_machine import IllegalTransitionError, OrderStateMachine
from tests.conftest import SESSION_TIME


@pytest.fixture
def sm() -> OrderStateMachine:
    return OrderStateMachine(clock=FrozenClock(current=SESSION_TIME))


def test_happy_path(sm):
    sm.create("o1")
    for state in [S.RISK_APPROVED, S.SUBMITTED, S.ACKNOWLEDGED, S.PARTIALLY_FILLED, S.FILLED]:
        sm.transition("o1", state)
    assert sm.get("o1").state is S.FILLED
    assert len(sm.get("o1").transitions) == 5


def test_cannot_skip_risk_approval(sm):
    sm.create("o1")
    with pytest.raises(IllegalTransitionError):
        sm.transition("o1", S.SUBMITTED)  # CREATED → SUBMITTED forbidden (§7)


def test_terminal_states_frozen(sm):
    sm.create("o1")
    sm.transition("o1", S.REJECTED)
    with pytest.raises(IllegalTransitionError):
        sm.transition("o1", S.SUBMITTED)


def test_unknown_resolves_via_reconciliation_only(sm):
    sm.create("o1")
    sm.transition("o1", S.RISK_APPROVED)
    sm.transition("o1", S.SUBMITTED)
    sm.transition("o1", S.UNKNOWN)
    assert sm.has_unknown
    sm.transition("o1", S.FILLED, reason="resolved via broker reconciliation")
    assert not sm.has_unknown


def test_event_log_is_append_only(sm):
    sm.create("o1")
    sm.transition("o1", S.RISK_APPROVED)
    sm.transition("o1", S.SUBMITTED)
    assert [t.to_state for t in sm.event_log] == [S.RISK_APPROVED, S.SUBMITTED]


def test_duplicate_create_rejected(sm):
    sm.create("o1")
    with pytest.raises(IllegalTransitionError):
        sm.create("o1")
