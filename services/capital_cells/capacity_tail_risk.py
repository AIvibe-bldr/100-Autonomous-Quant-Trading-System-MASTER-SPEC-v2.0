"""Capacity / Tail Risk (docs/capital_cell_architecture.md §20, §22, §23,
§41 priority 11).

Three separate concerns the doc groups together because they share one
theme — an uncertain number must say so, not pretend to be exact:

- **§20 Tail Risk**: Expected Shortfall and CVaR are the SAME metric under
  two historical names — `TailRiskMetrics` computes VaR and ES together and
  exposes `.cvar` as a plain alias for `.expected_shortfall`, so nothing
  downstream can double-count them as two independent risk indicators.
- **§22 Risk Contribution**: never a bare point estimate. `RangeEstimate`
  (low/base/high/confidence) from `services.capital_cells.estimation` is
  reused directly; `safe_side_risk_contribution` picks the conservative
  (highest-risk) bound when confidence is too low to trust the point value.
- **§23 Capacity**: same shape again — capacity_low/base/high/confidence,
  never a single "Estimated Capacity" number. `capacity_constrained_
  allocation` caps a request at the low bound when confidence is too low,
  mirroring the same "uncertain -> use the safe side" rule as §22.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from services.capital_cells.estimation import (
    InsufficientDataError,
    RangeEstimate,
    require_sample_size,
)

TAIL_RISK_METHOD_VERSION = "HISTORICAL_VAR_ES_V1"
RISK_CONTRIBUTION_METHOD_VERSION = "COVARIANCE_BLOCK_RANGE_V1"
CAPACITY_METHOD_VERSION = "ADV_PARTICIPATION_RATE_V1"


# ---------------------------------------------------------------------------
# §20: Tail Risk (VaR / Expected Shortfall == CVaR)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TailRiskMetrics:
    """VaR and Expected Shortfall — the same metric family as CVaR under a
    different name (§20) — computed together from one historical sample so
    nothing downstream treats them as two independent signals. Both are
    reported as positive loss magnitudes."""

    confidence_level: float   # e.g. 0.95
    var: float
    expected_shortfall: float
    sample_size: int
    method_version: str
    as_of: datetime

    @property
    def cvar(self) -> float:
        """Alias making 'ES and CVaR are the same number' unavoidable at
        the API level, not just in a comment (§20)."""
        return self.expected_shortfall


def compute_tail_risk(returns: list[float], confidence_level: float, as_of: datetime,
                      min_sample_size: int = 20) -> TailRiskMetrics:
    """Historical VaR/ES: sort returns ascending (worst first), take the
    worst `(1-confidence_level)` fraction as the tail. VaR is the boundary
    of that tail; ES is the tail's mean — always at least as severe as VaR,
    by construction."""
    require_sample_size(len(returns), min_sample_size, "tail risk")
    if not (0.0 < confidence_level < 1.0):
        raise ValueError(f"confidence_level must be in (0,1), got {confidence_level}")

    n = len(returns)
    ordered = sorted(returns)
    tail_count = max(1, round((1.0 - confidence_level) * n))
    tail = ordered[:tail_count]
    var = -tail[-1]
    expected_shortfall = -(sum(tail) / len(tail))
    return TailRiskMetrics(
        confidence_level=confidence_level, var=var, expected_shortfall=expected_shortfall,
        sample_size=n, method_version=TAIL_RISK_METHOD_VERSION, as_of=as_of)


# ---------------------------------------------------------------------------
# §22: Risk Contribution with a confidence range
# ---------------------------------------------------------------------------

def _risk_contribution_point(cell_pnl: list[float], portfolio_pnl: list[float]) -> float:
    """Euler/marginal contribution to portfolio variance: Cov(cell,
    portfolio) / Var(portfolio). Sums to ~1.0 across cells whose P&L sums to
    the portfolio's P&L (true here: the Master Ledger's real P&L is, by the
    §4 reconciliation invariant, the sum of the cells' allocated real P&L)."""
    n = len(cell_pnl)
    mc = sum(cell_pnl) / n
    mp = sum(portfolio_pnl) / n
    cov = sum((c - mc) * (p - mp) for c, p in zip(cell_pnl, portfolio_pnl)) / n
    var_p = sum((p - mp) ** 2 for p in portfolio_pnl) / n
    if var_p <= 1e-15:
        return 0.0
    return cov / var_p


