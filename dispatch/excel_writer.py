"""Write the Daily Route sheet and Working History rows into the workbook.

Tried in order: Excel through COM (Windows), Excel through xlwings (macOS), then
openpyxl, which loses Excel tables and custom XML and is refused unless
``allow_lossy=True``. A backup copy is made before every write.
"""
from __future__ import annotations

import contextlib
import importlib.util
import logging
import os
import shutil
import time
import zipfile
from datetime import datetime
from pathlib import Path

import pandas as pd

from . import config as cfg

log = logging.getLogger(__name__)

# Columns whose values must stay literal text in Excel (COM would otherwise
# coerce "02" -> 2, "09:04" -> time, "2024/05/03 ..." -> datetime).
TEXT_COLUMNS = {"Billing Day", "Estimated Arrival Time"}

HEADER_RGB = (0x1F, 0x4E, 0x78)
TEAM_RGBS = [(0xDD, 0xEB, 0xF7), (0xE2, 0xEF, 0xDA), (0xFF, 0xF2, 0xCC), (0xFC, 0xE4, 0xD6)]

COLUMN_WIDTHS = {
    "Date": 12, "Team": 7, "Sequence": 10, "Customer No.": 14, "Purpose": 20,
    "Billing Day": 11, "Days Until Reading": 12,
    "Billing State": 13,
    "Latitude": 11, "Longitude": 11, "Estimated Distance (km)": 20,
    "Estimated Arrival Time": 20, "Start Depot": 13, "End Depot": 13,
    "Navigation Link": 16,
}


#: How many copies of the workbook ``backup_workbook`` keeps; older ones are
#: deleted after each new copy.
BACKUPS_KEPT = 10


def backup_workbook(workbook_path: Path, keep: int = BACKUPS_KEPT) -> Path:
    """Copy the workbook to backups/<name>.<timestamp><ext>, keep the newest
    ``keep`` copies of it, and return the new copy's path."""
    backup_dir = workbook_path.parent / "backups"
    backup_dir.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    target = backup_dir / f"{workbook_path.stem}.{stamp}{workbook_path.suffix}"
    shutil.copy2(workbook_path, target)
    log.info("Backup created: %s", target)

    # The timestamp sorts by name; only this workbook's own copies are touched.
    copies = sorted(backup_dir.glob(
        f"{workbook_path.stem}.{'[0-9]' * 8}_{'[0-9]' * 6}{workbook_path.suffix}"))
    for old in copies[:-keep] if keep > 0 else []:
        try:
            old.unlink()
        except OSError as exc:                     # a locked copy is not fatal
            log.warning("Could not remove old backup %s (%s)", old.name, exc)
    return target


class LossyWriteRefused(RuntimeError):
    """Refused to rewrite a workbook whose structure openpyxl would damage."""


# Workbook parts that openpyxl drops, and how to spot them in the package.
_FRAGILE_PARTS = {
    "data connections":
        lambda n: n == "xl/connections.xml",
    "query tables":
        lambda n: n.startswith("xl/queryTables/") and n.endswith(".xml"),
    "Excel tables":
        lambda n: n.startswith("xl/tables/") and n.endswith(".xml")
        and "/_rels/" not in n,
    "threaded comments":
        lambda n: n.startswith("xl/threadedComments/") and n.endswith(".xml"),
    "custom XML parts":
        lambda n: n.startswith("customXml/item")
        and not n.startswith("customXml/itemProps"),
}


def describe_fragile_parts(workbook_path: Path) -> dict[str, int]:
    """Count the workbook parts an openpyxl rewrite would destroy.

    An Excel workbook is a zip package; this reads its directory only. An empty
    result means an openpyxl rewrite would lose nothing that we know of.
    """
    try:
        with zipfile.ZipFile(workbook_path) as zf:
            names = zf.namelist()
    except (OSError, zipfile.BadZipFile) as exc:
        # Unreadable package -> assume the worst rather than write blindly.
        log.warning("Could not inspect %s (%s)", workbook_path.name, exc)
        return {"unknown parts (workbook could not be inspected)": 1}

    found = {}
    for label, matches in _FRAGILE_PARTS.items():
        count = sum(1 for name in names if matches(name))
        if count:
            found[label] = count
    return found


