"""Shadow portfolios, ablation and alpha shadow-running (MASTER SPEC §23, §56-58), as an observer.

Each session, virtual portfolios that cost nothing to compute are run beside the real one:
  FULL        what the system actually did (equity change between sessions)
  QUANT_ONLY  the scanner's top candidates bought regardless of the AI stack
  BENCHMARK   the benchmark (SPY)
and every Alpha Factory candidate (momentum, mean reversion) is shadow-traded on the same
candidates. Variants that would need the LLM re-run with a feature off (NO_NEWS, NO_REGIME, ...)
are NOT simulated: that would spend money on every session.

Honest limits, repeated in the summary: shadow variants are hypothetical close-to-close holds
without costs, while FULL pays spreads, slippage and fees — so FULL vs the others is not
like-for-like, and FULL minus QUANT_ONLY ("ai_stack" contribution) is an indication to watch over
many sessions, not a verdict. `ChampionChallenger.consider_promotion` is never called here: it is
a decision for a human."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from services.alpha_factory.factory import Alpha, ChampionChallenger, MeanReversionAlpha, MomentumAlpha
from services.pdca.observer import DecisionEvent, SessionEndEvent, SessionObserver
from services.pdca.shadow import AblationEngine, ShadowPortfolioManager, ShadowVariant

BENCHMARK_SYMBOL = "SPY"
QUANT_ONLY_PICKS = 5


@dataclass
class _Candidate:
    symbol: str
    last_close: float
    score: float
    bars: tuple


class ShadowObserver(SessionObserver):
    name = "shadow"

    def __init__(self, initial_equity: float, alphas: Optional[list[Alpha]] = None,
                 picks: int = QUANT_ONLY_PICKS, benchmark: str = BENCHMARK_SYMBOL) -> None:
        self.manager = ShadowPortfolioManager(
            initial_equity, [ShadowVariant.FULL, ShadowVariant.QUANT_ONLY, ShadowVariant.BENCHMARK])
        self.ablation = AblationEngine()
        self.alphas = alphas if alphas is not None else [MomentumAlpha(), MeanReversionAlpha()]
        self.champions = ChampionChallenger()
        self._picks = picks
        self._benchmark = benchmark
        self._buffer: list[_Candidate] = []
        # what was "bought" at the previous session, to be marked at this one
        self._held: dict[str, list[tuple[str, float]]] = {}
        self._bench_prev: Optional[float] = None
        self._equity_prev: Optional[float] = None
        self.sessions = 0

    def on_decision(self, e: DecisionEvent) -> None:
        s = e.context.scan
        self._buffer.append(_Candidate(e.symbol, s.last_close, s.score, s.bars))

    def on_session_end(self, e: SessionEndEvent) -> None:
        returns: dict[ShadowVariant, float] = {}
        if self._equity_prev:
            returns[ShadowVariant.FULL] = e.equity / self._equity_prev - 1.0
        marked = {name: self._mark(held, e) for name, held in self._held.items()}
        if self._held:                                    # i.e. not the first session
            if marked.get("quant_only") is not None:
                returns[ShadowVariant.QUANT_ONLY] = marked["quant_only"]
            bench = e.price_on(self._benchmark, e.as_of)
            if bench and self._bench_prev:
                returns[ShadowVariant.BENCHMARK] = bench / self._bench_prev - 1.0
            for alpha in self.alphas:
                r = marked.get(f"alpha:{alpha.name}")
                if r is not None:
                    self.champions.record_shadow(alpha.name, r)
        if returns:
            self.manager.record_session(returns)
            self.sessions += 1
            full, quant = (self.manager.portfolios[v].total_return
                           for v in (ShadowVariant.FULL, ShadowVariant.QUANT_ONLY))
            self.ablation.record(set(), full)
            self.ablation.record({"ai_stack"}, quant)

        # hold for the next session: the same candidates every variant could have bought
        ranked = sorted(self._buffer, key=lambda c: c.score, reverse=True)
        self._held = {"quant_only": [(c.symbol, c.last_close) for c in ranked[:self._picks]]}
        for alpha in self.alphas:
            self._held[f"alpha:{alpha.name}"] = [
                (c.symbol, c.last_close) for c in ranked if alpha.signal(list(c.bars))[-1]]
        self._buffer = []
        self._bench_prev = e.price_on(self._benchmark, e.as_of)
        self._equity_prev = e.equity

    @staticmethod
    def _mark(held: list[tuple[str, float]], e: SessionEndEvent) -> Optional[float]:
        """Mean return of the held names since the last session; 0.0 if nothing was held (cash);
        None when names were held but none could be priced."""
        if not held:
            return 0.0
        rets = []
        for symbol, entry in held:
            px = e.price_on(symbol, e.as_of)
            if px and entry:
                rets.append(px / entry - 1.0)
        return sum(rets) / len(rets) if rets else None

    def summary(self) -> dict[str, Any]:
        return {
            "sessions": self.sessions,
            "ranking": [{"variant": v.value, "total_return": round(r, 5)}
                        for v, r in self.manager.ranking()],
            "ai_stack_contribution": self.ablation.contribution("ai_stack"),
            "alpha_shadow": {a: {"sessions": len(r), "mean_return": round(sum(r) / len(r), 5)}
                             for a, r in self.champions.shadow_results.items() if r},
            "caveats": ["shadow variants are hypothetical close-to-close holds without costs; FULL "
                        "pays spreads, slippage and fees (not like-for-like)",
                        "variants needing an LLM re-run (NO_NEWS, NO_REGIME, ...) are not simulated",
                        "too few sessions to rank anything until the paper period is long"]}
