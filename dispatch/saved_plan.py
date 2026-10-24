"""Keep the last dry run so ``main.py --write-saved`` can write it without solving again.

The write is refused if an input sheet changed, the first day has passed, or it
was already written.
"""
from __future__ import annotations

import hashlib
import pickle
from datetime import date, datetime
from pathlib import Path

import pandas as pd

from . import config as cfg

SAVED_PATH = cfg.PROJECT_DIR / "mapdata" / "last_dry_run.pkl"
FORMAT_VERSION = 1


class SavedPlanError(RuntimeError):
    """There is no saved dry run that can be written now. The message says why."""


def fingerprint(workbook_path: Path) -> str:
    """A hash of every input sheet's values (all sheets but Daily Route)."""
    sheets = pd.read_excel(workbook_path, sheet_name=None, dtype=str,
                           engine="openpyxl")
    digest = hashlib.sha256()
    for name in sorted(sheets):
        if name == cfg.OUTPUT_SHEET:
            continue
        digest.update(name.encode("utf-8"))
        digest.update(sheets[name].to_csv(index=False).encode("utf-8"))
    return digest.hexdigest()


def save(plan: pd.DataFrame, history: pd.DataFrame | None, workbook_path: Path,
         path: Path | None = None) -> None:
    """Store a dry run's result for ``main.py --write-saved``."""
    path = Path(path or SAVED_PATH)
    week = plan.attrs.get("week_table")
    first = str(plan["Date"].iloc[0]) if len(plan) and "Date" in plan.columns else ""
    record = {
        "version": FORMAT_VERSION,
        "created": datetime.now(),
        "workbook": str(Path(workbook_path).resolve()),
        "fingerprint": fingerprint(workbook_path),
        "first_day": first,
        "plan": pd.DataFrame(plan),          # without attrs
        "week": week,
        "pending": plan.attrs.get("pending"),
        "history": history,
    }
    record["plan"].attrs = {}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "wb") as fh:
        pickle.dump(record, fh)
    tmp.replace(path)


def clear(path: Path | None = None) -> None:
    """Forget the saved dry run (after it was written, or after a real run)."""
    path = Path(path or SAVED_PATH)
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def load(workbook_path: Path, today: date | None = None,
         path: Path | None = None) -> dict:
    """The saved dry run, if it can still be written to ``workbook_path``.

    Raises ``SavedPlanError`` (message in Thai) when there is none, it belongs to
    another workbook, its first day has passed, or an input sheet changed.
    """
    path = Path(path or SAVED_PATH)
    if not path.exists():
        raise SavedPlanError("ไม่มีผลทดลองคำนวณที่บันทึกได้ — กดวางแผนแบบทดลองคำนวณก่อน")
    try:
        with open(path, "rb") as fh:
            record = pickle.load(fh)
    except Exception as exc:                       # noqa: BLE001
        raise SavedPlanError(f"อ่านผลทดลองคำนวณที่เก็บไว้ไม่ได้ ({exc}) — "
                             f"ทดลองคำนวณใหม่อีกครั้ง") from None
    if record.get("version") != FORMAT_VERSION:
        raise SavedPlanError("ผลทดลองคำนวณที่เก็บไว้มาจากโปรแกรมรุ่นเก่า — "
                             "ทดลองคำนวณใหม่อีกครั้ง")
    if record["workbook"] != str(Path(workbook_path).resolve()):
        raise SavedPlanError(f"ผลทดลองคำนวณที่เก็บไว้เป็นของไฟล์อื่น "
                             f"({record['workbook']})")
    today = today or date.today()
    if record["first_day"] and date.fromisoformat(record["first_day"][:10]) < today:
        raise SavedPlanError(f"ผลทดลองคำนวณเป็นแผนของวันที่ {record['first_day'][:10]} "
                             f"ซึ่งผ่านไปแล้ว — ทดลองคำนวณใหม่อีกครั้ง")
    if fingerprint(workbook_path) != record["fingerprint"]:
        raise SavedPlanError("workforce.xlsx ถูกแก้ไขหลังทดลองคำนวณ (Pending Job, "
                             "Working Day, Working History หรือหน้าอื่น) — "
                             "ผลเดิมอาจไม่ตรงกับข้อมูลแล้ว ทดลองคำนวณใหม่อีกครั้ง")
    plan = record["plan"]
    if record.get("week") is not None:
        plan.attrs["week_table"] = record["week"]
    if record.get("pending") is not None:
        plan.attrs["pending"] = record["pending"]
    record["plan"] = plan
    return record
