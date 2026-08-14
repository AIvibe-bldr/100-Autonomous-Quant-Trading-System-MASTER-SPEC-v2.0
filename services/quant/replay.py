"""Replay Engine (MASTER SPEC §62).

Replays past sessions using only information available at each point in time
(the mock provider is deterministic per date, and the pipeline's FrozenClock
is advanced day by day).  Used for backtest regression, incident
reproduction and strategy comparison; chaos faults can be injected per day.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Callable, Optional

from packages.common.calendar import TradingCalendar
from services.pipeline import PipelineResult, TradingPipeline


@dataclass
class ReplayDay:
    day: date
    result: PipelineResult
    equity: float
    drawdown: float


@dataclass
class ReplayReport:
    days: list[ReplayDay] = field(default_factory=list)

    @property
    def total_sessions(self) -> int:
        return len(self.days)

    @property
    def sessions_traded(self) -> int:
        return sum(1 for d in self.days if d.result.traded)

    @property
    def final_equity(self) -> float:
        return self.days[-1].equity if self.days else 0.0

    @property
    def max_drawdown(self) -> float:
        return max((d.drawdown for d in self.days), default=0.0)

    @property
    def all_sessions_accounted(self) -> bool:
        """Every session either traded or explained itself (§93)."""
        return all(d.result.traded or d.result.no_trade_reasons for d in self.days)


class ReplayEngine:
    def __init__(self, pipeline: TradingPipeline, calendar: Optional[TradingCalendar] = None,
                 on_day: Optional[Callable[[date, TradingPipeline], None]] = None) -> None:
        self.pipeline = pipeline
        self.calendar = calendar or TradingCalendar()
        self.on_day = on_day  # chaos/fault injection hook per day (§62 chaos testing)

    def run(self, start: date, sessions: int, session_utc_hour: int = 15) -> ReplayReport:
        report = ReplayReport()
        d = start
        clock = self.pipeline.clock
        done = 0
        while done < sessions:
            if not self.calendar.is_trading_day(d):
                d += timedelta(days=1)
                continue
            at = datetime.combine(d, time(session_utc_hour, 0), tzinfo=timezone.utc)
            if hasattr(clock, "current"):
                clock.current = at  # FrozenClock: replay controls time (§62)
            if self.on_day:
                self.on_day(d, self.pipeline)
            result = self.pipeline.run_session(at)
            broker = self.pipeline.execution.broker
            if hasattr(broker, "settle"):
                broker.settle()  # T+1 between sessions (§14)
            prices = {s: self.pipeline.market_data.quote(s, at).mid
                      for s in self.pipeline.ledger.positions}
            snap = self.pipeline.ledger.snapshot(prices)
            report.days.append(ReplayDay(day=d, result=result, equity=snap.equity,
                                         drawdown=snap.drawdown))
            done += 1
            d += timedelta(days=1)
        return report
