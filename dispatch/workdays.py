"""Working-day calendar: which dates the teams go out, and which plan days each meter may use.

Days until reading are calendar days; plan day ``k`` indexes working days.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

def _as_date(value: date | datetime) -> date:
    return value.date() if isinstance(value, datetime) else value


@dataclass(frozen=True)
class WorkCalendar:
    """Which days the teams work. A date that is both extra and a holiday is a holiday."""

    weekdays: frozenset[int] = frozenset({0, 1, 2, 3, 4})   # Mon-Fri
    extra_days: frozenset[date] = field(default_factory=frozenset)
    holidays: frozenset[date] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        if not self.weekdays and not self.extra_days:
            raise ValueError(
                "WorkCalendar has no working days at all: set weekdays or "
                "extra_days, or the planner can never schedule anything."
            )
        for day in self.weekdays:
            if not 0 <= day <= 6:
                raise ValueError(
                    f"WorkCalendar.weekdays takes Python weekday numbers "
                    f"0 (Monday) to 6 (Sunday), got {day!r}"
                )

    def is_working(self, day: date | datetime) -> bool:
        """True when crews go out on this date."""
        day = _as_date(day)
        if day in self.holidays:
            return False
        return day.weekday() in self.weekdays or day in self.extra_days


#: Monday to Friday, no Saturdays, no holidays.
DEFAULT_CALENDAR = WorkCalendar()


def working_days(
    start: date | datetime,
    count: int,
    calendar: WorkCalendar = DEFAULT_CALENDAR,
    max_lookahead_days: int = 60,
) -> list[date]:
    """The dates behind plan days ``k = 0 .. count-1``, starting at the first working day."""
    if count <= 0:
        return []
    day = _as_date(start)
    days: list[date] = []
    for _ in range(max_lookahead_days):
        if calendar.is_working(day):
            days.append(day)
            if len(days) == count:
                return days
        day += timedelta(days=1)
    raise ValueError(
        f"Could not find {count} working days within {max_lookahead_days} days "
        f"of {_as_date(start)}. Check WorkCalendar.holidays and .weekdays."
    )


def next_working_day(after: date | datetime,
                      calendar: WorkCalendar = DEFAULT_CALENDAR,
                      max_lookahead_days: int = 60) -> date:
    """The first working day strictly after ``after``."""
    day = _as_date(after) + timedelta(days=1)
    for _ in range(max_lookahead_days):
        if calendar.is_working(day):
            return day
        day += timedelta(days=1)
    raise ValueError(
        f"No working day within {max_lookahead_days} days after {_as_date(after)}"
    )


def allowed_day_indices(
    days_until_reading: int | None,
    horizon: list[date],
    plan_date: date | datetime,
    buffer_days: int = 0,
) -> list[int]:
    """Which plan days ``k`` this meter may be repaired on: strictly before its reading day.

    Returns every index when the meter has no deadline inside the plan.
    """
    if not horizon:
        return []
    if days_until_reading is None or days_until_reading <= 0:
        return list(range(len(horizon)))

    plan_day = _as_date(plan_date)
    # Last calendar date on which a repair still saves this reading.
    deadline = plan_day + timedelta(days=int(days_until_reading) - 1 - int(buffer_days))
    allowed = [k for k, day in enumerate(horizon) if day <= deadline]
    if allowed:
        return allowed
    # No day in the plan can save the reading; the meter may go on any day.
    return list(range(len(horizon)))


# Deadline classes.
CLASS_AT_RISK = "A"   # this plan is the meter's last chance at its reading
CLASS_TARGET = "T"    # reading falls in the target window just beyond the plan
CLASS_AHEAD = "B"     # reading further out still: worth doing only when cheap
CLASS_LOST = "C"      # reading already gone, unreadable cycle, or unreachable


def target_window_end(
    horizon: list[date],
    target_days: int,
    calendar: WorkCalendar = DEFAULT_CALENDAR,
) -> date | None:
    """The last date of the advance window, counted in working days after the
    last-chance boundary. None when there is no window.
    """
    if not horizon or target_days <= 0:
        return None
    day = last_chance_boundary(horizon, calendar)
    for _ in range(int(target_days)):
        day = next_working_day(day, calendar)
    return day


def last_chance_boundary(
    horizon: list[date],
    calendar: WorkCalendar = DEFAULT_CALENDAR,
) -> date | None:
    """The latest reading date this plan is the last chance for: the first working
    day after the plan ends.
    """
    if not horizon:
        return None
    return next_working_day(horizon[-1], calendar)


def deadline_class(
    days_until_reading: int | None,
    horizon: list[date],
    plan_date: date | datetime,
    buffer_days: int = 0,
    calendar: WorkCalendar = DEFAULT_CALENDAR,
    target_end: date | None = None,
    last_dates=None,
) -> str:
    """The meter's deadline class.

    ``CLASS_AT_RISK``  last chance: no later plan can reach it in time
    ``CLASS_TARGET``   inside the advance window
    ``CLASS_AHEAD``    further out
    ``CLASS_LOST``     reading day passed, unreadable code, or no working day left

    ``last_dates``, when given, are the last-chance dates picked on the calendar.
    """
    if not horizon or days_until_reading is None or days_until_reading <= 0:
        return CLASS_LOST

    plan_day = _as_date(plan_date)
    reading_day = plan_day + timedelta(days=int(days_until_reading))
    deadline = reading_day - timedelta(days=1 + int(buffer_days))

    if deadline < horizon[0]:
        return CLASS_LOST            # no working day left before the reading

    if last_dates is not None:
        if reading_day in last_dates:
            return CLASS_AT_RISK
        return CLASS_TARGET if in_window(reading_day, target_end) else CLASS_AHEAD

    # Last chance means the next plan can't do it, which matters across a weekend.
    if deadline < next_working_day(horizon[-1], calendar):
        return CLASS_AT_RISK
    if in_window(reading_day, target_end):
        return CLASS_TARGET
    return CLASS_AHEAD


def in_window(reading_day: date, window) -> bool:
    """Whether a reading date is in the advance window.

    ``window`` is a last date, a set of picked dates, or None.
    """
    if window is None:
        return False
    if isinstance(window, (set, frozenset, list, tuple)):
        return reading_day in window
    return reading_day <= window


def describe_window(window) -> str:
    """The window in words for a log line."""
    if window is None:
        return "no advance window"
    if isinstance(window, (set, frozenset, list, tuple)):
        days = sorted(window)
        if not days:
            return "no advance window"
        return (f"{len(days)} picked reading date(s) "
                f"{days[0].isoformat()} to {days[-1].isoformat()}")
    return f"readings up to {window.isoformat()}"


def calendar_for_dates(
    work_dates,
    base: WorkCalendar = DEFAULT_CALENDAR,
) -> WorkCalendar:
    """A calendar on which exactly the picked dates are the working days.

    Outside the picked span the base calendar is unchanged.
    """
    days = sorted({_as_date(d) for d in work_dates})
    if not days:
        return base
    holidays = set(base.holidays)
    extra = set(base.extra_days)
    day = days[0]
    picked = set(days)
    while day <= days[-1]:
        if day in picked:
            holidays.discard(day)
            if not base.is_working(day):
                extra.add(day)
        elif base.is_working(day):
            holidays.add(day)
        day += timedelta(days=1)
    return WorkCalendar(
        weekdays=base.weekdays,
        extra_days=frozenset(extra),
        holidays=frozenset(holidays),
    )
