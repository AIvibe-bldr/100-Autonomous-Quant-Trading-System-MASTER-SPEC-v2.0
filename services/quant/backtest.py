"""Backtest Execution Simulator (MASTER SPEC §25) and backtester.

Naive close-price fills are forbidden (§25).  Fills model spread, slippage,
latency (fill on next bar's open), partial fills against volume, fees and
halts.  The backtester measures the gap between "ideal" close-price P&L and
simulated P&L so live-vs-backtest divergence can be tracked (§25).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from packages.schemas.core import Action, Bar


@dataclass(frozen=True)
class SimFill:
    symbol: str
    side: Action
    qty: float
    price: float
    fees: float
    bar_ts: object
    partial: bool


@dataclass
class MicrostructureSimulator:
    """Deterministic execution model per §25."""

    spread_bps: float = 5.0
    slippage_bps: float = 2.0
    fee_bps: float = 1.0
    max_volume_participation: float = 0.01  # fill at most 1% of bar volume
    halted_symbols: set[str] = field(default_factory=set)

    def fill(self, symbol: str, side: Action, qty: float, next_bar: Bar) -> Optional[SimFill]:
        """Order placed at bar t executes against bar t+1 (latency §25)."""
        if symbol in self.halted_symbols:
            return None  # trading halt: no fill (§25)
        max_qty = next_bar.volume * self.max_volume_participation
        fill_qty = min(qty, max_qty)
        if fill_qty <= 0:
            return None
        half_spread = next_bar.open * self.spread_bps / 20_000
        slip = next_bar.open * self.slippage_bps / 10_000
        if side is Action.BUY:
            px = next_bar.open + half_spread + slip
        else:
            px = max(0.01, next_bar.open - half_spread - slip)
        fees = fill_qty * px * self.fee_bps / 10_000
        return SimFill(symbol=symbol, side=side, qty=fill_qty, price=round(px, 6),
                       fees=round(fees, 6), bar_ts=next_bar.ts, partial=fill_qty < qty)


@dataclass
class BacktestResult:
    trades: int = 0
    wins: int = 0
    total_return: float = 0.0
    ideal_return: float = 0.0          # naive close-price fills, for divergence tracking
    max_drawdown: float = 0.0
    equity_curve: list[float] = field(default_factory=list)
    fees_paid: float = 0.0

    @property
    def win_rate(self) -> float:
        return self.wins / self.trades if self.trades else 0.0

    @property
    def execution_drag(self) -> float:
        """Backtest利益と実約定モデル利益の乖離 (§25)."""
        return self.ideal_return - self.total_return


def run_backtest(bars: list[Bar], entry_signal: list[bool], initial_cash: float = 10_000.0,
                 sim: Optional[MicrostructureSimulator] = None,
                 stop_pct: float = 0.05, target_pct: float = 0.10) -> BacktestResult:
    """Single-symbol long-only backtest with stop/target exits.

    entry_signal[i] refers to bar i (decision made on close of bar i,
    executed on bar i+1's open — no lookahead §10).
    """
    if len(entry_signal) != len(bars):
        raise ValueError("signal length must match bars")
    sim = sim or MicrostructureSimulator()
    result = BacktestResult()
    cash = initial_cash
    ideal_cash = initial_cash
    qty = 0.0
    ideal_qty = 0.0
    entry_px = 0.0
    peak = initial_cash

    for i in range(len(bars) - 1):
        nxt = bars[i + 1]
        holding = qty > 0
        if holding:
            # exit on stop / target using next bar
            hit_stop = nxt.low <= entry_px * (1 - stop_pct)
            hit_target = nxt.high >= entry_px * (1 + target_pct)
            if hit_stop or hit_target:
                f = sim.fill(bars[i].symbol, Action.SELL, qty, nxt)
                if f:
                    exit_px = (entry_px * (1 - stop_pct) if hit_stop
                               else entry_px * (1 + target_pct))
                    # simulated: fill model price bounded by trigger price
                    px = min(f.price, exit_px) if hit_stop else exit_px
                    cash += qty * px - f.fees
                    result.fees_paid += f.fees
                    result.trades += 1
                    if px > entry_px:
                        result.wins += 1
                    qty = 0.0
                    ideal_cash += ideal_qty * exit_px
                    ideal_qty = 0.0
        elif entry_signal[i]:
            f = sim.fill(bars[i].symbol, Action.BUY, cash * 0.95 / nxt.open, nxt)
            if f and f.qty * f.price + f.fees <= cash:
                cash -= f.qty * f.price + f.fees
                result.fees_paid += f.fees
                qty = f.qty
                entry_px = f.price
                ideal_qty = (ideal_cash * 0.95) / nxt.open  # naive close/open fill, no costs
                ideal_cash -= ideal_qty * nxt.open

        equity = cash + qty * nxt.close
        result.equity_curve.append(equity)
        peak = max(peak, equity)
        result.max_drawdown = max(result.max_drawdown, 1 - equity / peak)

    final = result.equity_curve[-1] if result.equity_curve else initial_cash
    ideal_final = ideal_cash + ideal_qty * bars[-1].close
    result.total_return = final / initial_cash - 1
    result.ideal_return = ideal_final / initial_cash - 1
    return result
