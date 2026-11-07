"""News source boundary (MASTER SPEC §17): where raw articles come from.

A source only fetches; ranking, dedup and injection screening stay in
`NewsEngine`. Failure is `NewsUnavailable` — never an empty list that would be
read as "no news"."""
from __future__ import annotations

from datetime import datetime
from typing import Protocol

from services.news.engine import NewsItem


class NewsUnavailable(RuntimeError):
    """The news could not be obtained (HTTP failure, missing credentials)."""


class NewsSource(Protocol):
    def fetch(self, symbols: list[str], since: datetime, until: datetime) -> list[NewsItem]:
        """Articles published in [since, until] mentioning any of `symbols`.
        Must never return an article published after `until` (no lookahead)."""
        ...
