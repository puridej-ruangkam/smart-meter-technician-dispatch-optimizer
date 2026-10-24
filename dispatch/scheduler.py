"""Hand the meters to the multi-day planner and turn its routes into the Daily Route table.

Customer No. is an id and never decides the order of anything.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

import pandas as pd

from dataclasses import dataclass, field

from . import config as cfg
from .config import AppConfig
from .distance import DistanceProvider
from .router import TeamRoute

log = logging.getLogger(__name__)

# The Daily Route sheet of workforce.xlsx.
OUTPUT_COLUMNS = [
    "Date",
    "Team",
    "Sequence",
    "Customer No.",
    "Purpose",
    "Billing Day",
    "Days Until Reading",
    "Billing State",
    "Latitude",
    "Longitude",
    "Estimated Distance (km)",
    "Estimated Arrival Time",
    "Start Depot",
    "End Depot",
    "Navigation Link",
]

# Google Maps links. Excel limits a link to 255 characters (six stops), so a
# longer route is split into several links.
_MAPS_DIR = "https://www.google.com/maps/dir/?api=1"
_MAX_WAYPOINTS = 6


#: End Depot shown for a day the crews do not drive back (Working Day "-").
NO_RETURN = "-"


def _point(lat: float, lon: float) -> str:
    return f"{lat:.5f},{lon:.5f}"


def navigation_links(points: list[tuple[float, float]]) -> list[tuple[int, str]]:
    """Google Maps driving directions through ``points`` in order.

    Returns ``(index of the point the link starts from, url)`` pairs; one pair
    unless the route has more than ``_MAX_WAYPOINTS`` stops in between.
    """
    links, start = [], 0
    while start < len(points) - 1:
        end = min(start + _MAX_WAYPOINTS + 1, len(points) - 1)
        via = points[start + 1:end]
        url = (f"{_MAPS_DIR}&origin={_point(*points[start])}"
               f"&destination={_point(*points[end])}"
               + (("&waypoints=" + "%7C".join(_point(*p) for p in via)) if via else "")
               + "&travelmode=driving")
        links.append((start, url))
        start = end
    return links


def _hyperlink(url: str, text: str) -> str:
    """An Excel formula that shows ``text`` and opens ``url``."""
    return f'=HYPERLINK("{url}","{text}")'


def roster_vehicles(app: AppConfig, days) -> tuple[list[int] | None, list | None, tuple | None]:
    """Per-vehicle job limits and depots for ``days``. Returns ``(capacities, depots, names)``.

    Vehicle ``k * teams + t`` is day ``k``, team ``t``. All None when there is no roster.
    """
    roster = app.roster
    if roster is None:
        return None, None, None
    caps, depots, names = [], [], []
    for day in days:
        d = day.date() if isinstance(day, datetime) else day
        crew_day = roster.on(d)
        if crew_day is None or not crew_day.works:
            raise ValueError(f"{d} has no work in the Working Day sheet")
        s, e = crew_day.start, crew_day.end      # e None: no drive back
        for jobs in crew_day.jobs:
            caps.append(int(jobs))
            depots.append(((s.lat, s.lon), None if e is None else (e.lat, e.lon)))
            names.append((s.name, NO_RETURN if e is None else e.name))
    return caps, depots, tuple(names)


def select_pool(scored: pd.DataFrame, app: AppConfig) -> pd.DataFrame:
    """Every routable meter, offered to the solver.

    Sorted by reading date (stable), which only fixes the node numbering.
    """
    routable = scored[scored["has_coords"]]
    skipped = len(scored) - len(routable)
    if skipped:
        log.warning("%d meters skipped: no coordinates", skipped)
    ordered = routable.sort_values(
        "Days Until Reading", na_position="last", kind="mergesort",
    ).reset_index(drop=True)
    log.debug(
        "Offering all %d routable meters to the solver for up to %d slots a day",
        len(ordered), app.daily_capacity,
    )
    return ordered


@dataclass
class PlanResult:
    """What the solve produced: the Daily Route table plus what the audit, map and history need."""

    table: pd.DataFrame
    routes: list[TeamRoute] = field(default_factory=list)
    pool: pd.DataFrame | None = None          # every meter offered
    selected_rows: list[int] = field(default_factory=list)  # positions in pool
    # The whole plan. The audit reads it; the Daily Route sheet never does.
    multiday: object | None = None
    # Every planned day as one table, for the map.
    week_table: pd.DataFrame | None = None


def _assemble_day(
    routes: list[TeamRoute],
    selected: pd.DataFrame,
    app: AppConfig,
    plan_date: datetime | None = None,
    multiday: object | None = None,
    quiet: bool = False,
) -> PlanResult:
    """Turn one day's solved routes into the Daily Route table."""
    fleet = app.fleet
    plan_date = plan_date or datetime.now()
    if not isinstance(plan_date, datetime):
        plan_date = datetime.combine(plan_date, datetime.min.time())
    day_start = datetime.combine(plan_date.date(), fleet.workday_start)
    crew_day = app.roster.on(plan_date.date()) if app.roster is not None else None

    # Fillers that were UNCRITICAL are shown as NEARBY.
    from .workdays import CLASS_AT_RISK, CLASS_TARGET

    classes = list(getattr(multiday, "classes", None) or [])

    def billing_state(job, index: int):
        state = job.get("Billing State", "")
        if (state == cfg.BILLING_UNCRITICAL and index < len(classes)
                and classes[index] not in (CLASS_AT_RISK, CLASS_TARGET)):
            return cfg.BILLING_NEARBY
        return state

    rows = []
    selected_rows: list[int] = []
    for route in routes:
        clock = day_start
        first_row = len(rows)
        for seq, stop in enumerate(route.stops, start=1):
            job = selected.iloc[stop.job_index]
            selected_rows.append(int(stop.job_index))
            if stop.leg_minutes is not None:
                travel_minutes = stop.leg_minutes      # real road driving time
            else:
                travel_minutes = stop.leg_km / fleet.avg_speed_kmh * 60.0
            clock += timedelta(minutes=travel_minutes)
            rows.append(
                {
                    "Date": plan_date.date().isoformat(),
                    "Team": route.team,
                    "Sequence": seq,
                    "Customer No.": job[cfg.COL_CUSTOMER],
                    "Purpose": job.get("Purpose", ""),
                    "Billing Day": str(job[cfg.COL_BILLING_CYCLE]).strip(),
                    "Days Until Reading": (
                        "" if pd.isna(job.get("Days Until Reading"))
                        else int(job["Days Until Reading"])
                    ),
                    "Billing State": billing_state(job, int(stop.job_index)),
                    "Latitude": round(float(job["Latitude"]), 6),
                    "Longitude": round(float(job["Longitude"]), 6),
                    "Estimated Distance (km)": round(stop.leg_km, 2),
                    "Estimated Arrival Time": clock.strftime("%H:%M"),
                    "Start Depot": route.start_depot or (
                        crew_day.start.name if crew_day else ""),
                    "End Depot": route.end_depot or (
                        crew_day.end.name if crew_day else ""),
                    "Navigation Link": "",
                }
            )
            clock += timedelta(minutes=fleet.service_minutes)

        # Links on the row of the stop each one starts from.
        if route.stops:
            start = ((crew_day.start.lat, crew_day.start.lon)
                     if crew_day and crew_day.start else (fleet.depot_lat, fleet.depot_lon))
            if crew_day is None:
                end = [start]
            elif crew_day.end is None:
                end = []
            else:
                end = [(crew_day.end.lat, crew_day.end.lon)]
            stops = [(r["Latitude"], r["Longitude"]) for r in rows[first_row:]]
            links = navigation_links([start] + stops + end)
            for part, (k, url) in enumerate(links, start=1):
                text = f"นำทางทีม {route.team}" + (f" ({part}/{len(links)})"
                                                    if len(links) > 1 else "")
                rows[first_row + max(k - 1, 0)]["Navigation Link"] = _hyperlink(url, text)

    plan = pd.DataFrame(rows, columns=OUTPUT_COLUMNS)
    detail = PlanResult(
        table=plan, routes=routes, pool=selected,
        selected_rows=selected_rows, multiday=multiday,
    )
    had_real_times = any(s.leg_minutes is not None for r in routes for s in r.stops)
    listed_km = plan["Estimated Distance (km)"].sum() if len(plan) else 0.0
    return_km = sum(r.return_km for r in routes)

    if quiet:                                # already reported
        return detail
    log.debug(
        "Day %s laid out: %d jobs, %.1f km listed in the sheet (%s)",
        plan_date.date().isoformat(), len(plan), listed_km,
        "real driving times" if had_real_times
        else f"times assumed at {fleet.avg_speed_kmh:.0f} km/h",
    )
    if crew_day is not None and crew_day.end is None:
        log.info("  No drive back to a depot on %s (Ending Branch Depot '-'): "
                 "each route ends at its last job", plan_date.date().isoformat())
    if return_km:
        # The sheet has one row per job, so the drive home has nowhere to sit.
        # Report it, or the day's real mileage looks smaller than it is.
        log.debug(
            "Plus %.1f km driving back to base (not listed as rows) "
            "= %.1f km actually driven", return_km, listed_km + return_km,
        )
        for route in routes:
            if route.stops and route.return_minutes is not None:
                back = (
                    datetime.strptime(
                        plan.loc[plan["Team"] == route.team,
                                 "Estimated Arrival Time"].iloc[-1], "%H:%M")
                    + timedelta(minutes=fleet.service_minutes + route.return_minutes)
                )
                log.debug(
                    "  Team %d back at base around %s (%.1f km)",
                    route.team, back.strftime("%H:%M"), route.return_km,
                )
    return detail