def _describe(fragile: dict[str, int]) -> str:
    return ", ".join(f"{count} {label}" for label, count in fragile.items())


def _automation_module_available() -> bool:
    """True when a library that can drive Excel is importable (checked before any backup is made)."""
    for module in ("win32com.client", "xlwings"):
        try:
            if importlib.util.find_spec(module) is not None:
                return True
        except (ImportError, ValueError):
            continue
    return False


def _lossy_message(workbook_path: Path, fragile: dict[str, int]) -> str:
    return (
        f"Refusing to write {workbook_path.name} with openpyxl: doing so would "
        f"drop {_describe(fragile)} from the workbook.\n"
        "Excel itself has to do the saving. Install whichever fits this machine:\n"
        "    Windows:  pip install pywin32     (drives Excel over COM)\n"
        "    macOS:    pip install xlwings     (needs Excel for Mac + Xcode CLT)\n"
        "Or accept the damage deliberately with --force-lossy. The workbook is "
        "copied to backups/ before any write, and OneDrive keeps version history."
    )


class WriteNotSaved(RuntimeError):
    """Excel said it saved, but the file on disk does not hold the new plan."""


def _norm_id(value) -> str:
    """Customer No. as text, whether Excel hands back 12345678 or 12345678.0."""
    text = "" if value is None else str(value).strip()
    return text[:-2] if text.endswith(".0") and text[:-2].isdigit() else text


def _on_disk(workbook_path: Path, sheet_name: str, history_sheet: str):
    """(Customer Nos. in ``sheet_name``, data rows in ``history_sheet``) as
    the file on disk holds them, or None when it cannot be read."""
    from openpyxl import load_workbook

    try:
        wb = load_workbook(workbook_path, read_only=True, data_only=True)
    except Exception as exc:                       # noqa: BLE001
        log.warning("Could not re-read %s to check the write (%s)",
                    workbook_path.name, exc)
        return None
    try:
        customers = None
        if sheet_name in wb.sheetnames:
            rows = list(wb[sheet_name].iter_rows(values_only=True))
            if rows and cfg.COL_CUSTOMER in rows[0]:
                i = list(rows[0]).index(cfg.COL_CUSTOMER)
                customers = sorted(_norm_id(r[i]) for r in rows[1:]
                                   if r and r[i] not in (None, ""))
        history_rows = 0
        if history_sheet in wb.sheetnames:
            history_rows = sum(
                1 for r in wb[history_sheet].iter_rows(min_row=2, values_only=True)
                if r and any(v not in (None, "") for v in r)
            )
        return customers, history_rows
    finally:
        wb.close()


def _verify_saved(plan, workbook_path, sheet_name, history, history_sheet,
                  history_before, attempts: int = 5, pause: float = 2.0) -> None:
    """Re-read the file and make sure the plan is really in it.

    Excel can report a save that never reached this file, for example when the
    workbook is open elsewhere. A few short retries allow for a slow flush.
    """
    want = (sorted(_norm_id(v) for v in plan[cfg.COL_CUSTOMER])
            if cfg.COL_CUSTOMER in plan.columns else None)
    want_hist = None if history is None or history_before is None else (
        history_before + len(history))
    got = None
    for attempt in range(attempts):
        got = _on_disk(workbook_path, sheet_name, history_sheet)
        if got is None:
            return                                 # cannot check: do not block
        customers, hist = got
        if (want is None or customers == want) and (
                want_hist is None or hist >= want_hist):
            log.info("Checked %s on disk: %d row(s) in %r%s", workbook_path.name,
                     len(plan), sheet_name,
                     "" if want_hist is None else
                     f", {hist} row(s) in {history_sheet!r}")
            return
        if attempt < attempts - 1:
            time.sleep(pause)
    customers, hist = got
    raise WriteNotSaved(
        f"Excel reported a save, but {workbook_path.name} on disk does not hold "
        f"the new plan ({sheet_name!r}: {len(customers or [])} row(s) on disk, "
        f"{'none' if customers is None else sum(1 for c in (want or []) if c in set(customers))} "
        f"of {len(want or [])} planned meters"
        + ("" if want_hist is None else f"; {history_sheet!r}: {hist} row(s), "
           f"expected {want_hist}")
        + "). Usually the workbook is open in Excel, or OneDrive/AutoSave kept "
        "a different copy. Close the file in Excel and run again. "
        "(ไฟล์บนดิสก์ไม่มีแผนใหม่ ปิดไฟล์ใน Excel แล้วรันอีกครั้ง)"
    )


