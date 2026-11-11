"""A company that reorganised into a holding company has its history under the old CIK.
Links are reviewed data (KNOWN_PREDECESSORS); a reorganisation we have NOT linked is warned about."""
import logging
from datetime import date

import pytest

from services.market_data.edgar_client import KNOWN_PREDECESSORS, EdgarClient, EdgarUnavailable
from services.market_data.edgar_earnings import EdgarEarningsEstimator
from services.market_data.event_calendar import EventType

UA = "QuantPaper ops@example.org"
NEW, OLD = 2115436, 34088
KEYS = ["accessionNumber", "filingDate", "acceptanceDateTime", "form", "items", "primaryDocument"]


def _sub(rows):
    return {"filings": {"recent": {k: [r[i] for r in rows] for i, k in enumerate(KEYS)}}}


def _row(acc, day, form="8-K", items="", doc="x.htm"):
    return (acc, day, f"{day}T21:00:00.000Z", form, items, doc)


NEW_ROWS = [_row("N-2", "2026-10-30", items="2.02,7.01"), _row("N-1", "2026-07-31", items="2.02,7.01"),
            _row("N-0", "2026-07-01", form="8-K12B", items="1.01,2.01"),
            _row("J-1", "2026-09-25", items="8.01,9.01")]               # joint filing, also under OLD
OLD_ROWS = [_row("J-1", "2026-09-25", items="8.01,9.01"),
            _row("O-3", "2026-05-01", items="2.02,7.01"), _row("O-2", "2026-01-30", items="2.02,7.01"),
            _row("O-1", "2025-10-31", items="2.02,7.01"), _row("O-0", "2025-08-01", items="2.02,7.01")]


def _client(predecessors=None, fail_old=False):
    calls = []

    def get(url, headers):
        calls.append(url)
        if url.endswith("company_tickers.json"):
            return {"0": {"cik_str": NEW, "ticker": "XOM", "title": "ExxonMobil Holdings Corp"}}
        if f"CIK{NEW:010d}" in url:
            return _sub(NEW_ROWS)
        if f"CIK{OLD:010d}" in url:
            if fail_old:
                raise EdgarUnavailable("SEC HTTP 503")
            return _sub(OLD_ROWS)
        raise EdgarUnavailable("SEC HTTP 404")
    kw = {} if predecessors is None else {"predecessors": predecessors}
    return EdgarClient(UA, http_get=get, sleep=lambda s: None, **kw), calls


def test_the_shipped_link_is_xom_to_the_old_exxon_cik():
    assert KNOWN_PREDECESSORS == {"XOM": (OLD,)}


def test_history_is_merged_deduplicated_and_newest_first():
    c, _ = _client()
    fs = c.recent_filings("XOM")
    assert [f.accession for f in fs] == ["N-2", "J-1", "N-1", "N-0", "O-3", "O-2", "O-1", "O-0"]
    assert len({f.accession for f in fs}) == len(fs)                         # joint filing once
    assert next(f for f in fs if f.accession == "J-1").cik == NEW           # own CIK wins the tie


def test_without_a_link_only_the_new_history_is_seen():
    c, _ = _client(predecessors={})
    assert [f.accession for f in c.recent_filings("XOM")] == ["N-2", "J-1", "N-1", "N-0"]


def test_each_cik_is_fetched_once():
    c, calls = _client()
    c.recent_filings("XOM")
    c.recent_filings("XOM")
    c.unlinked_reorganisation("XOM")
    assert sum(f"CIK{OLD:010d}" in u for u in calls) == 1 and sum(f"CIK{NEW:010d}" in u for u in calls) == 1


def test_a_failing_predecessor_fetch_raises_instead_of_reading_as_no_history():
    c, _ = _client(fail_old=True)
    with pytest.raises(EdgarUnavailable):
        c.recent_filings("XOM")


def test_linked_history_gives_the_normal_year_over_year_estimate_not_the_wide_fallback():
    c, _ = _client()
    (ev,) = EdgarEarningsEstimator(c).fetch(["XOM"], date(2026, 11, 8), date(2027, 2, 15))
    assert ev.event_type is EventType.EARNINGS and ev.on == date(2027, 1, 29)   # 2026-01-30 + 364d
    assert ev.uncertainty_days == 7


def test_without_the_link_the_estimate_falls_back_to_the_wide_quarterly_one():
    c, _ = _client(predecessors={})
    (ev,) = EdgarEarningsEstimator(c).fetch(["XOM"], date(2026, 11, 8), date(2027, 2, 15))
    assert ev.uncertainty_days == 14


def test_an_unlinked_reorganisation_is_detected_and_warned_about(caplog):
    c, _ = _client(predecessors={})
    notice = c.unlinked_reorganisation("XOM")
    assert notice.form == "8-K12B" and notice.filing_date == date(2026, 7, 1)
    with caplog.at_level(logging.WARNING, logger="quant.edgar"):
        EdgarEarningsEstimator(c).fetch(["XOM"], date(2026, 11, 8), date(2027, 2, 15))
    assert "8-K12B" in caplog.text and "no predecessor CIK is linked" in caplog.text


def test_a_linked_reorganisation_does_not_warn(caplog):
    c, _ = _client()
    assert c.unlinked_reorganisation("XOM") is None
    with caplog.at_level(logging.WARNING, logger="quant.edgar"):
        EdgarEarningsEstimator(c).fetch(["XOM"], date(2026, 11, 8), date(2027, 2, 15))
    assert caplog.text == ""
