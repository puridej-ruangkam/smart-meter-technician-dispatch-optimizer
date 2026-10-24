"""Read the planning workbook ``workforce.xlsx``."""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

import pandas as pd

from . import config as cfg

log = logging.getLogger(__name__)


class DataValidationError(RuntimeError):
    """Raised when the workbook does not match the expected structure."""

TEAM_COLUMN = re.compile(r"^\s*job\s+for\s+team\s+(\d+)\s*$", re.IGNORECASE)
NO_DEPOT = {"", "-", "none", "nan"}
#: Ending Branch Depot written as this: the crews do not drive back to any
#: depot that day -- each route ends at its last job.
OPEN_END = "-"


@dataclass(frozen=True)
class Depot:
    name: str
    lat: float
    lon: float


@dataclass(frozen=True)
class CrewDay:
    """One row of the Working Day sheet."""

    day: date
    jobs: tuple[int, ...]          # jobs per team, team 1 first
    start: Depot | None
    end: Depot | None              # None on a working day: no drive back

    @property
    def total(self) -> int:
        return sum(self.jobs)

    @property
    def works(self) -> bool:
        return self.total > 0


@dataclass(frozen=True)
class Roster:
    """Who works when, how much, and from which depot -- the Working Day sheet."""

    days: tuple[CrewDay, ...]
    teams: int
    depots: dict = field(default_factory=dict, compare=False)

    def working_days(self, since: date | None = None) -> list[CrewDay]:
        """Days with at least one job, from ``since`` on, in date order."""
        return [d for d in self.days if d.works and (since is None or d.day >= since)]

    def on(self, day: date) -> CrewDay | None:
        for d in self.days:
            if d.day == day:
                return d
        return None

    def restricted_to(self, days) -> "Roster":
        """The same roster holding only ``days`` (in date order)."""
        keep = {_as_date(d) for d in days}
        return Roster(
            days=tuple(d for d in self.days if d.day in keep),
            teams=self.teams, depots=self.depots,
        )

    @property
    def max_daily_jobs(self) -> int:
        return max((d.total for d in self.days), default=0)

    @property
    def max_team_jobs(self) -> int:
        return max((j for d in self.days for j in d.jobs), default=0)


def _as_date(value) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return pd.Timestamp(value).date()


def _parse_location(text, branch: str) -> tuple[float, float]:
    """'13.74, 100.50' -> (13.74, 100.50), checked against the service area."""
    parts = re.split(r"[,\s]+", str(text).strip())
    nums = [p for p in parts if p]
    try:
        lat, lon = float(nums[0]), float(nums[1])
    except (IndexError, ValueError):
        raise DataValidationError(
            f"Branch Location: {branch!r} has location {text!r}; expected "
            f"'latitude, longitude'"
        ) from None
    if not (cfg.LAT_BOUNDS[0] <= lat <= cfg.LAT_BOUNDS[1]
            and cfg.LON_BOUNDS[0] <= lon <= cfg.LON_BOUNDS[1]):
        raise DataValidationError(
            f"Branch Location: {branch!r} at ({lat}, {lon}) is outside the service area"
        )
    return lat, lon


def load_depots(workbook_path: Path = cfg.WORKBOOK_PATH,
                announce: bool = True) -> dict[str, Depot]:
    df = pd.read_excel(workbook_path, sheet_name=cfg.BRANCH_SHEET, engine="openpyxl")
    # The first column holds the branch name, whatever its header says.
    if "Location" not in df.columns or len(df.columns) < 2 or df.columns[0] == "Location":
        raise DataValidationError(
            f"Sheet {cfg.BRANCH_SHEET!r} needs the branch name in its first "
            f"column and a 'Location' column, has {list(df.columns)}"
        )
    name_column = df.columns[0]
    depots: dict[str, Depot] = {}
    for _, row in df.iterrows():
        name = str(row[name_column]).strip()
        if not name or name.lower() in NO_DEPOT:
            continue
        lat, lon = _parse_location(row["Location"], name)
        depots[name] = Depot(name, lat, lon)
    if announce:
        log.info("Branch depots: %s", ", ".join(depots) or "none")
    return depots


