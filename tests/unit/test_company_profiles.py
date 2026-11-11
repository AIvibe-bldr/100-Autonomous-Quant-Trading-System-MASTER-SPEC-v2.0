"""Who a ticker is, for the dashboard: the hand-written table first, the SEC's own record for anything else.
Display only — nothing here reaches trading."""
import dataclasses
import json

import pytest

from packages.common.durable_store import DurableSessionStore
from packages.common.environment import Environment
from packages.common.symbol_profiles import (SIC_CATEGORY_JA, SYMBOL_PROFILES, category_ja_from_sic,
                                             profile_from_industry, profile_of)
from services.decision.intent_inbox import IntentInbox
from services.market_data.edgar_client import EdgarClient, EdgarUnavailable
from services.market_data.edgar_company import edgar_company_profile
from services.supervisor.incident_log import IncidentLog
from services.supervisor.ops_state import OpsState
from services.supervisor.session_record import _auto_profile
from tests.unit.test_session_records import _rec

UA = "QuantPaper ops@example.org"


def test_a_known_industry_code_gets_a_japanese_category_and_an_unknown_one_its_broad_division():
    assert category_ja_from_sic(7372) == "ソフトウェア" and category_ja_from_sic(3674) == "半導体"
    assert category_ja_from_sic(5999) == "小売"                      # no exact entry: the division says it
    assert category_ja_from_sic(2399) == "製造業" and category_ja_from_sic(None) is None
    assert category_ja_from_sic(50) is None                          # outside every division: no guess


def test_the_automatic_profile_is_honest_about_what_it_is():
    p = profile_from_industry("COSTCO WHOLESALE CORP /NEW", 5331, "Retail-Variety Stores")
    assert p.auto and p.company == "COSTCO WHOLESALE CORP /NEW" and p.company_ja == ""      # name is not translated
    assert p.category == "小売（総合）" and "Retail-Variety Stores" in p.business
    assert "事業内容の説明ではありません" in p.business                  # an industry class, not a made-up summary
    unknown = profile_from_industry("ACME", None, "Weird Class")
    assert unknown.category == "Weird Class"                          # English kept when it cannot be mapped
    assert profile_from_industry("ACME", None, "").category == "業種は不明"


def test_the_hand_written_table_is_not_marked_automatic_and_its_codes_are_sane():
    assert all(not p.auto for p in SYMBOL_PROFILES.values()) and SIC_CATEGORY_JA[7372]


class _Http:
    def __init__(self, body, fail=False):
        self.body, self.fail = body, fail

    def __call__(self, url, headers):
        if self.fail:
            raise EdgarUnavailable("SEC HTTP 503")
        if url.endswith("company_tickers.json"):
            return {"0": {"cik_str": 909832, "ticker": "COST", "title": "x"}}
        if "CIK0000909832" in url:
            return self.body
        raise EdgarUnavailable("SEC HTTP 404")


BODY = {"name": "COSTCO WHOLESALE CORP /NEW", "sic": "5331", "sicDescription": "Retail-Variety Stores",
        "filings": {"recent": {"accessionNumber": [], "filingDate": [], "acceptanceDateTime": [], "form": [],
                               "items": [], "primaryDocument": []}}}


def test_the_company_record_comes_from_the_sec_and_an_unknown_ticker_is_none():
    c = EdgarClient(UA, http_get=_Http(BODY), sleep=lambda s: None)
    info = c.company_info("cost")
    assert (info.name, info.sic, info.sic_description) == ("COSTCO WHOLESALE CORP /NEW", 5331, "Retail-Variety Stores")
    assert c.company_info("ZZZZ") is None
    assert edgar_company_profile(c, "COST").category == "小売（総合）" and edgar_company_profile(c, "ZZZZ") is None


def test_a_failed_sec_request_raises_and_is_never_read_as_unknown():
    c = EdgarClient(UA, http_get=_Http(BODY, fail=True), sleep=lambda s: None)
    with pytest.raises(EdgarUnavailable):
        c.company_info("COST")


def test_a_bad_industry_code_is_missing_not_a_crash():
    c = EdgarClient(UA, http_get=_Http({**BODY, "sic": "n/a"}), sleep=lambda s: None)
    assert c.company_info("COST").sic is None


def test_the_recorder_looks_up_only_what_the_table_does_not_know_and_never_fails_on_it():
    calls = []

    def lookup(sym):
        calls.append(sym)
        return profile_from_industry("COSTCO", 5331, "Retail-Variety Stores")
    assert _auto_profile("NVDA", lookup) is None and calls == []                 # in the table: no lookup
    assert _auto_profile("COST", lookup)["category"] == "小売（総合）" and calls == ["COST"]
    assert _auto_profile("COST", None) is None

    def boom(sym):
        raise EdgarUnavailable("SEC HTTP 503")
    assert _auto_profile("COST", boom) is None                                    # logged, holding shown by symbol


def _holding(sym, **extra):
    return {"symbol": sym, "qty": 1.0, "avg_cost": 100.0, "price": 101.0, "value": 101.0, "pnl": 1.0, "pnl_pct": 0.01,
            "weight": 0.1, "plan": None, **extra}


def test_the_dashboard_shows_table_first_then_the_recorded_sec_profile_then_nothing(tmp_path):
    st = DurableSessionStore(str(tmp_path / "sessions.db"), Environment.PAPER)
    rec = dataclasses.replace(_rec("2026-08-11", 1000.0, 0.0), detail_json=json.dumps({"holdings": [
        _holding("NVDA"),
        _holding("COST", profile_auto={"company": "COSTCO WHOLESALE CORP /NEW", "category": "小売（総合）",
                                       "business": "SEC の業種分類: Retail-Variety Stores（自動取得。事業内容の説明ではありません）"}),
        _holding("ZZZZ", profile_auto=None)]}))
    st.record(rec)
    o = OpsState(tmp_path, incidents=IncidentLog(tmp_path / "i.jsonl"), intents=IntentInbox(tmp_path / "n.jsonl"))
    by = {x["symbol"]: x for x in o.holdings()["holdings"]}
    assert by["NVDA"]["profile_source"] == "table" and by["NVDA"]["company_ja"] == "エヌビディア"
    assert by["COST"]["profile_source"] == "sec" and by["COST"]["company"] == "COSTCO WHOLESALE CORP /NEW"
    assert by["COST"]["company_ja"] is None and by["COST"]["category"] == "小売（総合）"
    assert by["ZZZZ"]["profile_source"] is None and by["ZZZZ"]["company"] is None and profile_of("ZZZZ") is None
