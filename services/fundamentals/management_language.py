"""Management Language Tracker (research-instruction §7-8).

Classifies earnings-call/IR-statement text via fixed keyword lists (§4's
"Python arithmetic, not an LLM guess" ethos extended to text: this is a
deterministic keyword counter, not an LLM sentiment call — and it also
means raw transcript text never has to reach a prompt at all, unlike
`services.news`'s `<untrusted_external_data>` handling, since only the
structured verdict below is ever exposed to Decision AI). The per-quarter
score is handed to the same `detect_trend()` already used for financial
metrics (`services.fundamentals.detector`) rather than a second,
duplicate trend-detection implementation.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional, Protocol

from packages.common.clock import ensure_utc
from packages.schemas.fundamentals import (
    LanguageTone,
    ManagementLanguageSignal,
    ManagementStatement,
)
from services.fundamentals.detector import detect_trend
from services.fundamentals.mock_management_source import MockManagementStatementSource

_HEDGING_KEYWORDS = (
    "headwind", "challenging", "cautious", "uncertain", "soft demand",
    "pressure", "temporary setback", "difficult environment", "slowdown",
)
_CONFIDENT_KEYWORDS = (
    "record", "accelerating", "strong momentum", "exceeded expectations",
    "robust demand", "confident", "well positioned", "tailwind",
)


def _confidence_score(text: str) -> int:
    lowered = text.lower()
    confident_hits = sum(lowered.count(k) for k in _CONFIDENT_KEYWORDS)
    hedging_hits = sum(lowered.count(k) for k in _HEDGING_KEYWORDS)
    return confident_hits - hedging_hits


def _classify(score: int) -> LanguageTone:
    if score > 0:
        return LanguageTone.CONFIDENT
    if score < 0:
        return LanguageTone.HEDGING
    return LanguageTone.NEUTRAL


class ManagementStatementSource(Protocol):
    def fetch_history(self, symbol: str, as_of: datetime,
                      quarters: int) -> list[ManagementStatement]: ...


class ManagementLanguageTracker:
    def __init__(self, source: Optional[ManagementStatementSource] = None,
                min_periods: int = 3, lookback_quarters: int = 9) -> None:
        self.source = source or MockManagementStatementSource()
        self.min_periods = min_periods
        self.lookback_quarters = lookback_quarters

    def analyze(self, symbol: str, as_of: datetime) -> ManagementLanguageSignal:
        as_of = ensure_utc(as_of)
        statements = self.source.fetch_history(symbol, as_of, quarters=self.lookback_quarters)
        # §16 Point-in-Time: same future-leakage guard as
        # FundamentalInflectionEngine.analyze() — a statement not yet made
        # public as of `as_of` must not be visible.
        visible = [s for s in statements if s.received_timestamp <= as_of]

        scores = [_confidence_score(s.text) for s in visible]
        trend = detect_trend("confidence_score", scores, min_periods=self.min_periods)
        latest_tone = _classify(scores[-1]) if scores else LanguageTone.NEUTRAL
        return ManagementLanguageSignal(symbol=symbol, latest_tone=latest_tone,
                                        confidence_trend=trend, as_of=as_of,
                                        quarters_observed=len(visible))
