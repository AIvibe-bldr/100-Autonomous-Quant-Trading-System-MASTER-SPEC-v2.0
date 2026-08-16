"""Feature Store & Lifecycle (MASTER SPEC §22, §59, §61).

Every feature value carries value / timestamp / source / calculation_version /
data_version so any decision can be reproduced exactly (§22).

Lifecycle (§59): ACTIVE → REDUCED → SHADOW → DORMANT → RETIRED.
"最近役に立たない" alone never retires a feature — demotion walks one step at
a time and RETIRED requires an explicit human-reviewed reason.

Drift monitoring (§61): rolling contribution stats demote degrading features
stepwise (ACTIVE → REDUCED → SHADOW → DORMANT).
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from packages.common.clock import ensure_utc


class FeatureStatus(str, enum.Enum):
    ACTIVE = "ACTIVE"
    REDUCED = "REDUCED"
    SHADOW = "SHADOW"
    DORMANT = "DORMANT"
    RETIRED = "RETIRED"


_DEMOTION_ORDER = [FeatureStatus.ACTIVE, FeatureStatus.REDUCED, FeatureStatus.SHADOW,
                   FeatureStatus.DORMANT]


@dataclass(frozen=True)
class FeatureValue:
    feature: str
    symbol: str
    value: Any
    ts: datetime
    source: str
    calculation_version: str
    data_version: str


@dataclass
class FeatureMeta:
    name: str
    purpose: str
    status: FeatureStatus = FeatureStatus.SHADOW  # new features start in shadow (§23)
    contribution_history: list[float] = field(default_factory=list)
    status_history: list[tuple[FeatureStatus, str]] = field(default_factory=list)
    revival_condition: str = ""
    # index into contribution_history at the last demotion, so re-evaluating
    # without fresh evidence cannot cascade a feature down several levels
    demoted_at_observation: int = 0


class FeatureStore:
    def __init__(self) -> None:
        self._values: list[FeatureValue] = []
        self._meta: dict[str, FeatureMeta] = {}

    # -- registry (§94-95 Feature Center backend) ---------------------------
    def register(self, name: str, purpose: str,
                 status: FeatureStatus = FeatureStatus.SHADOW) -> FeatureMeta:
        meta = FeatureMeta(name=name, purpose=purpose, status=status)
        meta.status_history.append((status, "registered"))
        self._meta[name] = meta
        return meta

    def meta(self, name: str) -> FeatureMeta:
        return self._meta[name]

    def by_status(self, status: FeatureStatus) -> list[FeatureMeta]:
        return [m for m in self._meta.values() if m.status is status]

    # -- values (§22) --------------------------------------------------------
    def put(self, fv: FeatureValue) -> None:
        if fv.feature not in self._meta:
            raise KeyError(f"feature {fv.feature} not registered")
        self._values.append(fv)

    def latest(self, feature: str, symbol: str,
               as_of: Optional[datetime] = None) -> Optional[FeatureValue]:
        candidates = [v for v in self._values
                      if v.feature == feature and v.symbol == symbol
                      and (as_of is None or ensure_utc(v.ts) <= ensure_utc(as_of))]
        return max(candidates, key=lambda v: v.ts) if candidates else None

    # -- lifecycle (§59, §61) ------------------------------------------------
    def record_contribution(self, name: str, contribution: float) -> None:
        self._meta[name].contribution_history.append(contribution)

    def evaluate_drift(self, name: str, window: int = 10,
                       demote_below: float = 0.0) -> FeatureStatus:
        """Stepwise demotion when the rolling mean contribution degrades (§61)."""
        meta = self._meta[name]
        hist = meta.contribution_history[-window:]
        if len(hist) < window or meta.status in (FeatureStatus.DORMANT, FeatureStatus.RETIRED):
            return meta.status
        # one demotion per `window` of NEW observations: repeatedly calling
        # evaluate_drift on the same data must not walk a feature to DORMANT
        fresh = len(meta.contribution_history) - meta.demoted_at_observation
        if fresh < window:
            return meta.status
        mean = sum(hist) / len(hist)
        if mean < demote_below:
            idx = _DEMOTION_ORDER.index(meta.status)
            if idx < len(_DEMOTION_ORDER) - 1:
                new = _DEMOTION_ORDER[idx + 1]
                meta.status = new
                meta.demoted_at_observation = len(meta.contribution_history)
                meta.status_history.append((new, f"drift: mean contribution {mean:.4f}"))
        return meta.status

    def promote(self, name: str, to: FeatureStatus, reason: str) -> None:
        meta = self._meta[name]
        meta.status = to
        meta.status_history.append((to, reason))

    def retire(self, name: str, reason: str, human_approved: bool) -> None:
        """RETIRED is terminal and needs a human decision — recent
        underperformance alone is not enough (§59)."""
        if not human_approved:
            raise PermissionError("retiring a feature requires human approval (§59)")
        self.promote(name, FeatureStatus.RETIRED, f"human-approved: {reason}")

    def revive(self, name: str, reason: str) -> None:
        """Revival re-evaluates DORMANT features on regime/capital change (§60)."""
        meta = self._meta[name]
        if meta.status is FeatureStatus.DORMANT:
            self.promote(name, FeatureStatus.SHADOW, f"revival: {reason}")
