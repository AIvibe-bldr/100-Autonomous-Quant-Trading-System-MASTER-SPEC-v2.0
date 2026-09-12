"""Deterministic Mock Institutional Flow Source (MASTER SPEC §20).

Same rationale as services.news.mock_source: docs/MASTER_SPEC.md's V1 scope
calls for a "決定論的なMock実装" wherever a real external feed doesn't exist
yet. Same symbol + date -> same observations.
"""
from __future__ import annotations

import hashlib
from datetime import date, datetime

from services.institutional.engine import FlowFeature, FlowObservation

_FEATURES = list(FlowFeature)


def _seed(symbol: str, at_date: date, salt: str) -> int:
    return int(hashlib.sha256(f"{symbol}:{at_date}:{salt}".encode()).hexdigest()[:8], 16)


class MockInstitutionalFlowSource:
    """Stand-in for a real order-flow/filings feed. Most symbols see zero
    observations on a given day; a minority see two AGREEING features (same
    direction) so `InstitutionalSignal.actionable` (§20: never trade on a
    single feature) is reachable but not the common case."""

    def fetch(self, symbols: list[str], at: datetime) -> list[FlowObservation]:
        obs: list[FlowObservation] = []
        day = at.date()
        for symbol in symbols:
            gate = _seed(symbol, day, "gate") % 5
            if gate == 0:
                continue   # quiet day
            direction = 1.0 if _seed(symbol, day, "dir") % 2 == 0 else -1.0
            n_features = 2 if gate == 1 else 1   # gate==1: rarer, two-feature day
            feat_a = _FEATURES[_seed(symbol, day, "feat0") % len(_FEATURES)]
            features = [feat_a]
            if n_features == 2:
                feat_b = _FEATURES[_seed(symbol, day, "feat1") % (len(_FEATURES) - 1)]
                if feat_b == feat_a:
                    feat_b = _FEATURES[-1] if feat_a != _FEATURES[-1] else _FEATURES[-2]
                features.append(feat_b)
            for i, feature in enumerate(features):
                magnitude = 0.2 + (_seed(symbol, day, f"mag{i}") % 60) / 100.0   # 0.2..0.79
                obs.append(FlowObservation(
                    symbol=symbol, feature=feature, value=direction * magnitude,
                    observed_at=at, source="mock-flow"))
        return obs