def write_daily_route(
    plan: pd.DataFrame,
    workbook_path: Path = cfg.WORKBOOK_PATH,
    sheet_name: str = cfg.OUTPUT_SHEET,
    make_backup: bool = True,
    allow_lossy: bool = False,
    history: pd.DataFrame | None = None,
    history_sheet: str = cfg.HISTORY_SHEET,
) -> None:
    """Replace ``sheet_name`` with the plan table and append ``history`` rows.

    Tries Excel COM, then xlwings, then openpyxl. Raises ``LossyWriteRefused`` when
    only openpyxl is left and the workbook has parts it would destroy, unless
    ``allow_lossy`` is set.
    """
    fragile = describe_fragile_parts(workbook_path)

    # Pre-flight: refuse before touching anything -- not even a backup -- when
    # the only strategy that could possibly run is the destructive one.
    if fragile and not allow_lossy and not _automation_module_available():
        raise LossyWriteRefused(_lossy_message(workbook_path, fragile))

    if make_backup:
        backup_workbook(workbook_path)

    before = _on_disk(workbook_path, sheet_name, history_sheet)
    history_before = None if before is None else before[1]

    extra = {} if history is None else {"history": history, "history_sheet": history_sheet}
    for label, writer in (
        ("Excel COM", _write_via_com),
        ("xlwings", _write_via_xlwings),
    ):
        try:
            writer(plan, workbook_path, sheet_name, **extra)
        except _AutomationUnavailable as exc:
            log.info("%s writer unavailable: %s", label, exc)
            continue
        _verify_saved(plan, workbook_path, sheet_name, history, history_sheet,
                      history_before)
        return

    # Excel could not be driven. Everything below rewrites the package by hand.
    if fragile and not allow_lossy:
        raise LossyWriteRefused(_lossy_message(workbook_path, fragile))

    if fragile:
        log.warning(
            "--force-lossy: writing via openpyxl, which will drop %s.",
            _describe(fragile),
        )
    _write_via_openpyxl(plan, workbook_path, sheet_name, **extra)
    _verify_saved(plan, workbook_path, sheet_name, history, history_sheet,
                  history_before)


def _history_layout(history: pd.DataFrame, header) -> tuple[list, list[list]]:
    """The columns to write and the rows under them, matched by header name.

    Columns the sheet has but this version doesn't write stay blank; new ones are
    added at the right.
    """
    existing = [h for h in (header or [])]
    while existing and existing[-1] in (None, ""):
        existing.pop()
    columns = existing + [c for c in history.columns if c not in existing]
    frame = history.reindex(columns=columns)
    rows = [[None if pd.isna(v) else v for v in row]
            for row in frame.itertuples(index=False)]
    return columns, rows


# --------------------------------------------------------------------------
# Strategy 1: Excel COM
# --------------------------------------------------------------------------

class _AutomationUnavailable(RuntimeError):
    """Excel could not be driven in this environment; try the next strategy."""


class _ComUnavailable(_AutomationUnavailable):
    """COM automation cannot be used in this environment."""


class _XlwingsUnavailable(_AutomationUnavailable):
    """xlwings cannot be used in this environment."""


def _rgb_to_excel(rgb: tuple[int, int, int]) -> int:
    r, g, b = rgb
    return r + (g << 8) + (b << 16)


# Errors Excel returns to a COM call while it is busy; the call is retried.
_COM_BUSY = {
    -2147418111,    # RPC_E_CALL_REJECTED          0x80010001
    -2147417846,    # RPC_E_SERVERCALL_RETRYLATER  0x8001010A
}
#: How long to keep asking a busy Excel before giving up, in seconds.
COM_BUSY_TIMEOUT = 120.0


class ExcelBusy(RuntimeError):
    """Excel kept refusing calls: it is busy or waiting on a prompt."""


def _is_com_busy(exc: BaseException) -> bool:
    code = getattr(exc, "hresult", None)
    if code is None and getattr(exc, "args", None):
        code = exc.args[0]
    return code in _COM_BUSY


