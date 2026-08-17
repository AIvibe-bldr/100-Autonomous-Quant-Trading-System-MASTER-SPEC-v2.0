"""Model Disagreement Engine (MASTER SPEC §30).

Compares the directional views of Quant / Alpha / News / Institutional /
Decision AI / Skeptic / Regime and turns the disagreement itself into a risk
feature: high disagreement → lower calibrated confidence → smaller size.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class DisagreementReading:
    symbol: str
    views: dict[str, float]          # source -> direction in [-1, +1]
    mean_direction: float
    disagreement: float              # 0 = unanimous, 1 = maximal split

    @property
    def risk_multiplier(self) -> float:
        """Confidence multiplier for sizing: unanimous 1.0 → fully split 0.5."""
        return 1.0 - 0.5 * self.disagreement


class ModelDisagreementEngine:
    def measure(self, symbol: str, views: dict[str, float]) -> DisagreementReading:
        if not views:
            raise ValueError("no views to compare")
        vals = [max(-1.0, min(1.0, v)) for v in views.values()]
        mean = sum(vals) / len(vals)
        # normalized mean absolute deviation; max possible deviation is 1.0
        # (views split between -1 and +1)
        mad = sum(abs(v - mean) for v in vals) / len(vals)
        return DisagreementReading(symbol=symbol, views=dict(views),
                                   mean_direction=mean,
                                   disagreement=min(1.0, mad))
