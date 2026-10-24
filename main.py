"""Command-line entry point for a planning run.

    python main.py --dry-run       plan and print, write nothing
    python main.py --write-saved   write the last dry run without solving again
    python main.py --help          every option
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path

from dispatch import config as cfg
from dispatch.excel_writer import LossyWriteRefused, WriteNotSaved
from dispatch.pipeline import run_pipeline

PROJECT_DIR = Path(__file__).resolve().parent
log = logging.getLogger("main")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Smart technician dispatch planner")
    parser.add_argument(
        "--workbook", default=str(cfg.WORKBOOK_PATH),
        help="Path to workforce.xlsx (default: project copy)",
    )
    parser.add_argument(
        "--date", default=None,
        help="Plan date as YYYY-MM-DD (default: today)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Compute and print the plan without writing to the workbook",
    )
    parser.add_argument(
        "--write-saved", action="store_true",
        help=("Write the last dry run's plan to the workbook without solving "
              "again. Refused if any input sheet changed since that dry run."),
    )
    parser.add_argument(
        "--no-map", action="store_true",
        help="Skip drawing the route map after publishing",
    )
    parser.add_argument(
        "--working-days", type=int, default=None,
        dest="working_days", metavar="N",
        help=(
            "How many working days of crews to plan at once, just for this "
            "run. This is the plan's capacity: N days x teams x jobs per team. "
            "The days are laid out together, so a meter can be scheduled for "
            "the day a crew is already nearby. Default: every day with work in "
            "the Working Day sheet."
        ),
    )
    parser.add_argument(
        "--target-days", type=int, default=None, metavar="N",
        help=(
            "How many working days of billing the plan prepares for, counted "
            "from the day after the plan ends. A meter whose reading falls in "
            "that window is treated as must-do. Default: the same as "
            "--working-days. 0 turns the window off, leaving only meters this "
            "plan is the last chance for."
        ),
    )
    parser.add_argument(
        "--work-dates", default=None, metavar="YYYY-MM-DD,...",
        help=(
            "The exact days crews go out, comma-separated, just for this run. "
            "Overrides --date and --working-days. A weekday between the first "
            "and last date that is left out is treated as a holiday; a "
            "weekend day that is included is treated as a worked day. This is "
            "what the planning window's calendar sends."
        ),
    )
    parser.add_argument(
        "--last-dates", default=None, metavar="YYYY-MM-DD,...",
        help=(
            "The exact reading dates that must be repaired in time "
            "(รอบบิลที่ต้องซ่อมให้ทัน), comma-separated. Left out = worked out "
            "from the work days: readings up to the first working day after "
            "the plan. Sent by the multi-day window's calendar."
        ),
    )
    parser.add_argument(
        "--window-dates", default=None, metavar="YYYY-MM-DD,...",
        help=(
            "The exact reading dates to prepare in advance, comma-separated. "
            "Overrides --target-end and --target-days. Dates that are already "
            "last chance are ignored."
        ),
    )
    parser.add_argument(
        "--target-end", default=None, metavar="YYYY-MM-DD",
        help=(
            "Last reading date the advance-repair window covers, just for "
            "this run. Overrides --target-days. Readings on or before the "
            "first working day after the plan ends are already last chance, "
            "so a date on or before that day means no window at all."
        ),
    )
    parser.add_argument(
        "--path-metric", choices=("time", "distance"), default=None,
        help=(
            "What every leg is chosen to minimise, just for this run. "
            "time = the quickest road, weighing each by its speed (the "
            "default, and what the model has always done). "
            "distance = the shortest road in kilometres, whatever the speed "
            "limit. Measured over 23,670 real meter pairs in Bangkok, "
            "distance saves 8.7%% of the kilometres and costs 20.4%% more "
            "driving time."
        ),
    )
    parser.add_argument(
        "--speed-kmh", type=float, default=None, metavar="KMH",
        help=(
            "Average driving speed for this run, in km/h. Every road keeps its "
            "relative speed; the whole table is scaled so the average is this. "
            "It sets the arrival times. Default: "
            "RoutingConfig.target_average_speed_kmh (60)."
        ),
    )
    parser.add_argument(
        "--force-lossy", action="store_true",
        help=(
            "Write with openpyxl even though it destroys Power Query "
            "connections, Excel tables and threaded comments. Only when Excel "
            "cannot be driven (no pywin32 on Windows, no xlwings on macOS)."
        ),
    )
    return parser.parse_args(argv)


def parse_dates(text: str | None):
    """'2027-03-01,2027-03-02' -> [date, date]; None stays None.

    An empty string means "none picked", which for the window is a real
    choice -- no advance repair -- and must not be confused with "not given".
    """
    if text is None:
        return None
    return [datetime.strptime(part.strip(), "%Y-%m-%d").date()
            for part in text.split(",") if part.strip()]


def draw_map(plan, dry_run: bool = False) -> None:
    """Render the route map from the plan just computed.

    A failure here doesn't fail the run. A dry run writes to a separate file and
    says so on the page.
    """
    try:
        from dispatch.progress import report
        report(92, "วาดแผนที่", until=99, seconds=45)
        sys.path.insert(0, str(PROJECT_DIR / "tools"))
        import view_map

        name = "route_map_dryrun.html" if dry_run else "route_map.html"
        # The map shows every planned day, even when the sheet gets only the first.
        week = plan.attrs.get("week_table") if hasattr(plan, "attrs") else None
        issued = ""
        if week is not None and len(week) and "Date" in week.columns:
            issued = str(sorted(week["Date"].astype(str).unique())[0])
        notes = []
        if dry_run:
            notes.append("DRY RUN - not published")
        if issued:
            notes.append(f"ส่งทีมเฉพาะวันที่ {issued} · วันอื่นเป็นแผนล่วงหน้า "
                         f"จะวางใหม่ทุกเช้า")
        out = view_map.render(
            week if issued else plan,
            out=PROJECT_DIR / "mapdata" / name,
            note=" &middot; ".join(notes),
            issued=issued,
            pending=plan.attrs.get("pending") if hasattr(plan, "attrs") else None,
        )
        print(f"Route map{' (dry run)' if dry_run else ''}: {out}")
    except Exception as exc:                       # noqa: BLE001 - never fatal
        log.warning(
            "The plan was published successfully, but the route map could not "
            "be drawn: %s", exc,
        )
        log.warning("Draw it later with:  python tools/view_map.py")


def write_saved(args: argparse.Namespace) -> int:
    """Write the last dry run to the workbook, exactly as it was shown."""
    from dispatch import saved_plan
    from dispatch.excel_writer import write_daily_route
    from dispatch.progress import report

    workbook = Path(args.workbook)
    report(5, "ตรวจผลทดลองคำนวณ", until=40, seconds=10)
    try:
        record = saved_plan.load(workbook)
    except saved_plan.SavedPlanError as exc:
        log.error("%s", exc)
        return 4
    plan = record["plan"]
    log.info("Writing the dry run of %s (first day %s, %d jobs) without "
             "solving again", f"{record['created']:%d/%m/%Y %H:%M}",
             record["first_day"], len(plan))
    report(45, "เขียนลง Excel", until=90, seconds=20)
    try:
        write_daily_route(plan, workbook_path=workbook,
                          allow_lossy=args.force_lossy, history=record["history"])
    except LossyWriteRefused as exc:
        log.error("%s", exc)
        return 2
    except WriteNotSaved as exc:
        log.error("%s", exc)
        return 3
    saved_plan.clear()                       # written once, never twice

    print(f"\nDaily Route: {len(plan)} jobs across {plan['Team'].nunique()} teams")
    print(plan.to_string(index=False))
    if not args.no_map:
        print()
        draw_map(plan, dry_run=False)
    report(100, "เสร็จแล้ว")
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    args = parse_args(argv if argv is not None else sys.argv[1:])
    if args.write_saved:
        return write_saved(args)
    plan_date = datetime.strptime(args.date, "%Y-%m-%d") if args.date else None

    try:
        plan = run_pipeline(
            workbook_path=Path(args.workbook),
            plan_date=plan_date,
            write_output=not args.dry_run,
            allow_lossy=args.force_lossy,
            working_days=args.working_days,
            path_metric=args.path_metric,
            speed_kmh=args.speed_kmh,
            target_days=args.target_days,
            target_end=(
                datetime.strptime(args.target_end, "%Y-%m-%d").date()
                if args.target_end else None
            ),
            work_dates=parse_dates(args.work_dates),
            window_dates=parse_dates(args.window_dates),
            last_dates=parse_dates(args.last_dates),
        )
    except LossyWriteRefused as exc:
        # The plan was computed but deliberately not published. Report it as a
        # clean failure rather than a traceback -- the workbook is untouched.
        log.error("%s", exc)
        return 2
    except WriteNotSaved as exc:
        # Excel said it saved, but the file does not hold the plan: say so
        # plainly instead of finishing as though the crews had their sheet.
        log.error("%s", exc)
        return 3

    print(f"\nDaily Route: {len(plan)} jobs across {plan['Team'].nunique()} teams")
    print(plan.to_string(index=False))

    # A dry run draws its own map file, so the published map is never overwritten.
    if not args.no_map:
        print()
        draw_map(plan, dry_run=args.dry_run)

    from dispatch.progress import report
    report(100, "เสร็จแล้ว")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
