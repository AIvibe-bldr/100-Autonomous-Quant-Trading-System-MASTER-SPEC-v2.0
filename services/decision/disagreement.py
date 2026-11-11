"""Model Disagreement Engine (MASTER SPEC §30).

Compares the directional views of Quant / Alpha / News / Institutional /
Decision AI / Skeptic / Regime and turns the disagreement itself into a risk
feature: high disagreement → lower calibrated confidence → smaller size.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional, Sequence


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


# -- turning each source into a lean in [-1, +1] (how strongly it favours buying) -------------
# The mappings are judgement calls, kept in one place and covered by tests so they can be
# changed deliberately. Each answers "how much does THIS source favour the trade?".

def quant_lean(score: float) -> float:
    """Scanner score = 20d momentum / 20d daily volatility (typically 0-4)."""
    return math.tanh(score / 2.0)


def decision_lean(confidence: float) -> float:
    """A BUY at confidence 0.5 is a coin flip (neutral); 1.0 is full conviction."""
    return max(-1.0, min(1.0, 2.0 * confidence - 1.0))


def skeptic_lean(severity: float) -> float:
    """Severity 0 = no objection (agrees, +1), 0.5 = neutral, 1 = fatal flaw (-1). The Skeptic
    is built to object, so a low-severity critique counts as agreement, not as opposition."""
    return max(-1.0, min(1.0, 1.0 - 2.0 * severity))


def regime_lean(label: str) -> Optional[float]:
    return {"BULL": 0.5, "BEAR": -0.5}.get(label)


def news_lean(directions: Sequence[float]) -> Optional[float]:
    """Mean keyword-estimated direction of tradeable news (a rough signal; used here only as one
    view among several, never shown to the model as fact)."""
    return sum(directions) / len(directions) if directions else None
