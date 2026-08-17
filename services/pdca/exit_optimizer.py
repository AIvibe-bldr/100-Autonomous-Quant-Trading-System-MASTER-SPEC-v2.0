"""Exit Optimizer (MASTER SPEC §52).

Studies Post-Stop / Post-Profit outcomes per stop type and proposes better
exit methods.  A proposal can NEVER go live directly: it must walk
Research → Backtest → Walk-forward → Shadow → Judge → Promotion, and the
promotion itself is an Experiment (§98) requiring a human decision (§106).
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime

from packages.common.experiments import (
    Experiment,
    ExperimentDecision,
    ExperimentRegistry,
)
from packages.schemas.core import StopType
from services.pdca.post_trade import PostTradeTracker, StopGrade


class ExitProposalStage(str, enum.Enum):
    RESEARCH = "RESEARCH"
    BACKTEST = "BACKTEST"
    WALK_FORWARD = "WALK_FORWARD"
    SHADOW = "SHADOW"
    JUDGED = "JUDGED"
    PROMOTED = "PROMOTED"
    REJECTED = "REJECTED"


_STAGE_ORDER = [ExitProposalStage.RESEARCH, ExitProposalStage.BACKTEST,
                ExitProposalStage.WALK_FORWARD, ExitProposalStage.SHADOW,
                ExitProposalStage.JUDGED, ExitProposalStage.PROMOTED]


@dataclass
class ExitProposal:
    name: str
    stop_type: StopType
    change_description: str
    stage: ExitProposalStage = ExitProposalStage.RESEARCH
    evidence: dict[str, float] = field(default_factory=dict)
    experiment_id: str = ""
    reject_reason: str = ""


class StagePromotionError(RuntimeError):
    pass


class ExitOptimizer:
    def __init__(self, registry: ExperimentRegistry) -> None:
        self.registry = registry
        self.proposals: list[ExitProposal] = []

    # -- research ------------------------------------------------------------
    def analyze_stops(self, tracker: PostTradeTracker) -> dict[str, float]:
        """§52: what do stop outcomes say about our exits?"""
        graded = [t for t in tracker.tracked if t.grade is not None]
        if not graded:
            return {}
        n = len(graded)
        return {
            "graded": float(n),
            "good_rate": sum(1 for t in graded if t.grade is StopGrade.GOOD_STOP) / n,
            "early_rate": sum(1 for t in graded if t.grade is StopGrade.EARLY_STOP) / n,
            "neutral_rate": sum(1 for t in graded if t.grade is StopGrade.NEUTRAL) / n,
        }

    def propose(self, name: str, stop_type: StopType, change_description: str,
                at: datetime) -> ExitProposal:
        p = ExitProposal(name=name, stop_type=stop_type,
                         change_description=change_description)
        p.experiment_id = self.registry.register(Experiment(
            hypothesis=f"exit method '{name}' improves stop quality",
            change=change_description, baseline="current exit rules",
            challenger=name, period_start=at,
            rollback_plan="revert to previous stop configuration version (§99)"))
        self.proposals.append(p)
        return p

    # -- staged promotion (§52: 変更案を即LIVE化禁止) -------------------------
    def advance(self, proposal: ExitProposal, evidence_key: str,
                evidence_value: float) -> ExitProposalStage:
        if proposal.stage in (ExitProposalStage.PROMOTED, ExitProposalStage.REJECTED):
            raise StagePromotionError(f"{proposal.name} already terminal")
        proposal.evidence[evidence_key] = evidence_value
        idx = _STAGE_ORDER.index(proposal.stage)
        nxt = _STAGE_ORDER[idx + 1]
        if nxt is ExitProposalStage.PROMOTED:
            raise StagePromotionError(
                "promotion requires promote() with a human approver (§106)")
        proposal.stage = nxt
        return proposal.stage

    def promote(self, proposal: ExitProposal, decided_by: str,
                result: dict[str, float], at: datetime) -> None:
        if proposal.stage is not ExitProposalStage.JUDGED:
            raise StagePromotionError(
                f"cannot promote from {proposal.stage.value}; full ladder required (§52)")
        self.registry.record_result(proposal.experiment_id, dict(result), at)
        self.registry.decide(proposal.experiment_id, ExperimentDecision.ADOPT, decided_by)
        proposal.stage = ExitProposalStage.PROMOTED

    def reject(self, proposal: ExitProposal, reason: str) -> None:
        proposal.stage = ExitProposalStage.REJECTED
        proposal.reject_reason = reason
        self.registry.record_result(proposal.experiment_id, {"rejected": reason},
                                    self.registry.get(proposal.experiment_id).period_start)
        self.registry.decide(proposal.experiment_id, ExperimentDecision.REJECT,
                             decided_by="optimizer")
