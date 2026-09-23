"""Growth Catalyst Tracker (research-instruction §9).

Classifies already-clustered `NewsSignal`s (services.news.engine) into
growth-catalyst categories — new products, contracts, partnerships, market
entry, regulatory changes, hiring/capacity expansion, new technology,
competitor exits, market-structure shifts. Deliberately reuses
`NewsSignal` rather than re-fetching or re-parsing raw article text: the
clustering, source-reliability weighting, and prompt-injection detection
(§19) already happened in NewsEngine.process(). The `headline` it keyword-
matches is still untrusted third-party text (NewsEngine flags injection
attempts, it does not sanitize them) — only ever pattern-matched here, and
`CatalystEvent.headline` must never be placed into a prompt outside the
`<untrusted_external_data>` wrapper (services/decision/prompts.py renders
catalysts by type only). `sns_only`/`injection_flagged` are carried
straight through, never dropped, so a downstream consumer still knows to
discount or quarantine a catalyst the same way it would the underlying
news signal.
"""
from __future__ import annotations

import enum
import re
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
    # Explicit competitor/rival wording only: a NewsSignal's tickers name the
    # SUBJECT of the headline, so bare "files for bankruptcy"/"shuts down"
    # describes that company's own distress — reading it as a competitor
    # exiting would turn a bankruptcy into a growth catalyst for the very
    # company going bankrupt.
    CatalystType.COMPETITOR_EXIT: ("competitor exits", "rival exits", "competitor shuts down",
                                   "rival shuts down", "competitor files for bankruptcy",
                                   "rival files for bankruptcy"),
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


_NEGATION = re.compile(
    r"\b(loses|lost|cancel\w*|terminat\w*|scrap\w*|delay\w*|suspend\w*|"
    r"withdr\w*|abandon\w*|fails? to|halts?)\b")


class GrowthCatalystTracker:
    def detect(self, signals: list[NewsSignal]) -> list[CatalystEvent]:
        """A growth catalyst is bullish by definition, so bearish news
        (NewsSignal.direction < 0) and headlines that negate the keyword
        ("loses contract", "delays launch", "cancels partnership") are never
        classified as one. Competitor exits keep their own explicit-wording
        check instead of the direction gate: NewsEngine's lexicon scores
        "rival files for bankruptcy" as bearish for the tagged ticker."""
        events: list[CatalystEvent] = []
        for sig in signals:
            headline_lower = sig.headline.lower()
            if _NEGATION.search(headline_lower):
                continue
            for catalyst_type, keywords in _KEYWORDS.items():
                if sig.direction < 0 and catalyst_type is not CatalystType.COMPETITOR_EXIT:
                    continue
                if any(kw in headline_lower for kw in keywords):
                    events.append(CatalystEvent(
                        catalyst_type=catalyst_type, tickers=sig.tickers,
                        headline=sig.headline, cluster_id=sig.cluster_id,
                        sns_only=sig.sns_only, injection_flagged=sig.injection_flagged,
                        detected_at=sig.published_at))
        return events
