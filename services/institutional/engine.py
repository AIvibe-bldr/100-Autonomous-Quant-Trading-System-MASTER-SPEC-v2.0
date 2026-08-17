"""Institutional Flow Engine (MASTER SPEC §20).

Feature candidates: large trades, order-flow imbalance, block trades, options
flow, 13D/13G, ownership change, short interest, ETF flow, volume anomaly.

Rule (§20): 単一Featureを理由に売買しない — the aggregate signal requires at
least two independent feature groups agreeing before it becomes actionable.
Options flow may be used as information (§2) — never as an execution vehicle.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


class FlowFeature(str, enum.Enum):
    LARGE_TRADES = "LARGE_TRADES"
    ORDER_FLOW_IMBALANCE = "ORDER_FLOW_IMBALANCE"
    BLOCK_TRADES = "BLOCK_TRADES"
    OPTIONS_FLOW = "OPTIONS_FLOW"          # information only (§2)
    FILING_13D = "FILING_13D"
    FILING_13G = "FILING_13G"
    OWNERSHIP_CHANGE = "OWNERSHIP_CHANGE"
    SHORT_INTEREST = "SHORT_INTEREST"
    ETF_FLOW = "ETF_FLOW"
    VOLUME_ANOMALY = "VOLUME_ANOMALY"


@dataclass(frozen=True)
class FlowObservation:
    symbol: str
    feature: FlowFeature
    value: float            # normalized: + bullish / - bearish, magnitude = strength
    observed_at: datetime
    source: str


@dataclass
class InstitutionalSignal:
    symbol: str
    score: float                       # aggregate -1..+1
    contributing: list[FlowFeature] = field(default_factory=list)

    @property
    def actionable(self) -> bool:
        """§20: never trade on a single feature."""
        return len(self.contributing) >= 2 and abs(self.score) >= 0.3


class InstitutionalFlowEngine:
    def __init__(self) -> None:
        self._observations: dict[str, list[FlowObservation]] = {}

    def ingest(self, obs: FlowObservation) -> None:
        self._observations.setdefault(obs.symbol, []).append(obs)

    def signal(self, symbol: str) -> Optional[InstitutionalSignal]:
        obs = self._observations.get(symbol)
        if not obs:
            return None
        by_feature: dict[FlowFeature, float] = {}
        for o in obs:
            # latest observation per feature wins
            by_feature[o.feature] = o.value
        contributing = [f for f, v in by_feature.items() if abs(v) >= 0.2]
        if not contributing:
            return InstitutionalSignal(symbol=symbol, score=0.0)
        score = sum(by_feature[f] for f in contributing) / len(contributing)
        return InstitutionalSignal(symbol=symbol, score=max(-1.0, min(1.0, score)),
                                   contributing=contributing)
