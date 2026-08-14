"""Human Intent Analysis & Override tracking (MASTER SPEC §76-79).

「この株を買いたい」→ 即注文禁止 (§76).  The intent first produces a Manual
Analysis (the §77 UI payload); acting on it builds a TradeProposal with
source=HUMAN that flows through the SAME loss-control → sizing → risk
pipeline — the Master Risk Controller cannot be bypassed (§78, INV-8).

Overrides are tracked and compared: AI-only vs human-override performance
(§79); if humans persistently add value, that delta becomes a research
candidate for a new feature.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from packages.schemas.core import (
    Action,
    DecisionOutput,
    ProposalSource,
    ScenarioCase,
    TradeProposal,
)
from services.quant.scanner import ScanResult


class IntentAction(str, enum.Enum):
    WANT_BUY = "WANT_BUY"
    WANT_SELL = "WANT_SELL"


@dataclass(frozen=True)
class HumanIntent:
    symbol: str
    action: IntentAction
    stated_reason: str
    at: datetime


@dataclass
class ManualAnalysis:
    """§77: what the UI shows before any order is possible."""

    symbol: str
    current_price: float
    quant_summary: dict[str, float]
    news_headlines: list[str]
    regime: str
    existing_exposure_notional: float
    forecast: dict[str, ScenarioCase]      # bear / base / bull (§77)
    horizons: tuple[str, ...] = ("1d", "1w", "1m", "3m", "6m")


class HumanIntentAnalyzer:
    """Turns 'I want to buy X' into analysis first, proposal second (§76)."""

    def analyze(self, intent: HumanIntent, scan: ScanResult, regime: str,
                news_headlines: list[str],
                existing_exposure_notional: float) -> ManualAnalysis:
        px = scan.last_close
        return ManualAnalysis(
            symbol=intent.symbol, current_price=px,
            quant_summary={"momentum_20d": scan.momentum_20d,
                           "volatility": scan.volatility,
                           "dollar_volume": scan.dollar_volume},
            news_headlines=news_headlines, regime=regime,
            existing_exposure_notional=existing_exposure_notional,
            forecast={
                "bear": ScenarioCase(description="downside", target_price=px * 0.92,
                                     probability=0.25),
                "base": ScenarioCase(description="base drift", target_price=px * 1.02,
                                     probability=0.50),
                "bull": ScenarioCase(description="upside", target_price=px * 1.12,
                                     probability=0.25),
            })

    def to_proposal(self, intent: HumanIntent, analysis: ManualAnalysis,
                    at: datetime) -> TradeProposal:
        """The human's trade enters the SAME pipeline as AI trades (§78)."""
        decision = DecisionOutput(
            symbol=intent.symbol,
            action=Action.BUY if intent.action is IntentAction.WANT_BUY else Action.SELL,
            confidence=0.5,  # human intent gets neutral confidence; calibration applies
            expected_horizon="1w",
            expected_return_range=(-0.08, 0.12),
            bull_case=analysis.forecast["bull"], base_case=analysis.forecast["base"],
            bear_case=analysis.forecast["bear"],
            key_evidence=[f"human intent: {intent.stated_reason}"],
            counter_evidence=[], risk_factors=["human discretionary trade"],
            invalidation_conditions=["human changes mind", "thesis invalidated"],
            unknowns=["human rationale not modeled"], decision_version="human-1.0")
        return TradeProposal(symbol=intent.symbol, side=decision.action,
                             source=ProposalSource.HUMAN, decision=decision, created_at=at)


@dataclass
class OverrideRecord:
    symbol: str
    at: datetime
    ai_action: Action
    human_action: Action
    ai_pnl: Optional[float] = None       # what the AI's choice would have made
    human_pnl: Optional[float] = None    # what the human's choice made


class HumanVsAiAnalytics:
    """§79: value added / destroyed by human intervention."""

    def __init__(self) -> None:
        self.records: list[OverrideRecord] = []

    def record_override(self, rec: OverrideRecord) -> None:
        self.records.append(rec)

    def summary(self) -> dict[str, Any]:
        resolved = [r for r in self.records
                    if r.ai_pnl is not None and r.human_pnl is not None]
        if not resolved:
            return {"overrides": len(self.records), "resolved": 0,
                    "human_value_added": 0.0, "research_candidate": False}
        delta = sum(r.human_pnl - r.ai_pnl for r in resolved)
        wins = sum(1 for r in resolved if r.human_pnl > r.ai_pnl)
        return {
            "overrides": len(self.records),
            "resolved": len(resolved),
            "human_value_added": round(delta, 6),
            "human_win_rate": wins / len(resolved),
            # §79: persistent human edge becomes a feature-research candidate
            "research_candidate": len(resolved) >= 10 and wins / len(resolved) > 0.6,
        }
