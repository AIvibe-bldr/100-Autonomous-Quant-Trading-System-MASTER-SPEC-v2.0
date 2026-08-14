"""Position Sizing Engine (MASTER SPEC §35, §37).

Size is a deterministic function of the stop plan and portfolio state — the
AI never chooses its own size (§35).  Inputs: equity, stop distance, risk
budget, volatility, liquidity, spread, existing/theme exposure, correlation,
gap risk, calibrated confidence, cost.

Anti-Martingale (§37, INV-10): after a realized loss, the next trade's risk
budget may not exceed the previous trade's — "double down to win it back"
is structurally impossible.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from packages.common.risk_config import RiskConfig
from packages.schemas.core import Quote, SizedProposal, StopPlan, TradeProposal

SIZING_VERSION = "1.0.0"


class SizingRejected(RuntimeError):
    pass


@dataclass
class PortfolioContext:
    equity: float
    settled_cash: float
    existing_exposure: dict[str, float]        # symbol -> notional
    theme_exposure: dict[str, float]           # theme -> notional
    symbol_themes: dict[str, list[str]]
    correlation_to_book: float = 0.0           # max |corr| of candidate vs holdings
    adv_shares: float = 1_000_000              # average daily volume of candidate
    throttle_factor: float = 1.0               # from DrawdownThrottle (§39)


@dataclass
class PositionSizingEngine:
    config: RiskConfig
    _last_risk_amount: Optional[float] = None
    _last_trade_was_loss: bool = False
    history: list[dict] = field(default_factory=list)

    def record_trade_result(self, realized_pnl: float, risk_amount: float) -> None:
        self._last_trade_was_loss = realized_pnl < 0
        self._last_risk_amount = risk_amount

    def size(self, proposal: TradeProposal, stop: StopPlan, quote: Quote,
             ctx: PortfolioContext, calibrated_confidence: float) -> SizedProposal:
        cfg = self.config
        if stop.stop_distance <= 0:
            raise SizingRejected("non-positive stop distance")

        # 1. base risk budget, throttled by drawdown state (§39)
        risk_amount = ctx.equity * cfg.per_trade_risk_budget_pct * ctx.throttle_factor
        # confidence scales risk down, never up (calibrated, §31)
        risk_amount *= max(0.25, min(1.0, calibrated_confidence))

        # 2. anti-martingale cap (§37)
        if self._last_trade_was_loss and self._last_risk_amount is not None:
            if risk_amount > self._last_risk_amount:
                risk_amount = self._last_risk_amount

        # 3. qty from stop distance
        qty = risk_amount / stop.stop_distance

        # 4. hard caps ---------------------------------------------------------
        entry = stop.entry_price
        # position cap (% of equity), net of what we already hold in this symbol
        already_held = ctx.existing_exposure.get(proposal.symbol, 0.0)
        max_notional = max(0.0, ctx.equity * cfg.max_position_pct - already_held)
        # settled cash cap (§14): never spend money we don't have
        max_notional = min(max_notional, ctx.settled_cash)
        # liquidity cap (§41): stay under ADV participation limit
        max_notional = min(max_notional, ctx.adv_shares * cfg.max_adv_participation * entry)
        # theme concentration cap (§38, §90)
        for theme in ctx.symbol_themes.get(proposal.symbol, []):
            room = ctx.equity * cfg.max_theme_exposure_pct - ctx.theme_exposure.get(theme, 0.0)
            max_notional = min(max_notional, max(0.0, room))
        # correlation penalty (§38): NVDA/AMD/AVGO are not diversification
        if ctx.correlation_to_book > cfg.correlation_penalty_threshold:
            max_notional *= cfg.correlation_penalty_factor
        # gap risk (§40): high overnight gap risk shrinks size
        if stop.gap_risk_score > cfg.max_gap_risk_score:
            raise SizingRejected(
                f"gap risk {stop.gap_risk_score:.2f} exceeds cap {cfg.max_gap_risk_score:.2f}")

        if qty * entry > max_notional:
            qty = max_notional / entry
        # spread cost sanity (§35): spread must not eat the edge
        if quote.spread / quote.mid > 0.01:
            qty *= 0.5

        qty = float(int(qty * 10_000) / 10_000)  # fractional shares to 4dp
        notional = qty * entry
        if qty <= 0 or notional < 1.0:
            raise SizingRejected(
                f"{proposal.symbol}: size rounds to zero (caps/liquidity/cash too tight)")

        actual_risk = qty * stop.stop_distance
        self.history.append({"symbol": proposal.symbol, "qty": qty, "risk": actual_risk})
        return SizedProposal(proposal=proposal, stop_plan=stop, qty=qty,
                             risk_amount=round(actual_risk, 6), notional=round(notional, 6),
                             calibrated_confidence=calibrated_confidence,
                             sizing_version=SIZING_VERSION)
