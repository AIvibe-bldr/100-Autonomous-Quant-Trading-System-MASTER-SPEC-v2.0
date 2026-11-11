"""Alpaca market data provider (MASTER SPEC §8) — real US-equity daily bars
and latest quotes, behind the same `MarketDataProvider` Protocol as
`MockProvider`.

Data only: this module has no order endpoints and never touches the broker
API (`packages/broker_adapters` owns that). Credentials come from the
environment (`APCA_API_KEY_ID` / `APCA_API_SECRET_KEY`) and are never logged
or embedded in an exception message.

Rules that keep real data honest:
- Nothing is invented. A missing/zero/crossed quote, an empty series or an
  HTTP failure raises `MarketDataUnavailable`; callers fail closed.
- No lookahead. `get_bars(end=...)` returns only bars dated strictly before
  `end`'s calendar date; `get_quote(at=...)` refuses a time that is not "now"
  (the latest-quote endpoint cannot answer "as of" a past moment).
- Bars are split/dividend adjusted (`adjustment=all`). Completed DAILY bars come from the consolidated
  SIP feed (`source="alpaca-sip"`): the free plan allows SIP history older than 15 minutes, and IEX's own
  volume is only ~5% of the market (measured: PFE 2.7M on IEX vs 50M on SIP), which understated liquidity
  and distorted the volume-anomaly signal. If SIP is refused (subscription), bars fall back to IEX
  (`source="alpaca-iex"`) and `bars_feed_notice` says so — never silently.
- Quotes stay on IEX (`source="alpaca-iex"`): the latest SIP quote needs a paid subscription. An IEX quote is
  IEX's own top of book, not the national best bid/offer, so its spread can look far wider than the real
  market's. Every label is carried to the downstream AI.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

from packages.common.clock import ensure_utc
from packages.schemas.core import Bar, Quote
from services.market_data.service import MarketDataUnavailable

DATA_BASE_URL = "https://data.alpaca.markets"
SOURCE = "alpaca-iex"          # quotes, and bars when SIP is not available
SOURCE_SIP = "alpaca-sip"
BARS_FEEDS = ("sip", "iex")
# a bar's `ts` follows MockProvider's convention: the US close, 20:00 UTC
_BAR_CLOSE_HOUR_UTC = 20
_MAX_QUOTE_AGE_FROM_NOW = timedelta(days=1)

HttpGet = Callable[[str, dict[str, str]], dict[str, Any]]


def _urllib_get(url: str, headers: dict[str, str]) -> dict[str, Any]:
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        # status only: the body/URL could echo request details
        raise MarketDataUnavailable(f"Alpaca HTTP {e.code}") from None
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as e:
        raise MarketDataUnavailable(f"Alpaca request failed ({type(e).__name__})") from None


class AlpacaMarketDataProvider:
    def __init__(self, key_id: str, secret_key: str, *, http_get: Optional[HttpGet] = None,
                 now_fn: Optional[Callable[[], datetime]] = None,
                 base_url: str = DATA_BASE_URL, bars_feed: str = "sip") -> None:
        if bars_feed not in BARS_FEEDS:
            raise MarketDataUnavailable(f"bars feed must be one of {BARS_FEEDS}, got {bars_feed!r}")
        if not key_id or not secret_key:
            raise MarketDataUnavailable("Alpaca credentials are not configured")
        self._headers = {"APCA-API-KEY-ID": key_id, "APCA-API-SECRET-KEY": secret_key}
        self._get = http_get or _urllib_get
        self._now = now_fn or (lambda: datetime.now(timezone.utc))
        self._base = base_url
        self._bars_cache: dict[tuple[str, str, int], list[Bar]] = {}
        self._bars_feed = bars_feed
        self.bars_feed_notice: Optional[str] = None      # set when SIP was refused and bars fell back to IEX

    @classmethod
    def from_env(cls, **kw: Any) -> "AlpacaMarketDataProvider":
        kw.setdefault("bars_feed", os.environ.get("QUANT_ALPACA_BARS_FEED", "sip").strip().lower() or "sip")
        return cls(os.environ.get("APCA_API_KEY_ID", ""),
                   os.environ.get("APCA_API_SECRET_KEY", ""), **kw)

    @property
    def bars_feed(self) -> str:
        """The feed the daily bars are coming from right now (after any fallback)."""
        return self._bars_feed

    def _request(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        return self._get(f"{self._base}{path}?{urllib.parse.urlencode(params)}", self._headers)

    def _fetch_raw_bars(self, symbol: str, start: datetime, end: datetime, feed: str) -> list[dict[str, Any]]:
        # `end` is the last day wanted: yesterday. A date-only `end` of TODAY counts as "recent" data, which the
        # free plan refuses for SIP (HTTP 403) — and today's bar is never wanted anyway (no lookahead).
        raw: list[dict[str, Any]] = []
        token: Optional[str] = None
        while True:
            params: dict[str, Any] = {"timeframe": "1Day", "start": start.date().isoformat(),
                                      "end": (end.date() - timedelta(days=1)).isoformat(),
                                      "adjustment": "all", "feed": feed, "limit": 1000}
            if token:
                params["page_token"] = token
            page = self._request(f"/v2/stocks/{symbol}/bars", params)
            raw.extend(page.get("bars") or [])
            token = page.get("next_page_token")
            if not token:
                return raw

    def get_bars(self, symbol: str, end: datetime, days: int) -> list[Bar]:
        end = ensure_utc(end)
        key = (symbol, end.date().isoformat(), days)
        if key in self._bars_cache:
            return list(self._bars_cache[key])
        # ~5 trading days per 7 calendar days, plus slack for holidays
        start = end - timedelta(days=int(days * 7 / 5) + 10)
        try:
            raw = self._fetch_raw_bars(symbol, start, end, self._bars_feed)
        except MarketDataUnavailable as e:
            if self._bars_feed != "sip" or not any(code in str(e) for code in ("HTTP 403", "HTTP 422")):
                raise
            self._bars_feed = "iex"                       # SIP refused: say so, and use the labelled IEX bars
            self.bars_feed_notice = (f"consolidated (SIP) daily bars were refused ({e}); using IEX-only bars, "
                                     f"whose volume is roughly 5% of the market")
            raw = self._fetch_raw_bars(symbol, start, end, "iex")
        source = SOURCE_SIP if self._bars_feed == "sip" else SOURCE
        bars: list[Bar] = []
        for r in raw:
            day = datetime.fromisoformat(r["t"].replace("Z", "+00:00")).date()
            ts = datetime(day.year, day.month, day.day, _BAR_CLOSE_HOUR_UTC, tzinfo=timezone.utc)
            if ts.date() >= end.date():      # no lookahead: only completed earlier days
                continue
            try:
                bars.append(Bar(symbol=symbol, ts=ts, open=r["o"], high=r["h"], low=r["l"],
                                close=r["c"], volume=int(r["v"]),
                                vwap=r.get("vw") or None, source=source))
            except (KeyError, ValueError, TypeError) as e:
                raise MarketDataUnavailable(
                    f"{symbol}: malformed bar from Alpaca ({type(e).__name__})") from None
        bars = sorted(bars, key=lambda b: b.ts)[-days:]
        if not bars:
            raise MarketDataUnavailable(f"{symbol}: no bars returned")
        self._bars_cache[key] = list(bars)
        return bars

    def get_quote(self, symbol: str, at: datetime) -> Quote:
        at = ensure_utc(at)
        if abs(self._now() - at) > _MAX_QUOTE_AGE_FROM_NOW:
            raise MarketDataUnavailable(
                f"{symbol}: cannot serve a quote as of {at.isoformat()} (latest-quote only)")
        q = self._request(f"/v2/stocks/{symbol}/quotes/latest", {"feed": "iex"}).get("quote") or {}
        bid, ask = q.get("bp") or 0, q.get("ap") or 0
        if bid <= 0 or ask <= 0 or ask < bid:       # closed market / no book: never fabricate
            raise MarketDataUnavailable(f"{symbol}: no valid bid/ask (bid={bid}, ask={ask})")
        try:
            ts = datetime.fromisoformat(q["t"].replace("Z", "+00:00"))
        except (KeyError, ValueError) as e:
            raise MarketDataUnavailable(f"{symbol}: quote has no timestamp") from None
        return Quote(symbol=symbol, ts=ensure_utc(ts), bid=bid, ask=ask,
                     bid_size=int(q.get("bs") or 0), ask_size=int(q.get("as") or 0), source=SOURCE)