def _com_retry(action, what: str, timeout: float = COM_BUSY_TIMEOUT,
               pause: float = 0.5, sleep=time.sleep, clock=time.monotonic):
    """Run ``action`` and, while Excel says it is busy, wait and ask again."""
    deadline = clock() + timeout
    told = False
    while True:
        try:
            return action()
        except Exception as exc:                      # noqa: BLE001
            if not _is_com_busy(exc):
                raise
            if clock() >= deadline:
                raise ExcelBusy(
                    f"Excel did not answer for {timeout:.0f}s while {what}. It "
                    f"may be waiting on a prompt (sign-in, sensitivity label, "
                    f"'file in use'). Open the workbook in Excel yourself to "
                    f"see what it asks, close it, end any EXCEL.EXE left in "
                    f"Task Manager, then run again."
                ) from exc
            if not told:
                log.info("Excel is busy (%s); waiting for it ...", what)
                told = True
            sleep(pause)


def _install_busy_filter(pythoncom):
    """Ask COM to retry calls a busy Excel turns away.

    Returns the filter it replaced, or False when it could not be installed.
    """
    try:
        from win32com.server.util import wrap

        class _RetryWhileBusy:
            _com_interfaces_ = [pythoncom.IID_IMessageFilter]
            _public_methods_ = ["HandleInComingCall", "RetryRejectedCall",
                                "MessagePending"]

            def HandleInComingCall(self, call_type, caller, ticks, info):
                return 0                              # SERVERCALL_ISHANDLED

            def RetryRejectedCall(self, callee, ticks, reject_type):
                if reject_type == 2 and ticks < COM_BUSY_TIMEOUT * 1000:
                    return 250                        # retry in 250 ms
                return -1                             # give up

            def MessagePending(self, callee, ticks, pending_type):
                return 2                              # PENDINGMSG_WAITDEFPROCESS

        return pythoncom.CoRegisterMessageFilter(wrap(_RetryWhileBusy()))
    except Exception as exc:                          # noqa: BLE001
        log.debug("COM message filter not installed: %s", exc)
        return False


