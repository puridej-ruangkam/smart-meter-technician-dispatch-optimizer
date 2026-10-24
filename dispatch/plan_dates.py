"""Date rules behind the planning screen's calendar, kept free of drawing code."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from .workdays import (
    DEFAULT_CALENDAR,
    WorkCalendar,
    calendar_for_dates,
    last_chance_boundary,
    target_window_end,
    working_days,
)

#: At least one working day; the config caps a plan at fourteen.
MIN_WORK_DAYS = 1
MAX_WORK_DAYS = 14

# Reading-date classes, as the calendar shows them.
LOST = "lost"          # read on or before the first work day: too late
LAST = "last"          # รอบบิลที่ต้องซ่อมให้ทัน
WINDOW = "window"      # รอบบิลที่จะซ่อมล่วงหน้า
LATER = "later"        # not prepared for: filler only

THAI_MONTHS = ["", "ม.ค.", "ก.พ.", "มี.ค.", "เม.ย.", "พ.ค.", "มิ.ย.",
               "ก.ค.", "ส.ค.", "ก.ย.", "ต.ค.", "พ.ย.", "ธ.ค."]
THAI_MONTHS_FULL = ["", "มกราคม", "กุมภาพันธ์", "มีนาคม", "เมษายน", "พฤษภาคม",
                    "มิถุนายน", "กรกฎาคม", "สิงหาคม", "กันยายน", "ตุลาคม",
                    "พฤศจิกายน", "ธันวาคม"]


def thai_day(day: date) -> str:
    """'9 มี.ค.'"""
    return f"{day.day} {THAI_MONTHS[day.month]}"


def thai_days(days) -> str:
    """'1, 2, 4, 5 มี.ค.' -- the month written once per run of the same month."""
    days = sorted(days)
    if not days:
        return "-"
    parts, run = [], []
    for d in days:
        if run and d.month != run[-1].month:
            parts.append(", ".join(str(x.day) for x in run)
                         + f" {THAI_MONTHS[run[-1].month]}")
            run = []
        run.append(d)
    parts.append(", ".join(str(x.day) for x in run)
                 + f" {THAI_MONTHS[run[-1].month]}")
    return " · ".join(parts)


def thai_span(days) -> str:
    """'2 – 8 มี.ค.' when the days run without a gap, else ``thai_days``."""
    days = sorted(days)
    if not days:
        return "-"
    if len(days) > 2 and (days[-1] - days[0]).days == len(days) - 1:
        return f"{thai_day(days[0])} – {thai_day(days[-1])}"
    return thai_days(days)


def thai_month_title(first: date, last: date) -> str:
    """'มีนาคม 2570' or 'มีนาคม – เมษายน 2570', in the Buddhist era."""
    y1, y2 = first.year + 543, last.year + 543
    if (first.year, first.month) == (last.year, last.month):
        return f"{THAI_MONTHS_FULL[first.month]} {y1}"
    if first.year == last.year:
        return (f"{THAI_MONTHS_FULL[first.month]} – "
                f"{THAI_MONTHS_FULL[last.month]} {y1}")
    return (f"{THAI_MONTHS_FULL[first.month]} {y1} – "
            f"{THAI_MONTHS_FULL[last.month]} {y2}")


def _as_date(value) -> date:
    return value.date() if isinstance(value, datetime) else value


def _span(first: date, last: date):
    day = first
    while day <= last:
        yield day
        day += timedelta(days=1)


@dataclass
class PlanDates:
    """The two sets the planner has picked, and what follows from them."""

    work: set = field(default_factory=set)
    window: set = field(default_factory=set)
    calendar: WorkCalendar = field(default_factory=lambda: DEFAULT_CALENDAR)
    #: True while the advance window is still filled automatically.
    window_auto: bool = True
    #: รอบบิลที่ต้องซ่อมให้ทัน as picked by hand, or ``None`` while it still
    #: follows the work days (``default_last``).
    last: set | None = None

    @classmethod
    def default(cls, today: date, calendar: WorkCalendar = DEFAULT_CALENDAR,
                work_days: int = 5, window_days: int = 5) -> "PlanDates":
        """Five working days from today, and the five working days after
        the last-chance boundary prepared in advance -- every calendar day in
        that stretch, weekends included, because meters are read on weekends
        too."""
        today = _as_date(today)
        horizon = working_days(today, work_days, calendar)
        plan = cls(work=set(horizon), window=set(), calendar=calendar)
        end = target_window_end(horizon, window_days, calendar)
        if end is not None:
            plan.window = set(_span(plan.boundary + timedelta(days=1), end))
        return plan

    # -- derived ---------------------------------------------------------

    @property
    def horizon(self) -> list[date]:
        return sorted(self.work)

    @property
    def effective_calendar(self) -> WorkCalendar:
        """The base calendar with the picks applied, as main.py will use it."""
        return calendar_for_dates(self.work, self.calendar)

    @property
    def boundary(self) -> date | None:
        """The last reading date that counts as last chance."""
        if not self.work:
            return None
        return last_chance_boundary(self.horizon, self.effective_calendar)

    def is_work_day(self, day: date) -> bool:
        return day in self.work

    @property
    def default_last(self) -> set:
        """What the system works out: the day after the first work day up to
        the first working day after the plan."""
        if not self.work:
            return set()
        return set(_span(self.horizon[0] + timedelta(days=1), self.boundary))

    @property
    def last_days(self) -> set:
        """รอบบิลที่ต้องซ่อมให้ทัน in force: the picks, or the default."""
        return self.default_last if self.last is None else set(self.last)

    @property
    def last_is_picked(self) -> bool:
        return self.last is not None and self.last != self.default_last

    def reading_class(self, day: date) -> str:
        """Which group a meter read on ``day`` falls into for this plan."""
        if not self.work:
            return LATER
        if day <= self.horizon[0]:
            return LOST
        if day in self.last_days:
            return LAST
        if day in self.window:
            return WINDOW
        return LATER

    def unprotected(self) -> list[date]:
        """Readings no later plan can reach that are not picked as must-repair."""
        return sorted(self.default_last - self.last_days)

    def skipped_weekdays(self) -> list[date]:
        """Normal working days inside the work span that were left out."""
        if len(self.work) < 2:
            return []
        h = self.horizon
        return [d for d in _span(h[0], h[-1])
                if d not in self.work and self.calendar.is_working(d)]

    def extra_days(self) -> list[date]:
        """Picked days the normal calendar does not work -- a Saturday, say."""
        return sorted(d for d in self.work if not self.calendar.is_working(d))

    def window_gaps(self) -> list[date]:
        """Reading dates inside the picked window range that were not picked.

        Worth saying out loud: meters are read on weekends and holidays too,
        so a gap means those meters will not be prepared in advance.
        """
        if len(self.window) < 2:
            return []
        w, last = sorted(self.window), self.last_days
        return [d for d in _span(w[0], w[-1])
                if d not in self.window and d not in last]

    # -- picking ------------------------------------------------------------
    # Each call returns a short Thai message to show, or "" when all is fine.

    def toggle_work(self, day: date) -> str:
        day = _as_date(day)
        if day in self.work:
            self.work.discard(day)
            return ""
        if len(self.work) >= MAX_WORK_DAYS:
            return f"ออกงานได้ไม่เกิน {MAX_WORK_DAYS} วัน"
        self.work.add(day)
        if not self.calendar.is_working(day):
            return (f"{thai_day(day)} ปกติเป็นวันหยุด "
                    f"ระบบจะนับเป็นวันออกงานพิเศษสำหรับแผนนี้")
        return ""

    def toggle_window(self, day: date) -> str:
        day = _as_date(day)
        if day in self.window:
            self.window.discard(day)
            self.window_auto = False
            return ""
        if not self.work:
            return "เลือกวันทำงานก่อน"
        if day <= self.horizon[0]:
            return (f"มิเตอร์ที่อ่านหน่วย {thai_day(day)} ไม่มีวันทำงานเหลือ"
                    f"ก่อนวันอ่านแล้ว")
        if day in self.last_days:
            return (f"{thai_day(day)} เป็นรอบบิลที่ต้องซ่อมให้ทันอยู่แล้ว "
                    f"ถ้าจะย้ายมาซ่อมล่วงหน้า ให้เอาออกจากรอบบิลที่ต้องซ่อมให้ทันก่อน")
        self.window.add(day)
        self.window_auto = False
        return ""

    def toggle_last(self, day: date) -> str:
        """Add or remove a reading date from รอบบิลที่ต้องซ่อมให้ทัน."""
        day = _as_date(day)
        if not self.work:
            return "เลือกวันทำงานก่อน"
        picked = self.last_days
        if day in picked:
            picked.discard(day)
            self.last = picked
            if day in self.default_last:
                return (f"เอา {thai_day(day)} ออกแล้ว — แผนถัดไปมาไม่ทันวันอ่านนี้ "
                        f"ถ้าไม่ได้ทำในแผนนี้ มิเตอร์ที่อ่านวันนั้นจะเสียรอบบิล")
            return ""
        if day <= self.horizon[0]:
            return (f"มิเตอร์ที่อ่านหน่วย {thai_day(day)} ไม่มีวันทำงานเหลือ"
                    f"ก่อนวันอ่านแล้ว เลือกเป็นรอบบิลที่ต้องซ่อมให้ทันไม่ได้")
        picked.add(day)
        self.last = picked
        if day in self.window:
            self.window.discard(day)
            return (f"ย้าย {thai_day(day)} จากรอบบิลที่จะซ่อมล่วงหน้า "
                    f"มาเป็นรอบบิลที่ต้องซ่อมให้ทัน")
        return ""

    def reset_last(self) -> None:
        """Back to what the system works out from the work days."""
        self.last = None
        self.window -= self.default_last

    def finish_last(self) -> str:
        gaps = self.unprotected()
        self.window -= self.last_days
        if gaps:
            return (f"ไม่ได้เลือก {thai_days(gaps)} — แผนถัดไปมาไม่ทันวันอ่านเหล่านี้ "
                    f"มิเตอร์ที่อ่านวันนั้นจะไม่ได้ทำก่อนงานอื่น และอาจเสียรอบบิล")
        return ""

    def finish_work(self) -> str:
        """Check the work days; drop window dates the new plan has swallowed."""
        error = self.work_error()
        if error:
            return error
        if self.last is not None:
            self.last = {d for d in self.last if d > self.horizon[0]}
        if self.window_auto:
            self.window = self.default_window() - self.last_days
            return ""
        swallowed = sorted(d for d in self.window
                           if d <= self.horizon[0] or d in self.last_days)
        if swallowed:
            self.window -= set(swallowed)
            return (f"ตัด {thai_days(swallowed)} ออกจากรอบบิลที่จะซ่อมล่วงหน้า "
                    f"เพราะตอนนี้เป็นรอบบิลที่ต้องซ่อมให้ทันแล้ว")
        return ""

    def finish_window(self) -> str:
        gaps = self.window_gaps()
        if gaps:
            return (f"ไม่ได้เลือก {thai_days(gaps)} — มิเตอร์ที่อ่านหน่วยวันนั้น"
                    f"จะไม่ถูกเตรียมล่วงหน้า (เป็นงานเติมเท่านั้น)")
        return ""

    def work_error(self) -> str:
        if len(self.work) < MIN_WORK_DAYS:
            return f"ต้องเลือกวันทำงานอย่างน้อย {MIN_WORK_DAYS} วัน"
        if len(self.work) > MAX_WORK_DAYS:
            return f"เลือกวันทำงานได้ไม่เกิน {MAX_WORK_DAYS} วัน"
        return ""

    def clear_window(self) -> None:
        self.window = set()
        self.window_auto = False

    def default_window(self, window_days: int = 5) -> set:
        """The reading dates from the day after the last-chance boundary to the
        ``window_days``-th working day after the plan, weekends included,
        because meters are read on weekends too."""
        if not self.work:
            return set()
        end = target_window_end(self.horizon, window_days,
                                self.effective_calendar)
        if end is None:
            return set()
        return set(_span(self.boundary + timedelta(days=1), end))

    # -- checking against the Working Day sheet --------------------------

    def check_roster(self, roster, today: date) -> tuple[list[date], list[date]]:
        """``(missing, not_picked)`` compared with the Working Day sheet.

        ``missing`` are picked days with no jobs in the sheet. ``not_picked`` are days
        with jobs that were not picked, as a reminder.
        """
        today = _as_date(today)
        sheet = [c.day for c in roster.working_days(since=today)]
        missing = sorted(d for d in self.work
                         if roster.on(d) is None or not roster.on(d).works)
        if self.work:
            last = self.horizon[-1]
            not_picked = [d for d in sheet if d <= last and d not in self.work]
        else:
            not_picked = sheet
        return missing, not_picked

    # -- handing over ----------------------------------------------------

    def cli_args(self) -> list[str]:
        """The flags that make ``main.py`` plan exactly what is on screen.

        ``--window-dates`` is always sent, empty when nothing is picked.
        """
        h = self.horizon
        return [
            "--date", h[0].isoformat(),
            "--work-dates", ",".join(d.isoformat() for d in h),
            "--last-dates", ",".join(d.isoformat() for d in self._last_kept()),
            "--window-dates", ",".join(d.isoformat() for d in self._window_kept()),
        ]

    def _last_kept(self) -> list[date]:
        if not self.work:
            return []
        return sorted(d for d in self.last_days if d > self.horizon[0])

    def _window_kept(self) -> list[date]:
        if not self.work:
            return []
        last = self.last_days
        return sorted(d for d in self.window
                      if d > self.horizon[0] and d not in last)

    def summary(self) -> list[str]:
        """Lines of Thai for under the calendar."""
        if not self.work:
            return ["ยังไม่ได้เลือกวันทำงาน"]
        work = f"วันออกงาน {len(self.work)} วัน: {thai_days(self.work)}"
        skipped, extra = self.skipped_weekdays(), self.extra_days()
        if skipped:
            work += f" · ข้าม {thai_days(skipped)}"
        if extra:
            work += f" · รวมวันหยุด {thai_days(extra)}"
        last = self._last_kept()
        if not last:
            last_text = "รอบบิลที่ต้องซ่อมให้ทัน: ไม่มี"
        else:
            last_text = (f"รอบบิลที่ต้องซ่อมให้ทัน: อ่านหน่วย {thai_span(last)} "
                         f"({len(last)} วัน)")
        last_text += "  (เลือกเอง)" if self.last_is_picked else "  (ระบบคำนวณให้)"
        gaps = self.unprotected()
        if gaps:
            last_text += f" · ไม่รวม {thai_days(gaps)} ซึ่งแผนถัดไปมาไม่ทัน"
        lines = [work, last_text]
        kept = self._window_kept()
        if not kept:
            lines.append("รอบบิลที่จะซ่อมล่วงหน้า: ไม่มี")
        else:
            text = (f"รอบบิลที่จะซ่อมล่วงหน้า: อ่านหน่วย "
                    f"{thai_day(kept[0])} – {thai_day(kept[-1])} "
                    f"({len(kept)} วัน)")
            gaps = self.window_gaps()
            if gaps:
                text += f" · ยกเว้น {thai_days(gaps)}"
            lines.append(text)
        return lines
