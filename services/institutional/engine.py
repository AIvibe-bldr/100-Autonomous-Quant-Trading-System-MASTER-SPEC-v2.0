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
from datetime import datetime, timedelta
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

    def signal(self, symbol: str, as_of: Optional[datetime] = None,
               max_age: Optional[timedelta] = None) -> Optional[InstitutionalSignal]:
        """`as_of`/`max_age` restrict the aggregate to observations in
        (as_of - max_age, as_of]. Without a window, every observation ever
        ingested stays "latest for its feature" forever — so single-feature
        days weeks apart would add up to the multi-feature agreement §20
        requires, defeating the rule, and an observation from after `as_of`
        would leak into an earlier query."""
        obs = self._observations.get(symbol, [])
        if as_of is not None:
            obs = [o for o in obs if o.observed_at <= as_of
                   and (max_age is None or o.observed_at > as_of - max_age)]
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
