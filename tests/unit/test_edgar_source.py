"""Tests for services/fundamentals/edgar_source.py.

All tests below (except the opt-in live smoke test at the bottom) use
httpx.MockTransport — no real network calls, so the suite stays offline
and deterministic. The fixture mimics SEC's real companyfacts JSON shape
closely enough to exercise the parser's actual logic: mixed
quarterly/annual duration facts, a restated (re-filed) figure for the
same period, and one quarter missing a required tag.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

import httpx
import pytest

from services.fundamentals.edgar_source import EdgarFetchError, EdgarFinancialDataSource

_CIK = 1234567
_TICKER = "TESTCO"


def _duration_fact(start, end, val, fy, fp, filed, form="10-Q"):
    return {"start": start, "end": end, "val": val, "accn": f"acc-{end}-{filed}",
           "fy": fy, "fp": fp, "form": form, "filed": filed}


def _instant_fact(end, val, fy, fp, filed, form="10-Q"):
    return {"end": end, "val": val, "accn": f"acc-{end}-{filed}",
           "fy": fy, "fp": fp, "form": form, "filed": filed}


def _concept(*facts):
    return {"units": {"USD": list(facts)}}


# Five real quarters (no Q4 — SEC never reports a standalone Q4 duration
# fact): Q1-Q3 FY2025, Q1-Q2 FY2026.
_QUARTERS = [
    ("2025-01-01", "2025-03-31", 2025, "Q1", "2025-04-25"),
    ("2025-04-01", "2025-06-30", 2025, "Q2", "2025-07-25"),
    ("2025-07-01", "2025-09-30", 2025, "Q3", "2025-10-25"),
    ("2026-01-01", "2026-03-31", 2026, "Q1", "2026-04-25"),
    ("2026-04-01", "2026-06-30", 2026, "Q2", "2026-07-25"),
]


def _build_facts(skip_cost_of_revenue_for=None, include_restatement=False,
                 include_annual_duration=False):
    revenue_facts = []
    cost_facts = []
    for i, (start, end, fy, fp, filed) in enumerate(_QUARTERS):
        revenue = 100_000_000.0 + i * 5_000_000.0
        revenue_facts.append(_duration_fact(start, end, revenue, fy, fp, filed))
        if include_restatement and i == 0:
            # A later-filed restatement of Q1 2025's revenue — must win
            # over the original filing.
            revenue_facts.append(_duration_fact(start, end, revenue + 1_000_000.0,
                                                 fy, fp, "2025-05-01"))
        if skip_cost_of_revenue_for != end:
            cost_facts.append(_duration_fact(start, end, revenue * 0.6, fy, fp, filed))

    if include_annual_duration:
        # A cumulative annual figure spanning ~365 days, tagged fp="FY" —
        # must be excluded by the quarterly-duration filter, not
        # double-counted (also independently caught by the fp check).
        revenue_facts.append(_duration_fact("2025-01-01", "2025-12-31", 400_000_000.0,
                                            2025, "FY", "2026-02-15", form="10-K"))
        # A same-fp, same-end, ~180-day YTD (H1 cumulative) figure filed
        # under fp="Q2" with a LATER filed date than the real quarterly
        # fact — the realistic case the duration-day filter alone must
        # catch (fp recognition wouldn't reject this; real SEC data
        # sometimes tags a cumulative figure under the same fiscal-period
        # label as the true single quarter). A later filed date means a
        # broken filter would let this one win the "latest filed" tie-break
        # and silently overwrite Q2's real, correct revenue.
        revenue_facts.append(_duration_fact("2025-01-01", "2025-06-30", 250_000_000.0,
                                            2025, "Q2", "2025-07-28"))

    operating_income = [_duration_fact(s, e, (100_000_000.0 + i * 5_000_000.0) * 0.2,
                                       fy, fp, filed)
                        for i, (s, e, fy, fp, filed) in enumerate(_QUARTERS)]
    net_income = [_duration_fact(s, e, (100_000_000.0 + i * 5_000_000.0) * 0.15,
                                 fy, fp, filed)
                 for i, (s, e, fy, fp, filed) in enumerate(_QUARTERS)]
    eps = [_duration_fact(s, e, 1.0 + i * 0.05, fy, fp, filed)
          for i, (s, e, fy, fp, filed) in enumerate(_QUARTERS)]
    shares = [_duration_fact(s, e, 100_000_000.0, fy, fp, filed)
             for i, (s, e, fy, fp, filed) in enumerate(_QUARTERS)]
    ocf = [_duration_fact(s, e, (100_000_000.0 + i * 5_000_000.0) * 0.18,
                          fy, fp, filed)
          for i, (s, e, fy, fp, filed) in enumerate(_QUARTERS)]
    capex = [_duration_fact(s, e, 5_000_000.0, fy, fp, filed)
            for i, (s, e, fy, fp, filed) in enumerate(_QUARTERS)]
    interest = [_duration_fact(s, e, 1_000_000.0, fy, fp, filed)
               for i, (s, e, fy, fp, filed) in enumerate(_QUARTERS)]
    d_and_a = [_duration_fact(s, e, 3_000_000.0, fy, fp, filed)
              for i, (s, e, fy, fp, filed) in enumerate(_QUARTERS)]
    tax_expense = [_duration_fact(s, e, (100_000_000.0 + i * 5_000_000.0) * 0.2 * 0.21,
                                  fy, fp, filed)
                  for i, (s, e, fy, fp, filed) in enumerate(_QUARTERS)]
    pretax = [_duration_fact(s, e, (100_000_000.0 + i * 5_000_000.0) * 0.2,
                             fy, fp, filed)
             for i, (s, e, fy, fp, filed) in enumerate(_QUARTERS)]
    total_assets = [_instant_fact(e, 500_000_000.0, fy, fp, filed)
                   for (s, e, fy, fp, filed) in _QUARTERS]
    cash = [_instant_fact(e, 100_000_000.0, fy, fp, filed)
           for (s, e, fy, fp, filed) in _QUARTERS]
    lt_debt = [_instant_fact(e, 50_000_000.0, fy, fp, filed)
              for (s, e, fy, fp, filed) in _QUARTERS]

    us_gaap = {
        "Revenues": _concept(*revenue_facts),
        "OperatingIncomeLoss": _concept(*operating_income),
        "NetIncomeLoss": _concept(*net_income),
        "EarningsPerShareDiluted": _concept(*eps),
        "WeightedAverageNumberOfDilutedSharesOutstanding": _concept(*shares),
        "NetCashProvidedByUsedInOperatingActivities": _concept(*ocf),
        "PaymentsToAcquirePropertyPlantAndEquipment": _concept(*capex),
        "InterestExpense": _concept(*interest),
        "DepreciationDepletionAndAmortization": _concept(*d_and_a),
        "IncomeTaxExpenseBenefit": _concept(*tax_expense),
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest":
            _concept(*pretax),
        "Assets": _concept(*total_assets),
        "CashAndCashEquivalentsAtCarryingValue": _concept(*cash),
        "LongTermDebtNoncurrent": _concept(*lt_debt),
    }
    if cost_facts:
        us_gaap["CostOfRevenue"] = _concept(*cost_facts)
    return {"cik": _CIK, "entityName": "Test Co", "facts": {"us-gaap": us_gaap}}


def _make_source(facts_json, calls=None):
    calls = calls if calls is not None else []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        assert request.headers.get("User-Agent") == "TestApp test@example.com"
        if "company_tickers.json" in str(request.url):
            return httpx.Response(200, json={"0": {"cik_str": _CIK, "ticker": _TICKER,
                                                    "title": "Test Co"}})
        if f"CIK{_CIK:010d}.json" in str(request.url):
            return httpx.Response(200, json=facts_json)
        return httpx.Response(404, json={})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    source = EdgarFinancialDataSource(user_agent="TestApp test@example.com", client=client)
    return source, calls


FAR_FUTURE = datetime(2030, 1, 1, tzinfo=timezone.utc)


def test_requires_a_non_empty_user_agent():
    with pytest.raises(ValueError):
        EdgarFinancialDataSource(user_agent="")
    with pytest.raises(ValueError):
        EdgarFinancialDataSource(user_agent="   ")


def test_sends_the_configured_user_agent_and_resolves_the_correct_cik_url():
    source, calls = _make_source(_build_facts())
    source.fetch_history(_TICKER, FAR_FUTURE, quarters=9)
    assert any("company_tickers.json" in c for c in calls)
    assert any(f"CIK{_CIK:010d}.json" in c for c in calls)


def test_unknown_ticker_raises_edgar_fetch_error():
    source, _ = _make_source(_build_facts())
    with pytest.raises(EdgarFetchError):
        source.fetch_history("NOSUCHTICKER", FAR_FUTURE, quarters=9)


def test_non_200_response_raises_edgar_fetch_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    source = EdgarFinancialDataSource(user_agent="TestApp test@example.com", client=client)
    with pytest.raises(EdgarFetchError):
        source.fetch_history(_TICKER, FAR_FUTURE, quarters=9)


def test_parses_all_five_genuine_quarters_correctly():
    source, _ = _make_source(_build_facts())
    statements = source.fetch_history(_TICKER, FAR_FUTURE, quarters=9)
    assert len(statements) == 5
    assert [s.fiscal_quarter for s in statements] == [1, 2, 3, 1, 2]
    assert [s.fiscal_year for s in statements] == [2025, 2025, 2025, 2026, 2026]
    first = statements[0]
    assert first.revenue == 100_000_000.0
    assert first.cost_of_revenue == 60_000_000.0
    assert first.ebitda == first.operating_income + 3_000_000.0
    assert 0.0 <= first.effective_tax_rate <= 1.0
    assert first.source == "sec_edgar"


def test_annual_and_ytd_duration_facts_are_excluded_not_double_counted():
    source, _ = _make_source(_build_facts(include_annual_duration=True))
    statements = source.fetch_history(_TICKER, FAR_FUTURE, quarters=9)
    # Still exactly 5 — neither the ~365-day "FY" fact nor the ~180-day
    # same-fp H1 fact may appear as an extra statement or silently
    # overwrite a real quarter's revenue.
    assert len(statements) == 5
    assert all(s.revenue < 400_000_000.0 for s in statements)
    q2_2025 = next(s for s in statements if s.fiscal_year == 2025 and s.fiscal_quarter == 2)
    assert q2_2025.revenue == 105_000_000.0


def test_a_restated_figure_wins_over_the_original_filing():
    source, _ = _make_source(_build_facts(include_restatement=True))
    statements = source.fetch_history(_TICKER, FAR_FUTURE, quarters=9)
    first = next(s for s in statements if s.fiscal_year == 2025 and s.fiscal_quarter == 1)
    assert first.revenue == 101_000_000.0


def test_a_quarter_missing_a_required_tag_is_skipped_not_fabricated():
    source, _ = _make_source(_build_facts(skip_cost_of_revenue_for="2025-06-30"))
    statements = source.fetch_history(_TICKER, FAR_FUTURE, quarters=9)
    assert len(statements) == 4
    assert not any(s.fiscal_year == 2025 and s.fiscal_quarter == 2 for s in statements)


def test_point_in_time_filter_excludes_statements_not_yet_filed():
    source, _ = _make_source(_build_facts())
    as_of = datetime(2025, 8, 1, tzinfo=timezone.utc)  # only Q1/Q2 2025 filed by then
    statements = source.fetch_history(_TICKER, as_of, quarters=9)
    assert len(statements) == 2
    assert all(s.received_timestamp <= as_of for s in statements)


def test_company_facts_are_fetched_only_once_per_symbol():
    source, calls = _make_source(_build_facts())
    source.fetch_history(_TICKER, FAR_FUTURE, quarters=9)
    source.fetch_history(_TICKER, FAR_FUTURE, quarters=9)
    facts_calls = [c for c in calls if f"CIK{_CIK:010d}.json" in c]
    assert len(facts_calls) == 1


def test_a_same_end_comparative_under_a_different_fy_does_not_relabel_the_quarter():
    """The exact real-world SEC EDGAR bug this module has to defend
    against: the SAME real quarter (Q1 2025) reported once under its own
    original 10-Q (fy=2025/fp=Q1), and again as a same-period comparative
    figure inside the NEXT year's Q1 10-Q — same end date, later filed,
    but tagged fy=2026/fp=Q1. Taking "latest filed" naively would relabel
    2025's Q1 as 2026's Q1."""
    facts = _build_facts()
    q1_2025 = next(f for f in facts["facts"]["us-gaap"]["Revenues"]["units"]["USD"]
                  if f["end"] == "2025-03-31" and f["fp"] == "Q1")
    comparative = dict(q1_2025)
    comparative["fy"] = 2026
    comparative["filed"] = "2026-04-25"  # later than the original 2025-04-25
    facts["facts"]["us-gaap"]["Revenues"]["units"]["USD"].append(comparative)

    source, _ = _make_source(facts)
    statements = source.fetch_history(_TICKER, FAR_FUTURE, quarters=9)
    q1 = next(s for s in statements if s.effective_timestamp.strftime("%Y-%m-%d") == "2025-03-31")
    assert q1.fiscal_year == 2025 and q1.fiscal_quarter == 1
    assert not any(s.fiscal_year == 2026 and s.effective_timestamp.strftime("%Y-%m-%d") == "2025-03-31"
                  for s in statements)


_YTD_QUARTERS = [
    ("2025-01-01", "2025-03-31", 2025, "Q1", "2025-04-25"),
    ("2025-04-01", "2025-06-30", 2025, "Q2", "2025-07-25"),
    ("2025-07-01", "2025-09-30", 2025, "Q3", "2025-10-25"),
]


def _build_ytd_only_cashflow_facts():
    """Operating cash flow is reported as a discrete quarter figure for
    Q1 only; Q2/Q3 carry ONLY a fiscal-year-to-date cumulative duration
    fact (start pinned to the fiscal year's own Q1 start) — the realistic
    shape of a filer that reports its cash flow statement on a YTD basis
    in interim 10-Qs, which most large filers actually do."""
    revenue_facts, other_facts = [], {name: [] for name in (
        "CostOfRevenue", "OperatingIncomeLoss", "NetIncomeLoss", "EarningsPerShareDiluted",
        "WeightedAverageNumberOfDilutedSharesOutstanding", "PaymentsToAcquirePropertyPlantAndEquipment",
        "InterestExpense", "DepreciationDepletionAndAmortization", "IncomeTaxExpenseBenefit",
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest")}
    ocf_facts = []
    instant_facts = {"Assets": [], "CashAndCashEquivalentsAtCarryingValue": []}

    fy_start = _YTD_QUARTERS[0][0]
    ocf_ytd_by_quarter = {"2025-03-31": 10_000_000.0, "2025-06-30": 22_000_000.0,
                          "2025-09-30": 36_000_000.0}
    for i, (start, end, fy, fp, filed) in enumerate(_YTD_QUARTERS):
        revenue = 100_000_000.0 + i * 5_000_000.0
        revenue_facts.append(_duration_fact(start, end, revenue, fy, fp, filed))
        other_facts["CostOfRevenue"].append(_duration_fact(start, end, revenue * 0.6, fy, fp, filed))
        other_facts["OperatingIncomeLoss"].append(
            _duration_fact(start, end, revenue * 0.2, fy, fp, filed))
        other_facts["NetIncomeLoss"].append(_duration_fact(start, end, revenue * 0.15, fy, fp, filed))
        other_facts["EarningsPerShareDiluted"].append(_duration_fact(start, end, 1.0, fy, fp, filed))
        other_facts["WeightedAverageNumberOfDilutedSharesOutstanding"].append(
            _duration_fact(start, end, 100_000_000.0, fy, fp, filed))
        other_facts["PaymentsToAcquirePropertyPlantAndEquipment"].append(
            _duration_fact(start, end, 5_000_000.0, fy, fp, filed))
        other_facts["InterestExpense"].append(_duration_fact(start, end, 1_000_000.0, fy, fp, filed))
        other_facts["DepreciationDepletionAndAmortization"].append(
            _duration_fact(start, end, 3_000_000.0, fy, fp, filed))
        other_facts["IncomeTaxExpenseBenefit"].append(
            _duration_fact(start, end, revenue * 0.2 * 0.21, fy, fp, filed))
        other_facts["IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest"].append(
            _duration_fact(start, end, revenue * 0.2, fy, fp, filed))
        instant_facts["Assets"].append(_instant_fact(end, 500_000_000.0, fy, fp, filed))
        instant_facts["CashAndCashEquivalentsAtCarryingValue"].append(
            _instant_fact(end, 100_000_000.0, fy, fp, filed))

        if fp == "Q1":
            ocf_facts.append(_duration_fact(start, end, ocf_ytd_by_quarter[end], fy, fp, filed))
        else:
            # YTD-only: start pinned to the fiscal year's own Q1 start.
            ocf_facts.append(_duration_fact(fy_start, end, ocf_ytd_by_quarter[end], fy, fp, filed))

    us_gaap = {"Revenues": _concept(*revenue_facts),
              "NetCashProvidedByUsedInOperatingActivities": _concept(*ocf_facts)}
    for name, facts_list in other_facts.items():
        us_gaap[name] = _concept(*facts_list)
    for name, facts_list in instant_facts.items():
        us_gaap[name] = _concept(*facts_list)
    return {"cik": _CIK, "entityName": "Test Co", "facts": {"us-gaap": us_gaap}}


def test_ytd_only_cash_flow_is_differenced_into_a_discrete_quarter():
    source, _ = _make_source(_build_ytd_only_cashflow_facts())
    statements = source.fetch_history(_TICKER, FAR_FUTURE, quarters=9)
    assert len(statements) == 3
    by_quarter = {s.fiscal_quarter: s for s in statements}
    assert by_quarter[1].operating_cash_flow == 10_000_000.0
    assert by_quarter[2].operating_cash_flow == 12_000_000.0  # 22M YTD - 10M Q1
    assert by_quarter[3].operating_cash_flow == 14_000_000.0  # 36M YTD - 22M H1 YTD


def test_fetch_flags_honestly_returns_empty():
    source, _ = _make_source(_build_facts())
    assert source.fetch_flags(_TICKER, 2025, 1) == ()


@pytest.mark.skipif(not os.environ.get("QUANT_EDGAR_LIVE_TEST"),
                    reason="opt-in only (QUANT_EDGAR_LIVE_TEST=1) — hits the real "
                          "SEC EDGAR API, kept out of the default offline suite")
def test_live_fetch_against_real_sec_edgar():
    """Not part of the default suite (network calls don't belong in a
    deterministic, offline unit test run) — a manual sanity check that the
    parser actually works against real SEC data, not just the synthetic
    fixture above. Run with: QUANT_EDGAR_LIVE_TEST=1 pytest
    tests/unit/test_edgar_source.py -k live -q"""
    source = EdgarFinancialDataSource(user_agent="QuantResearch contact@example.com")
    statements = source.fetch_history("AAPL", datetime.now(timezone.utc), quarters=9)
    assert statements, "expected at least one real quarter for AAPL"
    assert all(s.symbol == "AAPL" for s in statements)
    assert all(s.total_assets > 0 for s in statements)
