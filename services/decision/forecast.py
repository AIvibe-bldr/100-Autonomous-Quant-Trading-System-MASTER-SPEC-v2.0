"""Forecast Tracker (MASTER SPEC §32).

Human-facing price forecasts are graded too.  Point forecasts are forbidden:
each forecast is Bear/Base/Bull ranges with probabilities over fixed
horizons (1d/1w/1m/3m/6m).  Outcomes feed calibration (§31).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

from packages.common.clock import ensure_utc
from packages.schemas.core import ScenarioCase

HORIZONS: dict[str, timedelta] = {
    "1d": timedelta(days=1), "1w": timedelta(weeks=1), "1m": timedelta(days=30),
    "3m": timedelta(days=91), "6m": timedelta(days=182),
}


@dataclass
class Forecast:
    symbol: str
    made_at: datetime
    horizon: str
    price_at_forecast: float
    bear: ScenarioCase
    base: ScenarioCase
    bull: ScenarioCase
    model: str = "unknown"
    resolved: bool = False
    actual_price: Optional[float] = None
    realized_case: Optional[str] = None

    def __post_init__(self) -> None:
        if self.horizon not in HORIZONS:
            raise ValueError(f"horizon must be one of {sorted(HORIZONS)} (§32)")
        total = self.bear.probability + self.base.probability + self.bull.probability
        if not (0.99 <= total <= 1.01):
            raise ValueError("scenario probabilities must sum to 1.0 (§32: 一点予測禁止)")

    @property
    def due_at(self) -> datetime:
        return ensure_utc(self.made_at) + HORIZONS[self.horizon]


class ForecastTracker:
    def __init__(self) -> None:
        self.forecasts: list[Forecast] = []

    def record(self, forecast: Forecast) -> None:
        self.forecasts.append(forecast)

    def resolve_due(self, now: datetime, price_fn) -> int:
        """Grade all matured forecasts: which scenario band did price land in?"""
        now = ensure_utc(now)
        resolved = 0
        for f in self.forecasts:
            if f.resolved or f.due_at > now:
                continue
            actual = price_fn(f.symbol, f.due_at)
            f.actual_price = actual
            # nearest scenario target wins the band
            cases = {"bear": f.bear, "base": f.base, "bull": f.bull}
            f.realized_case = min(cases, key=lambda k: abs(cases[k].target_price - actual))
            f.resolved = True
            resolved += 1
        return resolved

    def calibration_report(self) -> dict[str, dict[str, float]]:
        """Predicted probability vs realized frequency per scenario (§31-32)."""
        done = [f for f in self.forecasts if f.resolved]
        if not done:
            return {}
        report: dict[str, dict[str, float]] = {}
        for case in ("bear", "base", "bull"):
            predicted = sum(getattr(f, case).probability for f in done) / len(done)
            realized = sum(1 for f in done if f.realized_case == case) / len(done)
            report[case] = {"predicted": round(predicted, 4),
                            "realized": round(realized, 4),
                            "gap": round(realized - predicted, 4)}
        return report
