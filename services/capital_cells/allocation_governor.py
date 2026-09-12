"""Allocation Governor (docs/capital_cell_architecture.md §28-30,
§41 priority 12).

Three deterministic gates on capital — never AI-decided, same principle
§7/§42 already apply to order execution applied here to allocation:

- **§28 Uncertainty-adjusted Edge.** `uncertainty_adjusted_score` never
  ranks cells by raw expected return or recent win rate. It takes a
  `CellAllocationInputs` bundle that structurally cannot be built from a
  win rate alone — net edge is a `RangeEstimate` (confidence interval +
  sample size baked in), plus regime stability, capacity, tail risk,
  correlation and cost are all separate required fields. There is no
  codepath in this module that accepts a bare win-rate number as an
  allocation input.
- **§29 Allocation Reserve.** `allocate_with_reserve` never forces 100% of
  capital into cells. Cash/risk reserves are explicit fractions, and a
  shortage of qualifying opportunities (nothing clears `min_score_to_fund`)
  grows `unallocated_capital` instead of allocating to a cell whose score
  didn't earn it.
- **§30 Cell Creation Governor.** `CellCreationGovernor.review` requires
  all five evidence fields the doc names (distinct hypothesis, opportunity
  availability, independence evidence, expected incremental value,
  operational capacity) and rejects — with reasons — a proposal missing
  any of them. Cell count is never itself a goal.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from services.capital_cells.capacity_tail_risk import TailRiskMetrics
from services.capital_cells.estimation import PointEstimate, RangeEstimate

ALLOCATION_SCORE_METHOD_VERSION = "UNCERTAINTY_ADJUSTED_EDGE_V1"


# ---------------------------------------------------------------------------
# §28: Uncertainty-adjusted Edge
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CellAllocationInputs:
    """Everything §28 requires the Allocation Governor to weigh — deliberately
    with no field a caller could fill in from win rate alone. `net_edge` is
    a `RangeEstimate`, not a float, so confidence interval and sample size
    travel with it structurally rather than as an afterthought."""

    cell_id: str
    net_edge: RangeEstimate          # e.g. marginal edge after cost, per unit capital
    regime_stability: float          # 0-1: how much to trust the current regime read
    capacity: RangeEstimate
    tail_risk: TailRiskMetrics
    correlation_penalty: float       # 0-1: how correlated with the rest of the book
    drawdown: float                  # 0-1: this cell's current drawdown from its HWM
    cost_drag: float                 # estimated total cost, same units as net_edge

    def __post_init__(self) -> None:
        for name in ("regime_stability", "correlation_penalty", "drawdown"):
            v = getattr(self, name)
            if not (0.0 <= v <= 1.0):
                raise ValueError(f"{name} must be in [0,1], got {v}")


def uncertainty_adjusted_score(inputs: CellAllocationInputs, as_of: datetime,
                               low_confidence_threshold: float = 0.6) -> PointEstimate:
    """§28: never Highest Expected Return alone. Uses the conservative
    (low) edge bound when confidence is thin rather than the optimistic
    base estimate; penalizes by correlation, drawdown and tail risk;
    scales by regime stability. Confidence of the resulting score is the
    weakest of its inputs' confidences — the whole judgment is only as
    trustworthy as its least-certain ingredient."""
    edge = (inputs.net_edge.low if inputs.net_edge.confidence < low_confidence_threshold
           else inputs.net_edge.base)
    net_of_cost = edge - inputs.cost_drag

    # `quality` in [0,1]: 1.0 = stable regime, uncorrelated, no drawdown.
    quality = inputs.regime_stability * (1.0 - inputs.correlation_penalty) * \
        (1.0 - inputs.drawdown)
    tail_factor = 1.0 + max(0.0, inputs.tail_risk.expected_shortfall)

    # The penalties must worsen the score whichever side of zero the edge is
    # on. Multiplying a NEGATIVE edge by quality<1 (and dividing by
    # tail_factor>1) would shrink the loss toward zero — ranking the most
    # correlated, deepest-drawdown, fattest-tailed loser ABOVE a clean one.
    # So a negative edge is amplified by the same factors instead.
    if net_of_cost >= 0:
        score = net_of_cost * quality / tail_factor
    else:
        score = net_of_cost * (2.0 - quality) * tail_factor
    confidence = min(inputs.net_edge.confidence, inputs.capacity.confidence,
                     inputs.regime_stability)
    sample_size = min(inputs.net_edge.sample_size, inputs.capacity.sample_size,
                      inputs.tail_risk.sample_size)
    return PointEstimate(value=score, confidence=confidence, sample_size=sample_size,
                         method_version=ALLOCATION_SCORE_METHOD_VERSION, as_of=as_of)


def rank_cells(inputs: list[CellAllocationInputs], as_of: datetime) -> list[tuple[str, PointEstimate]]:
    """Highest uncertainty-adjusted score first. This is the only ranking
    function in this module — there is no separate "rank by expected
    return" or "rank by win rate" entry point (§28)."""
    scored = [(i.cell_id, uncertainty_adjusted_score(i, as_of)) for i in inputs]
    return sorted(scored, key=lambda pair: pair[1].value, reverse=True)


