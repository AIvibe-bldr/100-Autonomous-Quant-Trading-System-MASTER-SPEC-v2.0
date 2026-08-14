"""Post-trade tracking (MASTER SPEC §50-51, §53).

After a stop-out (or profit-take) the price path is tracked at 1h / 24h /
3d / 1w and the stop is graded GOOD / EARLY / LATE / NEUTRAL.  NO-TRADE and
risk-rejected candidates are also tracked (Counterfactual, §53).
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable, Optional

from packages.common.clock import ensure_utc

HORIZONS = {"1h": timedelta(hours=1), "24h": timedelta(days=1),
            "3d": timedelta(days=3), "1w": timedelta(weeks=1)}


class StopGrade(str, enum.Enum):
    GOOD_STOP = "GOOD_STOP"
    EARLY_STOP = "EARLY_STOP"
    LATE_STOP = "LATE_STOP"
    NEUTRAL = "NEUTRAL"


@dataclass
class TrackedExit:
    symbol: str
    exit_price: float
    exit_at: datetime
    kind: str                                 # "STOP" | "PROFIT" | "COUNTERFACTUAL"
    later_prices: dict[str, float] = field(default_factory=dict)
    grade: Optional[StopGrade] = None


@dataclass
class PostTradeTracker:
    tracked: list[TrackedExit] = field(default_factory=list)

    def track(self, symbol: str, exit_price: float, exit_at: datetime, kind: str) -> TrackedExit:
        t = TrackedExit(symbol=symbol, exit_price=exit_price, exit_at=ensure_utc(exit_at), kind=kind)
        self.tracked.append(t)
        return t

    def evaluate(self, now: datetime, price_fn: Callable[[str, datetime], float]) -> None:
        now = ensure_utc(now)
        for t in self.tracked:
            for label, delta in HORIZONS.items():
                when = t.exit_at + delta
                if label not in t.later_prices and when <= now:
                    t.later_prices[label] = price_fn(t.symbol, when)
            if t.grade is None and "1w" in t.later_prices and t.kind == "STOP":
                week_px = t.later_prices["1w"]
                move = (week_px - t.exit_price) / t.exit_price
                if move <= -0.03:
                    t.grade = StopGrade.GOOD_STOP     # kept falling: stop saved money
                elif move >= 0.03:
                    t.grade = StopGrade.EARLY_STOP    # bounced back: stop was early
                else:
                    t.grade = StopGrade.NEUTRAL
