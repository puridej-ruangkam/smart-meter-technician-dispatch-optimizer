"""End-to-end dispatch pipeline: load -> plan -> audit -> write."""
from __future__ import annotations

import logging
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import pandas as pd

from . import config as cfg
from .config import AppConfig
from .distance import make_provider
from .excel_writer import write_daily_route
from .audit import audit_solution
from .history import history_rows
from .progress import report
from .priority import add_billing_state
from .scheduler import build_plan, select_pool
from .workforce import load_pending_jobs, load_roster
from . import saved_plan

log = logging.getLogger(__name__)


def run_pipeline(
    workbook_path: Path = cfg.WORKBOOK_PATH,
    app: AppConfig = cfg.DEFAULT_CONFIG,
    plan_date: datetime | None = None,
    write_output: bool = True,
    output_path: Path | None = None,
    allow_lossy: bool = False,
    working_days: int | None = None,
    path_metric: str | None = None,
    speed_kmh: float | None = None,
    target_days: int | None = None,
    target_end=None,
    work_dates=None,
    window_dates=None,
    last_dates=None,
) -> pd.DataFrame:
    """Plan, audit and publish one run; return the Daily Route table.

    ``output_path`` writes to a copy instead of the live workbook. The other
    arguments override the matching config values for this run only.
    """
    if target_days is not None and target_days != app.horizon.target_days:
        app = replace(app, horizon=replace(app.horizon, target_days=target_days))
        log.info(
            "Billing window overridden for this run: %s",
            "off - only meters this plan is the last chance for count as "
            "must-do" if target_days <= 0 else
            f"the {target_days} working day(s) after the plan ends",
        )

    if last_dates is not None:
        app = replace(app, horizon=replace(
            app.horizon, last_chance_dates=frozenset(last_dates),
        ))
        log.info(
            "Last-chance reading dates (รอบบิลที่ต้องซ่อมให้ทัน) picked for this "
            "run: %s",
            ", ".join(d.isoformat() for d in sorted(last_dates)) or "none",
        )

    if window_dates is not None:
        app = replace(app, horizon=replace(
            app.horizon, target_dates=frozenset(window_dates),
        ))
        log.info(
            "Advance-repair reading dates picked for this run: %s",
            ", ".join(d.isoformat() for d in sorted(window_dates)) or "none",
        )

    if target_end is not None:
        app = replace(app, horizon=replace(app.horizon, target_end_date=target_end))
        log.info(
            "Advance-repair window overridden for this run: readings up to "
            "and including %s", target_end,
        )

    if path_metric is not None and path_metric != app.routing.path_metric:
        app = replace(app, routing=replace(app.routing, path_metric=path_metric))
        log.info(
            "Route choice overridden for this run: %s-optimal paths - %s",
            path_metric,
            "the shortest roads in kilometres, whatever the speed limit"
            if path_metric == "distance" else
            "the quickest roads, weighing each by its speed",
        )

    if speed_kmh is not None and speed_kmh != app.routing.target_average_speed_kmh:
        if not speed_kmh > 0:
            raise ValueError(f"speed_kmh must be positive, got {speed_kmh!r}")
        # One pace for the whole run: the arrival clock and the fallback when
        # a backend has no driving times both read it.
        app = replace(
            app,
            routing=replace(app.routing, target_average_speed_kmh=float(speed_kmh)),
            fleet=replace(app.fleet, avg_speed_kmh=float(speed_kmh)),
        )
        log.info("Average driving speed overridden for this run: %.0f km/h",
                 speed_kmh)

    report(3, "อ่านไฟล์ Excel", until=10, seconds=10)
    # Which days are planned, how many jobs each team takes and from which
    # depot: all from the Working Day sheet.
    roster = load_roster(workbook_path)
    app, plan_date = _apply_roster(
        app, roster, plan_date, work_dates, working_days,
    )
    app = replace(app, history_path=Path(workbook_path))
    # Pending Job is already filtered by the planner; no eligibility filter.
    jobs = load_pending_jobs(workbook_path)
    scored = add_billing_state(jobs, today=plan_date, pc=app.priority)

    # Every routable meter goes to the solver; the penalties decide.
    pool = select_pool(scored, app)
    report(12, "คัดมิเตอร์และจัดกลุ่มรอบบิล")

    detail = build_plan(
        pool, app, provider=make_provider(app.routing),
        plan_date=plan_date,
    )
    plan = detail.table
    # The full week rides along for the map only. pandas keeps ``attrs`` with
    # the frame, so main.py can find it without a second return value.
    if getattr(detail, "week_table", None) is not None:
        plan.attrs["week_table"] = detail.week_table
    # Every pending job, for the map's grey pins of jobs no route took.
    plan.attrs["pending"] = pending_for_map(scored)

    report(84, "ตรวจสอบแผน")
    audit_solution(detail, app)

    # Working History rows for the published day; only a real run writes them.
    history = history_rows(detail, app, plan_date, first_day_only=True)
    if write_output:
        report(88, "เขียนลง Excel", until=92, seconds=20)
        write_daily_route(
            plan,
            workbook_path=output_path or workbook_path,
            allow_lossy=allow_lossy,
            history=history,
        )
        saved_plan.clear()                  # an older dry run is stale now
    else:
        # Kept so the planner can write this plan without solving again
        # (main.py --write-saved). Never fatal: it is only a convenience.
        try:
            saved_plan.save(plan, history, Path(workbook_path))
        except Exception as exc:            # noqa: BLE001
            log.warning("Could not keep this dry run for writing later: %s", exc)
    return plan


