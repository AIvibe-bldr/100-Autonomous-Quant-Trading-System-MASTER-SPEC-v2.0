"""Which news may reach the Decision AI for one symbol (MASTER SPEC §17-19).

Pure. Quarantines directive-like text (§19), drops broad "N stocks to buy"
articles whose connection to this symbol is weak, and keeps the few most
credible/novel/impactful items — the model needs the catalyst, not a feed."""
from __future__ import annotations

from services.news.engine import NewsSignal

MAX_TAGGED_SYMBOLS = 6


def select_for_symbol(signals: list[NewsSignal], symbol: str,
                      max_items: int = 3) -> list[NewsSignal]:
    relevant = [s for s in signals
                if symbol in s.tickers and len(s.tickers) <= MAX_TAGGED_SYMBOLS
                and not s.injection_flagged]
    # the more symbols an article is tagged with, the weaker its tie to this one
    relevant.sort(key=lambda s: s.reliability * s.impact * s.novelty / len(s.tickers),
                  reverse=True)
    return relevant[:max_items]
