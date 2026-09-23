"""Shadow/Ablation Session Runner (MASTER SPEC §56-57, research-instruction
§17: "A: Full System + Fundamental Inflection" vs "B: Full System −
Fundamental Inflection").

`ShadowPortfolioManager`/`AblationEngine` (services.pdca.shadow) already
know how to SCORE variant results — `record_session`/`record` both simply
take a caller-supplied number. What was missing is what actually PRODUCES
those numbers: running each `ShadowVariant`'s own `TradingPipeline` across
a sequence of session dates and turning each session's ledger equity change
into the per-session return those scorers expect.

Deliberately pipeline-construction-agnostic, via `pipeline_factory`: this
module has no opinion on Mock vs real market data, which broker, which
decision model, etc. — that configuration already exists wherever a
`TradingPipeline` is normally built (see `tests.conftest.build_pipeline`
for the reference shape); this runner only adds `disabled_features` per
variant on top of it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

from services.pdca.shadow import (
    SHADOW_VARIANT_DISABLED_FEATURES,
    AblationEngine,
    ShadowPortfolioManager,
    ShadowVariant,
)
from services.pipeline import TradingPipeline

PipelineFactory = Callable[[frozenset[str]], TradingPipeline]


def _current_equity(pipeline: TradingPipeline, now: datetime) -> float:
    """Public-API-only mark-to-market (`market_data.quote`, `ledger.equity`)
    — no pipeline internals — of every symbol the pipeline currently holds,
    as of `now`."""
    prices = {sym: pipeline.market_data.quote(sym, now).mid
             for sym in pipeline.ledger.positions}
    return pipeline.ledger.equity(prices)


@dataclass
class VariantSessionRun:
    variant: ShadowVariant
    session_returns: list[float] = field(default_factory=list)

    @property
    def total_return(self) -> float:
        equity = 1.0
        for r in self.session_returns:
            equity *= (1 + r)
        return equity - 1


class ShadowAblationRunner:
    """Drives one independent `TradingPipeline` per variant across the same
    sequence of session dates — never a single pipeline reused across
    variants, which would let one variant's positions/cash contaminate
    another's."""

    def __init__(self, pipeline_factory: PipelineFactory,
                variants: list[ShadowVariant] | None = None) -> None:
        # Only variants expressible as a disabled_features set can be run
        # here. NO_LLM/QUANT_ONLY/MOONSHOT_ONLY/BENCHMARK need a different
        # pipeline configuration (services.pdca.shadow says so explicitly);
        # run through this runner they would silently execute as FULL, be
        # ranked as if they were distinct strategies, and overwrite FULL's
        # entry in the AblationEngine (both keyed by the empty set).
        supported = list(SHADOW_VARIANT_DISABLED_FEATURES)
        unsupported = [v for v in (variants or []) if v not in SHADOW_VARIANT_DISABLED_FEATURES]
        if unsupported:
            raise ValueError(
                f"variants not expressible as disabled_features: "
                f"{[v.value for v in unsupported]} — configure those pipelines directly")
        self.pipeline_factory = pipeline_factory
        self.variants = variants or supported
        self._last_session: datetime | None = None
        self._pipelines: dict[ShadowVariant, TradingPipeline] = {}
        self._results: dict[ShadowVariant, VariantSessionRun] = {
            v: VariantSessionRun(variant=v) for v in self.variants}
        # NAV-to-NAV tracking (last session's closing equity, carried
        # forward as next session's opening equity) — re-marking "before"
        # at the NEW session's own prices instead would silently drop
        # overnight price drift on already-held positions from every
        # return, understating exactly the "which variant's holdings did
        # better overnight" signal Shadow/Ablation comparison exists to
        # measure.
        self._last_equity: dict[ShadowVariant, float] = {}

    def _pipeline_for(self, variant: ShadowVariant) -> TradingPipeline:
        if variant not in self._pipelines:
            disabled = SHADOW_VARIANT_DISABLED_FEATURES.get(variant, frozenset())
            pipe = self.pipeline_factory(disabled)
            self._pipelines[variant] = pipe
            # A freshly built pipeline holds only cash — no mark-to-market
            # needed (or possible, before any `now` is known) for its
            # starting equity.
            self._last_equity[variant] = pipe.ledger.equity({})
        return self._pipelines[variant]

    def run_session(self, now: datetime) -> None:
        """Advance every variant's own pipeline through one session date.
        Dates must strictly increase: returns are NAV-to-NAV from the
        previous call, and re-running a date re-records the same
        decision_ids (DecisionQualityEngine rejects a changed snapshot
        under an existing id, INV-21)."""
        if self._last_session is not None and now <= self._last_session:
            raise ValueError(f"session {now.isoformat()} is not after the previous "
                             f"session {self._last_session.isoformat()}")
        self._last_session = now
        for variant in self.variants:
            pipe = self._pipeline_for(variant)
            equity_before = self._last_equity[variant]
            pipe.run_session(now)
            equity_after = _current_equity(pipe, now)
            session_return = (equity_after / equity_before - 1) if equity_before else 0.0
            self._results[variant].session_returns.append(session_return)
            self._last_equity[variant] = equity_after

    def run_sessions(self, dates: list[datetime]) -> None:
        for now in dates:
            self.run_session(now)

    def results(self) -> dict[ShadowVariant, VariantSessionRun]:
        return dict(self._results)

    def into_portfolio_manager(self, initial_equity: float = 1.0) -> ShadowPortfolioManager:
        """Replays every recorded session return, in order, into a fresh
        `ShadowPortfolioManager` — `ShadowPortfolio.mark`'s own compounding
        then agrees with `VariantSessionRun.total_return` above by
        construction."""
        manager = ShadowPortfolioManager(initial_equity=initial_equity, variants=self.variants)
        max_sessions = max((len(r.session_returns) for r in self._results.values()), default=0)
        for i in range(max_sessions):
            returns = {v: self._results[v].session_returns[i] for v in self.variants
                      if i < len(self._results[v].session_returns)}
            manager.record_session(returns)
        return manager

    def into_ablation_engine(self) -> AblationEngine:
        """One `AblationEngine.record()` per variant, keyed by that
        variant's own `disabled_features` — so `AblationEngine.
        contribution()`/`removable_candidates()` work directly off this
        runner's results without the caller re-deriving the mapping."""
        engine = AblationEngine()
        for variant in self.variants:
            disabled = SHADOW_VARIANT_DISABLED_FEATURES.get(variant, frozenset())
            engine.record(set(disabled), self._results[variant].total_return)
        return engine
