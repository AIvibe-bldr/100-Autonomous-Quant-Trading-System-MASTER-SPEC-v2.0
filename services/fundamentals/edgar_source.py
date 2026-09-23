"""Real SEC EDGAR XBRL Financial Data Source (research-instruction §3).

Implements the same `FinancialDataSource` Protocol
(`services.fundamentals.engine.FinancialDataSource`) that
`MockFinancialDataSource` already implements — an alternative, opt-in data
source, not a replacement of the Mock default. `services.pipeline` still
defaults `fundamental_engine` to a Mock source; switching to this one is an
explicit construction choice, e.g.
`FundamentalInflectionEngine(source=EdgarFinancialDataSource(user_agent=...))`
— real network calls on every pipeline session are too large a behavior
change to enable implicitly (unlike `resolve_decision_agent`'s auto-switch
on credential presence, this is deliberately not automatic).

SEC EDGAR specifics this module has to respect:

- Fair Access Policy: a descriptive User-Agent (app name + contact email)
  is REQUIRED on every request, and request rate must stay well under
  ~10/sec — enforced here via `packages.common.rate_limiter`.
- The XBRL "companyfacts" endpoint mixes quarterly (10-Q) and annual/YTD
  duration facts for the same concept in one array; this module keeps only
  duration facts whose (end - start) falls in an ~80-100 day window (a
  true single fiscal quarter, not a cumulative YTD or full-year figure) —
  see `_is_quarterly_duration`. SEC does not require a standalone Q4
  filing (a 10-K covers it), so Q4 is never directly observable this way;
  this V1 deliberately does not attempt to derive it
  (FY − Q1 − Q2 − Q3) and simply has no statement for Q4 quarters — an
  honest gap, not a fabricated one.
- companyfacts commonly reports the SAME real quarter twice: once under
  its own original 10-Q, and again as a same-period comparative figure
  embedded in the NEXT year's 10-Q under a DIFFERENT (fy, fp) label.
  Naively taking whichever instance was filed latest silently relabels
  the quarter under the wrong fiscal year — see `_quarterly_revenue_facts`
  for how the canonical (fy, fp) is picked (the earliest-filed instance)
  while still honoring a genuine same-period restatement (same fy/fp,
  later filed).
- Most filers report cash-flow-statement concepts (operating cash flow,
  capex, D&A, interest expense) only on a fiscal-year-to-date cumulative
  basis in interim 10-Qs, not as a discrete-quarter figure, unlike
  income-statement concepts — see `_flow_value`'s YTD-differencing
  fallback (Q2 = H1 YTD − Q1, Q3 = 9-month YTD − H1 YTD).
- No SEC tag maps 1:1 to a `TemporaryFactorFlag` reason (§6) — real
  MD&A/footnote text parsing is future work (the same gap
  `MockFinancialDataSource.fetch_flags`'s own docstring already names, for
  the Management Language Tracker). `fetch_flags()` here always returns
  `()` rather than guessing.
- A required `FinancialStatement` field this module cannot find a real
  value for (directly or via YTD-differencing) makes that entire quarter
  unavailable (skipped, not fabricated) — the same "honest unknown"
  precedent as `FinancialMetrics`' Optional fields elsewhere in this
  package.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

import httpx

from packages.common.clock import ensure_utc
from packages.common.rate_limiter import TokenBucketRateLimiter
from packages.schemas.fundamentals import FinancialStatement, TemporaryFactorFlag
from services.fundamentals import xbrl_tags as tags

_COMPANY_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
_COMPANY_FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"

# Real fiscal quarters run roughly 89-92 days (calendar) up to ~98 days for
# some 4-4-5 retail calendars; this window is a heuristic for "a genuine
# single quarter," not a full year or a cumulative YTD figure.
_MIN_QUARTER_DAYS = 80
_MAX_QUARTER_DAYS = 100

_FISCAL_PERIOD_TO_QUARTER = {"Q1": 1, "Q2": 2, "Q3": 3}


class EdgarFetchError(RuntimeError):
    """A call to SEC EDGAR failed, or returned data this module could not
    make sense of (unknown symbol, malformed response)."""


def _parse_date(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)


def _is_quarterly_duration(fact: dict[str, Any]) -> bool:
    start, end = fact.get("start"), fact.get("end")
    if not start or not end:
        return False
    days = (_parse_date(end) - _parse_date(start)).days
    return _MIN_QUARTER_DAYS <= days <= _MAX_QUARTER_DAYS


def _best_duration_value(concept: Optional[dict[str, Any]], quarter_end: str) -> Optional[float]:
    """Latest-filed genuinely-quarterly (non-YTD, non-annual) value for
    `concept` whose period ends exactly on `quarter_end`, or None."""
    if concept is None:
        return None
    best = None
    for unit_facts in concept.get("units", {}).values():
        for fact in unit_facts:
            if fact.get("end") == quarter_end and _is_quarterly_duration(fact):
                if best is None or fact["filed"] > best["filed"]:
                    best = fact
    return None if best is None else float(best["val"])


def _best_instant_value(concept: Optional[dict[str, Any]], quarter_end: str) -> Optional[float]:
    if concept is None:
        return None
    best = None
    for unit_facts in concept.get("units", {}).values():
        for fact in unit_facts:
            if fact.get("end") == quarter_end and not fact.get("start"):
                if best is None or fact["filed"] > best["filed"]:
                    best = fact
    return None if best is None else float(best["val"])


def _first_available(us_gaap: dict[str, Any], quarter_end: str,
                     candidates: tuple[str, ...], instant: bool) -> Optional[float]:
    getter = _best_instant_value if instant else _best_duration_value
    for name in candidates:
        value = getter(us_gaap.get(name), quarter_end)
        if value is not None:
            return value
    return None


def _known_as_of(us_gaap: dict[str, Any], as_of: datetime) -> dict[str, Any]:
    """§16 Point-in-Time: only facts actually filed by `as_of`. Every later
    "latest filed wins" choice (restatements, a quarter re-reported as a
    comparative in the next year's 10-Q) must pick among what was knowable
    then — otherwise a backtest silently reads figures published after its
    own date."""
    cutoff = as_of.date().isoformat()
    return {name: {**concept,
                   "units": {unit: [f for f in facts if f.get("filed", "") <= cutoff]
                             for unit, facts in concept.get("units", {}).items()}}
            for name, concept in us_gaap.items()}


def _ytd_value(us_gaap: dict[str, Any], candidates: tuple[str, ...],
               fy_start: str, quarter_end: str) -> Optional[float]:
    """Cumulative fiscal-year-to-date value of `candidates` as of
    `quarter_end` — a duration fact starting at the fiscal year's own Q1
    start and ending at `quarter_end`. Used only as an input to
    `EdgarFinancialDataSource._flow_value`'s YTD-differencing fallback,
    never returned directly as a single quarter's own figure."""
    for name in candidates:
        concept = us_gaap.get(name)
        if concept is None:
            continue
        best = None
        for unit_facts in concept.get("units", {}).values():
            for fact in unit_facts:
                if fact.get("end") == quarter_end and fact.get("start") == fy_start:
                    if best is None or fact["filed"] > best["filed"]:
                        best = fact
        if best is not None:
            return float(best["val"])
    return None


class EdgarFinancialDataSource:
    """`FinancialDataSource` backed by real SEC EDGAR XBRL data.

    `user_agent` is mandatory per SEC's Fair Access Policy — it must
    identify the requesting application and a contact (e.g.
    "MyTradingResearch contact@example.com"); SEC returns 403 without one.
    """

    def __init__(self, user_agent: str, client: Optional[httpx.Client] = None,
                rate_limiter: Optional[TokenBucketRateLimiter] = None) -> None:
        if not user_agent.strip():
            raise ValueError("SEC EDGAR requires a descriptive User-Agent "
                            "(app name + contact email)")
        self._client = client or httpx.Client(timeout=10.0)
        self._headers = {"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"}
        # SEC's fair-access policy asks for well under ~10 req/sec; 5/sec
        # leaves headroom rather than riding the limit.
        self._rate_limiter = rate_limiter or TokenBucketRateLimiter(max_calls=5, per_seconds=1.0)
        self._cik_cache: Optional[dict[str, str]] = None
        # keyed by symbol — companyfacts is a per-symbol payload that a
        # replay/backtest would otherwise refetch on every `analyze()` call
        # for the same symbol across many `as_of` dates.
        self._companyfacts_cache: dict[str, dict[str, Any]] = {}

    def _get(self, url: str) -> dict[str, Any]:
        self._rate_limiter.acquire()
        response = self._client.get(url, headers=self._headers)
        if response.status_code != 200:
            raise EdgarFetchError(f"SEC EDGAR request to {url} failed: "
                                  f"HTTP {response.status_code}")
        return response.json()

    def _resolve_cik(self, symbol: str) -> str:
        if self._cik_cache is None:
            data = self._get(_COMPANY_TICKERS_URL)
            self._cik_cache = {
                entry["ticker"].upper(): str(entry["cik_str"]).zfill(10)
                for entry in data.values()}
        cik = self._cik_cache.get(symbol.upper())
        if cik is None:
            raise EdgarFetchError(f"unknown ticker symbol for SEC EDGAR: {symbol!r}")
        return cik

    def _company_facts(self, symbol: str) -> dict[str, Any]:
        if symbol not in self._companyfacts_cache:
            cik = self._resolve_cik(symbol)
            url = _COMPANY_FACTS_URL.format(cik=int(cik))
            self._companyfacts_cache[symbol] = self._get(url)
        return self._companyfacts_cache[symbol]

    def fetch_history(self, symbol: str, as_of: datetime,
                      quarters: int = 9) -> list[FinancialStatement]:
        facts = self._company_facts(symbol)
        us_gaap = _known_as_of(facts.get("facts", {}).get("us-gaap", {}), ensure_utc(as_of))

        revenue_facts = self._quarterly_revenue_facts(us_gaap)
        statements = []
        for end in sorted(revenue_facts):
            statement = self._build_statement(symbol, us_gaap, end, revenue_facts[end],
                                              revenue_facts)
            if statement is not None and statement.received_timestamp <= as_of:
                statements.append(statement)
        statements.sort(key=lambda s: s.effective_timestamp)
        return statements[-quarters:]

    def _quarterly_revenue_facts(self, us_gaap: dict[str, Any]) -> dict[str, dict[str, Any]]:
        """quarter_end -> the canonical genuine quarterly revenue fact for
        that period, across all REVENUE_TAGS candidates. Revenue is the one
        concept guaranteed present for every usable quarter, so its fact
        also supplies this statement's fiscal_year/fiscal_quarter/
        filing_timestamp metadata (`fy`/`fp`/`filed`) rather than a second,
        separately-matched lookup.

        SEC companyfacts commonly reports the SAME real quarter twice: once
        under its own original 10-Q (e.g. fy=2021/fp=Q1/filed=2021-01-28),
        and again as a same-period comparative figure embedded in the NEXT
        year's 10-Q, which carries a DIFFERENT (fy, fp) — e.g.
        fy=2022/fp=Q1/filed=2022-01-28 for the exact same Oct-Dec 2020
        quarter. Naively taking whichever instance was filed latest would
        silently relabel the quarter under the wrong fiscal year. The
        earliest-filed instance for a given period is always this quarter's
        own original filing, so it alone decides (fy, fp); only a
        LATER-filed instance sharing that SAME (fy, fp) — a genuine
        restatement/amendment, not a different report's comparative — is
        allowed to override the value."""
        grouped: dict[str, list[dict[str, Any]]] = {}
        for name in tags.REVENUE_TAGS:
            concept = us_gaap.get(name)
            if concept is None:
                continue
            for unit_facts in concept.get("units", {}).values():
                for fact in unit_facts:
                    if _is_quarterly_duration(fact):
                        grouped.setdefault(fact["end"], []).append(fact)

        by_end: dict[str, dict[str, Any]] = {}
        for end, facts in grouped.items():
            original = min(facts, key=lambda f: f["filed"])
            same_period = [f for f in facts
                           if f["fy"] == original["fy"] and f["fp"] == original["fp"]]
            restated = max(same_period, key=lambda f: f["filed"])
            canonical = dict(original)
            canonical["val"] = restated["val"]
            canonical["filed"] = restated["filed"]
            by_end[end] = canonical
        return by_end

    def _fiscal_year_q1_start(self, revenue_facts: dict[str, dict[str, Any]],
                              fy: int) -> Optional[str]:
        for f in revenue_facts.values():
            if f["fy"] == fy and f["fp"] == "Q1":
                return f["start"]
        return None

    def _prior_quarter_end(self, revenue_facts: dict[str, dict[str, Any]],
                           fy: int, fp: str) -> Optional[str]:
        prior_fp = {"Q2": "Q1", "Q3": "Q2"}.get(fp)
        if prior_fp is None:
            return None
        for end, f in revenue_facts.items():
            if f["fy"] == fy and f["fp"] == prior_fp:
                return end
        return None

    def _flow_value(self, us_gaap: dict[str, Any], candidates: tuple[str, ...],
                    quarter_end: str, fy: int, fp: str,
                    revenue_facts: dict[str, dict[str, Any]]) -> Optional[float]:
        """Many filers report cash-flow-statement concepts (operating cash
        flow, capex, D&A, interest expense) only on a fiscal-year-to-date
        cumulative basis in interim 10-Qs, not as a discrete-quarter
        figure — unlike income-statement concepts, which are typically
        discrete. When no direct ~quarter-length fact exists, derive the
        discrete quarter as (this quarter's YTD) − (the prior quarter's
        YTD, or the prior quarter's own discrete value when the prior
        quarter is Q1, since Q1's YTD IS Q1)."""
        direct = _first_available(us_gaap, quarter_end, candidates, instant=False)
        if direct is not None:
            return direct
        if fp == "Q1":
            return None  # no YTD-differencing fallback for the first quarter.

        fy_start = self._fiscal_year_q1_start(revenue_facts, fy)
        if fy_start is None:
            return None
        ytd_now = _ytd_value(us_gaap, candidates, fy_start, quarter_end)
        if ytd_now is None:
            return None

        prior_end = self._prior_quarter_end(revenue_facts, fy, fp)
        if prior_end is None:
            return None
        prior_value = _first_available(us_gaap, prior_end, candidates, instant=False)
        if prior_value is None:
            prior_value = _ytd_value(us_gaap, candidates, fy_start, prior_end)
        if prior_value is None:
            return None
        return ytd_now - prior_value

    def _build_statement(self, symbol: str, us_gaap: dict[str, Any], quarter_end: str,
                         revenue_fact: dict[str, Any],
                         revenue_facts: dict[str, dict[str, Any]]) -> Optional[FinancialStatement]:
        fiscal_quarter = _FISCAL_PERIOD_TO_QUARTER.get(revenue_fact.get("fp"))
        if fiscal_quarter is None:
            return None  # "FY"/"Q4"/unrecognized — not a standalone 10-Q quarter.
        fy = int(revenue_fact["fy"])
        fp = revenue_fact["fp"]

        def duration(candidates: tuple[str, ...]) -> Optional[float]:
            return _first_available(us_gaap, quarter_end, candidates, instant=False)

        def flow(candidates: tuple[str, ...]) -> Optional[float]:
            return self._flow_value(us_gaap, candidates, quarter_end, fy, fp, revenue_facts)

        def instant(candidates: tuple[str, ...]) -> Optional[float]:
            return _first_available(us_gaap, quarter_end, candidates, instant=True)

        revenue = float(revenue_fact["val"])
        cost_of_revenue = duration(tags.COST_OF_REVENUE_TAGS)
        operating_income = duration(tags.OPERATING_INCOME_TAGS)
        net_income = duration(tags.NET_INCOME_TAGS)
        eps_diluted = duration(tags.EPS_DILUTED_TAGS)
        shares_diluted = duration(tags.SHARES_DILUTED_TAGS)
        operating_cash_flow = flow(tags.OPERATING_CASH_FLOW_TAGS)
        capex = flow(tags.CAPEX_TAGS)
        interest_expense = flow(tags.INTEREST_EXPENSE_TAGS)
        d_and_a = flow(tags.DEPRECIATION_AMORTIZATION_TAGS)
        tax_expense = duration(tags.INCOME_TAX_EXPENSE_TAGS)
        pretax_income = duration(tags.PRETAX_INCOME_TAGS)
        total_assets = instant(tags.TOTAL_ASSETS_TAGS)
        cash = instant(tags.CASH_TAGS)
        # Missing debt tags read as zero debt rather than "insufficient
        # data" — most filers that carry no debt simply never tag these
        # concepts at all, and treating that as unavailable would silently
        # drop every debt-free company's quarters. This can undercount a
        # levered filer if it uses a company-specific tag outside our
        # candidate list — a known, documented limitation, not a claim of
        # precision.
        long_term_debt = instant(tags.LONG_TERM_DEBT_TAGS) or 0.0
        current_debt = instant(tags.CURRENT_DEBT_TAGS) or 0.0

        required = (cost_of_revenue, operating_income, net_income, eps_diluted,
                   shares_diluted, operating_cash_flow, capex, interest_expense,
                   total_assets, cash)
        if any(v is None for v in required):
            return None
        if d_and_a is None:
            return None
        ebitda = operating_income + d_and_a
        if tax_expense is None or pretax_income is None or pretax_income <= 0:
            return None
        effective_tax_rate = tax_expense / pretax_income
        if not (0.0 <= effective_tax_rate <= 1.0):
            return None
        if (capex < 0 or interest_expense < 0 or cost_of_revenue < 0
                or long_term_debt < 0 or current_debt < 0
                or total_assets <= 0 or cash < 0 or cash > total_assets
                or shares_diluted <= 0):
            return None

        filed_dt = _parse_date(revenue_fact["filed"])
        end_dt = _parse_date(quarter_end)
        fiscal_year = fy

        try:
            return FinancialStatement(
                symbol=symbol, fiscal_year=fiscal_year, fiscal_quarter=fiscal_quarter,
                revenue=revenue, cost_of_revenue=cost_of_revenue,
                operating_income=operating_income, net_income=net_income,
                eps_diluted=eps_diluted, shares_diluted=shares_diluted,
                operating_cash_flow=operating_cash_flow, capital_expenditure=capex,
                total_assets=total_assets, total_debt=long_term_debt + current_debt,
                cash_and_equivalents=cash, ebitda=ebitda,
                interest_expense=interest_expense, effective_tax_rate=effective_tax_rate,
                filing_timestamp=filed_dt, publication_timestamp=filed_dt,
                received_timestamp=filed_dt, effective_timestamp=end_dt,
                data_version="1", source="sec_edgar",
                source_url=_COMPANY_FACTS_URL.format(cik=int(self._resolve_cik(symbol))))
        except ValueError:
            # A pydantic validator (e.g. balance-sheet sanity) rejected
            # this quarter's figures — treat as unavailable, not fabricated.
            return None

    def fetch_flags(self, symbol: str, fiscal_year: int,
                    fiscal_quarter: int) -> tuple[TemporaryFactorFlag, ...]:
        return ()
