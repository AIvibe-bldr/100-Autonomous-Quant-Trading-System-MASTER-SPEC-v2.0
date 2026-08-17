"""News Engine (MASTER SPEC §17-19).

Event driven — never "全銘柄×LLM検索".  Pipeline:
News → Deduplication → Event Clustering → Ticker Mapping → Source Reliability
→ Novelty → Impact → Sentiment/Direction → Decision input.

Source hierarchy (§18): SEC/primary > company IR > reliable wire > major
media > analyst > SNS.  SNS is a sensor only — it can NEVER be the sole
basis of an order (`sns_only` flag blocks it).

Prompt-injection defense (§19): article text is UNTRUSTED DATA.  This engine
never executes anything found in text; it additionally flags directive-like
content so downstream consumers can down-weight it.
"""
from __future__ import annotations

import enum
import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

from packages.common.clock import ensure_utc


class SourceTier(enum.IntEnum):
    """§18 hierarchy — lower value = more trusted."""

    SEC_PRIMARY = 1
    COMPANY_IR = 2
    RELIABLE_WIRE = 3
    MAJOR_MEDIA = 4
    ANALYST = 5
    SNS = 6


RELIABILITY = {SourceTier.SEC_PRIMARY: 1.0, SourceTier.COMPANY_IR: 0.9,
               SourceTier.RELIABLE_WIRE: 0.8, SourceTier.MAJOR_MEDIA: 0.6,
               SourceTier.ANALYST: 0.5, SourceTier.SNS: 0.2}

# §19: external text containing system-directive patterns is flagged, never obeyed
_INJECTION_PATTERNS = [
    r"システム設定を変更", r"この注文を実行", r"ignore (all )?(previous|prior) instructions",
    r"execute (this|the) (order|trade)", r"change (the )?risk (settings|rules)",
    r"api[_ ]?key", r"disregard your instructions",
]


@dataclass(frozen=True)
class NewsItem:
    """Raw external article. `text` is UNTRUSTED DATA (§19)."""

    title: str
    text: str
    url: str
    source: str
    tier: SourceTier
    published_at: datetime
    tickers: tuple[str, ...] = ()


@dataclass
class NewsSignal:
    """Decision input produced from clustered news (§17)."""

    cluster_id: str
    tickers: tuple[str, ...]
    headline: str
    tier: SourceTier
    reliability: float
    novelty: float                 # 1.0 = first time seen, decays with repetition
    impact: float                  # crude keyword-based impact estimate
    direction: float               # -1..+1 (bear..bull)
    sns_only: bool                 # §18: True → may never trigger an order alone
    injection_flagged: bool        # §19: directive-like content detected
    urls: list[str] = field(default_factory=list)
    published_at: Optional[datetime] = None

    @property
    def tradeable(self) -> bool:
        """SNS単独で発注禁止 (§18); flagged text is quarantined (§19)."""
        return not self.sns_only and not self.injection_flagged


_POSITIVE = ["beat", "beats", "record", "upgrade", "approval", "approved", "surge",
             "guidance raised", "buyback", "acquisition", "契約", "上方修正"]
_NEGATIVE = ["miss", "misses", "downgrade", "recall", "investigation", "lawsuit",
             "guidance cut", "bankruptcy", "delisting", "下方修正", "破産"]
_HIGH_IMPACT = ["fda", "merger", "acquisition", "bankruptcy", "sec charges", "halt",
                "guidance", "earnings"]


def _content_hash(item: NewsItem) -> str:
    normalized = re.sub(r"\W+", "", item.title.lower())
    return hashlib.sha256(normalized.encode()).hexdigest()[:16]


def detect_injection(text: str) -> bool:
    lower = text.lower()
    return any(re.search(p, lower) for p in _INJECTION_PATTERNS)


class NewsEngine:
    def __init__(self, novelty_window: timedelta = timedelta(days=3)) -> None:
        # a story only counts as a duplicate while it is still recent; after
        # the window the same headline is news again (e.g. a repeated event)
        self.novelty_window = novelty_window
        self._seen_hashes: dict[str, datetime] = {}
        self._cluster_counts: dict[str, int] = {}

    def process(self, items: list[NewsItem]) -> list[NewsSignal]:
        signals: list[NewsSignal] = []
        clusters: dict[str, list[NewsItem]] = {}

        # 1. dedup (exact content) then 2. cluster by ticker-set + day
        for item in items:
            h = _content_hash(item)
            published = ensure_utc(item.published_at)
            seen_at = self._seen_hashes.get(h)
            if seen_at is not None and abs(published - seen_at) <= self.novelty_window:
                continue
            self._seen_hashes[h] = published
            key = f"{','.join(sorted(item.tickers))}:{published.date()}"
            clusters.setdefault(key, []).append(item)

        for key, cluster in clusters.items():
            # best-tier item represents the cluster (§18 hierarchy)
            best = min(cluster, key=lambda i: i.tier)
            self._cluster_counts[key] = self._cluster_counts.get(key, 0) + 1
            novelty = 1.0 / self._cluster_counts[key]

            text_all = " ".join(i.title + " " + i.text for i in cluster).lower()
            pos = sum(text_all.count(w) for w in _POSITIVE)
            neg = sum(text_all.count(w) for w in _NEGATIVE)
            direction = 0.0 if pos + neg == 0 else (pos - neg) / (pos + neg)
            impact = min(1.0, 0.2 + 0.2 * sum(1 for w in _HIGH_IMPACT if w in text_all))

            signals.append(NewsSignal(
                cluster_id=key, tickers=best.tickers, headline=best.title,
                tier=best.tier, reliability=RELIABILITY[best.tier], novelty=novelty,
                impact=impact, direction=direction,
                sns_only=all(i.tier is SourceTier.SNS for i in cluster),
                injection_flagged=any(detect_injection(i.title + " " + i.text)
                                      for i in cluster),
                urls=[i.url for i in cluster], published_at=ensure_utc(best.published_at)))
        return signals

    def top_news(self, signals: list[NewsSignal], n: int = 3) -> list[NewsSignal]:
        """Important News Top N for the UI (§91), ranked by contribution proxy."""
        return sorted(signals, key=lambda s: s.reliability * s.impact * s.novelty,
                      reverse=True)[:n]
