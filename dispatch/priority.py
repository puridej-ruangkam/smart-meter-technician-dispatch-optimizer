"""Reading dates, Billing State labels and the deadline-class penalties.

    reading day ahead, within ``critical_days``  -> CRITICAL
    reading day further out                      -> UNCRITICAL
    reading day today or past                    -> MISSED
    billing code unreadable                      -> UNKNOWN
    Billing Day "Urgent"                         -> URGENT
"""
from __future__ import annotations

import calendar
import logging
import re
from datetime import date, datetime

import numpy as np
import pandas as pd

from . import config as cfg
from .config import PriorityConfig

log = logging.getLogger(__name__)

_OFFSET_CODE = re.compile(r"^SP(\d+)$", re.IGNORECASE)


def _reading_day(year: int, month: int, fixed_day: int | None,
                 offset: int | None) -> int:
    """The day of ``month`` this meter is read, clamped to the month's length."""
    last = calendar.monthrange(year, month)[1]
    if offset is not None:
        return max(1, last - offset)
    return min(fixed_day, last)          # e.g. cycle 31 in a 30-day month


def _days_until_reading(today: datetime, fixed_day: int | None,
                        offset: int | None) -> int:
    """Whole days from today to the next occurrence of this reading day."""
    today_d = today.date() if isinstance(today, datetime) else today

    this_month = _reading_day(today_d.year, today_d.month, fixed_day, offset)
    if today_d.day <= this_month:
        return this_month - today_d.day

    year, month = (today_d.year, today_d.month + 1)
    if month > 12:
        year, month = year + 1, 1
    nxt = _reading_day(year, month, fixed_day, offset)
    return (date(year, month, nxt) - today_d).days


def days_until_reading(cycle: object, today: datetime) -> int | None:
    """Whole days from ``today`` to this meter's next reading day, or None when the code can't be read."""
    text = str(cycle).strip() if cycle is not None else ""

    m = _OFFSET_CODE.match(text)
    if m:
        fixed_day, offset = None, int(m.group(1)) - 1
    else:
        try:
            fixed_day = int(text)
        except (TypeError, ValueError):
            return None
        if not 1 <= fixed_day <= 31:
            return None
        offset = None

    return _days_until_reading(today, fixed_day, offset)


def is_urgent(cycle: object) -> bool:
    """True for a Billing Day the planner marked as urgent ("Urgent")."""
    return str(cycle).strip().lower() in cfg.URGENT_CODES if cycle is not None else False


def billing_state(days_until: int | None, pc: PriorityConfig) -> str:
    """The Billing State label for this meter."""
    # pd.isna, because missing values arrive as NaN, not None.
    if days_until is None or pd.isna(days_until):
        return cfg.BILLING_UNKNOWN
    if days_until <= 0:
        return cfg.BILLING_MISSED
    if days_until <= pc.critical_days:
        return cfg.BILLING_CRITICAL
    return cfg.BILLING_UNCRITICAL


# --------------------------------------------------------------------------
# Penalties are counted in the solver's arc unit: seconds or metres.
# --------------------------------------------------------------------------

#: The longest any one vehicle could drive in a day.
_SECONDS_PER_DAY = 24 * 60 * 60


def travel_units_per_minute(metric: str = "time",
                            speed_kmh: float = 60.0) -> float:
    """What one minute of driving costs in the objective's unit (metres in distance mode)."""
    if str(metric).strip().lower() == "distance":
        return max(float(speed_kmh), 1e-9) * 1000.0 / 60.0    # metres/minute
    return 60.0                                               # seconds/minute


def full_travel_ceiling(n_slots: int, days: int, metric: str = "time",
                        speed_kmh: float = 60.0) -> int:
    """More travel than all the vehicles could do in the plan; the step between penalty bands."""
    per_vehicle_day = _SECONDS_PER_DAY * travel_units_per_minute(
        metric, speed_kmh
    ) / 60.0
    return int(round(per_vehicle_day * max(int(n_slots), 1) * max(int(days), 1)))


