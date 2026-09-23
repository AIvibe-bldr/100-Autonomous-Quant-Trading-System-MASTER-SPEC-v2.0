"""Tests for the §57 Ablation feature-toggle mechanism
(TradingPipeline.disabled_features + services.pdca.shadow.
SHADOW_VARIANT_DISABLED_FEATURES).

Before this, services.pdca.shadow.ShadowVariant/ShadowPortfolioManager/
AblationEngine had no way to actually produce a "feature off" session —
NO_NEWS, NO_INSTITUTIONAL, NO_REGIME (and now NO_FUNDAMENTAL) existed only
as enum values with nothing wired to turn the named engine off. These
tests drive the real toggle end-to-end via a spy DecisionContext, the same
pattern used for the regime/news/institutional/fundamental wiring tests in
test_security_review_regressions.py.
"""
from __future__ import annotations

from services.decision.models import MockDecisionModel
from services.pdca.shadow import SHADOW_VARIANT_DISABLED_FEATURES, ShadowVariant
from tests.conftest import SESSION_TIME


def _spy_contexts(pipeline):
    seen = []

    class _Spy(MockDecisionModel):
        def decide(self, context):
            seen.append(context)
            return super().decide(context)

    pipeline.decision_model = _Spy()
    pipeline.run_session(SESSION_TIME)
    return seen


def test_full_variant_disables_nothing(pipeline):
    pipeline.disabled_features = SHADOW_VARIANT_DISABLED_FEATURES[ShadowVariant.FULL]
    contexts = _spy_contexts(pipeline)
    assert contexts, "Decision AI was never consulted this session"
    assert any(ctx.regime != "UNKNOWN" for ctx in contexts)
    assert any(ctx.news for ctx in contexts)
    assert any(ctx.institutional is not None for ctx in contexts)
    assert any(ctx.fundamental is not None for ctx in contexts)
    assert any(ctx.management_language is not None for ctx in contexts)


def test_no_regime_variant_forces_unknown_everywhere(pipeline):
    pipeline.disabled_features = SHADOW_VARIANT_DISABLED_FEATURES[ShadowVariant.NO_REGIME]
    contexts = _spy_contexts(pipeline)
    assert contexts
    assert all(ctx.regime == "UNKNOWN" for ctx in contexts)


def test_no_news_variant_suppresses_news(pipeline):
    pipeline.disabled_features = SHADOW_VARIANT_DISABLED_FEATURES[ShadowVariant.NO_NEWS]
    contexts = _spy_contexts(pipeline)
    assert contexts
    assert all(not ctx.news for ctx in contexts)
    # everything else stays on
    assert any(ctx.institutional is not None for ctx in contexts)
    assert any(ctx.fundamental is not None for ctx in contexts)


def test_no_institutional_variant_suppresses_institutional_only(pipeline):
    pipeline.disabled_features = SHADOW_VARIANT_DISABLED_FEATURES[ShadowVariant.NO_INSTITUTIONAL]
    contexts = _spy_contexts(pipeline)
    assert contexts
    assert all(ctx.institutional is None for ctx in contexts)
    assert any(ctx.news for ctx in contexts)
    assert any(ctx.fundamental is not None for ctx in contexts)


def test_no_fundamental_variant_suppresses_fundamental_only(pipeline):
    pipeline.disabled_features = SHADOW_VARIANT_DISABLED_FEATURES[ShadowVariant.NO_FUNDAMENTAL]
    contexts = _spy_contexts(pipeline)
    assert contexts
    assert all(ctx.fundamental is None for ctx in contexts)
    assert any(ctx.news for ctx in contexts)
    assert any(ctx.institutional is not None for ctx in contexts)


def test_no_management_language_variant_suppresses_management_language_only(pipeline):
    pipeline.disabled_features = SHADOW_VARIANT_DISABLED_FEATURES[
        ShadowVariant.NO_MANAGEMENT_LANGUAGE]
    contexts = _spy_contexts(pipeline)
    assert contexts
    assert all(ctx.management_language is None for ctx in contexts)
    assert any(ctx.news for ctx in contexts)
    assert any(ctx.fundamental is not None for ctx in contexts)
    assert any(ctx.institutional is not None for ctx in contexts)


def test_regime_toggle_also_applies_to_exit_decisions(pipeline):
    """§57: a NO_REGIME variant must be "off" everywhere regime is
    consulted, including manage_open_positions()'s exit-decision recording
    — not just at entry. Directly exercises _current_regime() rather than
    the full session (exits only happen when a resting stop actually
    triggers, which isn't guaranteed on any given session)."""
    pipeline.disabled_features = frozenset({"regime"})
    assert pipeline._current_regime(SESSION_TIME) == "UNKNOWN"  # noqa: SLF001
    pipeline.disabled_features = frozenset()
    assert pipeline._current_regime(SESSION_TIME) != "UNKNOWN"  # noqa: SLF001
