"""Shadow Portfolios & Ablation (MASTER SPEC §56-57).

Multiple virtual portfolios run against the same market with feature toggles:
FULL / NO_NEWS / NO_INSTITUTIONAL / NO_LLM / QUANT_ONLY / NO_REGIME /
NO_FUNDAMENTAL / MOONSHOT_ONLY / BENCHMARK.  The ablation engine compares
performance with features off, alone and in combination (§57).
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from itertools import combinations


class ShadowVariant(str, enum.Enum):
    FULL = "FULL"
    NO_NEWS = "NO_NEWS"
    NO_INSTITUTIONAL = "NO_INSTITUTIONAL"
    NO_LLM = "NO_LLM"
    QUANT_ONLY = "QUANT_ONLY"
    NO_REGIME = "NO_REGIME"
    # Fundamental Inflection Engine (research-instruction §17: "A: Full
    # System + Fundamental Inflection" vs "B: Full System − Fundamental
    # Inflection").
    NO_FUNDAMENTAL = "NO_FUNDAMENTAL"
    # Management Language Tracker (research-instruction §7-8/§17: same
    # ablation rationale as NO_FUNDAMENTAL above).
    NO_MANAGEMENT_LANGUAGE = "NO_MANAGEMENT_LANGUAGE"
    MOONSHOT_ONLY = "MOONSHOT_ONLY"
    BENCHMARK = "BENCHMARK"


# services.pipeline.TradingPipeline.disabled_features consumes these names
# directly — this is the one place a ShadowVariant's meaning is translated
# into "which decision-input engines does this session actually turn off,"
# so the mapping can't drift out of sync with the variant list above.
SHADOW_VARIANT_DISABLED_FEATURES: dict[ShadowVariant, frozenset[str]] = {
    ShadowVariant.FULL: frozenset(),
    ShadowVariant.NO_NEWS: frozenset({"news"}),
    ShadowVariant.NO_INSTITUTIONAL: frozenset({"institutional"}),
    ShadowVariant.NO_REGIME: frozenset({"regime"}),
    ShadowVariant.NO_FUNDAMENTAL: frozenset({"fundamental"}),
    ShadowVariant.NO_MANAGEMENT_LANGUAGE: frozenset({"management_language"}),
    # NO_LLM, QUANT_ONLY, MOONSHOT_ONLY, BENCHMARK are deeper structural
    # differences (a different decision_model entirely, a position-count
    # cap, a benchmark-only passive comparator) — not expressible as
    # "which of these four engines is off," so intentionally absent here.
    # A caller building one of those variants configures the pipeline
    # directly rather than through this mapping.
}


@dataclass
class ShadowPortfolio:
    variant: ShadowVariant
    equity: float
    history: list[float] = field(default_factory=list)

    def mark(self, session_return: float) -> None:
        self.equity *= (1 + session_return)
        self.history.append(self.equity)

    def __post_init__(self) -> None:
        self._initial = self.equity

    @property
    def total_return(self) -> float:
        return self.equity / self._initial - 1


class ShadowPortfolioManager:
    def __init__(self, initial_equity: float,
                 variants: list[ShadowVariant] | None = None) -> None:
        self.portfolios = {v: ShadowPortfolio(variant=v, equity=initial_equity)
                           for v in (variants or list(ShadowVariant))}

    def record_session(self, returns_by_variant: dict[ShadowVariant, float]) -> None:
        for v, r in returns_by_variant.items():
            self.portfolios[v].mark(r)

    def ranking(self) -> list[tuple[ShadowVariant, float]]:
        return sorted(((v, p.total_return) for v, p in self.portfolios.items()),
                      key=lambda t: t[1], reverse=True)


@dataclass
class AblationEngine:
    """§57: measure performance with features OFF, alone and combined."""

    results: dict[frozenset[str], float] = field(default_factory=dict)

    def record(self, disabled_features: set[str], performance: float) -> None:
        self.results[frozenset(disabled_features)] = performance

    def contribution(self, feature: str) -> float | None:
        """Performance(full) - Performance(without feature): positive means the
        feature helps."""
        full = self.results.get(frozenset())
        without = self.results.get(frozenset({feature}))
        if full is None or without is None:
            return None
        return full - without

    def removable_candidates(self, threshold: float = 0.0) -> list[str]:
        """Pruner input (§60): features whose removal does not hurt."""
        singles = [(next(iter(k)), v) for k, v in self.results.items() if len(k) == 1]
        full = self.results.get(frozenset())
        if full is None:
            return []
        return [f for f, perf in singles if perf >= full - threshold]

    def pairwise_check(self, f1: str, f2: str) -> float | None:
        """Combined ablation (§57): interactions may hide single-feature value."""
        return self.results.get(frozenset({f1, f2}))
