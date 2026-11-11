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


UNIVERSE_COMPOSITE_SYMBOL = "UNIVERSE"


def universe_composite_bars(bars_by_symbol: dict[str, list[Bar]],
                            min_symbols: int = 3) -> list[Bar]:
    """Equal-weight composite of the universe, used as a stand-in market index.

    The universe holds no real index instrument yet, so this is an ESTIMATE
    built from the symbols we trade — callers must label it as such. Closes
    are rebased to 100 at the first common timestamp and averaged across
    symbols; only timestamps present for every symbol are used (no filling).
    Pure function: no I/O, deterministic.
    """
    usable = {s: b for s, b in bars_by_symbol.items() if b}
    if len(usable) < min_symbols:
        raise ValueError(f"need >= {min_symbols} symbols with bars, got {len(usable)}")
    common = set.intersection(*({b.ts for b in bars} for bars in usable.values()))
    stamps = sorted(common)
    if len(stamps) < 21:
        raise ValueError("need >= 21 common timestamps to build a composite")
    by_ts = {sym: {b.ts: b.close for b in bars} for sym, bars in usable.items()}
    out: list[Bar] = []
    for ts in stamps:
        level = sum(100.0 * by_ts[sym][ts] / by_ts[sym][stamps[0]] for sym in usable) / len(usable)
        out.append(Bar(symbol=UNIVERSE_COMPOSITE_SYMBOL, ts=ts, open=level, high=level,
                       low=level, close=level, volume=0, source="composite"))
    return out


def describe_universe_regime(reading: RegimeReading, n_symbols: int) -> str:
    return (f"{reading.primary.value} / vol={reading.volatility_regime.value} "
            f"(ESTIMATE: equal-weight composite of {n_symbols} traded symbols, not an "
            f"official index; 20d trend {reading.trend_20d:+.2%}, "
            f"daily vol {reading.realized_vol:.2%})")