def estimate_risk_contribution(cell_pnl: list[float], portfolio_pnl: list[float],
                               as_of: datetime, n_blocks: int = 4,
                               min_sample_size: int = 20) -> RangeEstimate:
    """§22: risk contribution carries model error too. The point estimate
    is the full-sample Euler contribution; low/high come from recomputing
    it on `n_blocks` contiguous sub-periods — a cheap, dependency-free
    stand-in for a proper bootstrap that still answers the question that
    matters here (how much does this number move if the sample had been
    slightly different), never collapsed to a single trusted value."""
    if len(cell_pnl) != len(portfolio_pnl):
        raise ValueError(
            f"cell_pnl length {len(cell_pnl)} != portfolio_pnl length {len(portfolio_pnl)}")
    require_sample_size(len(cell_pnl), min_sample_size, "risk contribution")

    base = _risk_contribution_point(cell_pnl, portfolio_pnl)
    block_size = len(cell_pnl) // n_blocks
    if block_size < 5:
        # too thin to sub-sample meaningfully -- report the point estimate
        # with reduced confidence rather than a fabricated range.
        return RangeEstimate(low=base, base=base, high=base, confidence=0.5,
                             sample_size=len(cell_pnl),
                             method_version=RISK_CONTRIBUTION_METHOD_VERSION, as_of=as_of)

    block_values = [base]
    for i in range(n_blocks):
        start = i * block_size
        end = start + block_size if i < n_blocks - 1 else len(cell_pnl)
        block_values.append(_risk_contribution_point(cell_pnl[start:end], portfolio_pnl[start:end]))
    low, high = min(block_values), max(block_values)
    spread = high - low
    confidence = max(0.0, 1.0 - min(1.0, spread))
    return RangeEstimate(low=low, base=base, high=high, confidence=confidence,
                         sample_size=len(cell_pnl),
                         method_version=RISK_CONTRIBUTION_METHOD_VERSION, as_of=as_of)


def safe_side_risk_contribution(estimate: RangeEstimate, min_confidence: float = 0.6) -> float:
    """§22: 'when precision is low, use the safe-side Allocation bound.'
    Below `min_confidence`, assume the cell contributes as much risk as the
    upper bound suggests, rather than trusting the (possibly understated)
    point estimate."""
    return estimate.base if estimate.confidence >= min_confidence else estimate.high


# ---------------------------------------------------------------------------
# §23: Capacity uncertainty
# ---------------------------------------------------------------------------

def estimate_capacity(adv_dollars_by_symbol: dict[str, float], as_of: datetime,
                      participation_rate_low: float = 0.01,
                      participation_rate_base: float = 0.02,
                      participation_rate_high: float = 0.03,
                      sample_size: int = 0, confidence: float = 1.0) -> RangeEstimate:
    """Capacity = capital deployable without unacceptable market impact,
    estimated as a range (§23) via three assumed participation rates of
    each symbol's dollar ADV rather than one point figure."""
    if not adv_dollars_by_symbol:
        raise InsufficientDataError("no symbols to estimate capacity for")
    if not (0.0 <= participation_rate_low <= participation_rate_base <= participation_rate_high):
        raise ValueError("participation rates must satisfy low <= base <= high")
    total_adv = sum(adv_dollars_by_symbol.values())
    return RangeEstimate(
        low=total_adv * participation_rate_low, base=total_adv * participation_rate_base,
        high=total_adv * participation_rate_high, confidence=confidence,
        sample_size=sample_size, method_version=CAPACITY_METHOD_VERSION, as_of=as_of)


def capacity_constrained_allocation(requested_capital: float, capacity: RangeEstimate,
                                    min_confidence: float = 0.5) -> float:
    """§23: 'Capital Allocation must consider uncertainty, not just Base.'
    Below `min_confidence`, cap the request at the LOW bound; otherwise the
    BASE bound. Never the high bound — capacity is a downside risk (too
    little liquidity to exit), so the safe side here is the smaller number,
    the mirror image of §22 where the safe side is the larger one."""
    ceiling = capacity.base if capacity.confidence >= min_confidence else capacity.low
    return min(requested_capital, ceiling)
