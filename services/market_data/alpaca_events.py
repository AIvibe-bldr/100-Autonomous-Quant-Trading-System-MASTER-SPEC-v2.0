"""Alpaca corporate-actions source for the Event Calendar (MASTER SPEC §16).

Same account/credentials as the market data provider; data only. What it
covers: ex-dividend dates, splits, mergers, spin-offs. What it does NOT cover:
EARNINGS dates (Alpaca has no such feed) — the prompt says so explicitly, so
a missing earnings date is never mistaken for "no earnings".

Honesty rules as elsewhere: failures raise `EventsUnavailable` (status code
only, never credentials); kinds we do not recognise are skipped; only requested
symbols inside the window are returned."""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import date
from typing import Any, Callable, Optional

from services.market_data.event_calendar import EventType, RiskEvent
from services.market_data.event_source import EventsUnavailable

URL = "https://data.alpaca.markets/v1/corporate-actions"
_TYPES = "cash_dividend,forward_split,reverse_split,cash_merger,stock_merger,spin_off"
HttpGet = Callable[[str, dict[str, str]], dict[str, Any]]


def _urllib_get(url: str, headers: dict[str, str]) -> dict[str, Any]:
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise EventsUnavailable(f"Alpaca events HTTP {e.code}") from None
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as e:
        raise EventsUnavailable(f"Alpaca events request failed ({type(e).__name__})") from None


def _day(row: dict[str, Any], *keys: str) -> Optional[date]:
    for k in keys:
        if row.get(k):
            return date.fromisoformat(str(row[k])[:10])
    return None


class AlpacaCorporateActionsSource:
    def __init__(self, key_id: str, secret_key: str, *, http_get: Optional[HttpGet] = None,
                 base_url: str = URL) -> None:
        if not key_id or not secret_key:
            raise EventsUnavailable("Alpaca credentials are not configured")
        self._headers = {"APCA-API-KEY-ID": key_id, "APCA-API-SECRET-KEY": secret_key}
        self._get = http_get or _urllib_get
        self._base = base_url

    @classmethod
    def from_env(cls, **kw: Any) -> "AlpacaCorporateActionsSource":
        return cls(os.environ.get("APCA_API_KEY_ID", ""),
                   os.environ.get("APCA_API_SECRET_KEY", ""), **kw)

    def fetch(self, symbols: list[str], start: date, end: date) -> list[RiskEvent]:
        params = {"symbols": ",".join(symbols), "types": _TYPES, "limit": 1000,
                  "start": start.isoformat(), "end": end.isoformat()}
        body = self._get(f"{self._base}?{urllib.parse.urlencode(params)}", self._headers)
        wanted = set(symbols)
        events: list[RiskEvent] = []
        for kind, rows in (body.get("corporate_actions") or {}).items():
            for row in rows or []:
                try:
                    events.extend(self._to_events(kind, row, wanted, start, end))
                except (KeyError, ValueError, TypeError):
                    continue                                 # one bad row must not hide the rest
        return sorted(events, key=lambda e: (e.on, e.symbol or ""))

    @staticmethod
    def _to_events(kind: str, row: dict[str, Any], wanted: set[str], start: date,
                   end: date) -> list[RiskEvent]:
        if kind == "cash_dividends":
            on = _day(row, "ex_date")
            what = [(row["symbol"], EventType.EX_DIVIDEND,
                     f"{'special ' if row.get('special') else ''}ex-dividend ${row['rate']}")]
        elif kind in ("forward_splits", "reverse_splits"):
            on = _day(row, "ex_date", "process_date")
            what = [(row["symbol"], EventType.STOCK_SPLIT,
                     f"{'reverse ' if kind == 'reverse_splits' else ''}split "
                     f"{row['new_rate']}-for-{row['old_rate']}")]
        elif kind in ("cash_mergers", "stock_mergers"):
            on = _day(row, "effective_date", "process_date")
            what = [(row["acquiree_symbol"], EventType.MERGER, "merger (acquired)")]
            if row.get("acquirer_symbol"):
                what.append((row["acquirer_symbol"], EventType.MERGER, "merger (acquirer)"))
        elif kind == "spin_offs":
            on = _day(row, "ex_date", "process_date")
            what = [(row.get("source_symbol") or row["new_symbol"], EventType.COMPANY_EVENT,
                     "spin-off")]
        else:
            return []
        if on is None or not (start <= on <= end):
            return []
        return [RiskEvent(t, on=on, symbol=sym, description=desc)
                for sym, t, desc in what if sym in wanted]
