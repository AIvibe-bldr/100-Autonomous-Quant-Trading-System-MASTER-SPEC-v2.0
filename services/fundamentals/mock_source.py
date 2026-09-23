"""Deterministic Mock Financial Data Source (research-instruction §3).

No SEC EDGAR/XBRL adapter exists in this repo yet — building one is real,
separate work (network access, rate limiting, parsing real filings) the
user explicitly deferred in favor of getting the detection pipeline
structurally correct first. Same rationale and same hash-seeded
determinism as services.news.mock_source / services.institutional.
mock_source (same symbol+date -> same statements, for replay §62).

Each symbol gets a stable "archetype" (structurally improving / deteriorating
/ flat-noisy) so the trend detector has real, deterministic signal to find —
not just noise — while staying reproducible.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

from packages.schemas.fundamentals import FinancialStatement, TemporaryFactorFlag

_ALL_FLAGS = list(TemporaryFactorFlag)

# One filing lag for the whole mock universe: real 10-Qs are typically due
# ~40-45 days after quarter end. This is what makes point-in-time filtering
# (§16) actually bite in FundamentalInflectionEngine.analyze() — the most
# recent quarter is invisible until this many days after its own end.
_FILING_LAG_DAYS = 45


def _seed(symbol: str, salt: str) -> int:
    return int(hashlib.sha256(f"{symbol}:{salt}".encode()).hexdigest()[:8], 16)


def _fiscal_quarters_ending_by(as_of: datetime, n: int) -> list[tuple[int, int]]:
    """[(year, quarter), ...] oldest -> newest, the `n` fiscal quarters
    whose calendar end falls on/before `as_of`."""
    year, quarter = as_of.year, (as_of.month - 1) // 3 + 1
    seq = []
    for _ in range(n):
        seq.append((year, quarter))
        quarter -= 1
        if quarter == 0:
            quarter, year = 4, year - 1
    return list(reversed(seq))


def _quarter_end(year: int, quarter: int) -> datetime:
    month = quarter * 3
    day = 30 if month in (4, 6, 9, 11) else 31 if month != 2 else 28
    return datetime(year, month, day, tzinfo=timezone.utc)


class MockFinancialDataSource:
    """Stand-in for a real SEC/XBRL feed. `fetch_history` returns
    `quarters` consecutive quarters ending on/before `as_of`'s own quarter
    — callers filter by `received_timestamp` themselves for point-in-time
    correctness (some of the returned statements may not be "visible" yet
    as of an earlier `as_of`; see FundamentalInflectionEngine)."""

    def fetch_history(self, symbol: str, as_of: datetime,
                      quarters: int = 9) -> list[FinancialStatement]:
        archetype = _seed(symbol, "archetype") % 6   # 0: improving, 1: deteriorating, else flat
        base_revenue = 50_000_000.0 + (_seed(symbol, "size") % 200) * 5_000_000.0
        base_gross_margin = 0.35 + (_seed(symbol, "margin_base") % 40) / 100.0
        opex_ratio = 0.30 + (_seed(symbol, "opex_base") % 20) / 100.0

        statements = []
        for i, (fy, fq) in enumerate(_fiscal_quarters_ending_by(as_of, quarters)):
            noise = ((_seed(symbol, f"noise:{fy}:{fq}") % 2000) - 1000) / 100_000.0  # +-1%
            if archetype == 0:
                margin = base_gross_margin + 0.006 * i + noise
                growth = 0.02 + 0.003 * i
            elif archetype == 1:
                margin = base_gross_margin - 0.006 * i + noise
                growth = 0.01 - 0.003 * i
            else:
                margin = base_gross_margin + noise
                growth = 0.01 + noise
            margin = max(0.05, min(0.90, margin))
            growth = max(-0.15, min(0.25, growth))

            revenue = max(1_000_000.0, base_revenue * ((1 + growth) ** i))
            cost_of_revenue = revenue * (1 - margin)
            opex = revenue * opex_ratio
            operating_income = revenue * margin - opex
            tax_rate = 0.21
            interest_expense = base_revenue * 0.01
            net_income = (operating_income - interest_expense) * (1 - tax_rate)
            dilution_rate = 0.01 if archetype == 1 else 0.003
            shares = 100_000_000.0 * ((1 + dilution_rate) ** i)
            eps = net_income / shares
            ocf = net_income * 1.2 + noise * revenue
            capex = revenue * 0.05
            total_assets = revenue * 2.0
            cash = revenue * 0.3
            total_debt = base_revenue * 0.5
            ebitda = operating_income * 1.15

            end = _quarter_end(fy, fq)
            filed = end + timedelta(days=_FILING_LAG_DAYS)
            statements.append(FinancialStatement(
                symbol=symbol, fiscal_year=fy, fiscal_quarter=fq,
                revenue=revenue, cost_of_revenue=max(0.0, cost_of_revenue),
                operating_income=operating_income, net_income=net_income,
                eps_diluted=eps, shares_diluted=shares,
                operating_cash_flow=ocf, capital_expenditure=max(0.0, capex),
                total_assets=total_assets, total_debt=max(0.0, total_debt),
                cash_and_equivalents=max(0.0, cash), ebitda=ebitda,
                interest_expense=max(0.0, interest_expense), effective_tax_rate=tax_rate,
                filing_timestamp=filed, publication_timestamp=filed,
                received_timestamp=filed, effective_timestamp=end,
                data_version="1", source="mock",
                source_url=f"https://mock-sec.invalid/{symbol}/{fy}Q{fq}"))
        return statements

    def fetch_flags(self, symbol: str, fiscal_year: int,
                    fiscal_quarter: int) -> tuple[TemporaryFactorFlag, ...]:
        """Stands in for §7's Management Language Tracker / real filing-text
        analysis (Phase B) — roughly one quarter in eight carries a flag, for
        any symbol, independent of its underlying trend archetype."""
        if _seed(symbol, f"flaggate:{fiscal_year}:{fiscal_quarter}") % 8 != 0:
            return ()
        pick = _seed(symbol, f"flagpick:{fiscal_year}:{fiscal_quarter}") % len(_ALL_FLAGS)
        return (_ALL_FLAGS[pick],)