def mandatory_penalties(
    scored: pd.DataFrame,
    horizon: list,
    plan_date,
    hc,
    capacity: int,
    travel_ceiling: int | None = None,
    calendar=None,
    cost_matrix=None,
) -> tuple[np.ndarray, list[str]]:
    """Penalties for leaving each meter out. Returns ``(penalties, classes)``.

    Each band is larger than everything below it put together:

        fill   > all the driving the teams could do
        window > every filler + all that driving
        last   > every window meter + every filler + all that driving

    With ``cost_matrix`` a filler's penalty also rises by how much closer it is to
    a must-do meter than the farthest filler.
    """
    from .workdays import (
        CLASS_AT_RISK, CLASS_TARGET, DEFAULT_CALENDAR, deadline_class,
        describe_window,
    )

    calendar = calendar or DEFAULT_CALENDAR
    n_slots = max(int(capacity), 1)
    days = max(len(horizon), 1)

    travel_ceiling = (
        int(travel_ceiling) if travel_ceiling is not None
        else full_travel_ceiling(n_slots, days)
    )
    target_end = hc.window_end(horizon, calendar)

    # Classify first: the size of each band depends on how many are below it.
    classes: list[str] = []
    for value in scored["Days Until Reading"]:
        d = None if value is None or pd.isna(value) else int(value)
        classes.append(deadline_class(
            d, horizon, plan_date, hc.buffer_days, calendar, target_end,
            last_dates=hc.last_chance_dates,
        ))
    # A job marked Urgent is must-do in the last-chance band. It has no
    # reading day, so no day of the plan is closed to it.
    n_urgent = 0
    if "Billing State" in scored.columns:
        for i, state in enumerate(scored["Billing State"]):
            if state == cfg.BILLING_URGENT:
                classes[i] = CLASS_AT_RISK
                n_urgent += 1
    n_last = sum(k == CLASS_AT_RISK for k in classes)
    n_window = sum(k == CLASS_TARGET for k in classes)
    n_fill = len(classes) - n_last - n_window

    # How far each filler is from the meters the routes must visit anyway.
    must = [i for i, k in enumerate(classes) if k in (CLASS_AT_RISK, CLASS_TARGET)]
    near = np.zeros(len(classes), dtype=np.int64)
    if cost_matrix is not None and must and n_fill:
        c = np.asarray(cost_matrix, dtype=np.int64)
        side = c[:, must] + c[must, :].T              # out to j and back
        detour = side.min(axis=1)
        fillers = [i for i, k in enumerate(classes)
                   if k not in (CLASS_AT_RISK, CLASS_TARGET)]
        top = int(detour[fillers].max())
        near[fillers] = top - detour[fillers]
    spread = int(near.max()) if len(near) else 0

    fill = travel_ceiling + 1
    fill_top = fill + spread
    window = fill_top * n_fill + travel_ceiling + 1
    last = window * n_window + fill_top * n_fill + travel_ceiling + 1

    # Every penalty dropped at once must still fit in a signed 64-bit integer.
    worst = last * n_last + window * n_window + fill_top * n_fill + travel_ceiling
    if worst >= 2 ** 62:
        raise ValueError(
            f"{len(scored)} meters is too many to rank safely "
            f"(the worst-case objective would be {worst:.3e}, past the 64-bit "
            f"limit). Plan fewer working days, or clear long-resolved meters "
            f"out of the Pending Job sheet."
        )

    band = {CLASS_AT_RISK: last, CLASS_TARGET: window}
    out = np.array([band.get(k, fill) for k in classes], dtype=np.int64)
    out = out + np.where(
        [k in (CLASS_AT_RISK, CLASS_TARGET) for k in classes], 0, near,
    ).astype(np.int64)
    if spread:
        log.debug(
            "Filler meters ranked by their side trip from the nearest must-do "
            "meter (0 to %d %s): nearer fillers are dropped last",
            spread, "s" if cost_matrix is not None else "",
        )

    room = n_slots * days
    log.debug(
        "Deadline bands: %d of %d meters are must-do - %d last chance (%s)%s, "
        "%d in the advance window%s",
        n_last + n_window, len(scored), n_last - n_urgent,
        f"read by the first working day after {horizon[-1]}"
        if hc.last_chance_dates is None else
        "reading dates picked on the calendar: "
        + (", ".join(d.isoformat() for d in sorted(hc.last_chance_dates)) or "none"),
        f" + {n_urgent} urgent (same band)" if n_urgent else "",
        n_window, f" ({describe_window(target_end)})",
    )
    if n_last > room:
        log.warning(
            "%d last-chance meters against %d slots. Some readings will be "
            "lost whatever the routes: that is a fleet-size problem, not a "
            "routing one.", n_last, room,
        )
    elif n_last + n_window > room:
        log.warning(
            "%d must-do meters against %d slots. Every last-chance meter that "
            "can be reached comes first; the advance window takes what is "
            "left, nearest first.", n_last + n_window, room,
        )
    return out, classes


def add_billing_state(
    eligible: pd.DataFrame,
    today: datetime | None = None,
    pc: PriorityConfig = cfg.DEFAULT_CONFIG.priority,
) -> pd.DataFrame:
    """Return a copy of ``eligible`` with Days Until Reading and Billing State."""
    today = today or datetime.now()
    df = eligible.copy()
    days_until = df[cfg.COL_BILLING_CYCLE].map(lambda c: days_until_reading(c, today))
    df["Days Until Reading"] = days_until
    df["Billing State"] = days_until.map(lambda d: billing_state(d, pc))
    urgent = df[cfg.COL_BILLING_CYCLE].map(is_urgent).astype(bool)
    df.loc[urgent, "Billing State"] = cfg.BILLING_URGENT

    counts = df["Billing State"].value_counts()
    log.info(
        "%d meters offered to the solver - %d urgent, %d reading within %d days, "
        "%d further out, %d missed",
        len(df), int(counts.get(cfg.BILLING_URGENT, 0)),
        int(counts.get(cfg.BILLING_CRITICAL, 0)), pc.critical_days,
        int(counts.get(cfg.BILLING_UNCRITICAL, 0)),
        int(counts.get(cfg.BILLING_MISSED, 0)),
    )
    return df
