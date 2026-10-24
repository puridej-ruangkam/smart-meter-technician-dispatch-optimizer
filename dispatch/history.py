"""The Working History sheet: one row per team per published day.

``workload_so_far`` gives each team's total so far, which ``multiday.assign_crews``
uses to hand out the routes. It never becomes a constraint on the solver.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from pathlib import Path

from .config import AppConfig

log = logging.getLogger(__name__)

# The Working History sheet of workforce.xlsx. The reader lower-cases these
# and turns spaces into underscores ("Travel km" -> travel_km).
SHEET_COLUMNS = [
    "Date", "Team", "Jobs", "Travel km", "Travel minutes", "Service minutes",
    "Workload minutes", "Start Depot", "End Depot", "Recorded At",
]


def _as_day(value) -> date:
    """A plain date from a datetime, a date or None (today).

    Callers hand over a ``date`` or a ``datetime``; both must work.
    """
    if value is None:
        return datetime.now().date()
    return value.date() if isinstance(value, datetime) else value


def workload_so_far(
    plan_date=None,
    teams: int = 4,
    metric: str = "km",
    period: str = "week",
    path: Path | None = None,
) -> tuple[list[float], list[int]]:
    """(workload per team, days worked per team) before ``plan_date``.

    ``metric`` is "km" or "minutes"; ``period`` is "week" or "all". When a day was
    published twice, the last row wins. A missing sheet means everyone starts at zero.
    """
    from . import config as cfg

    path = Path(path or cfg.WORKBOOK_PATH)
    n = max(int(teams), 1)
    if not path.exists():
        return [0.0] * n, [0] * n
    return _so_far_from_rows(_sheet_rows(path), plan_date, n, metric, period)


def _sheet_rows(path: Path) -> list[dict]:
    """The Working History sheet as rows of text keyed lower_case."""
    import pandas as pd

    from . import config as cfg

    try:
        df = pd.read_excel(path, sheet_name=cfg.HISTORY_SHEET, engine="openpyxl",
                           dtype=object)
    except (OSError, ValueError, KeyError) as exc:
        log.warning("Could not read %r in %s (%s); starting fairness from zero.",
                    cfg.HISTORY_SHEET, path.name, exc)
        return []

    def text(v) -> str:
        if v is None or (isinstance(v, float) and v != v):
            return ""
        if hasattr(v, "date") and callable(v.date):
            return v.date().isoformat()
        return str(v)

    return [
        {str(k).strip().lower().replace(" ", "_"): text(v) for k, v in rec.items()}
        for rec in df.to_dict("records")
    ]


def _so_far_from_rows(rows, plan_date, n, metric, period):
    """``workload_so_far`` over already-read rows."""
    day = _as_day(plan_date)
    since = (day - timedelta(days=day.weekday())
             if str(period).lower() == "week" else date.min)
    read = _km_of if str(metric).lower() == "km" else _minutes_of
    seen: dict[tuple[str, int], float] = {}
    for row in rows:
        try:
            when = date.fromisoformat((row.get("date") or "").strip()[:10])
            team = int(float(row["team"]))
        except (ValueError, TypeError, KeyError):
            continue
        if not (since <= when < day) or not 1 <= team <= n:
            continue
        amount = read(row)
        if amount is not None:
            seen[(when.isoformat(), team)] = amount
    totals, days = [0.0] * n, [0] * n
    for (_, team), amount in seen.items():
        totals[team - 1] += amount
        days[team - 1] += 1
    if any(totals):
        log.debug(
            "Fairness carried forward from Working History (%s, %s, before %s): "
            "%s per crew over %s day(s)",
            metric, "this week" if period == "week" else "all history", day,
            [round(t) for t in totals], days,
        )
    return totals, days


def history_rows(detail, app: AppConfig, plan_date=None,
                 first_day_only: bool = True):
    """One Working History row per team for the published day(s).

    Teams with no jobs that day are written as zero rows: an absent row and a
    zero row mean different things.
    """
    import pandas as pd

    day = _as_day(plan_date)
    service = app.fleet.service_minutes
    horizon = getattr(getattr(detail, "multiday", None), "horizon", None)
    roster = getattr(app, "roster", None)

    def date_of(route) -> date:
        k = getattr(route, "day", None)
        if horizon and isinstance(k, int) and 0 <= k < len(horizon):
            return horizon[k]
        return day

    by_key = {(date_of(r), r.team): r for r in detail.routes}
    dates = sorted({date_of(r) for r in detail.routes} | {day})
    if first_day_only:
        dates = [day]
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    rows = []
    for d in dates:
        crew_day = roster.on(d) if roster is not None else None
        for team in range(1, app.fleet.num_teams + 1):
            route = by_key.get((d, team))
            stops = len(route.stops) if route else 0
            drive = route.drive_minutes if route else 0.0
            work = route.work_minutes if route else 0.0
            rows.append({
                "Date": d.isoformat(),
                "Team": team,
                "Jobs": stops,
                "Travel km": round(route.total_km, 2) if route else 0.0,
                "Travel minutes": "" if drive is None else round(drive, 1),
                "Service minutes": round(stops * service, 1),
                "Workload minutes": "" if work is None else round(work, 1),
                "Start Depot": (route.start_depot if route and route.start_depot
                                else crew_day.start.name if crew_day and crew_day.start else ""),
                "End Depot": (route.end_depot if route and route.end_depot
                              else crew_day.end.name if crew_day and crew_day.end else ""),
                "Recorded At": stamp,
            })
    return pd.DataFrame(rows, columns=SHEET_COLUMNS)


def _km_of(row: dict) -> float | None:
    """One row's kilometres, or None when the row has none."""
    raw = (row.get("travel_km") or "").strip()
    try:
        return float(raw) if raw else None
    except ValueError:
        return None


def _minutes_of(row: dict) -> float | None:
    """One row's workload in minutes; a row with no minutes at all is skipped."""
    raw = (row.get("workload_minutes") or "").strip()
    if raw:
        try:
            return float(raw)
        except ValueError:
            pass
    parts = []
    for key in ("travel_minutes", "service_minutes"):
        value = (row.get(key) or "").strip()
        if value:
            try:
                parts.append(float(value))
            except ValueError:
                return None
    return sum(parts) if parts else None
