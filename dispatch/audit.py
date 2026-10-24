"""Check a solved plan before anything is written.

Every check works from the routes themselves, not from the solver's constraints.
A failed check raises.
"""
from __future__ import annotations

import logging
from collections import Counter

import pandas as pd

from . import config as cfg
from .config import AppConfig

log = logging.getLogger(__name__)


class AuditError(RuntimeError):
    """The plan breaks a rule it is not allowed to break."""


def audit_multiday(week, app: AppConfig) -> dict:
    """Validate a multi-day plan before any of it is published.

    Raises if a team has two routes on one day or too many stops, if a meter
    appears twice, or if a meter comes on or after its reading day.
    """
    from .workdays import allowed_day_indices

    problems: list[str] = []
    seen: dict[int, tuple[int, int]] = {}        # job -> (day, team)
    per_slot: Counter = Counter()

    for route in week.routes:
        per_slot[(route.day, route.team)] += 1
        limit = app.team_limit(route.day, route.team)
        if len(route.stops) > limit:
            problems.append(
                f"day {route.day} team {route.team} has {len(route.stops)} "
                f"jobs, more than the {limit} the Working Day sheet allows"
            )
        for stop in route.stops:
            if stop.job_index in seen:
                day, team = seen[stop.job_index]
                problems.append(
                    f"meter at row {stop.job_index} is scheduled twice: "
                    f"day {day} team {team} and day {route.day} team {route.team}"
                )
            seen[stop.job_index] = (route.day, route.team)

    doubled = [slot for slot, n in per_slot.items() if n > 1]
    if doubled:
        problems.append(
            f"{len(doubled)} crew-day(s) carry more than one route: {doubled[:10]}"
        )

    # The deadline check, re-derived from the dates.
    late = []
    if week.pool is not None and "Days Until Reading" in week.pool.columns:
        for job, (day, _team) in seen.items():
            value = week.pool.iloc[job]["Days Until Reading"]
            d = None if value is None or pd.isna(value) else int(value)
            allowed = allowed_day_indices(
                d, week.horizon, week.plan_date, app.horizon.buffer_days,
            )
            if day not in allowed:
                late.append((job, day, d))
    if late:
        problems.append(
            f"{len(late)} meter(s) are scheduled on a day that is too late for "
            f"their reading: rows {[r[0] for r in late[:10]]}"
        )

    if problems:
        raise AuditError(
            "The multi-day plan failed validation and will not be published:\n  - "
            + "\n  - ".join(problems)
        )

    jobs = sum(len(r.stops) for r in week.routes)
    log.info(
        "Multi-day audit passed: %d jobs over %d day(s), no duplicates, no crew "
        "double-booked, every job inside its deadline",
        jobs, len(week.horizon),
    )
    return {
        "multiday_jobs": jobs,
        "multiday_days": len(week.horizon),
        "multiday_km": round(sum(r.total_km for r in week.routes), 1),
    }


def audit_solution(detail, app: AppConfig) -> dict:
    """Validate the day's routes and return the figures worth reporting.

    Raises ``AuditError`` on capacity, duplication or assignment violations.
    """
    routes = detail.routes
    pool = detail.pool
    fleet = app.fleet
    problems: list[str] = []

    # When the day came out of a multi-day layout, the week is checked first: a
    # sound Monday inside an unsound week is still not publishable.
    multiday_report = (
        audit_multiday(detail.multiday, app)
        if getattr(detail, "multiday", None) is not None else {}
    )

    # -- capacity ----------------------------------------------------------
    for route in routes:
        limit = app.team_limit(getattr(route, "day", 0) or 0, route.team)
        if len(route.stops) > limit:
            problems.append(
                f"team {route.team} has {len(route.stops)} jobs, more than the "
                f"{limit} it may take that day"
            )

    # -- one team per meter, one visit per meter ---------------------------
    visits = Counter()
    team_of: dict[int, int] = {}
    for route in routes:
        for stop in route.stops:
            visits[stop.job_index] += 1
            if stop.job_index in team_of and team_of[stop.job_index] != route.team:
                problems.append(
                    f"meter at row {stop.job_index} is assigned to both team "
                    f"{team_of[stop.job_index]} and team {route.team}"
                )
            team_of.setdefault(stop.job_index, route.team)

    repeated = [i for i, n in visits.items() if n > 1]
    if repeated:
        problems.append(
            f"{len(repeated)} meter(s) are visited more than once: "
            f"rows {sorted(repeated)[:10]}"
        )

    # -- nothing invented --------------------------------------------------
    if pool is not None:
        out_of_range = [i for i in visits if not 0 <= i < len(pool)]
        if out_of_range:
            problems.append(f"routed rows outside the candidate list: {out_of_range[:10]}")

    if problems:
        raise AuditError(
            "The routing solution failed validation and will not be "
            "published:\n  - " + "\n  - ".join(problems)
        )

    # -- figures worth reporting ------------------------------------------
    jobs = sum(len(r.stops) for r in routes)
    travel_km = sum(r.total_km for r in routes)
    drive = [r.drive_minutes for r in routes]
    travel_min = sum(m for m in drive if m is not None) if all(
        m is not None for m in drive) else None
    service_min = jobs * fleet.service_minutes

    report = {
        "teams_used": sum(1 for r in routes if r.stops),
        "jobs": jobs,
        "travel_km": travel_km,
        "travel_minutes": travel_min,
        "service_minutes": service_min,
        "workload_minutes": None if travel_min is None else travel_min + service_min,
    }

    if pool is not None and "Billing State" in pool.columns:
        chosen = pool.iloc[sorted(visits)] if visits else pool.iloc[[]]
        for state in (cfg.BILLING_CRITICAL, cfg.BILLING_UNCRITICAL,
                      cfg.BILLING_MISSED, cfg.BILLING_UNKNOWN):
            available = int((pool["Billing State"] == state).sum())
            taken = int((chosen["Billing State"] == state).sum()) if len(chosen) else 0
            report[f"{state.lower()}_taken"] = taken
            report[f"{state.lower()}_available"] = available

    report.update(multiday_report)
    log.info(
        "Audit passed: %d jobs across %d team(s), no duplicates, no team over "
        "its limit for the day", jobs, report["teams_used"],
    )
    return report
