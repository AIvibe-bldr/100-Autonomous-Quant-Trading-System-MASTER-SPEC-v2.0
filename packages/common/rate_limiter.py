"""Generic Token-Bucket Rate Limiter.

Cross-cutting infrastructure (no dependency on any particular external
service) for any adapter that must respect a remote API's request-rate
policy — e.g. `services.fundamentals.edgar_source` honoring SEC EDGAR's
fair-access policy (max ~10 req/sec, mandatory User-Agent). Injectable
clock/sleep so callers can test rate-limiting behavior without a real
wall-clock wait, the same determinism precedent `packages.common.clock.
FrozenClock` already established for backtest/replay (§62).
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable

from packages.common.clock import Clock


@dataclass
class TokenBucketRateLimiter:
    max_calls: int
    per_seconds: float
    clock: Clock = field(default_factory=Clock)
    sleep: Callable[[float], None] = time.sleep
    _tokens: float = field(init=False, repr=False, default=0.0)
    _last_refill: float = field(init=False, repr=False, default=0.0)

    def __post_init__(self) -> None:
        if self.max_calls <= 0 or self.per_seconds <= 0:
            raise ValueError("max_calls and per_seconds must both be positive")
        self._tokens = float(self.max_calls)
        self._last_refill = self.clock.now().timestamp()

    def _refill(self) -> None:
        now = self.clock.now().timestamp()
        elapsed = max(0.0, now - self._last_refill)
        rate = self.max_calls / self.per_seconds
        self._tokens = min(float(self.max_calls), self._tokens + elapsed * rate)
        self._last_refill = now

    def acquire(self) -> None:
        """Blocks (via `self.sleep`) until a token is available, then
        consumes one. Callers using the real `Clock`/`time.sleep` defaults
        get genuine rate limiting; tests inject both to stay deterministic."""
        self._refill()
        if self._tokens < 1.0:
            wait = (1.0 - self._tokens) * (self.per_seconds / self.max_calls)
            self.sleep(wait)
            self._refill()
        self._tokens -= 1.0
