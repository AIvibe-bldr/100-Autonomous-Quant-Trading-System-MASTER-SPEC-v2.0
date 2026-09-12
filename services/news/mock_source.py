"""Deterministic Mock News Source (MASTER SPEC §17).

docs/MASTER_SPEC.md's own V1 scope note: "Decision AI / News / Institutional
等の LLM 依存部分は インターフェースのみ 定義し、決定論的な Mock 実装を同梱
する" — `NewsEngine` (services/news/engine.py) has always had that interface;
this is the mock data source it was missing. Same symbol + date -> same
headline, mirroring services.market_data.service.MockProvider's own
hash-seeded determinism, so replay (§62) stays reproducible.
"""
from __future__ import annotations

import hashlib
from datetime import date, datetime

from services.news.engine import NewsItem, SourceTier

_TIERS = list(SourceTier)

_POSITIVE_TEMPLATES = [
    "{symbol} beats quarterly estimates, guidance raised",
    "{symbol} announces buyback program",
    "Analyst upgrades {symbol} on strong demand",
    "{symbol} wins regulatory approval for new product",
]
_NEGATIVE_TEMPLATES = [
    "{symbol} misses quarterly estimates, guidance cut",
    "{symbol} faces investigation over accounting practices",
    "Analyst downgrades {symbol} citing weak outlook",
    "{symbol} recalls product after safety concerns",
]
_NEUTRAL_TEMPLATES = [
    "{symbol} to present at upcoming investor conference",
    "{symbol} appoints new board member",
]


def _seed(symbol: str, at_date: date, salt: str) -> int:
    return int(hashlib.sha256(f"{symbol}:{at_date}:{salt}".encode()).hexdigest()[:8], 16)


class MockNewsSource:
    """Stand-in for a real news feed. Event-driven, not 全銘柄×LLM検索 (§17):
    roughly one symbol in five gets a headline on a given day, matching
    NewsEngine's own design that most days are quiet for most names."""

    def fetch(self, symbols: list[str], at: datetime) -> list[NewsItem]:
        items: list[NewsItem] = []
        day = at.date()
        for symbol in symbols:
            if _seed(symbol, day, "gate") % 5 != 0:
                continue   # quiet day for this symbol
            polarity = _seed(symbol, day, "polarity") % 3   # 0 pos / 1 neg / 2 neutral
            templates = (_POSITIVE_TEMPLATES if polarity == 0
                        else _NEGATIVE_TEMPLATES if polarity == 1 else _NEUTRAL_TEMPLATES)
            headline = templates[_seed(symbol, day, "template") % len(templates)].format(
                symbol=symbol)
            tier = _TIERS[_seed(symbol, day, "tier") % len(_TIERS)]
            items.append(NewsItem(
                title=headline, text=headline,
                url=f"https://mock-news.invalid/{symbol}/{day}",
                source="mock-wire", tier=tier, published_at=at, tickers=(symbol,)))
        return items