def _write_via_com(plan: pd.DataFrame, workbook_path: Path, sheet_name: str,
                   history: pd.DataFrame | None = None,
                   history_sheet: str = cfg.HISTORY_SHEET) -> None:
    try:
        import pythoncom
        import win32com.client
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise _ComUnavailable(f"pywin32 not installed: {exc}") from exc

    pythoncom.CoInitialize()
    previous_filter = _install_busy_filter(pythoncom)
    xl = None
    wb = None
    try:
        try:
            xl = win32com.client.DispatchEx("Excel.Application")
        except Exception as exc:  # Excel not installed / COM broken
            raise _ComUnavailable(f"cannot start Excel: {exc}") from exc

        xl.Visible = False
        xl.DisplayAlerts = False
        log.info("Opening workbook via Excel COM ...")
        wb = _com_retry(
            lambda: xl.Workbooks.Open(str(workbook_path), UpdateLinks=0,
                                      ReadOnly=False),
            "opening the workbook",
        )
        # Opening returns before a OneDrive workbook has finished loading;
        # the first property read is where Excel turned calls away.
        if _com_retry(lambda: wb.ReadOnly, "loading the workbook"):
            raise RuntimeError(
                f"{workbook_path.name} opened read-only (is it open in Excel?)"
            )

        names = [w.Name for w in wb.Worksheets]
        if sheet_name in names:
            # Clear rather than delete and re-add: keeps the sheet order and
            # formulas that point at the sheet.
            ws = wb.Worksheets(sheet_name)
            if ws.AutoFilterMode:
                ws.AutoFilterMode = False
            ws.Cells.Clear()
        else:
            ws = wb.Worksheets.Add(After=wb.Worksheets(wb.Worksheets.Count))
            ws.Name = sheet_name
        ws.Activate()            # freeze panes below act on the active window

        n_rows, n_cols = plan.shape

        # Text formats first so Excel does not coerce cycle codes/timestamps.
        for j, name in enumerate(plan.columns, start=1):
            col_cells = ws.Range(ws.Cells(2, j), ws.Cells(n_rows + 1, j))
            if name in TEXT_COLUMNS:
                col_cells.NumberFormat = "@"
            ws.Columns(j).ColumnWidth = COLUMN_WIDTHS.get(name, 14)

        header = ws.Range(ws.Cells(1, 1), ws.Cells(1, n_cols))
        header.Value = list(plan.columns)
        header.Font.Name = "Arial"
        header.Font.Bold = True
        header.Font.Size = 10
        header.Font.Color = _rgb_to_excel((0xFF, 0xFF, 0xFF))
        header.Interior.Color = _rgb_to_excel(HEADER_RGB)
        header.HorizontalAlignment = -4108  # xlCenter

        if n_rows:
            body = ws.Range(ws.Cells(2, 1), ws.Cells(n_rows + 1, n_cols))
            body.Value = [
                [None if pd.isna(v) else v for v in row]
                for row in plan.itertuples(index=False)
            ]
            body.Font.Name = "Arial"
            body.Font.Size = 10
            body.HorizontalAlignment = -4108

            teams = plan["Team"].to_numpy()
            start = 0
            for i in range(1, n_rows + 1):  # contiguous same-team blocks
                if i == n_rows or teams[i] != teams[start]:
                    color = _rgb_to_excel(TEAM_RGBS[(int(teams[start]) - 1) % len(TEAM_RGBS)])
                    ws.Range(ws.Cells(start + 2, 1), ws.Cells(i + 1, n_cols)).Interior.Color = color
                    start = i

        ws.Range(ws.Cells(1, 1), ws.Cells(n_rows + 1, n_cols)).Borders.Weight = 2  # xlThin
        xl.ActiveWindow.FreezePanes = False
        xl.ActiveWindow.SplitRow = 1
        xl.ActiveWindow.FreezePanes = True
        ws.Range(ws.Cells(1, 1), ws.Cells(n_rows + 1, n_cols)).AutoFilter()

        if history is not None and len(history):
            names = [w.Name for w in wb.Worksheets]
            if history_sheet in names:
                hs = wb.Worksheets(history_sheet)
            else:
                hs = wb.Worksheets.Add(After=wb.Worksheets(wb.Worksheets.Count))
                hs.Name = history_sheet
            last = hs.Cells(hs.Rows.Count, 1).End(-4162).Row     # xlUp
            width = hs.UsedRange.Columns.Count
            header = []
            if hs.Cells(1, 1).Value not in (None, ""):
                cells = hs.Range(hs.Cells(1, 1), hs.Cells(1, max(width, 1))).Value
                header = list(cells[0]) if isinstance(cells, tuple) else [cells]
            else:
                last = 1
            columns, values = _history_layout(history, header)
            if columns != header[:len(columns)] or len(header) < len(columns):
                hs.Range(hs.Cells(1, 1), hs.Cells(1, len(columns))).Value = [columns]
                hs.Range(hs.Cells(1, 1), hs.Cells(1, len(columns))).Font.Bold = True
            hs.Range(hs.Cells(last + 1, 1),
                     hs.Cells(last + len(values), len(columns))).Value = values
            log.info("Sheet %r: %d history row(s) appended", history_sheet, len(history))

        _com_retry(wb.Save, "saving the workbook")
        log.info("Sheet %r written via Excel COM: %d rows", sheet_name, n_rows)
    finally:
        # Never let cleanup hide the error that got us here, and always try
        # to quit: a hidden Excel left running keeps the workbook locked.
        if wb is not None:
            try:
                _com_retry(lambda: wb.Close(SaveChanges=False),
                           "closing the workbook", timeout=30)
            except Exception as exc:                  # noqa: BLE001
                log.warning("Could not close the workbook in Excel: %s", exc)
        if xl is not None:
            try:
                _com_retry(xl.Quit, "quitting Excel", timeout=30)
            except Exception as exc:                  # noqa: BLE001
                log.warning(
                    "Excel may still be running in the background and holding "
                    "%s (%s). End EXCEL.EXE in Task Manager before the next run.",
                    workbook_path.name, exc,
                )
        if previous_filter is not False:
            with contextlib.suppress(Exception):
                pythoncom.CoRegisterMessageFilter(previous_filter)
        pythoncom.CoUninitialize()


