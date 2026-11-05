"""AlpacaMarketDataProvider — no network: an injected `http_get` returns
canned payloads shaped like Alpaca's v2 data API."""
from datetime import datetime, timedelta, timezone

import pytest

from services.market_data.alpaca_provider import SOURCE, AlpacaMarketDataProvider
from services.market_data.service import MarketDataUnavailable

NOW = datetime(2026, 11, 5, 15, 0, tzinfo=timezone.utc)


def _payload(n=40, last_day=None):
    last = last_day or (NOW.date() - timedelta(days=1))
    bars, d = [], last
    while len(bars) < n:
        if d.weekday() < 5:
            bars.append({"t": f"{d.isoformat()}T05:00:00Z", "o": 100.0, "h": 102.0, "l": 99.0,
                         "c": 101.0, "v": 1_000_000, "vw": 100.5})
        d -= timedelta(days=1)
    return {"bars": list(reversed(bars)), "next_page_token": None}


def _prov(handler):
    calls = []

    def get(url, headers):
        calls.append((url, headers))
        return handler(url)
    return AlpacaMarketDataProvider("KID", "SECRET", http_get=get, now_fn=lambda: NOW), calls


def test_bars_are_returned_adjusted_sorted_labelled_and_capped_to_days():
    p, calls = _prov(lambda url: _payload(60))
    bars = p.get_bars("AAPL", NOW, 40)
    assert len(bars) == 40 and [b.ts for b in bars] == sorted(b.ts for b in bars)
    assert all(b.source == SOURCE for b in bars)
    url, headers = calls[0]
    assert "adjustment=all" in url and "feed=iex" in url and "timeframe=1Day" in url
    assert headers["APCA-API-KEY-ID"] == "KID"


def test_no_lookahead_bars_on_or_after_end_date_are_dropped():
    payload = _payload(30, last_day=NOW.date())          # includes "today"
    p, _ = _prov(lambda url: payload)
    bars = p.get_bars("AAPL", NOW, 25)
    assert bars and max(b.ts for b in bars).date() < NOW.date()


def test_empty_series_and_malformed_bar_fail_closed():
    p, _ = _prov(lambda url: {"bars": []})
    with pytest.raises(MarketDataUnavailable):
        p.get_bars("AAPL", NOW, 40)
    bad = _payload(30); bad["bars"][3]["c"] = None
    p2, _ = _prov(lambda url: bad)
    with pytest.raises(MarketDataUnavailable):
        p2.get_bars("AAPL", NOW, 25)


def test_bars_are_cached_per_request_key():
    p, calls = _prov(lambda url: _payload(60))
    p.get_bars("AAPL", NOW, 40); p.get_bars("AAPL", NOW, 40)
    assert len(calls) == 1


def test_quote_valid_and_invalid_cases():
    good = {"quote": {"t": "2026-11-05T14:59:59Z", "bp": 100.0, "ap": 100.1, "bs": 2, "as": 3}}
    p, _ = _prov(lambda url: good)
    q = p.get_quote("AAPL", NOW)
    assert (q.bid, q.ask, q.source) == (100.0, 100.1, SOURCE)
    for bad in ({"quote": {"t": "2026-11-05T14:59:59Z", "bp": 0, "ap": 0}},       # market closed
                {"quote": {"t": "2026-11-05T14:59:59Z", "bp": 101, "ap": 100}},  # crossed
                {}):
        pb, _ = _prov(lambda url, b=bad: b)
        with pytest.raises(MarketDataUnavailable):
            pb.get_quote("AAPL", NOW)


def test_quote_refuses_a_past_as_of_time_instead_of_leaking_the_latest():
    p, _ = _prov(lambda url: {"quote": {"t": "2026-11-05T14:59:59Z", "bp": 1, "ap": 2}})
    with pytest.raises(MarketDataUnavailable):
        p.get_quote("AAPL", NOW - timedelta(days=30))


def test_missing_credentials_and_http_errors_never_leak_secrets(monkeypatch):
    monkeypatch.delenv("APCA_API_KEY_ID", raising=False)
    monkeypatch.delenv("APCA_API_SECRET_KEY", raising=False)
    with pytest.raises(MarketDataUnavailable):
        AlpacaMarketDataProvider.from_env()

    def boom(url, headers):
        raise MarketDataUnavailable("Alpaca HTTP 403")
    p = AlpacaMarketDataProvider("KID", "SECRET-XYZ", http_get=boom, now_fn=lambda: NOW)
    with pytest.raises(MarketDataUnavailable) as e:
        p.get_bars("AAPL", NOW, 40)
    assert "SECRET-XYZ" not in str(e.value)


def test_scanner_and_pipeline_skip_unavailable_symbols_instead_of_crashing():
    from datetime import date, time
    from packages.common.clock import FrozenClock
    from services.market_data.service import MockProvider
    from services.market_data.universe import UniverseManager, UniverseSymbol
    from tests.conftest import SYMBOLS, build_pipeline

    class Flaky(MockProvider):
        def get_bars(self, symbol, end, days):
            if symbol == SYMBOLS[0]:
                raise MarketDataUnavailable("no data")
            return super().get_bars(symbol, end, days)

        def get_quote(self, symbol, at):
            if symbol == SYMBOLS[1]:
                raise MarketDataUnavailable("closed")
            return super().get_quote(symbol, at)

    u = UniverseManager()
    for s in SYMBOLS:
        u.add(UniverseSymbol(symbol=s, listed_from=date(2015, 1, 1)))
    clock = FrozenClock(current=datetime.combine(date(2026, 8, 10), time(15, 0),
                                                 tzinfo=timezone.utc))
    pipe = build_pipeline(clock, u)
    pipe.market_data.provider = Flaky()
    pipe.scanner.md.provider = pipe.market_data.provider
    result = pipe.run_session(clock.now())          # must not raise
    assert pipe.scanner.last_funnel.data_unavailable >= 1
