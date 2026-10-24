"""Every setting in one place."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time
from pathlib import Path

from .workdays import DEFAULT_CALENDAR, WorkCalendar

PROJECT_DIR = Path(__file__).resolve().parent.parent
# The planning workbook (see dispatch/workforce.py). Pending Job arrives
# already filtered: there is no eligibility filter here.
WORKBOOK_PATH = PROJECT_DIR / "workforce.xlsx"

PENDING_SHEET = "Pending Job"
LOCATION_SHEET = "Meter Location"
WORKDAY_SHEET = "Working Day"
BRANCH_SHEET = "Branch Location"
OUTPUT_SHEET = "Daily Route"
HISTORY_SHEET = "Working History"

# --- Source column names (exactly as they appear in the workbook) ---
COL_CUSTOMER = "Customer No."
COL_BILLING_CYCLE = "รอบบิล"

# Sanity bounding box for coordinates (greater Bangkok and its neighbouring provinces)
LAT_BOUNDS = (13.0, 14.5)
LON_BOUNDS = (100.0, 101.2)


# Customer No. is a unique id, not a rank, and must never order meters
# anywhere in this system.

# Billing state of a meter.
BILLING_CRITICAL = "CRITICAL"      # reading day within PriorityConfig.critical_days
BILLING_UNCRITICAL = "UNCRITICAL"  # reading day still ahead, further out
BILLING_MISSED = "MISSED"          # today is the reading day or later: lost
BILLING_UNKNOWN = "UNKNOWN"        # cycle code unreadable
# Daily Route only: an UNCRITICAL meter taken as a filler near the route.
BILLING_NEARBY = "NEARBY"
# Billing Day "Urgent": a job the planner marks by hand. It is
# must-do in the same band as the last-chance readings, on any day of the plan.
BILLING_URGENT = "URGENT"
URGENT_CODES = frozenset({"urgent"})       # Billing Day values, case-insensitive


@dataclass(frozen=True)
class PriorityConfig:
    """When a meter's reading counts as close, for the Billing State column.

    Display only: what the plan does is decided by the deadline classes on
    ``HorizonConfig``, not by this label.
    """

    #: A meter read within this many days is CRITICAL in the Daily Route sheet.
    critical_days: int = 3

    def __post_init__(self) -> None:
        if self.critical_days < 0:
            raise ValueError("PriorityConfig.critical_days cannot be negative")


@dataclass(frozen=True)
class FleetConfig:
    """Team fleet and workday parameters."""

    num_teams: int = 4
    jobs_per_team: int = 8
    # Fallback only: real runs take every depot from the Branch Location sheet.
    depot_lat: float = 13.7563       # central Bangkok
    depot_lon: float = 100.5018
    workday_start: time = time(8, 30)
    service_minutes: int = 20        # on-site time per job
    # Routes end at the day's Ending Branch Depot, or at the last job when it is "-".

    # The vehicle speed set on screen. Used only where a leg has no driving
    # time of its own; the road graph always gives one.
    avg_speed_kmh: float = 60.0

    def __post_init__(self) -> None:
        if self.avg_speed_kmh <= 0:
            raise ValueError("FleetConfig.avg_speed_kmh must be positive")


@dataclass(frozen=True)
class HorizonConfig:
    """How far ahead the plan looks, and which readings it works for."""

    #: Write every planned day to Daily Route, not only the first. Off because
    #: the plan is rebuilt every morning; the map still shows every day.
    publish_all_days: bool = False

    #: End of the advance window as a date. Overrides ``target_days``.
    target_end_date: date | None = None

    #: Advance-window reading dates picked on the calendar. Overrides both above.
    target_dates: frozenset | None = None

    #: Last-chance reading dates picked on the calendar. None works them out
    #: from the work days (``workdays.last_chance_boundary``).
    last_chance_dates: frozenset | None = None

    # -- workload balancing --------------------------------------------------
    # Each team's total so far comes from Working History. It only decides
    # which team drives which route, never what the routes are.
    carry_fairness: bool = True

    #: Workload measure: "km" (driving, including the drive back) or "minutes".
    fairness_metric: str = "km"

    #: How far back the history is read: "week" or "all".
    fairness_period: str = "all"

    #: Compare teams by "total" or "per_day" (total divided by days worked,
    #: so a team back from leave isn't given every heavy route).
    fairness_basis: str = "per_day"

    #: Working days planned at once. A run sets this from the picked days.
    plan_days: int = 1

    #: Length of the advance window in working days. None means the same
    #: length as the plan; zero turns the window off.
    target_days: int | None = None

    #: Safety limit; every extra day enlarges the routing problem.
    max_days: int = 14

    #: Extra working days required before the reading day.
    buffer_days: int = 0

    def __post_init__(self) -> None:
        if not 1 <= self.plan_days <= self.max_days:
            raise ValueError(
                f"HorizonConfig.plan_days must be between 1 and max_days "
                f"({self.max_days}), got {self.plan_days!r}"
            )
        if self.target_days is not None and self.target_days < 0:
            raise ValueError("HorizonConfig.target_days cannot be negative")
        if self.buffer_days < 0:
            raise ValueError("HorizonConfig.buffer_days cannot be negative")

    def target_window_days(self) -> int:
        """``target_days``, resolved -- ``None`` means "as long as the plan"."""
        return self.plan_days if self.target_days is None else int(self.target_days)

    def window_end(self, horizon, calendar=None):
        """The end of the advance window, or None.

        Either the last reading date of the window or the set of picked dates.
        Picked dates win, then a set date, then a day count.
        """
        from .workdays import (
            DEFAULT_CALENDAR, last_chance_boundary, target_window_end,
        )

        calendar = calendar or DEFAULT_CALENDAR
        if self.target_dates is not None and self.last_chance_dates is not None:
            # Both picked on the calendar: a date is one or the other.
            first = horizon[0] if horizon else None
            kept = frozenset(
                d for d in (x.date() if isinstance(x, datetime) else x
                            for x in self.target_dates)
                if d not in self.last_chance_dates and first is not None and d > first
            )
            return kept or None
        if self.target_dates is not None:
            boundary = last_chance_boundary(horizon, calendar)
            picked = frozenset(
                d.date() if isinstance(d, datetime) else d
                for d in self.target_dates
            )
            kept = frozenset(d for d in picked
                             if boundary is not None and d > boundary)
            return kept or None
        if self.target_end_date is not None:
            boundary = last_chance_boundary(horizon, calendar)
            end = self.target_end_date
            if isinstance(end, datetime):
                end = end.date()
            return end if boundary is not None and end > boundary else None
        return target_window_end(horizon, self.target_window_days(), calendar)


@dataclass(frozen=True)
class SolverConfig:
    """OR-Tools routing solver parameters."""

    # Default limit for solve_routes when the caller gives none (the tests).
    # The plan itself uses multiday_time_limit_seconds below.
    time_limit_seconds: int = 120
    distance_scale: int = 1000       # km -> integer metres for the solver
    # Driving time is fed to the solver in seconds, unscaled, so the
    # objective reads directly as seconds of driving.

    # The plan's search time: the ceiling every run is held to.
    multiday_time_limit_seconds: int = 180

    # -- stop rule ------------------------------------------------------------
    # The search ends after ``flat_patience_seconds`` without a better choice
    # of meters or at least ``flat_min_gain_pct`` less driving, or at the time
    # limit. With patience equal to the limit, every run uses the full limit.
    stop_when_flat: bool = True

    #: Seconds without a meaningful gain before the search stops.
    flat_patience_seconds: float = 180.0

    #: What counts as a meaningful cut in driving, as a percentage of the
    #: plan's driving. Below this, an improvement is treated as noise.
    flat_min_gain_pct: float = 0.1

    #: Never stop before this, however flat the start looks. The first
    #: solution often sits alone for a while before the search gets going.
    min_search_seconds: float = 20.0

    #: Log the objective each time it improves, with the elapsed time. This is
    #: the convergence curve -- the thing to read before arguing about limits.
    log_improvement_curve: bool = True


@dataclass(frozen=True)
class RoutingConfig:
    """Road routing settings. Distances always come from the local road graph."""

    backend: str = "local_graph"     # the only backend

    # -- travel matrix cache --------------------------------------------------
    # Only meters not seen before are computed. Delete the file to rebuild.
    matrix_cache_path: Path = PROJECT_DIR / "mapdata" / "travel_matrix_cache.npz"
    use_matrix_cache: bool = True

    # -- local_graph backend -----------------------------------------------
    graph_path: Path = PROJECT_DIR / "mapdata" / "bangkok_road_graph.npz"
    # A meter further than this from any road means bad coordinates or a
    # location outside the mapped area; the run stops rather than guessing.
    snap_max_km: float = 2.0

    # -- quality checks -------------------------------------------------------
    # Stop the run if a road distance is shorter than the straight line.
    verify_against_straight_line: bool = True

    # Map speeds are free-flow, so driving times are scaled until the average
    # speed equals this. None uses the raw map speeds.
    target_average_speed_kmh: float | None = 60.0

    # What each route minimises: "time" (quickest) or "distance" (shortest).
    # The other quantity is measured along the same route. Each metric has
    # its own cache file.
    path_metric: str = "time"


    def __post_init__(self) -> None:
        metric = str(self.path_metric).strip().lower()
        if metric not in ("time", "distance"):
            raise ValueError(
                f"RoutingConfig.path_metric must be 'time' or 'distance', "
                f"got {self.path_metric!r}"
            )


@dataclass(frozen=True)
class AppConfig:
    priority: PriorityConfig = field(default_factory=PriorityConfig)
    fleet: FleetConfig = field(default_factory=FleetConfig)
    solver: SolverConfig = field(default_factory=SolverConfig)
    routing: RoutingConfig = field(default_factory=RoutingConfig)
    horizon: HorizonConfig = field(default_factory=HorizonConfig)
    # Which days the teams go out (see dispatch/workdays.py).
    calendar: WorkCalendar = field(default_factory=lambda: DEFAULT_CALENDAR)
    # The Working Day sheet for the planned days: jobs per team and depots.
    # None keeps the fixed fleet and depot of FleetConfig.
    roster: object | None = None
    # Workbook to read Working History from; None means WORKBOOK_PATH.
    history_path: Path | None = None

    @property
    def daily_capacity(self) -> int:
        """The most jobs any one planned day holds."""
        if self.roster is not None and getattr(self.roster, "max_daily_jobs", 0):
            return int(self.roster.max_daily_jobs)
        return self.fleet.num_teams * self.fleet.jobs_per_team

    def team_limit(self, day_index: int, team: int) -> int:
        """Most jobs ``team`` (1-based) may take on planned day ``day_index``."""
        r = self.roster
        if r is None or not 0 <= day_index < len(r.days):
            return self.fleet.jobs_per_team
        jobs = r.days[day_index].jobs
        return int(jobs[team - 1]) if 1 <= team <= len(jobs) else 0


DEFAULT_CONFIG = AppConfig()
