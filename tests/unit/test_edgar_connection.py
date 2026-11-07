"""SEC EDGAR (MASTER SPEC §18 tier 1): 8-K filings as SEC_PRIMARY news and
earnings-date ESTIMATES from past 8-K Item 2.02 filings. Fixtures follow the
documented shapes of company_tickers.json and data.sec.gov submissions; real
shapes are checked by scripts/check_edgar.py once a contact is configured."""
from datetime import date, datetime, time, timedelta, timezone

import pytest

from packages.common.clock import FrozenClock
from services.market_data.edgar_client import EdgarClient, EdgarUnavailable
from services.market_data.edgar_earnings import EdgarEarningsEstimator
from services.market_data.event_calendar import EventCalendar, EventType, RiskEvent
from services.market_data.event_composite import CompositeEventSource
from services.market_data.event_source import EventsUnavailable
from services.market_data.universe import UniverseManager, UniverseSymbol
from services.news.composite import CompositeNewsSource
from services.news.edgar_news import EdgarFilingsNewsSource
from services.news.engine import NewsItem, SourceTier
from services.news.source import NewsUnavailable
from tests.conftest import SYMBOLS, build_pipeline

UA = "QuantPaper ops@example.org"
NOW = datetime(2026, 11, 6, 15, 0, tzinfo=timezone.utc)


def _recent(rows):
    keys = ["accessionNumber", "filingDate", "acceptanceDateTime", "form", "items", "primaryDocument"]
    return {"cik": "320193", "filings": {"recent": {k: [r[i] for r in rows] for i, k in enumerate(keys)}}}


ROWS = [
    ("0000320193-26-000100", "2026-11-05", "2026-11-05T21:30:19.000Z", "8-K", "2.02,9.01", "a8k.htm"),
    ("0000320193-26-000099", "2026-11-06", "2026-11-06T22:00:00.000Z", "8-K", "5.02", "future.htm"),  # after NOW
    ("0000320193-26-000090", "2026-10-01", "2026-10-01T20:00:00.000Z", "10-Q", "", "q.htm"),
    ("0000320193-26-000050", "2026-08-01", "2026-08-01T21:00:00.000Z", "8-K", "2.02", "b.htm"),
    ("0000320193-26-000020", "2026-05-01", "2026-05-01T21:00:00.000Z", "8-K", "2.02", "c.htm"),
    ("0000320193-25-000120", "2025-11-06", "2025-11-06T21:00:00.000Z", "8-K", "2.02", "d.htm"),
    ("0000320193-25-000080", "2025-08-01", "2025-08-01T21:00:00.000Z", "8-K", "2.02", "e.htm"),
]


class _Http:
    def __init__(self, fail=False):
        self.urls, self.headers, self.fail = [], [], fail

    def __call__(self, url, headers):
        self.urls.append(url)
        self.headers.append(headers)
        if self.fail:
            raise EdgarUnavailable("SEC HTTP 503")
        if url.endswith("company_tickers.json"):
            return {"0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."}}
        if "CIK0000320193" in url:
            return _recent(ROWS)
        raise EdgarUnavailable("SEC HTTP 404")


def _client(http=None):
    return EdgarClient(UA, http_get=http or _Http(), sleep=lambda s: None)


# ---- client ------------------------------------------------------------------

@pytest.mark.parametrize("ua", ["", "QuantPaper", "ops@example.org", "  "])
def test_a_user_agent_without_a_name_and_contact_is_refused(ua):
    with pytest.raises(EdgarUnavailable):
        EdgarClient(ua)


def test_from_env_fails_closed_when_unset(monkeypatch):
    monkeypatch.delenv("QUANT_SEC_USER_AGENT", raising=False)
    with pytest.raises(EdgarUnavailable):
        EdgarClient.from_env()
    monkeypatch.setenv("QUANT_SEC_USER_AGENT", UA)
    assert EdgarClient.from_env()


def test_every_request_carries_the_user_agent_and_results_are_cached():
    http = _Http()
    c = _client(http)
    assert len(c.recent_filings("AAPL")) == len(ROWS)
    c.recent_filings("AAPL")
    assert all(h["User-Agent"] == UA for h in http.headers)
    assert len(http.urls) == 2                                   # tickers map + one submissions call


def test_unknown_ticker_has_no_filings_and_http_failure_is_unavailable():
    assert _client().recent_filings("ZZZZ") == []
    with pytest.raises(EdgarUnavailable):
        _client(_Http(fail=True)).recent_filings("AAPL")


# ---- news --------------------------------------------------------------------

