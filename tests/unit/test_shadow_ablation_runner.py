"""Tests for services/pdca/shadow_runner.py (MASTER SPEC §56-57)."""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from packages.common.clock import FrozenClock
from services.market_data.universe import UniverseManager, UniverseSymbol
from services.pdca.shadow import ShadowVariant
from services.pdca.shadow_runner import ShadowAblationRunner
from tests.conftest import SESSION_TIME, SYMBOLS, build_pipeline


def _pipeline_factory(disabled_features: frozenset[str]):
    """Mirrors tests.conftest's `universe`/`pipeline` fixtures, but builds a
    FRESH clock+universe per call — each ShadowVariant needs its own
    independent pipeline, not one shared instance."""
    clock = FrozenClock(current=SESSION_TIME)
    universe = UniverseManager()
    for s in SYMBOLS:
        universe.add(UniverseSymbol(symbol=s, listed_from=date(2015, 1, 1)))
    pipe = build_pipeline(clock, universe)
    pipe.disabled_features = disabled_features
    return pipe


def test_runner_produces_a_session_return_per_variant_per_day():
    runner = ShadowAblationRunner(_pipeline_factory,
                                  variants=[ShadowVariant.FULL, ShadowVariant.NO_FUNDAMENTAL])
    dates = [SESSION_TIME + timedelta(days=i) for i in range(3)]
    runner.run_sessions(dates)

    results = runner.results()
    assert set(results) == {ShadowVariant.FULL, ShadowVariant.NO_FUNDAMENTAL}
    for run in results.values():
        assert len(run.session_returns) == 3
        assert all(isinstance(r, float) for r in run.session_returns)


def test_each_variant_gets_its_own_independent_pipeline_and_ledger():
    """FULL and NO_NEWS must not share cash/positions — running one must
    never affect the other's equity trajectory."""
    runner = ShadowAblationRunner(_pipeline_factory,
                                  variants=[ShadowVariant.FULL, ShadowVariant.NO_NEWS])
    runner.run_session(SESSION_TIME)
    full_pipe = runner._pipeline_for(ShadowVariant.FULL)  # noqa: SLF001
    no_news_pipe = runner._pipeline_for(ShadowVariant.NO_NEWS)  # noqa: SLF001
    assert full_pipe is not no_news_pipe
    assert full_pipe.ledger is not no_news_pipe.ledger


def test_no_fundamental_variant_actually_disables_fundamental_on_its_pipeline():
    runner = ShadowAblationRunner(_pipeline_factory, variants=[ShadowVariant.NO_FUNDAMENTAL])
    runner.run_session(SESSION_TIME)
    pipe = runner._pipeline_for(ShadowVariant.NO_FUNDAMENTAL)  # noqa: SLF001
    assert "fundamental" in pipe.disabled_features


def test_into_portfolio_manager_compounds_the_same_returns_the_runner_recorded():
    runner = ShadowAblationRunner(_pipeline_factory,
                                  variants=[ShadowVariant.FULL, ShadowVariant.NO_FUNDAMENTAL])
    dates = [SESSION_TIME + timedelta(days=i) for i in range(3)]
    runner.run_sessions(dates)

    manager = runner.into_portfolio_manager(initial_equity=1000.0)
    for variant, run in runner.results().items():
        expected_equity = 1000.0 * (1 + run.total_return)
        assert manager.portfolios[variant].equity == pytest.approx(expected_equity)


def test_recorded_returns_match_the_pipelines_own_real_equity_change():
    """Independent check against the pipeline's real ledger state (public
    API only, computed separately from ShadowAblationRunner's own
    _current_equity helper) — catches a runner that recorded plausible-
    looking but fabricated returns (e.g. always 0.0) despite real trading
    having actually moved the ledger."""
    runner = ShadowAblationRunner(_pipeline_factory, variants=[ShadowVariant.FULL])
    dates = [SESSION_TIME + timedelta(days=i) for i in range(5)]
    runner.run_sessions(dates)

    pipe = runner._pipeline_for(ShadowVariant.FULL)  # noqa: SLF001
    final_prices = {sym: pipe.market_data.quote(sym, dates[-1]).mid
                    for sym in pipe.ledger.positions}
    final_equity = pipe.ledger.equity(final_prices)
    initial_cash = pipe.ledger.initial_cash

    run = runner.results()[ShadowVariant.FULL]
    assert final_equity != pytest.approx(initial_cash), (
        "test setup produced no trading activity over 5 sessions — "
        "cannot distinguish a real return from a fabricated 0.0 one")
    assert initial_cash * (1 + run.total_return) == pytest.approx(final_equity)


def test_into_ablation_engine_keys_results_by_the_variants_own_disabled_features():
    runner = ShadowAblationRunner(_pipeline_factory,
                                  variants=[ShadowVariant.FULL, ShadowVariant.NO_FUNDAMENTAL])
    runner.run_sessions([SESSION_TIME, SESSION_TIME + timedelta(days=1)])

    engine = runner.into_ablation_engine()
    assert engine.results[frozenset()] == runner.results()[ShadowVariant.FULL].total_return
    assert (engine.results[frozenset({"fundamental"})]
           == runner.results()[ShadowVariant.NO_FUNDAMENTAL].total_return)
    # contribution() should now be directly computable off real run results.
    contribution = engine.contribution("fundamental")
    assert contribution is not None


def test_default_variants_are_only_the_ones_expressible_as_disabled_features():
    """NO_LLM/QUANT_ONLY/MOONSHOT_ONLY/BENCHMARK need a different pipeline
    configuration; run here they would execute as FULL under another name
    and overwrite FULL's AblationEngine entry (both keyed by frozenset())."""
    from services.pdca.shadow import SHADOW_VARIANT_DISABLED_FEATURES

    runner = ShadowAblationRunner(_pipeline_factory)
    assert set(runner.variants) == set(SHADOW_VARIANT_DISABLED_FEATURES)
    with pytest.raises(ValueError):
        ShadowAblationRunner(_pipeline_factory, variants=[ShadowVariant.FULL, ShadowVariant.NO_LLM])


def test_session_dates_must_strictly_increase():
    runner = ShadowAblationRunner(_pipeline_factory, variants=[ShadowVariant.FULL])
    runner.run_session(SESSION_TIME)
    with pytest.raises(ValueError):
        runner.run_session(SESSION_TIME)
    with pytest.raises(ValueError):
        runner.run_session(SESSION_TIME - timedelta(days=1))