def load_roster(workbook_path: Path = cfg.WORKBOOK_PATH) -> Roster:
    """Read the Working Day sheet."""
    depots = load_depots(workbook_path)
    df = pd.read_excel(workbook_path, sheet_name=cfg.WORKDAY_SHEET, engine="openpyxl")
    team_cols = sorted(
        ((int(m.group(1)), c) for c in df.columns
         if (m := TEAM_COLUMN.match(str(c)))),
    )
    if not team_cols:
        raise DataValidationError(
            f"Sheet {cfg.WORKDAY_SHEET!r} has no 'Job for Team N' columns"
        )
    numbers = [n for n, _ in team_cols]
    if numbers != list(range(1, len(numbers) + 1)):
        raise DataValidationError(
            f"Sheet {cfg.WORKDAY_SHEET!r}: team columns must be Team 1..N, got {numbers}"
        )
    for col in ("Date", "Starting Branch Depot", "Ending Branch Depot"):
        if col not in df.columns:
            raise DataValidationError(f"Sheet {cfg.WORKDAY_SHEET!r} is missing {col!r}")

    def depot(name, when, which) -> Depot | None:
        text = "" if name is None or pd.isna(name) else str(name).strip()
        if text.lower() in NO_DEPOT:
            return None
        if text not in depots:
            raise DataValidationError(
                f"Working Day {when}: {which} depot {text!r} is not in "
                f"{cfg.BRANCH_SHEET!r} ({', '.join(depots)})"
            )
        return depots[text]

    days: list[CrewDay] = []
    for _, row in df.iterrows():
        if pd.isna(row["Date"]):
            continue
        when = _as_date(row["Date"])
        jobs = []
        for n, col in team_cols:
            v = row[col]
            if v is None or pd.isna(v) or str(v).strip() in ("", "-"):
                jobs.append(0)
                continue
            try:
                k = int(float(v))
            except ValueError:
                raise DataValidationError(
                    f"Working Day {when}: Team {n} jobs {v!r} is not a number"
                ) from None
            if k < 0:
                raise DataValidationError(f"Working Day {when}: Team {n} jobs is negative")
            jobs.append(k)
        start = depot(row["Starting Branch Depot"], when, "starting")
        end = depot(row["Ending Branch Depot"], when, "ending")
        raw_end = row["Ending Branch Depot"]
        open_end = (raw_end is not None and not pd.isna(raw_end)
                    and str(raw_end).strip() == OPEN_END)
        day = CrewDay(when, tuple(jobs), start, end)
        if day.works and start is None:
            raise DataValidationError(
                f"Working Day {when}: {day.total} jobs but no starting depot")
        if day.works and end is None and not open_end:
            raise DataValidationError(
                f"Working Day {when}: {day.total} jobs but no ending depot "
                f"(write {OPEN_END!r} if the crews do not drive back)")
        days.append(day)
    days.sort(key=lambda d: d.day)
    roster = Roster(days=tuple(days), teams=len(team_cols), depots=depots)
    working = roster.working_days()
    log.info(
        "Working Day: %d team(s), %d dated row(s), %d with work%s",
        roster.teams, len(days), len(working),
        "" if not working else
        f" ({working[0].day} .. {working[-1].day})",
    )
    return roster


def _billing_text(value) -> str:
    """Billing Day as text, with day numbers zero-padded: '04'."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    if isinstance(value, (int, float)) and float(value).is_integer():
        return f"{int(value):02d}"
    return str(value).strip()


def load_pending_jobs(workbook_path: Path = cfg.WORKBOOK_PATH) -> pd.DataFrame:
    """Read Pending Job and give every row coordinates.

    Missing Lat/Long are looked up in Meter Location by Customer No. Rows still
    without coordinates get ``has_coords = False``.
    """
    df = pd.read_excel(workbook_path, sheet_name=cfg.PENDING_SHEET, engine="openpyxl")
    need = [cfg.COL_CUSTOMER, "Lat", "Long", "Purpose", "Billing Day"]
    missing = [c for c in need if c not in df.columns]
    if missing:
        raise DataValidationError(
            f"Sheet {cfg.PENDING_SHEET!r} is missing required columns: {missing}"
        )
    df = df[df[cfg.COL_CUSTOMER].notna()].copy()
    dupes = int(df[cfg.COL_CUSTOMER].duplicated().sum())
    if dupes:
        log.warning("%d duplicate Customer No. in %r; keeping the first of each",
                    dupes, cfg.PENDING_SHEET)
        df = df.drop_duplicates(subset=cfg.COL_CUSTOMER, keep="first")

    lat = pd.to_numeric(df["Lat"], errors="coerce")
    lon = pd.to_numeric(df["Long"], errors="coerce")
    no_own = lat.isna() | lon.isna()
    if no_own.any():
        loc = pd.read_excel(workbook_path, sheet_name=cfg.LOCATION_SHEET, engine="openpyxl")
        loc = loc.dropna(subset=[cfg.COL_CUSTOMER]).drop_duplicates(cfg.COL_CUSTOMER)
        key = loc.set_index(loc[cfg.COL_CUSTOMER].astype(str).str.strip())
        ids = df[cfg.COL_CUSTOMER].astype(str).str.strip()
        lat = lat.where(~no_own, ids.map(pd.to_numeric(key["Lat"], errors="coerce")))
        lon = lon.where(~no_own, ids.map(pd.to_numeric(key["Long"], errors="coerce")))
        log.info("%d pending job(s) without Lat/Long looked up in %r",
                 int(no_own.sum()), cfg.LOCATION_SHEET)

    inside = (lat.between(*cfg.LAT_BOUNDS) & lon.between(*cfg.LON_BOUNDS)).fillna(False)
    out = pd.DataFrame({
        cfg.COL_CUSTOMER: df[cfg.COL_CUSTOMER].to_numpy(),
        "Purpose": df["Purpose"].fillna("").astype(str).str.strip().to_numpy(),
        cfg.COL_BILLING_CYCLE: [_billing_text(v) for v in df["Billing Day"]],
        "Latitude": lat.where(inside).to_numpy(),
        "Longitude": lon.where(inside).to_numpy(),
        "has_coords": inside.to_numpy(),
    })
    bad = int((~out["has_coords"]).sum())
    log.info("Pending Job loaded: %d meter(s), %d with usable coordinates%s",
             len(out), len(out) - bad, f", {bad} without" if bad else "")
    return out
