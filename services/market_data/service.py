"""Market Data Service (MASTER SPEC §8).

Providers implement `MarketDataProvider`; the service stamps every record with
the Unified Clock timestamps (§9) and hands raw data to the Data Integrity
Engine before anything downstream may consume it.

`MockProvider` generates a deterministic synthetic market (seeded per symbol)
so the whole pipeline runs end-to-end in PAPER/RESEARCH without network access.
"""
from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from datetime import date as _date, datetime, timedelta
from typing import Protocol

from packages.common.clock import EventTimes, ensure_utc
from packages.schemas.core import Bar, Quote


class MarketDataProvider(Protocol):
    def get_bars(self, symbol: str, end: datetime, days: int) -> list[Bar]: ...
    def get_quote(self, symbol: str, at: datetime) -> Quote: ...


def _seed(symbol: str) -> int:
    return int(hashlib.sha256(symbol.encode()).hexdigest()[:8], 16)


# All price paths are walked from this fixed epoch so that the price on a
# given day is a function of the DAY, never of how many days the caller asked
# for. Previously the walk restarted from the base price at `end - days`, so
# get_bars(sym, at, 1) and get_bars(sym, at, 60) returned different prices for
# the same date — and since get_quote used days=1, quotes were pinned within
# ±4% of base while the bar series random-walked ±50% from it. Fills, marks and
# stop triggers all use the quote, so stops effectively never fired: 200
# sessions produced realized_pnl == 0.0 and the drawdown throttle never left
# NORMAL. Any test relying on stops firing or on P&L realism was passing
# vacuously.
_WALK_EPOCH_ORD = _date(2025, 1, 1).toordinal()


class MockProvider:
    """Deterministic random-walk market. Same symbol + date → same data,
    which keeps replay (§62) and tests reproducible."""

    def __init__(self, base_price_range: tuple[float, float] = (5.0, 400.0)) -> None:
        self.lo, self.hi = base_price_range
        self._walk: dict[str, dict[int, float]] = {}

    def _price_at(self, symbol: str, day_ordinal: int) -> float:
        """Close on `day_ordinal`, walked from the shared epoch and memoized."""
        cache = self._walk.setdefault(symbol, {_WALK_EPOCH_ORD: self._base_price(symbol)})
        if day_ordinal <= _WALK_EPOCH_ORD:
            return cache[_WALK_EPOCH_ORD]
        if day_ordinal in cache:
            return cache[day_ordinal]
        d = max(k for k in cache if k <= day_ordinal)
        price = cache[d]
        while d < day_ordinal:
            d += 1
            price = max(0.01, price * (1 + self._daily_return(symbol, d)))
            cache[d] = price
        return price

    def _base_price(self, symbol: str) -> float:
        s = _seed(symbol)
        return self.lo + (s % 10_000) / 10_000 * (self.hi - self.lo)

    def _daily_return(self, symbol: str, day_ordinal: int) -> float:
        h = int(hashlib.sha256(f"{symbol}:{day_ordinal}".encode()).hexdigest()[:8], 16)
        u = h / 0xFFFFFFFF
        # deterministic pseudo-normal in ±4% with mild per-symbol drift
        drift = ((_seed(symbol) % 21) - 10) / 10_000
        # +σ²/2 offsets the volatility drag of a geometric walk, so a long run
        # from the epoch stays near the base price instead of decaying toward
        # the 0.01 floor. For uniform(-4%,+4%), σ² = 0.08²/12.
        vol_drag = (0.08 ** 2) / 12 / 2
        return drift + vol_drag + (u - 0.5) * 0.08

    def get_bars(self, symbol: str, end: datetime, days: int) -> list[Bar]:
        end = ensure_utc(end)
        bars: list[Bar] = []
        start_ord = end.toordinal() - days
        for i in range(days):
            d_ord = start_ord + i
            r = self._daily_return(symbol, d_ord)
            o = self._price_at(symbol, d_ord - 1)
            c = self._price_at(symbol, d_ord)
            hi = max(o, c) * (1 + abs(r) / 2)
            lo = min(o, c) * (1 - abs(r) / 2)
            vol_seed = int(hashlib.sha256(f"v:{symbol}:{d_ord}".encode()).hexdigest()[:8], 16)
            volume = 200_000 + vol_seed % 5_000_000
            ts = datetime.fromordinal(d_ord).replace(tzinfo=end.tzinfo, hour=20)  # close in UTC
            bars.append(Bar(symbol=symbol, ts=ts, open=round(o, 4), high=round(hi, 4),
                            low=round(lo, 4), close=round(c, 4), volume=volume,
                            vwap=round((hi + lo + c) / 3, 4), source="mock"))
        return bars

    def get_quote(self, symbol: str, at: datetime) -> Quote:
        at = ensure_utc(at)
        # Same walk as get_bars — quote and bar series are one price process.
        px = self._price_at(symbol, at.toordinal())
        spread = max(0.01, px * 0.0005)
        return Quote(symbol=symbol, ts=at, bid=round(px - spread / 2, 4),
                     ask=round(px + spread / 2, 4), bid_size=500, ask_size=500, source="mock")


@dataclass(frozen=True)
class StampedBar:
    bar: Bar
    times: EventTimes


class MarketDataService:
    def __init__(self, provider: MarketDataProvider) -> None:
        self.provider = provider

    def bars(self, symbol: str, end: datetime, days: int, received_at: datetime) -> list[StampedBar]:
        received_at = ensure_utc(received_at)
        out = []
        for bar in self.provider.get_bars(symbol, end, days):
            times = EventTimes(event_time=bar.ts, source_time=bar.ts,
                              received_time=received_at).with_processed(received_at)
            out.append(StampedBar(bar=bar, times=times))
        return out

    def quote(self, symbol: str, at: datetime) -> Quote:
        return self.provider.get_quote(symbol, at)


def atr(bars: list[Bar], period: int = 14) -> float:
    """Average True Range for ATR stops (§34)."""
    if len(bars) < 2:
        raise ValueError("need at least 2 bars for ATR")
    trs: list[float] = []
    for prev, cur in zip(bars[:-1], bars[1:]):
        tr = max(cur.high - cur.low, abs(cur.high - prev.close), abs(cur.low - prev.close))
        trs.append(tr)
    window = trs[-period:]
    return sum(window) / len(window)
