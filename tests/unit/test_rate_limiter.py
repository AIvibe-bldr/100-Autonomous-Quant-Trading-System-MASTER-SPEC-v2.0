"""Tests for packages/common/rate_limiter.py."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from packages.common.clock import FrozenClock
from packages.common.rate_limiter import TokenBucketRateLimiter

AT = datetime(2026, 1, 1, tzinfo=timezone.utc)


def test_max_calls_and_per_seconds_must_be_positive():
    with pytest.raises(ValueError):
        TokenBucketRateLimiter(max_calls=0, per_seconds=1.0)
    with pytest.raises(ValueError):
        TokenBucketRateLimiter(max_calls=1, per_seconds=0.0)


def test_calls_within_the_bucket_never_sleep():
    clock = FrozenClock(current=AT)
    calls: list[float] = []
    limiter = TokenBucketRateLimiter(max_calls=3, per_seconds=1.0, clock=clock,
                                     sleep=calls.append)
    limiter.acquire()
    limiter.acquire()
    limiter.acquire()
    assert calls == []


def test_exceeding_the_bucket_sleeps_for_a_positive_duration():
    clock = FrozenClock(current=AT)
    calls: list[float] = []

    def fake_sleep(seconds: float) -> None:
        calls.append(seconds)
        clock.advance(seconds)

    limiter = TokenBucketRateLimiter(max_calls=2, per_seconds=1.0, clock=clock, sleep=fake_sleep)
    limiter.acquire()
    limiter.acquire()
    limiter.acquire()  # bucket empty -> must wait for a refill
    assert len(calls) == 1
    assert calls[0] > 0


def test_tokens_refill_over_elapsed_time():
    clock = FrozenClock(current=AT)
    calls: list[float] = []
    limiter = TokenBucketRateLimiter(max_calls=2, per_seconds=1.0, clock=clock,
                                     sleep=calls.append)
    limiter.acquire()
    limiter.acquire()
    clock.advance(1.0)  # a full bucket's worth of time passes
    limiter.acquire()   # should not need to sleep — tokens refilled
    assert calls == []
