"""Grades the Decision AI's scenario forecasts (MASTER SPEC §32, §31).

Every validated decision carries Bear/Base/Bull targets with probabilities over a horizon.
Each is recorded as a Forecast; when its horizon has passed, the realized price decides which
scenario band it landed in, and the report compares predicted probability with realized
frequency per scenario. Observation only — it feeds nothing back into sizing or risk."""
from __future__ import annotations

from typing import Any

from services.pdca.observer import DecisionEvent, SessionEndEvent, SessionObserver
from services.decision.forecast import HORIZONS, Forecast, ForecastTracker


class ForecastObserver(SessionObserver):
    name = "forecast"

    def __init__(self, tracker: ForecastTracker | None = None) -> None:
        self.tracker = tracker or ForecastTracker()
        self.skipped = 0                 # decisions whose horizon/probabilities we cannot grade

    def on_decision(self, event: DecisionEvent) -> None:
        d = event.decision
        if event.price is None or d.expected_horizon not in HORIZONS:
            self.skipped += 1
            return
        try:
            self.tracker.record(Forecast(
                symbol=event.symbol, made_at=event.as_of, horizon=d.expected_horizon,
                price_at_forecast=event.price, bear=d.bear_case, base=d.base_case,
                bull=d.bull_case, model=d.decision_version))
        except ValueError:               # probabilities not summing to 1 within tolerance
            self.skipped += 1

    def on_session_end(self, event: SessionEndEvent) -> None:
        self.tracker.resolve_due(event.as_of, event.price_on)

    def summary(self) -> dict[str, Any]:
        done = sum(f.resolved for f in self.tracker.forecasts)
        return {"recorded": len(self.tracker.forecasts), "resolved": done,
                "pending": len(self.tracker.forecasts) - done, "skipped": self.skipped,
                "calibration": self.tracker.calibration_report()}