#: What the map needs of each pending job.
MAP_PENDING_COLUMNS = [cfg.COL_CUSTOMER, "Purpose", cfg.COL_BILLING_CYCLE,
                       "Days Until Reading", "Billing State", "Latitude", "Longitude"]


def pending_for_map(scored: pd.DataFrame) -> pd.DataFrame:
    """The pending jobs that have coordinates, with only the map's columns."""
    usable = scored[scored["has_coords"]] if "has_coords" in scored else scored
    cols = [c for c in MAP_PENDING_COLUMNS if c in usable.columns]
    return usable[cols].reset_index(drop=True)


def _apply_roster(app: AppConfig, roster, plan_date, work_dates, working_days):
    """Fit the run to the Working Day sheet. Returns ``(app, plan_date)``."""
    from .workdays import calendar_for_dates

    start = (plan_date or datetime.now()).date()

    available = {d.day: d for d in roster.working_days(since=start)}
    if work_dates:
        days = sorted(set(work_dates))
        idle = [d for d in days if d not in available]
        if idle:
            raise ValueError(
                "No work in the Working Day sheet on "
                + ", ".join(d.isoformat() for d in idle)
            )
    else:
        days = sorted(available)
        if working_days is not None:
            days = days[:working_days]
    if not days:
        raise ValueError(
            f"The Working Day sheet has no day with work on or after {start}"
        )
    if len(days) > app.horizon.max_days:
        log.warning("Working Day lists %d days with work; planning the "
                    "first %d (HorizonConfig.max_days)",
                    len(days), app.horizon.max_days)
        days = days[: app.horizon.max_days]

    planned = roster.restricted_to(days)
    first = planned.on(days[0])
    app = replace(
        app,
        roster=planned,
        calendar=calendar_for_dates(days, app.calendar),
        horizon=replace(app.horizon, plan_days=len(days)),
        fleet=replace(
            app.fleet,
            num_teams=roster.teams,
            jobs_per_team=max(planned.max_team_jobs, 1),
            depot_lat=first.start.lat, depot_lon=first.start.lon,
        ),
    )
    for d in days:
        crew = planned.on(d)
        log.info("Working Day %s: jobs per team %s (%d), %s -> %s",
                 d, list(crew.jobs), crew.total, crew.start.name,
                 crew.end.name if crew.end else "no return to a depot")
    return app, datetime.combine(days[0], datetime.min.time())
