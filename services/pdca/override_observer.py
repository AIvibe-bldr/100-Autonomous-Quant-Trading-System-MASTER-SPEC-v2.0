"""Human vs AI (MASTER SPEC §79): did the human's choice add or destroy value compared with what the AI
said about the same symbol? One week after a human buy, the symbol's move decides: the human's result is
that move if an order was actually placed (else 0), the AI's is that move if it also said BUY (else 0 —
it would not have traded). A persistent human edge becomes a research candidate; nothing here changes
what the system does. Overrides where the AI could not be asked stay unresolved, not guessed."""
from __future__ import annotations

from datetime import timedelta
from typing import Any

from packages.schemas.core import DecisionAction
from services.decision.human_intent import HumanVsAiAnalytics, OverrideRecord
from services.pdca.observer import OverrideEvent, SessionEndEvent, SessionObserver

RESOLVE_AFTER = timedelta(days=7)


class HumanOverrideObserver(SessionObserver):
    name = "human_vs_ai"

    def __init__(self) -> None:
        self.analytics = HumanVsAiAnalytics()
        self._pending: list[OverrideEvent] = []

    def on_human_override(self, e: OverrideEvent) -> None:
        self._pending.append(e)

    def on_session_end(self, e: SessionEndEvent) -> None:
        still = []
        for ov in self._pending:
            if e.as_of - ov.as_of < RESOLVE_AFTER:
                still.append(ov)
                continue
            now_px = e.price_on(ov.symbol, e.as_of)
            if not ov.entry_price or not now_px:
                still.append(ov)                              # price not available yet: try next session
                continue
            move = now_px / ov.entry_price - 1.0
            ai = DecisionAction(ov.ai_action) if ov.ai_action else None
            self.analytics.record_override(OverrideRecord(
                symbol=ov.symbol, at=ov.as_of, ai_action=ai or DecisionAction.WAIT,
                human_action=DecisionAction.BUY,
                ai_pnl=None if ai is None else (move if ai is DecisionAction.BUY else 0.0),
                human_pnl=move if ov.traded else 0.0))
        self._pending = still

    def summary(self) -> dict[str, Any]:
        return {**self.analytics.summary(), "pending": len(self._pending),
                "unit": "fractional move over one week per unit bought (not money)"}