def _assemble_week(week, selected: pd.DataFrame, app: AppConfig,
                   first_reported: bool = False) -> PlanResult:
    """Every planned day in one table, each row dated with its own day.

    Each day is assembled on its own, so the arrival clock restarts at the
    start of each working day rather than running on from the day before.
    """
    parts, detail = [], None
    for k, day in enumerate(week.horizon):
        routes = week.day(k)
        if not routes:
            continue
        when = datetime.combine(day, datetime.min.time())
        one = _assemble_day(routes, selected, app, when, multiday=week,
                            quiet=first_reported and k == 0)
        table = one.table.copy()
        table["Date"] = day.isoformat()
        parts.append(table)
        if detail is None:
            detail = one
        else:
            detail.routes.extend(one.routes)
            detail.selected_rows.extend(one.selected_rows)

    if detail is None:                       # nothing was scheduled at all
        return PlanResult(
            table=pd.DataFrame(columns=OUTPUT_COLUMNS),
            pool=selected, multiday=week,
        )
    detail.table = pd.concat(parts, ignore_index=True)
    log.debug(
        "All %d planned day(s) laid out: %d rows (%s)",
        len(parts), len(detail.table),
        "all go to the Daily Route sheet" if app.horizon.publish_all_days
        else "for the map; the Daily Route sheet gets the first day only",
    )
    return detail


def build_plan(
    selected: pd.DataFrame,
    app: AppConfig,
    provider: DistanceProvider,
    plan_date: datetime | None = None,
) -> PlanResult:
    """Plan every working day in one solve and return the published day.

    ``provider`` is injected rather than constructed here so that the caller
    owns the routing engine's lifetime -- see ``pipeline.run_pipeline``.
    """
    from .multiday import plan_week

    when = plan_date or datetime.now()
    week = plan_week(selected, app, provider, when)
    if app.horizon.publish_all_days:
        return _assemble_week(week, selected, app)
    detail = _assemble_day(week.day(0), selected, app, when, multiday=week)
    # The sheet gets the first day only; the map gets every day.
    if len(week.horizon) > 1:
        try:
            detail.week_table = _assemble_week(week, selected, app,
                                               first_reported=True).table
        except Exception as exc:              # the map is never fatal
            log.warning("Could not lay out every day for the map (%s); it "
                        "will show the first day only.", exc)
    return detail
