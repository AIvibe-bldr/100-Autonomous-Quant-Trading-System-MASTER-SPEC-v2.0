"""Loss Control Engine (MASTER SPEC §33-34).

最重要 — the exit is designed BEFORE the entry.  A proposal without a
computable stop plan dies here (INV-4).  V1 default is an ATR stop (§34);
the optimal stop type per alpha/regime/volatility is later research (§52).
"""
from __future__ import annotations

from dataclasses import dataclass

from packages.schemas.core import Bar, StopPlan, StopType, TradeProposal
from services.market_data.service import atr


class NoStopPlanError(RuntimeError):
    """Raised when no valid stop can be designed — the trade must not happen."""


@dataclass
class LossControlEngine:
    atr_multiple: float = 2.0
    max_stop_distance_pct: float = 0.15   # stop further than 15% away → refuse
    profit_target_rr: float = 2.0         # reward:risk >= 2 by default

    def plan(self, proposal: TradeProposal, bars: list[Bar], entry_price: float,
             gap_risk_score: float) -> StopPlan:
        if entry_price <= 0:
            raise NoStopPlanError(f"{proposal.symbol}: invalid entry price")
        try:
            a = atr(bars)
        except ValueError as e:
            raise NoStopPlanError(f"{proposal.symbol}: cannot compute ATR ({e})") from e

        stop_price = entry_price - self.atr_multiple * a
        if stop_price <= 0:
            raise NoStopPlanError(f"{proposal.symbol}: ATR stop below zero")
        distance_pct = (entry_price - stop_price) / entry_price
        if distance_pct > self.max_stop_distance_pct:
            raise NoStopPlanError(
                f"{proposal.symbol}: stop distance {distance_pct:.1%} exceeds "
                f"{self.max_stop_distance_pct:.0%} — volatility too high for account size")

        profit_target = entry_price + self.profit_target_rr * (entry_price - stop_price)
        d = proposal.decision
        return StopPlan(
            entry_price=round(entry_price, 6),
            stop_price=round(stop_price, 6),
            stop_type=StopType.ATR,
            stop_reason=f"{self.atr_multiple}x ATR({round(a, 4)}) below entry",
            holding_horizon=d.expected_horizon,
            thesis=d.base_case.description,
            invalidation="; ".join(d.invalidation_conditions) or "thesis invalidated",
            profit_target=round(profit_target, 6),
            gap_risk_score=gap_risk_score,
        )
