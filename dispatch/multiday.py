"""Plan several working days in one solve, with one vehicle per (day, team) pair.

A meter may only go on days before its reading date. Routes are handed to teams
after the solve (``assign_crews``), and only the first day is published by default.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime

import pandas as pd

from .config import AppConfig
from .distance import DistanceProvider
from .priority import full_travel_ceiling, mandatory_penalties
from .router import TeamRoute, solve_routes
from .workdays import allowed_day_indices, working_days

log = logging.getLogger(__name__)


@dataclass
class MultiDayPlan:
    """One week's layout: every day's routes, and what was left out."""

    plan_date: date
    horizon: list[date]
    routes: list[TeamRoute]                      # every day, already crewed
    pool: pd.DataFrame
    classes: list[str] = field(default_factory=list)
    allowed_days: list[list[int]] = field(default_factory=list)

    def day(self, k: int) -> list[TeamRoute]:
        """The routes for one plan day, ordered by crew number."""
        return sorted(
            (r for r in self.routes if r.day == k), key=lambda r: r.team,
        )

    @property
    def scheduled_rows(self) -> list[int]:
        """Positions in ``pool`` that the week schedules, in no order."""
        return sorted({s.job_index for r in self.routes for s in r.stops})

    def summary(self) -> dict:
        """The figures worth logging and saving, without the route geometry."""
        per_day = {
            k: {
                "date": self.horizon[k].isoformat(),
                "jobs": sum(len(r.stops) for r in self.day(k)),
                "km": round(sum(r.total_km for r in self.day(k)), 1),
            }
            for k in range(len(self.horizon))
        }
        return {
            "plan_date": self.plan_date.isoformat(),
            "horizon": [d.isoformat() for d in self.horizon],
            "pool_size": int(len(self.pool)),
            "scheduled": len(self.scheduled_rows),
            "total_km": round(sum(r.total_km for r in self.routes), 1),
            "days": per_day,
        }


def _vehicle_layout(days: int, teams: int) -> tuple[list[int], list[int]]:
    """Vehicle ``v`` is day ``v // teams``, team slot ``v % teams``, as two lookup lists."""
    total = days * teams
    return (
        [v // teams for v in range(total)],
        [v % teams for v in range(total)],
    )


def build_allowed_vehicles(
    pool: pd.DataFrame,
    horizon: list[date],
    plan_date: date,
    teams: int,
    buffer_days: int = 0,
) -> tuple[list[list[int]], list[list[int]]]:
    """Which vehicles may serve each meter, and which plan days those are."""
    day_of, _ = _vehicle_layout(len(horizon), teams)
    allowed_vehicles: list[list[int]] = []
    allowed_days: list[list[int]] = []

    for value in pool["Days Until Reading"]:
        d = None if value is None or pd.isna(value) else int(value)
        days = allowed_day_indices(d, horizon, plan_date, buffer_days)
        allowed_days.append(days)
        permitted = set(days)
        allowed_vehicles.append(
            [v for v in range(len(day_of)) if day_of[v] in permitted]
        )
    return allowed_vehicles, allowed_days


def _route_load(route, metric: str) -> float:
    """How heavy a route is, in the measure fairness is kept in."""
    if metric == "km":
        return float(getattr(route, "total_km", 0.0) or 0.0)
    return float(route.work_minutes or 0.0)


def assign_crews(
    routes: list[TeamRoute],
    days: int,
    teams: int,
    carried_minutes: list[float] | None = None,
    metric: str = "minutes",
    carried_days: list[int] | None = None,
    basis: str = "total",
    capacities: list[list[int]] | None = None,
) -> list[TeamRoute]:
    """Hand each day's routes to teams so the workload evens out over time.

    Heaviest route first, to the team with the least so far (per worked day when
    ``basis="per_day"``). Routes only move between teams with the same job limit
    that day.
    """
    metric = "km" if str(metric).lower() == "km" else "minutes"
    running = list(carried_minutes or [0.0] * teams)
    worked = list(carried_days or [0] * teams)
    if len(running) != teams or len(worked) != teams:
        raise ValueError(
            f"carried_minutes has {len(running)} entries for {teams} crews"
        )
    per_day = str(basis).lower() == "per_day"

    def standing(t: int) -> float:
        if per_day:
            return running[t] / worked[t] if worked[t] else 0.0
        return running[t]

    out: list[TeamRoute] = []
    for k in range(days):
        todays = [r for r in routes if r.day == k]
        # Heaviest first, so the big routes are placed while there is still
        # room to balance them; ties broken on the solver's own order.
        todays.sort(key=lambda r: -_route_load(r, metric))
        taken: set[int] = set()
        for route in todays:
            slot = (route.team - 1) % teams        # the team it was built for
            same = (
                None if capacities is None
                else capacities[k][slot]
            )
            crew = min(
                (t for t in range(teams) if t not in taken
                 and (same is None or capacities[k][t] == same)),
                key=standing,
            )
            taken.add(crew)
            route.team = crew + 1
            running[crew] += _route_load(route, metric)
            worked[crew] += 1
            out.append(route)

    spread = max(running) - min(running) if len(running) > 1 else 0.0
    log.debug(
        "Crew assignment by %s (%s): totals %s (spread %.0f), "
        "bought with zero extra driving",
        metric, "per day worked" if per_day else "total",
        [round(w) for w in running], spread,
    )
    return out


def plan_week(
    pool: pd.DataFrame,
    app: AppConfig,
    provider: DistanceProvider,
    plan_date: datetime | date | None = None,
    carried_minutes: list[float] | None = None,
) -> MultiDayPlan:
    """Lay out ``HorizonConfig.plan_days`` working days of work in one solve."""
    fleet, hc = app.fleet, app.horizon
    when = plan_date or datetime.now()
    plan_day = when.date() if isinstance(when, datetime) else when

    horizon = working_days(plan_day, hc.plan_days, app.calendar)
    if horizon[0] != plan_day:
        log.warning(
            "%s is not a working day; the plan starts on %s instead",
            plan_day, horizon[0],
        )
        plan_day = horizon[0]

    days, teams = len(horizon), fleet.num_teams
    # The band step must exceed all the driving in the plan, in the solver's unit.
    metric = str(getattr(app.routing, "path_metric", "time")).lower()
    speed = getattr(app.routing, "target_average_speed_kmh", None) or 60.0
    cost_metric = "distance" if metric == "distance" else "time"
    from .router import pool_cost_matrix

    penalties, classes = mandatory_penalties(
        pool, horizon, plan_day, hc, app.daily_capacity,
        calendar=app.calendar,
        travel_ceiling=full_travel_ceiling(
            app.daily_capacity, days, cost_metric, speed,
        ),
        # Fillers nearest the must-do meters are kept first.
        cost_matrix=pool_cost_matrix(
            pool["Latitude"].to_numpy(), pool["Longitude"].to_numpy(),
            provider, cost_metric, app.solver,
        ),
    )
    # Jobs per team per day and the day's branch depots, from the Working Day
    # sheet; None without a roster (a fixed fleet at one depot).
    from .scheduler import roster_vehicles

    caps, depots, names = roster_vehicles(app, horizon)
    if caps is not None and len(caps) != days * teams:
        raise ValueError(
            f"Working Day gives {len(caps)} crew-days for {days} day(s) x "
            f"{teams} team(s)"
        )
    depot_lat, depot_lon = (
        depots[0][0] if depots else (fleet.depot_lat, fleet.depot_lon)
    )
    allowed_vehicles, allowed_days = build_allowed_vehicles(
        pool, horizon, plan_day, teams, hc.buffer_days,
    )

    log.debug(
        "Multi-day plan %s to %s: %d meters offered, %d vehicles (%d days x %d "
        "crews), up to %d jobs each",
        horizon[0], horizon[-1], len(pool), days * teams, days, teams,
        fleet.jobs_per_team,
    )

    routes = solve_routes(
        lats=pool["Latitude"].to_numpy(),
        lons=pool["Longitude"].to_numpy(),
        depot_lat=depot_lat,
        depot_lon=depot_lon,
        provider=provider,
        fleet=fleet,
        solver_cfg=app.solver,
        num_vehicles=days * teams,
        penalties=penalties,
        cost_metric=cost_metric,
        time_limit_seconds=app.solver.multiday_time_limit_seconds,
        allowed_vehicles=allowed_vehicles,
        vehicle_capacities=caps,
        vehicle_depots=depots,
    )

    # solve_routes numbers vehicles 1..days*teams; turn that back into days.
    day_of, _ = _vehicle_layout(days, teams)
    for route in routes:
        if names:
            route.start_depot, route.end_depot = names[route.team - 1]
        route.day = day_of[route.team - 1]

    routes = [r for r in routes if r.stops]
    if carried_minutes is None and getattr(hc, "carry_fairness", False):
        # What each team has already worked, from Working History.
        from .history import workload_so_far

        carried_minutes, carried_days = workload_so_far(
            plan_day, teams, getattr(hc, "fairness_metric", "km"),
            getattr(hc, "fairness_period", "week"),
            getattr(app, "history_path", None),
        )
    else:
        carried_days = None

    routes = assign_crews(
        routes, days, teams, carried_minutes,
        metric=getattr(hc, "fairness_metric", "km"),
        carried_days=carried_days,
        basis=getattr(hc, "fairness_basis", "total"),
        capacities=(
            None if caps is None
            else [caps[k * teams:(k + 1) * teams] for k in range(days)]
        ),
    )

    plan = MultiDayPlan(
        plan_date=plan_day, horizon=horizon, routes=routes, pool=pool,
        classes=classes, allowed_days=allowed_days,
    )

    scheduled = len(plan.scheduled_rows)
    log.debug(
        "Multi-day plan built: %d of %d meters scheduled across %d days, %.1f km "
        "total (%s jobs per day)",
        scheduled, len(pool), days, sum(r.total_km for r in routes),
        [sum(len(r.stops) for r in plan.day(k)) for k in range(days)],
    )
    _log_deadline_outcome(plan, classes)
    return plan


def _log_deadline_outcome(plan: MultiDayPlan, classes: list[str]) -> None:
    """Report what the week did for the meters whose billing depends on it."""
    from .workdays import CLASS_AT_RISK, CLASS_TARGET

    done = set(plan.scheduled_rows)
    for label, klass in (("last chance + urgent", CLASS_AT_RISK),
                         ("target window", CLASS_TARGET)):
        rows = [i for i, c in enumerate(classes) if c == klass]
        if not rows:
            continue
        taken = sum(1 for i in rows if i in done)
        log.debug(
            "  %s: %d of %d scheduled%s", label, taken, len(rows),
            "" if taken == len(rows) else "  <-- the rest do not fit",
        )
        if klass == CLASS_AT_RISK and taken < len(rows):
            log.warning(
                "%d last-chance or urgent meter(s) did not fit: no day in the "
                "plan had room for them. This is a capacity fact, not a routing "
                "one -- it is the first number to take to management.",
                len(rows) - taken,
            )