# ---------------------------------------------------------------------------
# §29: Allocation Reserve
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AllocationReserve:
    """§29: capital need not be 100% allocated to cells. Explicit reserve
    categories, not an implicit "whatever's left over"."""

    total_capital: float
    unallocated_capital: float = 0.0
    cash_reserve: float = 0.0
    risk_reserve: float = 0.0

    def __post_init__(self) -> None:
        if self.allocatable_to_cells < -1e-9:
            raise ValueError("reserves exceed total capital")

    @property
    def allocatable_to_cells(self) -> float:
        return self.total_capital - self.unallocated_capital - self.cash_reserve - self.risk_reserve


def allocate_with_reserve(
        total_capital: float, ranked_cells: list[tuple[str, PointEstimate]],
        min_score_to_fund: float = 0.0, cash_reserve_fraction: float = 0.0,
        risk_reserve_fraction: float = 0.0) -> tuple[dict[str, float], AllocationReserve]:
    """Equal-weights capital across every cell whose score clears
    `min_score_to_fund` (§28's ranking already did the hard uncertainty-
    adjustment work; this only decides how many of the top cells get
    funded). §29: cells that don't clear the bar are never funded anyway —
    a shortage of qualifying opportunities grows `unallocated_capital`
    instead of forcing an allocation to something that didn't earn it."""
    if not (0.0 <= cash_reserve_fraction <= 1.0):
        raise ValueError(f"cash_reserve_fraction must be in [0,1], got {cash_reserve_fraction}")
    if not (0.0 <= risk_reserve_fraction <= 1.0):
        raise ValueError(f"risk_reserve_fraction must be in [0,1], got {risk_reserve_fraction}")
    if cash_reserve_fraction + risk_reserve_fraction > 1.0:
        raise ValueError("cash_reserve_fraction + risk_reserve_fraction exceeds 1.0")

    cash_reserve = total_capital * cash_reserve_fraction
    risk_reserve = total_capital * risk_reserve_fraction
    allocatable = total_capital - cash_reserve - risk_reserve

    qualifying = [(cid, score) for cid, score in ranked_cells if score.value >= min_score_to_fund]
    if not qualifying:
        return {}, AllocationReserve(total_capital=total_capital, unallocated_capital=allocatable,
                                     cash_reserve=cash_reserve, risk_reserve=risk_reserve)

    per_cell = allocatable / len(qualifying)
    allocations = {cid: per_cell for cid, _ in qualifying}
    return allocations, AllocationReserve(total_capital=total_capital, unallocated_capital=0.0,
                                          cash_reserve=cash_reserve, risk_reserve=risk_reserve)


# ---------------------------------------------------------------------------
# §30: Cell Creation Governor
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CellCreationProposal:
    """All five pieces of evidence §30 requires. There is no constructor
    path that skips one — a caller (AI or otherwise) proposing a new cell
    must supply every field."""

    proposed_cell_id: str
    alpha_hypothesis: str
    existing_hypotheses: tuple[str, ...]
    opportunity_availability: PointEstimate
    independence_evidence: PointEstimate
    expected_incremental_value: RangeEstimate
    operational_capacity_available: bool


@dataclass(frozen=True)
class CellCreationDecision:
    approved: bool
    reasons: tuple[str, ...]   # non-empty iff not approved


class CellCreationGovernor:
    """§30: 'AI does not create unlimited new cells.' `review` is
    deterministic — no AI call inside it — same no-AI-in-the-gate principle
    the Master Risk Controller already applies to orders (§42). Cell count
    is never itself a target: nothing here rewards proposing more cells."""

    def __init__(self, min_confidence: float = 0.5, min_incremental_value: float = 0.0) -> None:
        self._min_confidence = min_confidence
        self._min_incremental_value = min_incremental_value

    def review(self, proposal: CellCreationProposal) -> CellCreationDecision:
        reasons: list[str] = []

        existing = {h.strip().lower() for h in proposal.existing_hypotheses}
        if proposal.alpha_hypothesis.strip().lower() in existing:
            reasons.append(
                "alpha hypothesis is not distinct from an existing cell's (§30)")

        if proposal.opportunity_availability.confidence < self._min_confidence:
            reasons.append("insufficient confidence in opportunity availability (§30)")

        if proposal.independence_evidence.confidence < self._min_confidence:
            reasons.append("insufficient independence evidence (§30, §19)")

        value_estimate = proposal.expected_incremental_value
        conservative_value = (value_estimate.low if value_estimate.confidence < self._min_confidence
                              else value_estimate.base)
        if conservative_value < self._min_incremental_value:
            reasons.append("expected incremental value is not clearly positive (§30)")

        if not proposal.operational_capacity_available:
            reasons.append("no operational capacity available for a new cell (§30)")

        return CellCreationDecision(approved=not reasons, reasons=tuple(reasons))