# --------------------------------------------------------------------------
# Strategy 2: xlwings (macOS + Excel for Mac; AppleScript instead of COM)
# --------------------------------------------------------------------------

XL_CENTER = -4108  # xlCenter, the COM constant


def _best_effort(label: str, action) -> None:
    """Run a cosmetic step. Formatting must never fail a published plan."""
    try:
        action()
    except Exception as exc:  # noqa: BLE001 - cosmetics are never fatal
        log.info("xlwings: %s skipped on this platform (%s)", label, exc)


def _set_center(rng) -> None:
    """Centre a range. xlwings exposes no cross-platform alignment API."""
    try:
        rng.api.HorizontalAlignment = XL_CENTER          # Windows COM
    except Exception:                                    # noqa: BLE001
        from appscript import k                          # macOS AppleScript

        rng.api.horizontal_alignment.set(k.center)


# Excel for Mac sets borders through the `get_border` command, one edge at a time.
MAC_BORDER_EDGES = (
    "border_top", "border_bottom", "border_left", "border_right",
    "inside_horizontal", "inside_vertical",
)


def _thin_borders(rng) -> None:
    try:
        rng.api.Borders.Weight = 2                       # xlThin, Windows COM
        return
    except Exception:                                    # noqa: BLE001
        pass

    from appscript import k                              # macOS AppleScript

    applied = []
    for edge in MAC_BORDER_EDGES:
        try:
            border = rng.api.get_border(which_border=getattr(k, edge))
        except Exception:                                # noqa: BLE001
            continue                                     # edge Excel lacks
        with contextlib.suppress(Exception):
            border.line_style.set(k.continuous)          # else nothing shows
        # The accepted spelling of "thin" is not documented anywhere we can
        # read, so try the plausible ones and keep the first Excel takes.
        for weight in (k.border_weight_thin, k.thin, 2):
            try:
                border.weight.set(weight)
                applied.append(edge)
                break
            except Exception:                            # noqa: BLE001
                continue

    if not applied:
        raise RuntimeError("no border edge accepted a weight")
    log.info(
        "xlwings: borders applied to %d of %d edges (%s)",
        len(applied), len(MAC_BORDER_EDGES), ", ".join(applied),
    )


def _freeze_header(sheet) -> None:
    window = sheet.book.app.api
    try:
        window.ActiveWindow.SplitRow = 1                 # Windows COM
        window.ActiveWindow.FreezePanes = True
    except Exception:                                    # noqa: BLE001
        window.windows[1].split_row.set(1)               # macOS AppleScript
        window.windows[1].freeze_panes.set(True)


# The sheet is rebuilt every run, so its table goes with it; the name only
# has to be unique within the workbook at the moment it is created.
FILTER_TABLE_NAME = "DailyRouteFilter"


def _autofilter(sheet, rng) -> None:
    """Put filter dropdowns on the header row.

    Excel for Mac has no AutoFilter on a plain range through AppleScript, so on
    macOS the range becomes an Excel Table instead.
    """
    try:
        rng.api.AutoFilter(1)                            # Windows COM
        return
    except Exception:                                    # noqa: BLE001
        pass
    sheet.tables.add(source=rng, name=FILTER_TABLE_NAME, table_style_name=None)


def _replace_sheet(book, sheet_name: str):
    """Drop any existing ``sheet_name`` and add a fresh one in its place.

    Names are resolved before anything is deleted, because xlwings sheet objects on
    macOS are index references that shift when a sheet is removed.
    """
    names = [existing.name for existing in book.sheets]  # resolve, then mutate
    position = None
    if sheet_name in names:
        position = names.index(sheet_name)
        book.sheets[sheet_name].delete()
        names.remove(sheet_name)
    if not names:
        return book.sheets.add(name=sheet_name)
    if position is None:
        return book.sheets.add(name=sheet_name, after=book.sheets[names[-1]])
    if position == 0:
        return book.sheets.add(name=sheet_name, before=book.sheets[names[0]])
    return book.sheets.add(name=sheet_name, after=book.sheets[names[position - 1]])