def test_8k_filings_become_sec_primary_news_with_item_names_and_no_lookahead():
    src = EdgarFilingsNewsSource(_client())
    items = src.fetch(["AAPL"], since=NOW - timedelta(days=3), until=NOW)
    assert len(items) == 1
    it = items[0]
    assert it.tier is SourceTier.SEC_PRIMARY and it.tickers == ("AAPL",)
    assert "Results of Operations" in it.text and "2.02" in it.title
    assert it.url.startswith("https://www.sec.gov/Archives/edgar/data/320193/")
    assert it.published_at <= NOW                                # the 22:00 filing is excluded
    assert all("future.htm" not in i.url for i in items)


def test_edgar_news_failure_is_news_unavailable():
    with pytest.raises(NewsUnavailable):
        EdgarFilingsNewsSource(_client(_Http(fail=True))).fetch(["AAPL"], NOW - timedelta(days=1), NOW)


# ---- earnings estimate ----------------------------------------------------------

def test_next_earnings_is_estimated_from_last_years_item_202_dates():
    est = EdgarEarningsEstimator(_client())
    ev = est.fetch(["AAPL"], date(2026, 10, 20), date(2026, 11, 20))     # not yet reported
    assert len(ev) == 1
    e = ev[0]
    assert e.event_type is EventType.EARNINGS and e.symbol == "AAPL"
    assert e.uncertainty_days >= 5 and "estimated" in e.description.lower()
    assert abs((e.on - date(2026, 11, 5)).days) <= 7


def test_a_cycle_that_has_already_reported_is_not_estimated_again():
    # live regression (PFE): last year's date + 364d = 2026-11-03, but the 8-K
    # Item 2.02 for that quarter was already filed — nothing is upcoming
    est = EdgarEarningsEstimator(_client())
    assert est.fetch(["AAPL"], date(2026, 11, 6), date(2026, 11, 20)) == []


def test_no_estimate_without_history_and_none_inside_a_window_that_misses_it():
    assert EdgarEarningsEstimator(_client()).fetch(["ZZZZ"], date(2026, 11, 6), date(2026, 11, 20)) == []
    assert EdgarEarningsEstimator(_client()).fetch(["AAPL"], date(2026, 12, 20), date(2026, 12, 30)) == []


# ---- calendar uncertainty -----------------------------------------------------

def test_an_uncertain_event_counts_when_its_window_touches_the_horizon():
    cal = EventCalendar()
    cal.add(RiskEvent(EventType.EARNINGS, on=date(2026, 11, 20), symbol="AAPL", uncertainty_days=7))
    assert cal.has_event_before("AAPL", date(2026, 11, 6), horizon_days=7)       # 11/13 reaches it
    assert not cal.has_event_before("AAPL", date(2026, 11, 1), horizon_days=7)   # 11/8 does not


# ---- composites -----------------------------------------------------------------

class _Ok:
    def __init__(self, items):
        self.items = items

    def fetch(self, *a):
        return list(self.items)


class _Bad:
    def fetch(self, *a):
        raise NewsUnavailable("down")


def _n(t):
    return NewsItem(title=t, text="", url="u" + t, source="s", tier=SourceTier.SNS,
                    published_at=NOW, tickers=("AAPL",))


def test_composite_news_survives_one_failing_source_and_records_it():
    comp = CompositeNewsSource([_Ok([_n("a")]), _Bad()])
    assert [i.title for i in comp.fetch(["AAPL"], NOW, NOW)] == ["a"]
    assert len(comp.last_errors) == 1


def test_composite_news_raises_only_when_every_source_fails():
    with pytest.raises(NewsUnavailable):
        CompositeNewsSource([_Bad(), _Bad()]).fetch(["AAPL"], NOW, NOW)


def test_composite_events_survive_one_failing_source():
    class BadEv:
        def fetch(self, *a):
            raise EventsUnavailable("down")

    class OkEv:
        def fetch(self, *a):
            return [RiskEvent(EventType.EX_DIVIDEND, on=date(2026, 11, 12), symbol="AAPL")]

    comp = CompositeEventSource([OkEv(), BadEv()])
    assert len(comp.fetch(["AAPL"], date(2026, 11, 6), date(2026, 11, 20))) == 1
    assert comp.last_errors
    with pytest.raises(EventsUnavailable):
        CompositeEventSource([BadEv()]).fetch(["AAPL"], date(2026, 11, 6), date(2026, 11, 20))


def test_pipeline_reports_a_partially_failed_source_as_a_data_warning():
    clock = FrozenClock(current=datetime.combine(date(2026, 8, 10), time(15, 0), tzinfo=timezone.utc))
    uni = UniverseManager()
    for s in SYMBOLS:
        uni.add(UniverseSymbol(symbol=s, listed_from=date(2015, 1, 1)))
    p = build_pipeline(clock, uni, news_source=CompositeNewsSource([_Ok([]), _Bad()]))
    result = p.run_session(clock.now())
    assert any("partial" in w and "news" in w for w in result.data_warnings)
