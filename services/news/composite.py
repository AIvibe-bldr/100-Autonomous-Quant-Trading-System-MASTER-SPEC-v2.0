"""Combine several news sources. One failing source must not blind the others;
`last_errors` lets the caller report a partial failure. Raises only when every
source failed (an empty result is then not mistaken for "no news")."""
from __future__ import annotations

from datetime import datetime

from services.news.engine import NewsItem
from services.news.source import NewsSource, NewsUnavailable


class CompositeNewsSource:
    def __init__(self, sources: list[NewsSource]) -> None:
        self._sources = sources
        self.last_errors: list[str] = []

    def fetch(self, symbols: list[str], since: datetime, until: datetime) -> list[NewsItem]:
        self.last_errors = []
        items: list[NewsItem] = []
        for src in self._sources:
            try:
                items.extend(src.fetch(symbols, since, until))
            except NewsUnavailable as e:
                self.last_errors.append(str(e))
        if len(self.last_errors) == len(self._sources):
            raise NewsUnavailable("; ".join(self.last_errors) or "no news sources")
        return items
