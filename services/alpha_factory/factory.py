"""Alpha Factory (MASTER SPEC §23, §58, §60).

Builder generates candidate alphas; candidates can NEVER go live directly
(直接LIVE禁止 §23).  The Judge evaluates independently with the
anti-overfitting toolkit (§24); promotion follows
Research → Backtest → Walk-forward → Shadow → Judge → Promotion (§52), and
Champion/Challenger keeps the incumbent until a challenger proves better
(§58).  Every promotion decision is recorded as an Experiment (§98).
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Callable, Protocol

from packages.schemas.core import Bar
from packages.strategy_sdk.validation import (
    MIN_SAMPLE_TRADES,
    monte_carlo_p_value,
    walk_forward_windows,
)
from services.quant.backtest import BacktestResult, MicrostructureSimulator, run_backtest


class AlphaStage(str, enum.Enum):
    RESEARCH = "RESEARCH"
    BACKTEST = "BACKTEST"
    WALK_FORWARD = "WALK_FORWARD"
    SHADOW = "SHADOW"
    PROMOTED = "PROMOTED"
    REJECTED = "REJECTED"


class Alpha(Protocol):
    name: str
    version: str

    def signal(self, bars: list[Bar]) -> list[bool]:
        """entry_signal[i] for bar i, decided on close of bar i (no lookahead)."""
        ...


@dataclass
class MomentumAlpha:
    lookback: int = 20
    threshold: float = 0.05
    name: str = "momentum"
    version: str = "1.0.0"

    def signal(self, bars: list[Bar]) -> list[bool]:
        out: list[bool] = []
        for i in range(len(bars)):
            if i < self.lookback:
                out.append(False)
                continue
            r = bars[i].close / bars[i - self.lookback].close - 1
            out.append(r > self.threshold)
        return out


@dataclass
class MeanReversionAlpha:
    lookback: int = 5
    drop: float = 0.05
    name: str = "mean_reversion"
    version: str = "1.0.0"

    def signal(self, bars: list[Bar]) -> list[bool]:
        out: list[bool] = []
        for i in range(len(bars)):
            if i < self.lookback:
                out.append(False)
                continue
            r = bars[i].close / bars[i - self.lookback].close - 1
            out.append(r < -self.drop)
        return out


@dataclass
class JudgeVerdict:
    alpha: str
    stage: AlphaStage
    reasons: list[str] = field(default_factory=list)
    backtest: BacktestResult | None = None
    oos_return: float = 0.0
    p_value: float = 1.0


@dataclass
class AlphaJudge:
    """Independent evaluation (§60): the Judge is not the Builder and applies
    §24 anti-overfitting gates mechanically."""

    max_p_value: float = 0.20
    min_trades: int = MIN_SAMPLE_TRADES

    def judge(self, alpha: Alpha, bars: list[Bar]) -> JudgeVerdict:
        verdict = JudgeVerdict(alpha=alpha.name, stage=AlphaStage.BACKTEST)
        sim = MicrostructureSimulator()
        signal = alpha.signal(bars)

        # 1. full-sample backtest with microstructure costs (§25)
        bt = run_backtest(bars, signal, sim=sim)
        verdict.backtest = bt
        if bt.trades < self.min_trades:
            verdict.stage = AlphaStage.REJECTED
            verdict.reasons.append(
                f"insufficient sample: {bt.trades} < {self.min_trades} trades (§24)")
            return verdict
        if bt.total_return <= 0:
            verdict.stage = AlphaStage.REJECTED
            verdict.reasons.append("negative return after transaction costs (§24)")
            return verdict

        # 2. walk-forward: judged only on out-of-sample windows (§24)
        verdict.stage = AlphaStage.WALK_FORWARD
        windows = walk_forward_windows(len(bars), train_len=len(bars) // 3,
                                       test_len=len(bars) // 6, embargo=2)
        oos_returns: list[float] = []
        for w in windows:
            s0, s1 = w.test
            oos = run_backtest(bars[s0:s1], signal[s0:s1], sim=sim)
            oos_returns.append(oos.total_return)
        verdict.oos_return = sum(oos_returns) / len(oos_returns)
        if verdict.oos_return <= 0:
            verdict.stage = AlphaStage.REJECTED
            verdict.reasons.append(f"out-of-sample mean return {verdict.oos_return:.2%} <= 0")
            return verdict

        # 3. Monte Carlo luck test (§24)
        per_trade = [r2 - r1 for r1, r2 in zip(bt.equity_curve[:-1], bt.equity_curve[1:])
                     if abs(r2 - r1) > 1e-9]
        verdict.p_value = monte_carlo_p_value(per_trade)
        if verdict.p_value > self.max_p_value:
            verdict.stage = AlphaStage.REJECTED
            verdict.reasons.append(f"monte carlo p={verdict.p_value:.2f} — likely luck (§24)")
            return verdict

        verdict.stage = AlphaStage.SHADOW  # never straight to live (§23)
        verdict.reasons.append("passed backtest, walk-forward and luck test → shadow")
        return verdict


@dataclass
class ChampionChallenger:
    """§58: the challenger must beat the champion in shadow before promotion."""

    champion: str | None = None
    shadow_results: dict[str, list[float]] = field(default_factory=dict)
    min_shadow_sessions: int = 10

    def record_shadow(self, alpha: str, session_return: float) -> None:
        self.shadow_results.setdefault(alpha, []).append(session_return)

    def consider_promotion(self, challenger: str) -> tuple[bool, str]:
        runs = self.shadow_results.get(challenger, [])
        if len(runs) < self.min_shadow_sessions:
            return False, f"needs {self.min_shadow_sessions} shadow sessions, has {len(runs)}"
        challenger_mean = sum(runs) / len(runs)
        if self.champion is None:
            if challenger_mean > 0:
                self.champion = challenger
                return True, "no incumbent; positive shadow record → promoted"
            return False, "no incumbent but shadow mean <= 0"
        champ_runs = self.shadow_results.get(self.champion, [])
        champ_mean = sum(champ_runs) / len(champ_runs) if champ_runs else 0.0
        if challenger_mean > champ_mean:
            old = self.champion
            self.champion = challenger
            return True, f"beat champion {old}: {challenger_mean:.3%} > {champ_mean:.3%}"
        return False, f"did not beat champion: {challenger_mean:.3%} <= {champ_mean:.3%}"
