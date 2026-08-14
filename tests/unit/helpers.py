from __future__ import annotations

from datetime import datetime, timezone

from packages.schemas.core import (
    Action,
    DecisionOutput,
    ProposalSource,
    ScenarioCase,
    StopPlan,
    StopType,
    TradeProposal,
)

TS = datetime(2026, 8, 11, 15, 0, tzinfo=timezone.utc)


def make_decision(symbol: str = "AAPL", action: Action = Action.BUY) -> DecisionOutput:
    return DecisionOutput(
        symbol=symbol, action=action, confidence=0.7, expected_horizon="1w",
        expected_return_range=(-0.05, 0.10),
        bull_case=ScenarioCase(description="bull", target_price=110, probability=0.3),
        base_case=ScenarioCase(description="base", target_price=102, probability=0.5),
        bear_case=ScenarioCase(description="bear", target_price=94, probability=0.2),
        key_evidence=["e1"], counter_evidence=["c1"], risk_factors=["r1"],
        invalidation_conditions=["i1"], unknowns=["u1"], decision_version="1.0.0")


def make_proposal(symbol: str = "AAPL") -> TradeProposal:
    return TradeProposal(symbol=symbol, side=Action.BUY, source=ProposalSource.AI,
                         decision=make_decision(symbol), created_at=TS)


def make_stop(entry: float = 100.0, stop: float = 96.0, gap: float = 0.2) -> StopPlan:
    return StopPlan(entry_price=entry, stop_price=stop, stop_type=StopType.ATR,
                    stop_reason="test stop", holding_horizon="1w", thesis="test thesis",
                    invalidation="thesis broken", profit_target=entry + 2 * (entry - stop),
                    gap_risk_score=gap)
