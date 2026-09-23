"""Deterministic Mock Management Statement Source (research-instruction §7-8).

No real earnings-call-transcript/IR-feed adapter exists in this repo yet —
same rationale as `services.fundamentals.mock_source`: get the detection
pipeline structurally correct first, real transcript ingestion is separate,
later work. Same hash-seeded determinism pattern (same symbol+date ->
same statements, for replay).

Each symbol gets its own "tone archetype", seeded independently from its
financial-statement archetype in `mock_source.py` — a company's management
language and its actual reported numbers do not always move together,
which is exactly the scenario the Divergence Engine (§10) is meant to
catch elsewhere; this module doesn't couple the two together itself.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

from packages.schemas.fundamentals import ManagementStatement

# Earnings-call transcripts/IR statements become available at reporting
# time, not before — same filing-lag rationale as mock_source.py.
_STATEMENT_LAG_DAYS = 45

_CONFIDENT_LINE = ("We delivered record results with accelerating demand, "
                   "exceeded expectations, and see robust demand and strong "
                   "tailwinds ahead; we remain confident and well positioned.")
_HEDGING_LINE = ("We faced significant headwinds and a challenging environment "
                 "this quarter amid soft demand and pressure; results reflect "
                 "a temporary setback and broader industry slowdown, and we "
                 "remain cautious given near-term uncertainty in a difficult "
                 "environment.")
_NEUTRAL_LINE = "Results this quarter were in line with our internal plan and prior guidance."


def _seed(symbol: str, salt: str) -> int:
    return int(hashlib.sha256(f"{symbol}:{salt}".encode()).hexdigest()[:8], 16)


def _fiscal_quarters_ending_by(as_of: datetime, n: int) -> list[tuple[int, int]]:
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


class MockManagementStatementSource:
    """`fetch_history` returns `quarters` consecutive statements ending
    on/before `as_of`'s own quarter — callers filter by `received_timestamp`
    themselves for point-in-time correctness, same contract as
    `MockFinancialDataSource.fetch_history`."""

    def fetch_history(self, symbol: str, as_of: datetime,
                      quarters: int = 9) -> list[ManagementStatement]:
        # 0: increasingly confident language, 1: increasingly hedging
        # language, else: flat/neutral.
        archetype = _seed(symbol, "tone_archetype") % 3

        statements = []
        for i, (fy, fq) in enumerate(_fiscal_quarters_ending_by(as_of, quarters)):
            if archetype == 0:
                text = " ".join([_CONFIDENT_LINE] * (i + 1))
            elif archetype == 1:
                text = " ".join([_HEDGING_LINE] * (i + 1))
            else:
                text = _NEUTRAL_LINE

            end = _quarter_end(fy, fq)
            filed = end + timedelta(days=_STATEMENT_LAG_DAYS)
            statements.append(ManagementStatement(
                symbol=symbol, fiscal_year=fy, fiscal_quarter=fq, text=text,
                filing_timestamp=filed, publication_timestamp=filed,
                received_timestamp=filed, effective_timestamp=end,
                source="mock", source_url=f"https://mock-ir.invalid/{symbol}/{fy}Q{fq}"))
        return statements
