"""NYSE calendar data for 2027-2028 (source: NYSE Group announcement of 2025-12-23).

Two independent checks on the shipped DATA: the official table typed out below, and a
computation from NYSE's published holiday rules that must reproduce every year we ship
(including 2025-2026, which predate this change) — so a typo in either cannot go unnoticed."""
from datetime import date, datetime, time, timedelta, timezone

import pytest

from packages.common.calendar import ET, EARLY_CLOSE, REGULAR, SessionStatus, StaticCalendarProvider, TradingCalendar

P, CAL = StaticCalendarProvider(), TradingCalendar()

OFFICIAL = {
    2027: {"holidays": {(1, 1), (1, 18), (2, 15), (3, 26), (5, 31), (6, 18), (7, 5), (9, 6),
                        (11, 25), (12, 24)},
           "early": {(11, 26)}},
    2028: {"holidays": {(1, 17), (2, 21), (4, 14), (5, 29), (6, 19), (7, 4), (9, 4), (11, 23),
                        (12, 25)},
           "early": {(7, 3), (11, 24)}},
}


@pytest.mark.parametrize("year", [2027, 2028])
def test_shipped_data_equals_the_official_table(year):
    assert P.holidays(year) == {date(year, m, d) for m, d in OFFICIAL[year]["holidays"]}
    assert P.early_closes(year) == {date(year, m, d) for m, d in OFFICIAL[year]["early"]}


# -- an independent computation from the rules ---------------------------------

def _nth(y, m, wd, n):
    d = date(y, m, 1)
    d += timedelta((wd - d.weekday()) % 7)
    return d + timedelta(7 * (n - 1))


def _last(y, m, wd):
    d = date(y + (m == 12), (m % 12) + 1, 1) - timedelta(1)
    while d.weekday() != wd:
        d -= timedelta(1)
    return d


def _easter(y):
    a, b, c = y % 19, y // 100, y % 100
    d, e, f = b // 4, b % 4, (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    return date(y, (h + l - 7 * m + 114) // 31, (h + l - 7 * m + 114) % 31 + 1)


def _observed(d):
    return d - timedelta(1) if d.weekday() == 5 else d + timedelta(1) if d.weekday() == 6 else d


def _rule_holidays(y):
    h = {_nth(y, 1, 0, 3), _nth(y, 2, 0, 3), _easter(y) - timedelta(2), _last(y, 5, 0),
         _observed(date(y, 6, 19)), _observed(date(y, 7, 4)), _nth(y, 9, 0, 1),
         _nth(y, 11, 3, 4), _observed(date(y, 12, 25))}
    if date(y, 1, 1).weekday() != 5:               # a Saturday Jan 1 is not observed on Dec 31
        h.add(_observed(date(y, 1, 1)))
    return {d for d in h if d.year == y}


def _rule_early(y, hol):
    e = {_nth(y, 11, 3, 4) + timedelta(1)}
    for d in (date(y, 12, 24), date(y, 7, 3)):
        if d.weekday() < 5 and d not in hol and (d.month == 12 or date(y, 7, 4).weekday() < 5):
            e.add(d)
    return e


@pytest.mark.parametrize("year", [2025, 2026, 2027, 2028])
def test_shipped_data_matches_the_rule_based_computation(year):
    hol = _rule_holidays(year)
    assert P.holidays(year) == hol
    assert P.early_closes(year) == _rule_early(year, hol)


# -- behaviour on the interesting days -------------------------------------------

def _at(d, h, m=0):
    return datetime.combine(d, time(h, m), tzinfo=ET).astimezone(timezone.utc)


@pytest.mark.parametrize("d,trading", [
    (date(2027, 12, 31), True),            # Friday: open, no New Year's Eve closure
    (date(2027, 12, 24), False),           # Christmas observed
    (date(2027, 7, 5), False),             # Independence Day observed (Jul 4 is a Sunday)
    (date(2027, 7, 2), True),              # no early close either
    (date(2027, 6, 18), False),            # Juneteenth observed (Jun 19 is a Saturday)
    (date(2028, 1, 3), True),              # Jan 1 2028 is a Saturday: nothing observed
    (date(2028, 12, 25), False),
])
def test_trading_days(d, trading):
    assert CAL.is_trading_day(d) is trading


def test_day_after_thanksgiving_2027_closes_at_one_pm_and_a_normal_day_at_four():
    assert CAL.session_hours(date(2027, 11, 26)) == EARLY_CLOSE
    assert CAL.status(_at(date(2027, 11, 26), 12, 59)) is SessionStatus.OPEN
    assert CAL.status(_at(date(2027, 11, 26), 13, 0)) is SessionStatus.CLOSED
    assert CAL.session_hours(date(2027, 11, 24)) == REGULAR


def test_the_provider_still_refuses_a_year_it_has_no_data_for():
    assert P.last_year == 2028
    with pytest.raises(ValueError, match="refusing to guess"):
        P.holidays(2029)
    with pytest.raises(ValueError):
        CAL.is_trading_day(date(2029, 3, 5))
