"""A prolific filer's `recent` list is cut off at about a year; older filings sit in dated shards.
Shards are read only when needed and only when they overlap the period we need."""
from datetime import date, timedelta

import pytest

from services.market_data.edgar_client import HISTORY_DAYS, EdgarClient, EdgarUnavailable
from services.market_data.edgar_earnings import _LOOKBACK, EdgarEarningsEstimator

UA = "QuantPaper ops@example.org"
TODAY = date(2026, 11, 8)
CIK = 19617
KEYS = ["accessionNumber", "filingDate", "acceptanceDateTime", "form", "items", "primaryDocument"]


def _cols(rows):
    return {k: [r[i] for r in rows] for i, k in enumerate(KEYS)}


def _row(acc, day, form="8-K", items=""):
    return (acc, day, f"{day}T21:00:00.000Z", form, items, "d.htm")


# newest -> oldest. `recent` reaches back only to 2025-11-07, like JPM's.
RECENT = [_row("R-3", "2026-10-13", items="2.02,9.01"), _row("R-2", "2026-07-14", items="2.02,9.01"),
          _row("R-1", "2026-04-14", items="2.02,9.01"), _row("R-0", "2025-11-07", form="424B2")]
SHARDS = {  # name: (filingFrom, filingTo, rows)
    "S-001": ("2025-10-07", "2025-11-05", [_row("S1-b", "2026-01-13", items="2.02,9.01"),   # a stray newer row
                                           _row("S1-a", "2025-10-14", items="2.02,9.01")]),
    "S-002": ("2025-03-10", "2025-06-30", [_row("S2-b", "2025-07-15", items="2.02,9.01"),    # inside the period
                                           _row("S2-a", "2025-03-11", items="2.02,9.01")]),  # older than the cutoff
    "S-003": ("2024-01-01", "2025-03-09", [_row("S3-a", "2024-10-15", items="2.02,9.01")]),  # wholly too old
}


def _client(fail=None, today=TODAY, shards=SHARDS, files=None, recent=None):
    calls = []

    def get(url, headers):
        name = url.rsplit("/", 1)[-1]
        calls.append(name)
        if name == "company_tickers.json":
            return {"0": {"cik_str": CIK, "ticker": "JPM", "title": "JPMorgan"}}
        if name == f"CIK{CIK:010d}.json":
            listing = files if files is not None else [
                {"name": n, "filingFrom": a, "filingTo": b} for n, (a, b, _) in shards.items()]
            return {"filings": {"recent": _cols(RECENT if recent is None else recent), "files": listing}}
        if name in shards:
            if name == fail:
                raise EdgarUnavailable("SEC HTTP 503")
            return _cols(shards[name][2])
        raise EdgarUnavailable("SEC HTTP 404")
    return EdgarClient(UA, http_get=get, sleep=lambda s: None, predecessors={}, today=lambda: today), calls


def test_history_window_covers_the_estimators_lookback():
    assert HISTORY_DAYS >= _LOOKBACK.days


def test_only_shards_overlapping_the_needed_period_are_read():
    c, calls = _client()
    fs = c.recent_filings("JPM")
    assert [n for n in calls if n.startswith("S-")] == ["S-001", "S-002"]       # S-003 is wholly too old
    cutoff = TODAY - timedelta(days=HISTORY_DAYS)
    assert all(f.filing_date >= cutoff for f in fs if f.accession.startswith("S"))
    assert "S2-a" not in {f.accession for f in fs}                              # older than the cutoff: dropped
    assert {"S1-a", "S2-b"} <= {f.accession for f in fs}
    assert [f.accepted_at for f in fs] == sorted((f.accepted_at for f in fs), reverse=True)


def test_no_shard_is_read_when_recent_already_reaches_the_cutoff():
    long_recent = RECENT + [_row("R-old", "2024-06-01")]          # reaches back before the cutoff
    c, calls = _client(recent=long_recent)
    fs = c.recent_filings("JPM")
    assert not any(n.startswith("S-") for n in calls)
    assert {f.accession for f in fs} == {"R-3", "R-2", "R-1", "R-0", "R-old"}


def test_a_company_with_no_shards_makes_no_shard_requests():
    c, calls = _client(files=[])
    c.recent_filings("JPM")
    assert not any(n.startswith("S-") for n in calls)


def test_a_failing_shard_raises_instead_of_reading_as_less_history():
    c, _ = _client(fail="S-002")
    with pytest.raises(EdgarUnavailable):
        c.recent_filings("JPM")


def test_a_malformed_shard_descriptor_is_skipped_not_fatal():
    c, calls = _client(files=[{"name": "S-001"}, {"name": "S-002", "filingFrom": "2025-03-10",
                                                  "filingTo": "2025-06-30"}])
    fs = c.recent_filings("JPM")
    assert "S2-b" in {f.accession for f in fs} and "S1-a" not in {f.accession for f in fs}


def test_shard_history_gives_the_normal_estimate_instead_of_the_wide_fallback():
    c, _ = _client()
    (ev,) = EdgarEarningsEstimator(c).fetch(["JPM"], TODAY, TODAY + timedelta(days=100))
    assert ev.on == date(2027, 1, 12) and ev.uncertainty_days == 7             # 2026-01-13 + 364d
    c2, _ = _client(files=[])                                                   # without shards: wide fallback
    (ev2,) = EdgarEarningsEstimator(c2).fetch(["JPM"], TODAY, TODAY + timedelta(days=100))
    assert ev2.uncertainty_days == 14


def test_each_shard_is_fetched_once_per_client():
    c, calls = _client()
    c.recent_filings("JPM")
    c.recent_filings("JPM")
    c.unlinked_reorganisation("JPM")
    assert calls.count("S-001") == 1 and calls.count(f"CIK{CIK:010d}.json") == 1
