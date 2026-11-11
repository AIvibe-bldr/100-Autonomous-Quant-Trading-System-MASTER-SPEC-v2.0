"""Rejection circuit breaker (SAFETY_AUDIT F6).

Single-event triggers (broker timeout, disconnect) already halt on the first
occurrence. This adds the missing *rate* trigger: N broker rejections inside a
rolling window means something systematic is wrong (credentials, account
status, a malformed-order bug), and new entries should stop until a human
looks. Pure logic over an injected Clock; it only answers "tripped?" — the
caller applies the state change through MasterRiskController.set_state."""
from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta

from packages.common.clock import Clock
from packages.schemas.core import Action


def adverse_slippage_bps(side: Action, reference_price: float, fill_price: float) -> float:
    """How much WORSE than the reference the fill was, in bps (negative = better).
    BUY pays more than the reference, SELL receives less. Pure."""
    signed = (fill_price - reference_price) if side is Action.BUY else (reference_price - fill_price)
    return signed / reference_price * 10_000


class RateCircuitBreaker:
    """Counts events inside a rolling window; tripped once `max_events` is reached."""

    def __init__(self, max_rejections: int, window_sec: float, clock: Clock) -> None:
        if max_rejections < 1 or not window_sec > 0:
            raise ValueError("circuit breaker needs max_rejections >= 1 and window_sec > 0")
        self._max = max_rejections
        self._window = timedelta(seconds=window_sec)
        self._clock = clock
        self._events: deque[datetime] = deque()

    def record(self) -> bool:
        """Record one event; True when the breaker has tripped."""
        now = self._clock.now()
        self._events.append(now)
        while self._events and now - self._events[0] > self._window:
            self._events.popleft()
        return len(self._events) >= self._max


class RejectionCircuitBreaker(RateCircuitBreaker):
    """Broker rejections (F6)."""

    def record_rejection(self) -> bool:
        return self.record()


class SlippageCircuitBreaker(RateCircuitBreaker):
    """Fills that were abnormally worse than the reference price (F6)."""

    def record_abnormal_fill(self) -> bool:
        return self.record()
