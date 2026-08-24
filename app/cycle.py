"""The monthly reporting cycle.

A cycle is named for the month the DATA covers - the "2026-07" cycle is July's
numbers, assembled and sent in August. Everything else follows from that:

  * Monthly reports are due for every client with an order live in July.
  * A lifetime report is due for every order that ENDED in the window running
    from July 1 through the 3rd business day of August. An order ending Aug 3
    is close enough to fold into this cycle; one ending on the 4th or 5th
    business day is not, and waits for next month.
  * The whole cycle goes out on the 5th business day of August.

BUSINESS DAYS SKIP FEDERAL HOLIDAYS, not just weekends. Counting weekdays only
is right for eight months of the year and quietly wrong for the other four -
January's MLK day, and the run of Thanksgiving, Christmas and New Year's that
lands squarely in the cycles nobody has slack to fix.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

# Which business day of the following month each thing happens on.
LIFETIME_CUTOFF_BDAY = 3     # orders ending on or before this join the cycle
DELIVERY_BDAY = 5            # the cycle is due out


# ---------------------------------------------------------------- holidays
def _nth_weekday(year: int, month: int, weekday: int, n: int) -> dt.date:
    """The nth <weekday> of a month; n = -1 means the last one."""
    if n > 0:
        d = dt.date(year, month, 1)
        offset = (weekday - d.weekday()) % 7
        return d + dt.timedelta(days=offset + 7 * (n - 1))
    d = dt.date(year + (month == 12), (month % 12) + 1, 1) - dt.timedelta(days=1)
    return d - dt.timedelta(days=(d.weekday() - weekday) % 7)


def _observed(d: dt.date) -> dt.date:
    """A fixed-date holiday on a weekend is observed on the nearest weekday."""
    if d.weekday() == 5:
        return d - dt.timedelta(days=1)
    if d.weekday() == 6:
        return d + dt.timedelta(days=1)
    return d


def federal_holidays(year: int) -> set[dt.date]:
    return {
        _observed(dt.date(year, 1, 1)),                    # New Year's Day
        _nth_weekday(year, 1, 0, 3),                       # MLK Day
        _nth_weekday(year, 2, 0, 3),                       # Presidents Day
        _nth_weekday(year, 5, 0, -1),                      # Memorial Day
        _observed(dt.date(year, 6, 19)),                   # Juneteenth
        _observed(dt.date(year, 7, 4)),                    # Independence Day
        _nth_weekday(year, 9, 0, 1),                       # Labor Day
        _nth_weekday(year, 10, 0, 2),                      # Columbus Day
        _observed(dt.date(year, 11, 11)),                  # Veterans Day
        _nth_weekday(year, 11, 3, 4),                      # Thanksgiving
        _observed(dt.date(year, 12, 25)),                  # Christmas
    }


def is_business_day(d: dt.date) -> bool:
    return d.weekday() < 5 and d not in federal_holidays(d.year)


def nth_business_day(year: int, month: int, n: int) -> dt.date:
    """The nth business day of a month, counting from 1."""
    d, seen = dt.date(year, month, 1), 0
    while True:
        if is_business_day(d):
            seen += 1
            if seen == n:
                return d
        d += dt.timedelta(days=1)


# ---------------------------------------------------------------- the cycle
def period_bounds(period: str) -> tuple[dt.date, dt.date]:
    y, m = (int(x) for x in period.split("-"))
    start = dt.date(y, m, 1)
    end = dt.date(y + (m == 12), (m % 12) + 1, 1) - dt.timedelta(days=1)
    return start, end


def next_month(period: str) -> tuple[int, int]:
    y, m = (int(x) for x in period.split("-"))
    return (y + (m == 12), (m % 12) + 1)


@dataclass(frozen=True)
class Cycle:
    period: str                  # the month the data covers, "2026-07"
    starts_on: dt.date           # first day of that month
    ends_on: dt.date             # last day of that month
    lifetime_cutoff: dt.date     # 3rd business day of the following month
    due_on: dt.date              # 5th business day of the following month

    @property
    def label(self) -> str:
        return self.starts_on.strftime("%B %Y")

    def days_until_due(self, today: dt.date | None = None) -> int:
        return (self.due_on - (today or dt.date.today())).days

    def is_open(self, today: dt.date | None = None) -> bool:
        """Still being assembled - the data month is over but delivery is not."""
        t = today or dt.date.today()
        return self.ends_on < t <= self.due_on

    def needs_lifetime(self, ends_on: dt.date | None) -> bool:
        """An order ending inside the window owes a lifetime report in this
        cycle. The window deliberately reaches a few days past month end."""
        return bool(ends_on and self.starts_on <= ends_on <= self.lifetime_cutoff)

    def was_live(self, starts_on: dt.date | None, ends_on: dt.date | None) -> bool:
        """Delivered at some point during the data month, so it owes a monthly.

        A missing date is treated as open-ended rather than as a reason to
        exclude: a blank end date means still running, and dropping those would
        silently lose the longest campaigns.
        """
        if ends_on and ends_on < self.starts_on:
            return False
        if starts_on and starts_on > self.ends_on:
            return False
        return True


def cycle_for(period: str) -> Cycle:
    start, end = period_bounds(period)
    y, m = next_month(period)
    return Cycle(period=period, starts_on=start, ends_on=end,
                 lifetime_cutoff=nth_business_day(y, m, LIFETIME_CUTOFF_BDAY),
                 due_on=nth_business_day(y, m, DELIVERY_BDAY))


def current_period(today: dt.date | None = None) -> str:
    """The cycle people are working on right now.

    Through the delivery date it is last month's data. Once that has shipped
    the useful default rolls forward, so the board is never showing a finished
    cycle to someone who came to work on the live one.
    """
    t = today or dt.date.today()
    prev = (t.replace(day=1) - dt.timedelta(days=1)).strftime("%Y-%m")
    return prev if t <= cycle_for(prev).due_on else t.strftime("%Y-%m")


def recent_periods(n: int = 13, today: dt.date | None = None) -> list[str]:
    """Newest first, starting at the current cycle."""
    y, m = (int(x) for x in current_period(today).split("-"))
    out = []
    for _ in range(n):
        out.append(f"{y:04d}-{m:02d}")
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return out
