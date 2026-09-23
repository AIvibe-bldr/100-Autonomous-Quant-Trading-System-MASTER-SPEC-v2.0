"""Growth Catalyst Tracker (research-instruction §9).

Classifies already-clustered `NewsSignal`s (services.news.engine) into
growth-catalyst categories — new products, contracts, partnerships, market
entry, regulatory changes, hiring/capacity expansion, new technology,
competitor exits, market-structure shifts. Deliberately reuses
`NewsSignal` rather than re-fetching or re-parsing raw article text: the
clustering, source-reliability weighting, and prompt-injection detection
(§19) already happened in NewsEngine.process(), so this module never
touches untrusted text directly — it only ever reads a `NewsSignal`'s
already-vetted `headline` and structured fields. `sns_only`/
`injection_flagged` are carried straight through, never dropped, so a
downstream consumer still knows to discount or quarantine a catalyst the
same way it would the underlying news signal.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from services.news.engine import NewsSignal


class CatalystType(str, enum.Enum):
    NEW_PRODUCT = "NEW_PRODUCT"
    MAJOR_CONTRACT = "MAJOR_CONTRACT"
    KEY_PARTNERSHIP = "KEY_PARTNERSHIP"
    NEW_MARKET_ENTRY = "NEW_MARKET_ENTRY"
    REGULATORY_CHANGE = "REGULATORY_CHANGE"
    HIRING_EXPANSION = "HIRING_EXPANSION"
    CAPACITY_EXPANSION = "CAPACITY_EXPANSION"
    NEW_TECHNOLOGY = "NEW_TECHNOLOGY"
    COMPETITOR_EXIT = "COMPETITOR_EXIT"
    MARKET_STRUCTURE_CHANGE = "MARKET_STRUCTURE_CHANGE"


# Keyword lists, not a claim of NLP sophistication — the same crude,
# auditable approach NewsEngine itself already uses for direction/impact
# (_POSITIVE/_NEGATIVE/_HIGH_IMPACT). A headline may match more than one
# category; that is reported as multiple CatalystEvents, not collapsed to
# "the first match wins."
_KEYWORDS: dict[CatalystType, tuple[str, ...]] = {
    CatalystType.NEW_PRODUCT: ("new product", "launches", "unveils", "unveiled", "rolls out"),
    CatalystType.MAJOR_CONTRACT: ("contract", "purchase agreement", "wins order", "multi-year deal"),
    CatalystType.KEY_PARTNERSHIP: ("partnership", "strategic alliance", "collaborat", "teams up"),
    CatalystType.NEW_MARKET_ENTRY: ("enters the", "expansion into", "expands into", "new market"),
    CatalystType.REGULATORY_CHANGE: ("regulatory approval", "fda approval", "regulation change",
                                     "wins approval"),
    CatalystType.HIRING_EXPANSION: ("hiring surge", "headcount growth", "workforce expansion"),
    CatalystType.CAPACITY_EXPANSION: ("new facility", "new plant", "capacity expansion",
                                      "expands production"),
    CatalystType.NEW_TECHNOLOGY: ("breakthrough", "patent granted", "new technology",
                                  "next-generation"),
    CatalystType.COMPETITOR_EXIT: ("competitor exits", "ceases operations", "shuts down",
                                   "files for bankruptcy"),
    CatalystType.MARKET_STRUCTURE_CHANGE: ("industry consolidation", "market disruption",
                                           "structural shift"),
}


@dataclass(frozen=True)
class CatalystEvent:
    catalyst_type: CatalystType
    tickers: tuple[str, ...]
    headline: str
    cluster_id: str
    sns_only: bool
    injection_flagged: bool
    detected_at: Optional[datetime]

    @property
    def tradeable(self) -> bool:
        """Same rule as NewsSignal.tradeable (§18/§19) — a catalyst built
        from an SNS-only or injection-flagged signal inherits the same
        quarantine, not a fresh (weaker) standard of its own."""
        return not self.sns_only and not self.injection_flagged


class GrowthCatalystTracker:
    def detect(self, signals: list[NewsSignal]) -> list[CatalystEvent]:
        events: list[CatalystEvent] = []
        for sig in signals:
            headline_lower = sig.headline.lower()
            for catalyst_type, keywords in _KEYWORDS.items():
                if any(kw in headline_lower for kw in keywords):
                    events.append(CatalystEvent(
                        catalyst_type=catalyst_type, tickers=sig.tickers,
                        headline=sig.headline, cluster_id=sig.cluster_id,
                        sns_only=sig.sns_only, injection_flagged=sig.injection_flagged,
                        detected_at=sig.published_at))
        return events
