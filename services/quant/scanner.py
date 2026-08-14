"""Quant Scanner (MASTER SPEC §21).

The funnel BEFORE any LLM: 5000 symbols → Python scan → 200 → advanced quant
→ 20 → Decision AI → 1-5.  Anything Python/SQL can do never reaches an LLM.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from packages.schemas.core import Bar
from services.market_data.service import MarketDataService


@dataclass(frozen=True)
class ScanResult:
    symbol: str
    last_close: float
    momentum_20d: float
    dollar_volume: float
    volatility: float
    score: float
    bars: tuple[Bar, ...]


@dataclass
class ScannerFunnel:
    scanned: int = 0
    passed_basic: int = 0
    passed_advanced: int = 0


class QuantScanner:
    def __init__(self, market_data: MarketDataService,
                 min_dollar_volume: float = 5_000_000, min_price: float = 3.0,
                 basic_top_n: int = 200, advanced_top_n: int = 20) -> None:
        self.md = market_data
        self.min_dollar_volume = min_dollar_volume
        self.min_price = min_price
        self.basic_top_n = basic_top_n
        self.advanced_top_n = advanced_top_n
        self.last_funnel = ScannerFunnel()

    def scan(self, symbols: list[str], as_of: datetime) -> list[ScanResult]:
        funnel = ScannerFunnel(scanned=len(symbols))
        stage1: list[ScanResult] = []
        for sym in symbols:
            stamped = self.md.bars(sym, as_of, days=40, received_at=as_of)
            bars = [s.bar for s in stamped]
            if len(bars) < 21:
                continue
            close = bars[-1].close
            if close < self.min_price:
                continue
            dollar_vol = sum(b.close * b.volume for b in bars[-20:]) / 20
            if dollar_vol < self.min_dollar_volume:
                continue
            momentum = close / bars[-21].close - 1
            rets = [(b2.close / b1.close - 1) for b1, b2 in zip(bars[-21:-1], bars[-20:])]
            mean = sum(rets) / len(rets)
            vol = (sum((r - mean) ** 2 for r in rets) / len(rets)) ** 0.5
            score = momentum / (vol + 1e-9)
            stage1.append(ScanResult(symbol=sym, last_close=close, momentum_20d=momentum,
                                     dollar_volume=dollar_vol, volatility=vol, score=score,
                                     bars=tuple(bars)))
        funnel.passed_basic = len(stage1)
        stage1.sort(key=lambda r: r.score, reverse=True)
        stage1 = stage1[:self.basic_top_n]

        # advanced stage: require positive momentum and sane volatility
        stage2 = [r for r in stage1 if r.momentum_20d > 0 and r.volatility < 0.06]
        stage2 = stage2[:self.advanced_top_n]
        funnel.passed_advanced = len(stage2)
        self.last_funnel = funnel
        return stage2
