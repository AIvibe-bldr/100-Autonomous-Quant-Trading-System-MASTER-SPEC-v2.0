"""Model disagreement (§30) shrinks size when the sources split — and can only shrink it."""
from datetime import date, datetime, timezone

import pytest

from packages.common.clock import FrozenClock
from services.decision.disagreement import (
    DisagreementReading,
    ModelDisagreementEngine,
    decision_lean,
    news_lean,
    quant_lean,
    regime_lean,
    skeptic_lean,
)
from services.market_data.universe import UniverseManager, UniverseSymbol
from tests.conftest import SYMBOLS, build_pipeline

T = datetime(2026, 8, 11, 15, 0, tzinfo=timezone.utc)


def test_each_source_maps_to_a_lean_in_minus_one_to_plus_one():
    assert quant_lean(0.0) == 0.0 and 0.7 < quant_lean(2.0) < 0.8 and quant_lean(50) <= 1.0 and quant_lean(-3) < 0
    assert decision_lean(0.5) == 0.0 and decision_lean(1.0) == 1.0 and decision_lean(0.0) == -1.0
    # the Skeptic is built to object: little objection counts as agreement
    assert skeptic_lean(0.0) == 1.0 and skeptic_lean(0.5) == 0.0 and skeptic_lean(1.0) == -1.0
    assert skeptic_lean(0.2) == pytest.approx(0.6)
    assert regime_lean("BULL") == 0.5 and regime_lean("BEAR") == -0.5 and regime_lean("UNKNOWN") is None
    assert news_lean([]) is None and news_lean([0.5, -0.1]) == pytest.approx(0.2)


def test_unanimous_views_keep_full_size_and_a_split_halves_the_multiplier():
    eng = ModelDisagreementEngine()
    assert eng.measure("A", {"a": 0.6, "b": 0.6, "c": 0.6}).risk_multiplier == 1.0
    split = eng.measure("A", {"a": 1.0, "b": -1.0})
    assert split.disagreement == 1.0 and split.risk_multiplier == 0.5


def _session(force=None):
    clock = FrozenClock(current=T)
    universe = UniverseManager()
    for s in SYMBOLS:
        universe.add(UniverseSymbol(symbol=s, listed_from=date(2015, 1, 1)))
    p = build_pipeline(clock, universe)
    if force is not None:
        p.disagreement.measure = lambda symbol, views: DisagreementReading(
            symbol=symbol, views=dict(views), mean_direction=0.0, disagreement=force)
    return p, p.run_session(T)


def test_the_multiplier_is_recorded_per_decision_and_stays_in_half_to_one():
    p, result = _session()
    assert result.disagreement_multipliers
    assert all(0.5 <= m <= 1.0 for m in result.disagreement_multipliers.values())
    rec = next(r for r in p.provenance.all() if r.disagreement)
    assert {"quant", "decision", "skeptic"} <= set(rec.disagreement["views"])
    assert rec.disagreement["size_multiplier"] == pytest.approx(1.0 - 0.5 * rec.disagreement["disagreement"], abs=1e-3)


def test_disagreement_can_only_make_positions_smaller():
    full_p, _ = _session(force=0.0)                  # as if every source agreed
    split_p, _ = _session(force=1.0)                 # as if they were maximally split
    qty = lambda p: {r.output["symbol"]: r.position_size["qty"] for r in p.provenance.all() if r.position_size}
    full, split = qty(full_p), qty(split_p)
    assert full and set(split) <= set(full) | set(split)
    for sym in set(full) & set(split):
        assert split[sym] <= full[sym] + 1e-9
    assert any(split[s] < full[s] for s in set(full) & set(split))               # and it does bite
    assert sum(split.values()) < sum(full.values())
