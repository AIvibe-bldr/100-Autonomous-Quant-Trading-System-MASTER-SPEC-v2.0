"""Market Regime Engine (MASTER SPEC §26).

Classifies Bull / Bear / Range / High-Vol / Low-Vol / Panic / Euphoria from
index (or sector) bars.  Per-alpha regime performance is stored so alphas can
be judged per regime.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field

from packages.schemas.core import Bar


class Regime(str, enum.Enum):
    BULL = "BULL"
    BEAR = "BEAR"
    RANGE = "RANGE"
    HIGH_VOLATILITY = "HIGH_VOLATILITY"
    LOW_VOLATILITY = "LOW_VOLATILITY"
    PANIC = "PANIC"
    EUPHORIA = "EUPHORIA"


@dataclass
class RegimeReading:
    primary: Regime
    volatility_regime: Regime
    trend_20d: float
    realized_vol: float


class RegimeEngine:
    def __init__(self, panic_vol: float = 0.04, euphoria_trend: float = 0.15,
                 high_vol: float = 0.025, low_vol: float = 0.008,
                 trend_threshold: float = 0.03) -> None:
        self.panic_vol = panic_vol
        self.euphoria_trend = euphoria_trend
        self.high_vol = high_vol
        self.low_vol = low_vol
        self.trend_threshold = trend_threshold
        # per-alpha, per-regime performance (§26)
        self.alpha_regime_pnl: dict[tuple[str, Regime], list[float]] = {}

    def classify(self, index_bars: list[Bar]) -> RegimeReading:
        if len(index_bars) < 21:
            raise ValueError("need >= 21 bars to classify regime")
        closes = [b.close for b in index_bars]
        trend = closes[-1] / closes[-21] - 1
        rets = [c2 / c1 - 1 for c1, c2 in zip(closes[-21:-1], closes[-20:])]
        mean = sum(rets) / len(rets)
        vol = (sum((r - mean) ** 2 for r in rets) / len(rets)) ** 0.5

        if vol >= self.panic_vol and trend < 0:
            primary = Regime.PANIC
        elif trend >= self.euphoria_trend:
            primary = Regime.EUPHORIA
        elif trend >= self.trend_threshold:
            primary = Regime.BULL
        elif trend <= -self.trend_threshold:
            primary = Regime.BEAR
        else:
            primary = Regime.RANGE

        if vol >= self.high_vol:
            vol_regime = Regime.HIGH_VOLATILITY
        elif vol <= self.low_vol:
            vol_regime = Regime.LOW_VOLATILITY
        else:
            vol_regime = Regime.RANGE

        return RegimeReading(primary=primary, volatility_regime=vol_regime,
                             trend_20d=trend, realized_vol=vol)

    def record_alpha_result(self, alpha: str, regime: Regime, pnl: float) -> None:
        self.alpha_regime_pnl.setdefault((alpha, regime), []).append(pnl)

    def alpha_regime_performance(self, alpha: str) -> dict[Regime, float]:
        out: dict[Regime, float] = {}
        for (a, regime), pnls in self.alpha_regime_pnl.items():
            if a == alpha and pnls:
                out[regime] = sum(pnls) / len(pnls)
        return out
