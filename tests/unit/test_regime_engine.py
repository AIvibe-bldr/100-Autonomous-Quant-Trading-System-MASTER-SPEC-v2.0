"""Tests for services/regime/engine.py (MASTER SPEC §26).

No coverage existed for this module before it was wired into
services/pipeline.py (docs/MASTER_SPEC.md Requirement Map item §26) — it
became load-bearing there (feeds DecisionContext.regime, which the real
Claude/OpenAI prompts already read, and DecisionSnapshot.regime, the A2-4
PDCA panel), so it needs its own tests now.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from packages.schemas.core import Bar
from services.regime.engine import Regime, RegimeEngine

START = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _bars_from_returns(start_price: float, returns: list[float]) -> list[Bar]:
    """21 bars: a start bar plus one per return in `returns` (len 20 for a
    classify() call, matching its own `closes[-1] / closes[-21] - 1`)."""
    bars = []
    price = start_price
    for i, r in enumerate([0.0] + returns):
        price = price * (1 + r) if i else price
        bars.append(Bar(symbol="SPY", ts=START + timedelta(days=i),
                        open=price, high=price * 1.001, low=price * 0.999,
                        close=price, volume=1_000_000))
    return bars


def test_classify_requires_at_least_21_bars():
    with pytest.raises(ValueError, match="21"):
        RegimeEngine().classify(_bars_from_returns(100.0, [0.001] * 10))


def test_bull_regime_from_steady_uptrend():
    bars = _bars_from_returns(100.0, [0.003] * 20)   # ~6.2% over 20 days
    reading = RegimeEngine().classify(bars)
    assert reading.primary is Regime.BULL
    assert reading.trend_20d == pytest.approx(1.003 ** 20 - 1)


def test_bear_regime_from_steady_downtrend():
    bars = _bars_from_returns(100.0, [-0.003] * 20)   # ~-5.8% over 20 days
    reading = RegimeEngine().classify(bars)
    assert reading.primary is Regime.BEAR


def test_range_regime_from_flat_drift():
    bars = _bars_from_returns(100.0, [0.0005] * 20)   # ~1% over 20 days
    reading = RegimeEngine().classify(bars)
    assert reading.primary is Regime.RANGE


def test_euphoria_regime_from_extreme_uptrend():
    bars = _bars_from_returns(100.0, [0.01] * 20)   # ~22% over 20 days
    reading = RegimeEngine().classify(bars)
    assert reading.primary is Regime.EUPHORIA


def test_panic_regime_requires_both_high_vol_and_negative_trend():
    """A merely-volatile-but-flat/up market is not panic (§26): the same
    volatility with a positive trend must NOT classify as PANIC."""
    volatile_down = [0.08, -0.10] * 10   # large swings, net negative, high vol
    volatile_up = [0.10, -0.08] * 10     # same magnitude swings, net positive

    down_reading = RegimeEngine().classify(_bars_from_returns(100.0, volatile_down))
    up_reading = RegimeEngine().classify(_bars_from_returns(100.0, volatile_up))

    assert down_reading.trend_20d < 0
    assert down_reading.realized_vol >= RegimeEngine().panic_vol
    assert down_reading.primary is Regime.PANIC
    assert up_reading.primary is not Regime.PANIC


def test_volatility_regime_is_independent_of_trend_direction():
    high_vol_bars = _bars_from_returns(100.0, [0.03, -0.03] * 10)
    reading = RegimeEngine().classify(high_vol_bars)
    assert reading.volatility_regime is Regime.HIGH_VOLATILITY


def test_low_volatility_regime_from_tiny_steady_moves():
    low_vol_bars = _bars_from_returns(100.0, [0.0001] * 20)
    reading = RegimeEngine().classify(low_vol_bars)
    assert reading.volatility_regime is Regime.LOW_VOLATILITY


def test_record_and_query_alpha_regime_performance():
    engine = RegimeEngine()
    engine.record_alpha_result("momentum", Regime.BULL, 0.05)
    engine.record_alpha_result("momentum", Regime.BULL, 0.03)
    engine.record_alpha_result("momentum", Regime.BEAR, -0.02)
    perf = engine.alpha_regime_performance("momentum")
    assert perf[Regime.BULL] == pytest.approx(0.04)
    assert perf[Regime.BEAR] == pytest.approx(-0.02)
    assert engine.alpha_regime_performance("unknown-alpha") == {}
