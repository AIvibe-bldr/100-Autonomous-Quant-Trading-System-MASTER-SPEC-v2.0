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


class RejectionCircuitBreaker:
    def __init__(self, max_rejections: int, window_sec: float, clock: Clock) -> None:
        if max_rejections < 1 or not window_sec > 0:
            raise ValueError("circuit breaker needs max_rejections >= 1 and window_sec > 0")
        self._max = max_rejections
        self._window = timedelta(seconds=window_sec)
        self._clock = clock
        self._events: deque[datetime] = deque()

    def record_rejection(self) -> bool:
        """Record one broker rejection; True when the breaker has tripped."""
        now = self._clock.now()
        self._events.append(now)
        while self._events and now - self._events[0] > self._window:
            self._events.popleft()
        return len(self._events) >= self._max
