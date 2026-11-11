"""Alpaca news source (MASTER SPEC §17) — same account/credentials as the market
data provider (`APCA_API_KEY_ID` / `APCA_API_SECRET_KEY`), data only.

Honesty rules, mirroring alpaca_provider: failures raise `NewsUnavailable`
(status code only, never credentials or URLs); rows that cannot be parsed are
skipped; nothing published after `until` is returned; outlets we have not
classified get the lowest tier (SNS), because an unknown outlet must not
outrank a known one."""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from services.news.engine import NewsItem, SourceTier
from services.news.source import NewsUnavailable

NEWS_URL = "https://data.alpaca.markets/v1beta1/news"
_MAX_ITEMS = 50
HttpGet = Callable[[str, dict[str, str]], dict[str, Any]]

# only outlets we have positively classified; everything else is SNS (lowest)
_TIER_BY_SOURCE = {"benzinga": SourceTier.MAJOR_MEDIA}


def _urllib_get(url: str, headers: dict[str, str]) -> dict[str, Any]:
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise NewsUnavailable(f"Alpaca news HTTP {e.code}") from None
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as e:
        raise NewsUnavailable(f"Alpaca news request failed ({type(e).__name__})") from None


def _parse_time(raw: str) -> datetime:
    return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(timezone.utc)


class AlpacaNewsSource:
    def __init__(self, key_id: str, secret_key: str, *, http_get: Optional[HttpGet] = None,
                 base_url: str = NEWS_URL) -> None:
        if not key_id or not secret_key:
            raise NewsUnavailable("Alpaca credentials are not configured")
        self._headers = {"APCA-API-KEY-ID": key_id, "APCA-API-SECRET-KEY": secret_key}
        self._get = http_get or _urllib_get
        self._base = base_url

    @classmethod
    def from_env(cls, **kw: Any) -> "AlpacaNewsSource":
        return cls(os.environ.get("APCA_API_KEY_ID", ""),
                   os.environ.get("APCA_API_SECRET_KEY", ""), **kw)

    def fetch(self, symbols: list[str], since: datetime, until: datetime) -> list[NewsItem]:
        params = {"symbols": ",".join(symbols), "limit": _MAX_ITEMS, "sort": "desc",
                  "include_content": "false",
                  "start": since.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                  "end": until.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
        body = self._get(f"{self._base}?{urllib.parse.urlencode(params)}", self._headers)
        items: list[NewsItem] = []
        for row in body.get("news", []):
            try:
                published = _parse_time(row["created_at"])
                if published > until:                       # no lookahead, whatever the API did
                    continue
                source = str(row.get("source", "")).lower()
                items.append(NewsItem(
                    title=str(row["headline"]), text=str(row.get("summary", "")),
                    url=str(row["url"]), source=source or "unknown",
                    tier=_TIER_BY_SOURCE.get(source, SourceTier.SNS), published_at=published,
                    tickers=tuple(str(s) for s in row.get("symbols", []))))
            except (KeyError, ValueError, TypeError):
                continue                                    # one bad row must not hide the rest
        return items
