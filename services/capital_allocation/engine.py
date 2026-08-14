"""Capital Allocation Engine (MASTER SPEC §36).

Multiple sized candidates compete for limited settled cash.  Candidates are
ranked by calibrated edge; allocation stops when cash or the absolute
exposure ceiling (Total Exposure <= 100%, INV-11) is reached.
"""
from __future__ import annotations

from dataclasses import dataclass

from packages.common.risk_config import RiskConfig
from packages.schemas.core import SizedProposal


@dataclass(frozen=True)
class AllocationResult:
    accepted: list[SizedProposal]
    skipped: list[tuple[SizedProposal, str]]


@dataclass
class CapitalAllocationEngine:
    config: RiskConfig

    def allocate(self, candidates: list[SizedProposal], equity: float,
                 settled_cash: float, current_exposure_notional: float) -> AllocationResult:
        # rank: calibrated confidence × midpoint of expected return range
        def edge(sp: SizedProposal) -> float:
            lo, hi = sp.proposal.decision.expected_return_range
            return sp.calibrated_confidence * (lo + hi) / 2

        ranked = sorted(candidates, key=edge, reverse=True)
        max_total = equity * self.config.max_total_exposure_pct * self.config.exposure_cap_stage

        accepted: list[SizedProposal] = []
        skipped: list[tuple[SizedProposal, str]] = []
        cash_left = settled_cash
        exposure = current_exposure_notional
        for sp in ranked:
            if sp.notional > cash_left + 1e-9:
                skipped.append((sp, "insufficient settled cash"))
                continue
            if exposure + sp.notional > max_total + 1e-9:
                skipped.append((sp, f"total exposure cap {max_total:.2f} reached (§36)"))
                continue
            accepted.append(sp)
            cash_left -= sp.notional
            exposure += sp.notional
        return AllocationResult(accepted=accepted, skipped=skipped)