def _append_history_xlwings(book, history: pd.DataFrame, sheet_name: str) -> None:
    """Append ``history`` below the rows ``sheet_name`` already holds."""
    names = [s.name for s in book.sheets]
    if sheet_name in names:
        sheet = book.sheets[sheet_name]
    else:
        sheet = book.sheets.add(name=sheet_name, after=book.sheets[names[-1]])
    if sheet.range((1, 1)).value in (None, ""):
        header, last = [], 1
    else:
        header = sheet.range((1, 1)).expand("right").value
        header = header if isinstance(header, list) else [header]
        last = sheet.range((sheet.cells.last_cell.row, 1)).end("up").row
    columns, values = _history_layout(history, header)
    if columns != header:
        cells = sheet.range((1, 1), (1, len(columns)))
        cells.value = columns
        cells.font.bold = True
    sheet.range((last + 1, 1)).value = values
    log.info("Sheet %r: %d history row(s) appended after row %d",
             sheet_name, len(history), last)


def _write_via_xlwings(plan: pd.DataFrame, workbook_path: Path, sheet_name: str,
                       history: pd.DataFrame | None = None,
                       history_sheet: str = cfg.HISTORY_SHEET) -> None:
    """Write the sheet by driving Excel for Mac, so Excel saves the file.

    Structurally identical to the COM writer: every part of the workbook the
    openpyxl path would drop survives, because openpyxl never touches it.
    """
    try:
        import xlwings as xw
    except ImportError as exc:
        raise _XlwingsUnavailable(f"xlwings not installed: {exc}") from exc

    # Remove xlwings' exit hook: it can print a traceback after a successful
    # write while Excel is shutting down, and it only resets VBA macros.
    with contextlib.suppress(Exception):
        import atexit

        from xlwings import _xlmac
        atexit.unregister(_xlmac.cleanup)

    # Reuse a running Excel and only quit one we started.
    try:
        running = list(xw.apps)
    except Exception:  # noqa: BLE001 - treat an unreadable app list as none
        running = []

    started_here = False
    if running:
        app = running[0]
        log.info("Reusing the Excel already running (%d instance(s) found)", len(running))
    else:
        # DISPATCH_XLWINGS_VISIBLE=1 shows Excel, so a modal dialog that would
        # otherwise hang the run invisibly can be seen and dismissed.
        visible = os.environ.get("DISPATCH_XLWINGS_VISIBLE") == "1"
        try:
            app = xw.App(visible=visible, add_book=False)
            started_here = True
        except Exception as exc:  # Excel not installed / automation denied
            raise _XlwingsUnavailable(f"cannot start Excel: {exc}") from exc
        log.info("Started Excel (visible=%s)", visible)

    book = None
    try:
        with contextlib.suppress(Exception):
            app.display_alerts = False

        for open_book in app.books:
            if open_book.name == workbook_path.name:
                raise RuntimeError(
                    f"{workbook_path.name} is already open in Excel. Close it "
                    f"(and any leftover hidden Excel: pkill -f 'Microsoft Excel') "
                    f"and run again."
                )

        log.info("Opening %s via xlwings ...", workbook_path.name)
        book = app.books.open(str(workbook_path), update_links=False)
        log.info("Workbook open. Replacing sheet %r ...", sheet_name)

        sheet = _replace_sheet(book, sheet_name)
        log.info("Sheet %r created. Writing %d rows ...", sheet_name, len(plan))

        n_rows, n_cols = plan.shape

        # Text formats first, so Excel does not coerce "02" -> 2 or "09:04"
        # -> a time value. Same reasoning as the COM writer.
        for j, name in enumerate(plan.columns, start=1):
            if name in TEXT_COLUMNS and n_rows:
                sheet.range((2, j), (n_rows + 1, j)).number_format = "@"
            sheet.range((1, j)).column_width = COLUMN_WIDTHS.get(name, 14)

        header = sheet.range((1, 1), (1, n_cols))
        header.value = list(plan.columns)
        header.font.name = "Arial"
        header.font.bold = True
        header.font.size = 10
        header.font.color = (0xFF, 0xFF, 0xFF)
        header.color = HEADER_RGB
        _best_effort("header alignment", lambda: _set_center(header))

        if n_rows:
            body = sheet.range((2, 1), (n_rows + 1, n_cols))
            body.value = [
                [None if pd.isna(v) else v for v in row]
                for row in plan.itertuples(index=False)
            ]
            body.font.name = "Arial"
            body.font.size = 10
            _best_effort("body alignment", lambda: _set_center(body))

            teams = plan["Team"].to_numpy()
            start = 0
            for i in range(1, n_rows + 1):  # contiguous same-team blocks
                if i == n_rows or teams[i] != teams[start]:
                    color = TEAM_RGBS[(int(teams[start]) - 1) % len(TEAM_RGBS)]
                    sheet.range((start + 2, 1), (i + 1, n_cols)).color = color
                    start = i

        table = sheet.range((1, 1), (n_rows + 1, n_cols))
        _best_effort("borders", lambda: _thin_borders(table))
        _best_effort("freeze panes", lambda: _freeze_header(sheet))
        _best_effort("autofilter", lambda: _autofilter(sheet, table))

        if history is not None and len(history):
            _append_history_xlwings(book, history, history_sheet)

        try:
            book.save()
        except Exception as exc:  # noqa: BLE001 - fatal: do NOT fall through
            raise RuntimeError(
                f"Excel could not save {workbook_path.name} -- is the workbook "
                f"open in Excel, or read-only? ({exc})"
            ) from exc
        log.info("Sheet %r written via xlwings: %d rows", sheet_name, n_rows)
    finally:
        if book is not None:
            with contextlib.suppress(Exception):
                book.close()
        if started_here:  # never quit an Excel the user had open themselves
            with contextlib.suppress(Exception):
                app.quit()


