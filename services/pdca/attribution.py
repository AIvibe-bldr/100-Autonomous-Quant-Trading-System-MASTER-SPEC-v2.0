"""P&L Attribution & Profit Quality (MASTER SPEC §54-55).

Decomposes realized P&L into alpha / market beta / entry / exit / slippage /
fees / residual, then scores whether profit came from a repeatable edge,
broad market lift, or a lucky outlier (§55).
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field


@dataclass(frozen=True)
class TradeRecord:
    symbol: str
    qty: float
    entry_price: float
    exit_price: float
    market_return_during_hold: float   # index return over the same period
    slippage_cost: float
    fees: float
    alpha_name: str = "unknown"


@dataclass
class Attribution:
    total_pnl: float
    market_beta_pnl: float
    alpha_pnl: float
    slippage: float
    fees: float

    def as_dict(self) -> dict[str, float]:
        return {"total": self.total_pnl, "market_beta": self.market_beta_pnl,
                "alpha": self.alpha_pnl, "slippage": -self.slippage, "fees": -self.fees}


def attribute(trade: TradeRecord, beta: float = 1.0) -> Attribution:
    gross = trade.qty * (trade.exit_price - trade.entry_price)
    notional = trade.qty * trade.entry_price
    market_pnl = notional * trade.market_return_during_hold * beta
    alpha_pnl = gross - market_pnl
    return Attribution(total_pnl=gross - trade.slippage_cost - trade.fees,
                       market_beta_pnl=market_pnl, alpha_pnl=alpha_pnl,
                       slippage=trade.slippage_cost, fees=trade.fees)


class ProfitSource(str, enum.Enum):
    REPEATABLE_EDGE = "REPEATABLE_EDGE"
    MARKET_LIFT = "MARKET_LIFT"
    LUCKY_OUTLIER = "LUCKY_OUTLIER"
    NO_PROFIT = "NO_PROFIT"


@dataclass
class ProfitQualityScore:
    source: ProfitSource
    score: float                     # 0..1, higher = more repeatable
    detail: str


def profit_quality(attributions: list[Attribution],
                   outlier_share_threshold: float = 0.5) -> ProfitQualityScore:
    """§55: P&Lだけで評価しない — where did the profit come from?"""
    total = sum(a.total_pnl for a in attributions)
    if total <= 0 or not attributions:
        return ProfitQualityScore(source=ProfitSource.NO_PROFIT, score=0.0,
                                  detail="no net profit to grade")
    alpha_total = sum(a.alpha_pnl for a in attributions)
    beta_total = sum(a.market_beta_pnl for a in attributions)
    biggest = max(a.total_pnl for a in attributions)
    if biggest / total >= outlier_share_threshold and len(attributions) > 3:
        return ProfitQualityScore(
            source=ProfitSource.LUCKY_OUTLIER, score=0.2,
            detail=f"single trade contributed {biggest / total:.0%} of profit")
    if alpha_total > beta_total:
        return ProfitQualityScore(
            source=ProfitSource.REPEATABLE_EDGE,
            score=min(1.0, 0.5 + alpha_total / (abs(total) + 1e-9) / 2),
            detail=f"alpha {alpha_total:.2f} vs beta {beta_total:.2f}")
    return ProfitQualityScore(
        source=ProfitSource.MARKET_LIFT, score=0.4,
        detail=f"market beta {beta_total:.2f} dominates alpha {alpha_total:.2f}")
