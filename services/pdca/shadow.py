"""Shadow Portfolios & Ablation (MASTER SPEC §56-57).

Multiple virtual portfolios run against the same market with feature toggles:
FULL / NO_NEWS / NO_INSTITUTIONAL / NO_LLM / QUANT_ONLY / NO_REGIME /
MOONSHOT_ONLY / BENCHMARK.  The ablation engine compares performance with
features off, alone and in combination (§57).
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
    MOONSHOT_ONLY = "MOONSHOT_ONLY"
    BENCHMARK = "BENCHMARK"


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