# --------------------------------------------------------------------------
# Strategy 3: openpyxl fallback (lossy)
# --------------------------------------------------------------------------

def _write_via_openpyxl(plan: pd.DataFrame, workbook_path: Path, sheet_name: str,
                        history: pd.DataFrame | None = None,
                        history_sheet: str = cfg.HISTORY_SHEET) -> None:
    from openpyxl import load_workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    header_fill = PatternFill("solid", fgColor="1F4E78")
    header_font = Font(name="Arial", bold=True, color="FFFFFF", size=10)
    body_font = Font(name="Arial", size=10)
    team_fills = ["DDEBF7", "E2EFDA", "FFF2CC", "FCE4D6"]
    thin = Border(*(Side(style="thin", color="BFBFBF"),) * 4)

    log.info("Opening workbook via openpyxl (keep_vba=True) ...")
    wb = load_workbook(workbook_path, keep_vba=True)
    position = None
    if sheet_name in wb.sheetnames:
        position = wb.sheetnames.index(sheet_name)
        del wb[sheet_name]
    ws = wb.create_sheet(sheet_name, position)

    for col_idx, name in enumerate(plan.columns, start=1):
        cell = ws.cell(row=1, column=col_idx, value=name)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cell.border = thin
        ws.column_dimensions[get_column_letter(col_idx)].width = COLUMN_WIDTHS.get(name, 14)

    team_col = list(plan.columns).index("Team") if "Team" in plan.columns else 0
    for row_idx, record in enumerate(plan.itertuples(index=False), start=2):
        team = record[team_col]
        fill = PatternFill("solid", fgColor=team_fills[(int(team) - 1) % len(team_fills)])
        for col_idx, value in enumerate(record, start=1):
            cell = ws.cell(row=row_idx, column=col_idx, value=value)
            cell.font = body_font
            cell.fill = fill
            cell.border = thin
            cell.alignment = Alignment(horizontal="center")

    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(plan.columns))}{len(plan) + 1}"

    if history is not None and len(history):
        hs = (wb[history_sheet] if history_sheet in wb.sheetnames
              else wb.create_sheet(history_sheet))
        empty = hs.max_row == 1 and hs.cell(1, 1).value in (None, "")
        header = [] if empty else [c.value for c in hs[1]]
        columns, values = _history_layout(history, header)
        if columns != header:
            for j, name in enumerate(columns, start=1):
                hs.cell(row=1, column=j, value=name).font = Font(bold=True)
        for row in values:
            hs.append(row)
        log.info("Sheet %r: %d history row(s) appended", history_sheet, len(history))

    log.info("Saving workbook ...")
    wb.save(workbook_path)
    wb.close()
    log.info("Sheet %r written via openpyxl: %d rows", sheet_name, len(plan))
