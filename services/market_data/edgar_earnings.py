"""Earnings-date ESTIMATES from SEC filings (MASTER SPEC §16).

No free feed gives confirmed earnings dates, but a company's earnings release
is filed as an 8-K Item 2.02 the same day. So the next report is estimated as
"one year after a past Item 2.02 date" (same quarter last year — less drift than
adding ~91 days to the latest). The result is explicitly an ESTIMATE
(`uncertainty_days`) and says so in its description; it widens the gap-risk
window instead of pretending to precision."""
from __future__ import annotations

from datetime import date, timedelta

from services.market_data.edgar_client import EdgarClient, EdgarUnavailable
from services.market_data.event_calendar import EventType, RiskEvent
from services.market_data.event_source import EventsUnavailable

UNCERTAINTY_DAYS = 7
_YEAR = timedelta(days=364)           # keeps the weekday; the uncertainty covers the drift
_LOOKBACK = timedelta(days=500)
_SAME_CYCLE_DAYS = 45
# A ticker with under ~a year of 8-K history (e.g. XOM after its 2026-07 holding-
# company reorganisation, which gave it a new CIK) has no "same quarter last year".
# Fall back to the quarterly cadence, with a wider margin.
_MIN_YEAR_HISTORY = timedelta(days=300)
QUARTER = timedelta(days=91)
QUARTERLY_UNCERTAINTY_DAYS = 14


class EdgarEarningsEstimator:
    def __init__(self, client: EdgarClient) -> None:
        self._client = client

    def fetch(self, symbols: list[str], start: date, end: date) -> list[RiskEvent]:
        events: list[RiskEvent] = []
        try:
            for sym in symbols:
                past = sorted({f.filing_date for f in self._client.recent_filings(sym)
                               if f.form == "8-K" and "2.02" in f.items
                               and start - _LOOKBACK <= f.filing_date < start})
                if past and past[0] > start - _MIN_YEAR_HISTORY:
                    events.extend(self._quarterly(sym, past[-1], start, end))
                    continue
                for d in past:
                    est = d + _YEAR
                    # this quarter already reported (a real 2.02 sits near the
                    # estimate and before `start`): it is not upcoming
                    if any(abs((est - p).days) <= _SAME_CYCLE_DAYS for p in past if p != d):
                        continue
                    if start - timedelta(days=UNCERTAINTY_DAYS) <= est <= end + timedelta(
                            days=UNCERTAINTY_DAYS):
                        events.append(RiskEvent(
                            EventType.EARNINGS, on=est, symbol=sym,
                            uncertainty_days=UNCERTAINTY_DAYS,
                            description=f"earnings (estimated ±{UNCERTAINTY_DAYS}d from last "
                                        f"year's 8-K Item 2.02 on {d.isoformat()})"))
        except EdgarUnavailable as e:
            raise EventsUnavailable(str(e)) from None
        return events

    @staticmethod
    def _quarterly(sym: str, latest: date, start: date, end: date) -> list[RiskEvent]:
        est = latest + QUARTER
        if not (start - timedelta(days=QUARTERLY_UNCERTAINTY_DAYS) <= est
                <= end + timedelta(days=QUARTERLY_UNCERTAINTY_DAYS)):
            return []
        return [RiskEvent(
            EventType.EARNINGS, on=est, symbol=sym, uncertainty_days=QUARTERLY_UNCERTAINTY_DAYS,
            description=f"earnings (estimated ±{QUARTERLY_UNCERTAINTY_DAYS}d: latest 8-K Item 2.02 "
                        f"on {latest.isoformat()} + ~91d; under a year of EDGAR history)")]
