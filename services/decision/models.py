"""Decision AI interface (MASTER SPEC §27-28) + deterministic V1 mock.

The decision model sees a context and returns raw JSON.  `validate_decision`
enforces the fixed schema; malformed output is rejected (§28, §74, INV-14).
No broker access exists anywhere in this module (§27: Brokerアクセス権なし).

External text (news etc.) reaches the model only as UntrustedText (§19).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from pydantic import ValidationError

from packages.schemas.core import DecisionOutput, SkepticOutput
from services.quant.scanner import ScanResult

DECISION_VERSION = "1.0.0"


@dataclass(frozen=True)
class UntrustedText:
    """External content wrapper (§19).  Content is data, never instructions —
    nothing in this codebase parses commands out of it."""

    source: str
    url: str
    text: str


@dataclass
class DecisionContext:
    scan: ScanResult
    regime: str = "UNKNOWN"
    news: list[UntrustedText] = field(default_factory=list)
    portfolio_summary: dict[str, Any] = field(default_factory=dict)
    risk_context: dict[str, Any] = field(default_factory=dict)


class MalformedDecisionError(RuntimeError):
    """Decision output failed schema validation → Reject (§28)."""


def validate_decision(raw: dict[str, Any]) -> DecisionOutput:
    try:
        return DecisionOutput.model_validate(raw)
    except ValidationError as e:
        raise MalformedDecisionError(str(e)) from e


class DecisionModel(Protocol):
    name: str
    version: str

    def decide(self, context: DecisionContext) -> dict[str, Any]:
        """Returns raw JSON-like dict; caller MUST pass it through validate_decision."""
        ...


class MockDecisionModel:
    """Deterministic stand-in for the production decision model (§27 assumes an
    external GPT-class model; V1 runs fully offline)."""

    name = "mock-decision"
    version = DECISION_VERSION

    def decide(self, context: DecisionContext) -> dict[str, Any]:
        s = context.scan
        buy = s.momentum_20d > 0.02 and s.volatility < 0.05
        px = s.last_close
        return {
            "symbol": s.symbol,
            # WAIT when momentum is there but volatility disqualifies it (the
            # setup may return); NO_TRADE when there is simply no edge (§2)
            "action": ("BUY" if buy
                       else "WAIT" if s.momentum_20d > 0.02 else "NO_TRADE"),
            "confidence": min(0.9, 0.5 + s.score / 100),
            "expected_horizon": "1w",
            "expected_return_range": (-0.05, 0.10),
            "bull_case": {"description": f"momentum continuation (+{s.momentum_20d:.1%}/20d)",
                          "target_price": round(px * 1.10, 4), "probability": 0.30},
            "base_case": {"description": "drift with market", "target_price": round(px * 1.02, 4),
                          "probability": 0.50},
            "bear_case": {"description": "momentum reversal", "target_price": round(px * 0.94, 4),
                          "probability": 0.20},
            "key_evidence": [f"20d momentum {s.momentum_20d:.2%}",
                             f"dollar volume {s.dollar_volume:,.0f}"],
            "counter_evidence": ["momentum is a crowded factor"],
            "risk_factors": ["gap risk", "regime shift"],
            "invalidation_conditions": ["close below 20d low", "volume collapse"],
            "unknowns": ["earnings timing not checked in V1"],
            "decision_version": DECISION_VERSION,
        }


class MockSkepticModel:
    """Deterministic skeptic (§29): argues against the trade; different
    'model family' from the decision mock by construction."""

    name = "mock-skeptic"
    version = DECISION_VERSION

    def critique(self, decision: DecisionOutput, context: DecisionContext) -> SkepticOutput:
        objections: list[str] = []
        severity = 0.2
        if context.scan.volatility > 0.04:
            objections.append(f"volatility {context.scan.volatility:.2%} is elevated")
            severity += 0.2
        if context.scan.momentum_20d > 0.30:
            objections.append("momentum this extreme mean-reverts")
            severity += 0.3
        if not objections:
            objections.append("no strong objection; base rates still apply")
        return SkepticOutput(proposal_symbol=decision.symbol, objections=objections,
                             severity=min(1.0, severity),
                             recommends_veto=severity >= 0.7, model_family="mock-skeptic-family")


@dataclass
class CalibrationTracker:
    """Confidence calibration (§31): predicted vs actual, never trust raw AI
    confidence.  With no history, confidence is shrunk toward 0.5."""

    records: list[tuple[float, bool]] = field(default_factory=list)

    def record(self, predicted: float, outcome_win: bool) -> None:
        self.records.append((predicted, outcome_win))

    def calibrate(self, predicted: float) -> float:
        if len(self.records) < 20:
            return 0.5 + (predicted - 0.5) * 0.5  # shrink until we have evidence
        wins = [o for p, o in self.records if abs(p - predicted) < 0.15]
        if not wins:
            return 0.5
        hit_rate = sum(wins) / len(wins)
        return max(0.0, min(1.0, hit_rate))
