"""The planning screen (tkinter). It collects the dates and settings and runs main.py.

The on-screen text is Thai, for the planners who use it.
"""
from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from datetime import date, timedelta
from pathlib import Path
from tkinter import font as tkfont
from tkinter import messagebox, scrolledtext, ttk

PROJECT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT))

from dispatch.plan_dates import (  # noqa: E402
    LAST, LOST, THAI_MONTHS, WINDOW, PlanDates, thai_month_title,
)
from dispatch.progress import parse as parse_progress                 # noqa: E402
from dispatch.progress import position as progress_position           # noqa: E402
from map_opener import MapOpener                                      # noqa: E402

try:
    from dispatch.config import AppConfig

    _app = AppConfig()
    CALENDAR = _app.calendar
    DEFAULT_SPEED = _app.routing.target_average_speed_kmh or 60.0
    from dispatch.config import WORKBOOK_PATH as WORKBOOK
except Exception:                       # the screen still opens without the config
    from dispatch.workdays import DEFAULT_CALENDAR as CALENDAR
    DEFAULT_SPEED = 60.0
    WORKBOOK = PROJECT / "workforce.xlsx"

# Month names for the crew list ("14 Oct"), fixed so the machine's locale
# cannot change them.
MONTH_ABBR = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
              "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

# How jobs are chosen, shown at the top of the screen. There is one way; the
# box explains it rather than offering a choice.
SELECTION_TITLE = "ซ่อมตามรอบบิลเป็นหลัก"
SELECTION_HINT = ("ซ่อมตามรอบบิลเป็นหลัก เสริมด้วยรอบบิลที่จะซ่อมล่วงหน้า หากไม่ครบจำนวน"
                  "จะเติมด้วยงานที่อยู่ใกล้เส้นทาง")

THAI_WEEKDAYS = ["จ", "อ", "พ", "พฤ", "ศ", "ส", "อา"]

# Calendar colours
WORK_BG = "#FFE27A"        # picked work day: yellow
WINDOW_BG = "#BDE3F8"      # picked advance-window reading date: light blue
PLAIN_BG = "#FFFFFF"
LAST_BORDER = "#E07B00"    # last-chance reading date: orange frame (filled in, editable)
TEXT = "#222222"
OFF_TEXT = "#D11A1A"       # weekends and holidays: red text
LOST_TEXT = "#A0A0A0"      # reading date already too late


class CalendarPicker(ttk.Frame):
    """A six-week calendar where dates are picked one at a time.

    The date rules live in PlanDates; this class only turns clicks into calls and
    draws the result.
    """

    WEEKS = 6

    def __init__(self, parent, dates: PlanDates, on_change) -> None:
        super().__init__(parent)
        self.dates = dates
        self.on_change = on_change
        self.mode: str | None = None          # None, "work", "last" or "window"
        self.view_start = self._monday(dates.horizon[0] if dates.horizon else date.today())
        self.font_normal = tkfont.nametofont("TkDefaultFont").copy()
        self.font_bold = tkfont.nametofont("TkDefaultFont").copy()
        self.font_bold.configure(weight="bold")
        self.font_title = tkfont.nametofont("TkDefaultFont").copy()
        self.font_title.configure(size=self.font_title.cget("size") + 4,
                                  weight="bold")

        # --- mode buttons ---
        pick = ttk.Frame(self)
        pick.grid(row=0, column=0, sticky="w")
        self.work_btn = ttk.Button(pick, text="วันทำงาน",
                                   command=lambda: self.enter("work"))
        self.work_btn.grid(row=0, column=0)
        self.last_btn = ttk.Button(pick, text="รอบบิลที่ต้องซ่อมให้ทัน",
                                   command=lambda: self.enter("last"))
        self.last_btn.grid(row=0, column=1, padx=(6, 0))
        self.window_btn = ttk.Button(pick, text="รอบบิลที่จะซ่อมล่วงหน้า",
                                     command=lambda: self.enter("window"))
        self.window_btn.grid(row=0, column=2, padx=(6, 0))
        self.done_btn = ttk.Button(pick, text="เสร็จสิ้น", command=self.finish,
                                   state="disabled")
        self.done_btn.grid(row=0, column=3, padx=(18, 0))
        self.hint = ttk.Label(self, text="", foreground="#1F4E78")
        self.hint.grid(row=1, column=0, sticky="w", pady=(6, 0))

        # --- month title and scroll buttons ---
        head = ttk.Frame(self)
        head.grid(row=2, column=0, sticky="ew", pady=(10, 4))
        ttk.Button(head, text="◀", width=3, command=lambda: self.shift(-7)
                   ).grid(row=0, column=0)
        self.title = ttk.Label(head, text="", font=self.font_title)
        self.title.grid(row=0, column=1, padx=12)
        ttk.Button(head, text="▶", width=3, command=lambda: self.shift(7)
                   ).grid(row=0, column=2)
        ttk.Button(head, text="รีเซ็ตรอบบิลที่ต้องซ่อมให้ทัน", command=self.reset_last
                   ).grid(row=0, column=3, padx=(24, 0))
        ttk.Button(head, text="ล้างรอบบิลล่วงหน้า", command=self.clear_window
                   ).grid(row=0, column=4, padx=(6, 0))

        # --- the grid of days ---
        grid = tk.Frame(self, background=PLAIN_BG)
        grid.grid(row=3, column=0, sticky="w")
        for c, name in enumerate(THAI_WEEKDAYS):
            tk.Label(grid, text=name, width=8, background=PLAIN_BG,
                     foreground=OFF_TEXT if c >= 5 else "#555555",
                     font=self.font_bold).grid(row=0, column=c, pady=(2, 2))
        self.cells: list[tk.Label] = []
        self.frames: list[tk.Frame] = []
        for r in range(self.WEEKS):
            for c in range(7):
                # The orange frame is a Frame around the Label: macOS ignores a Label's highlight
                frame = tk.Frame(grid, background=PLAIN_BG, padx=3, pady=3)
                frame.grid(row=r + 1, column=c, padx=1, pady=1)
                cell = tk.Label(frame, width=8, height=2, relief="flat",
                                borderwidth=0, cursor="hand2")
                cell.pack()
                self.frames.append(frame)
                cell.bind("<Button-1>", self._clicked)
                self.cells.append(cell)

        # --- colour legend ---
        legend = ttk.Frame(self)
        legend.grid(row=4, column=0, sticky="w", pady=(8, 0))
        items = [
            (WORK_BG, None, "วันทำงาน"),
            (WINDOW_BG, None, "รอบบิลที่จะซ่อมล่วงหน้า"),
            (PLAIN_BG, LAST_BORDER, "รอบบิลที่ต้องซ่อมให้ทัน"),
        ]
        for i, (bg, border, text) in enumerate(items):
            box = tk.Frame(legend, background=border or bg, padx=3, pady=3)
            box.grid(row=0, column=i * 2, padx=(0 if i == 0 else 14, 4))
            tk.Label(box, width=2, background=bg).pack()
            ttk.Label(legend, text=text).grid(row=0, column=i * 2 + 1, sticky="w")
        ttk.Label(legend, text="ตัวแดง = เสาร์ อาทิตย์ และวันหยุด",
                  foreground=OFF_TEXT).grid(row=1, column=0, columnspan=6,
                                            sticky="w", pady=(4, 0))

        # --- written summary ---
        self.summary = ttk.Label(self, text="", foreground="#1F4E78",
                                 justify="left", wraplength=560)
        self.summary.grid(row=5, column=0, sticky="w", pady=(10, 0))
        self.note = ttk.Label(self, text="", foreground="#9A5B00",
                              justify="left", wraplength=560)
        self.note.grid(row=6, column=0, sticky="w", pady=(4, 0))

        self.enter(None)

    # -- helpers ---------------------------------------------------------

    @staticmethod
    def _monday(day: date) -> date:
        return day - timedelta(days=day.weekday())

    def _day_at(self, index: int) -> date:
        return self.view_start + timedelta(days=index)

    # -- modes -----------------------------------------------------------

    def enter(self, mode: str | None) -> None:
        """Enter a picking mode, finishing the current one first."""
        if self.mode is not None and mode is not None and mode != self.mode:
            if self.finish():
                return                          # work days not valid yet: stay in that mode
        self.mode = mode
        if not self.dates.work and mode is None:
            self.hint.configure(text="เริ่มจากกดปุ่ม วันทำงาน แล้วคลิกวันที่จะออกงาน")
        elif mode == "work":
            self.hint.configure(text="กำลังเลือกวันทำงาน — คลิกวันที่จะออกงาน "
                                     "(คลิกซ้ำเพื่อยกเลิก) แล้วกด เสร็จสิ้น")
        elif mode == "last":
            self.hint.configure(text="กำลังเลือกรอบบิลที่ต้องซ่อมให้ทัน — คลิกวัน"
                                     "อ่านหน่วย (คลิกซ้ำเพื่อเอาออก) แล้วกด เสร็จสิ้น")
        elif mode == "window":
            self.hint.configure(text="กำลังเลือกรอบบิลที่จะซ่อมล่วงหน้า — คลิกวัน"
                                     "อ่านหน่วย (คลิกซ้ำเพื่อยกเลิก) แล้วกด เสร็จสิ้น")
        else:
            self.hint.configure(text="กดปุ่มด้านบนเพื่อแก้วันทำงาน "
                                     "รอบบิลที่ต้องซ่อมให้ทัน หรือรอบบิลที่จะซ่อมล่วงหน้า")
        self.done_btn.configure(state="normal" if mode else "disabled")
        self.work_btn.configure(
            state="disabled" if mode == "work" else "normal")
        self.last_btn.configure(state="disabled" if mode == "last" else "normal")
        self.window_btn.configure(state="disabled" if mode == "window" else "normal")
        self.render()

    def finish(self) -> str:
        """The done button: check the picks, return an error message if they can't be used yet."""
        if self.mode == "work":
            message = self.dates.finish_work()
            if self.dates.work_error():
                self.note.configure(text=message)
                return message                  # stay in the mode so it can be fixed
        elif self.mode == "last":
            message = self.dates.finish_last()
        elif self.mode == "window":
            message = self.dates.finish_window()
        else:
            return ""
        self.mode = None
        self.note.configure(text=message)
        self.enter(None)
        self.on_change()
        return ""

    # -- actions ---------------------------------------------------------

    def _clicked(self, event) -> None:
        day = self._day_at(self.cells.index(event.widget))
        if self.mode == "work":
            message = self.dates.toggle_work(day)
        elif self.mode == "last":
            message = self.dates.toggle_last(day)
        elif self.mode == "window":
            message = self.dates.toggle_window(day)
        else:
            message = ("กดปุ่ม วันทำงาน รอบบิลที่ต้องซ่อมให้ทัน หรือ "
                       "รอบบิลที่จะซ่อมล่วงหน้า ก่อน แล้วจึงคลิกวัน")
        self.note.configure(text=message)
        self.render()
        self.on_change()

    def shift(self, days: int) -> None:
        self.view_start += timedelta(days=days)
        self.render()

    def reset_last(self) -> None:
        self.dates.reset_last()
        self.note.configure(text="รอบบิลที่ต้องซ่อมให้ทันกลับเป็นค่าที่ระบบคำนวณจาก"
                                 "วันทำงาน")
        self.render()
        self.on_change()

    def clear_window(self) -> None:
        self.dates.clear_window()
        self.note.configure(text="ไม่ซ่อมล่วงหน้า: จะทำเฉพาะรอบบิลที่ต้องซ่อม"
                                 "ให้ทัน ช่องที่เหลือเติมตามระยะทาง")
        self.render()
        self.on_change()

    # -- drawing ---------------------------------------------------------

    def render(self) -> None:
        first = self._day_at(0)
        last = self._day_at(len(self.cells) - 1)
        self.title.configure(text=thai_month_title(first, last))
        for i, cell in enumerate(self.cells):
            day = self._day_at(i)
            klass = self.dates.reading_class(day)
            if self.dates.is_work_day(day):
                bg = WORK_BG
            elif day in self.dates.window and klass == WINDOW:
                bg = WINDOW_BG
            else:
                bg = PLAIN_BG
            if not CALENDAR.is_working(day):
                fg = OFF_TEXT
            elif klass == LOST and not self.dates.is_work_day(day):
                fg = LOST_TEXT
            else:
                fg = TEXT
            border = LAST_BORDER if klass == LAST else bg
            # Month on every cell, so the days around a month end can't be confused
            text = f"{day.day} {THAI_MONTHS[day.month]}"
            self.frames[i].configure(background=border)
            cell.configure(
                text=text, background=bg, foreground=fg,
                font=self.font_bold if bg != PLAIN_BG else self.font_normal,
            )
        self.summary.configure(text="\n".join(self.dates.summary()))


class App:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.proc: subprocess.Popen | None = None
        self.kind = ""          # what is running: "dry", "real" or "save"
        self.changed = False    # days or settings changed since the run started
        self.lines: queue.Queue[str | None] = queue.Queue()
        self.roster, self.roster_error = None, ""
        # Work days are picked on the calendar; the Working Day sheet only checks them
        self.dates = PlanDates(calendar=CALENDAR)
        self.load_roster()

        # Progress bar state: (now, may creep up to, rough seconds, started at)
        self.stage = (0, 0, 0, time.monotonic())

        root.title("Smart Meter Technician Dispatch & Routing Optimizer")
        root.geometry("1200x920")
        root.minsize(1000, 700)
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(2, weight=1)

        top = ttk.Frame(root, padding=(16, 14, 16, 8))
        top.grid(row=0, column=0, sticky="ew")
        top.columnconfigure(0, weight=0)
        top.columnconfigure(1, weight=1)

        # ======== left column: settings ========
        left = ttk.Frame(top)
        left.grid(row=0, column=0, sticky="nw", padx=(0, 18))

        box = ttk.LabelFrame(left, text=" เลือกงานเข้าแผนอย่างไร ", padding=10)
        box.grid(row=0, column=0, sticky="ew")
        self.font_bold = tkfont.nametofont("TkDefaultFont").copy()
        self.font_bold.configure(weight="bold")
        ttk.Label(box, text=SELECTION_TITLE, font=self.font_bold).grid(
            row=0, column=0, sticky="w")
        ttk.Label(box, text=SELECTION_HINT, foreground="#666", justify="left",
                  wraplength=420).grid(row=1, column=0, sticky="w", pady=(0, 4))

        crews = ttk.LabelFrame(left, text=" Technician Info. ", padding=10)
        crews.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        self.capacity = ttk.Label(crews, text="", foreground="#1F4E78",
                                  justify="left", wraplength=420)
        self.capacity.grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Button(crews, text="อ่าน workforce.xlsx ใหม่", command=self.reload
                   ).grid(row=0, column=2, sticky="ne", padx=(8, 0))
        ttk.Label(crews, text="").grid(row=1, column=0)
        ttk.Label(crews, text="ความเร็วเฉลี่ยของรถ").grid(
            row=2, column=0, sticky="w", pady=(8, 0))
        self.speed = tk.StringVar(value=f"{DEFAULT_SPEED:g}")
        ttk.Spinbox(crews, from_=10, to=120, increment=5, width=5,
                    textvariable=self.speed, justify="center").grid(
            row=2, column=1, padx=(8, 4), pady=(8, 0))
        ttk.Label(crews, text=f"กม./ชม. (ค่าเริ่มต้น {DEFAULT_SPEED:g})").grid(
            row=2, column=2, sticky="w", pady=(8, 0))

        route = ttk.LabelFrame(left, text=" จัดเส้นทางอย่างไร ", padding=10)
        route.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        self.metric = tk.StringVar(value="time")
        ttk.Radiobutton(route, text="เร็วที่สุด · ใช้เวลาเดินทางน้อยที่สุด (คำนวณจากความเร็ว)",
                        value="time", variable=self.metric).grid(row=0, column=0, sticky="w")
        ttk.Radiobutton(route, text="สั้นที่สุด · ใช้ระยะทางน้อยที่สุด",
                        value="distance", variable=self.metric).grid(row=1, column=0, sticky="w")

        opts = ttk.Frame(left)
        opts.grid(row=3, column=0, sticky="w", pady=(10, 0))
        self.dry = tk.BooleanVar(value=True)
        ttk.Checkbutton(opts, text="ทดลองคำนวณ ไม่เขียนลงไฟล์ Excel",
                        variable=self.dry).grid(row=0, column=0, sticky="w")
        self.nomap = tk.BooleanVar(value=False)
        ttk.Checkbutton(opts, text="ไม่ต้องวาดแผนที่",
                        variable=self.nomap).grid(row=0, column=1, sticky="w",
                                                  padx=(18, 0))

        # ======== right column: calendar ========
        cal_box = ttk.LabelFrame(top, text=" ช่วงวันของแผน ", padding=10)
        cal_box.grid(row=0, column=1, sticky="nw")
        self.calendar = CalendarPicker(cal_box, self.dates, self.refresh)
        self.calendar.grid(row=0, column=0, sticky="w")
        ttk.Label(
            cal_box,
            text="เลือกวันทำงานเองบนปฏิทิน · จำนวนงานของแต่ละทีมและ depot อ่านจาก"
                 "หน้า Working Day ใน workforce.xlsx ซึ่งใช้ตรวจวันที่เลือกด้วย",
            foreground="#666", justify="left", wraplength=560,
        ).grid(row=1, column=0, sticky="w", pady=(10, 0))

        # ======== run button and progress bar ========
        bar = ttk.Frame(root, padding=(16, 0, 16, 8))
        bar.grid(row=1, column=0, sticky="ew")
        bar.columnconfigure(2, weight=1)
        self.run_btn = ttk.Button(bar, text="วางแผน", command=self.start)
        self.run_btn.grid(row=0, column=0, rowspan=2, sticky="n")
        # Enabled after a successful trial run, until any day or setting changes
        self.save_btn = ttk.Button(bar, text="บันทึกผลลง Excel", state="disabled",
                                   command=self.save_dry_run)
        self.save_btn.grid(row=0, column=4, rowspan=2, sticky="n", padx=(12, 0))
        # The map button works only once a run has finished and drawn a map
        self.map_btn = ttk.Button(bar, text="เปิดแผนที่", state="disabled",
                                  command=self.open_map)
        self.map_btn.grid(row=0, column=3, rowspan=2, sticky="n", padx=(12, 0))
        self.progress = ttk.Progressbar(bar, mode="determinate", maximum=100,
                                        length=420)
        self.progress.grid(row=0, column=1, sticky="w", padx=(12, 0))
        self.stage_label = ttk.Label(bar, text="", foreground="#1F4E78")
        self.stage_label.grid(row=0, column=2, sticky="w", padx=(10, 0))
        self.status = ttk.Label(bar, text="พร้อมรัน", foreground="#444")
        self.status.grid(row=1, column=1, columnspan=2, sticky="w",
                         padx=(12, 0), pady=(4, 0))

        self.out = scrolledtext.ScrolledText(
            root, wrap="none", font=("Menlo" if sys.platform == "darwin"
                                     else "Consolas", 10))
        self.out.grid(row=2, column=0, sticky="nsew", padx=16, pady=(0, 16))
        self.out.configure(state="disabled")

        self.map = MapOpener(PROJECT, root.after, self.map_message)

        # A trial run's result only fits the settings it was computed with
        for var in (self.metric, self.speed):
            var.trace_add("write", lambda *_: self.forget_dry_run())

        self.refresh()

    # ------------------------------------------------------------------

    def load_roster(self) -> None:
        """Read the Working Day sheet (jobs per team per day, and the depots)."""
        try:
            from dispatch.workforce import load_roster
            self.roster, self.roster_error = load_roster(WORKBOOK), ""
        except Exception as exc:                 # shown on screen; never closes the window
            self.roster, self.roster_error = None, str(exc)

    def reload(self) -> None:
        """Re-read workforce.xlsx after the Working Day sheet was edited.
        The days picked on the calendar stay; they are only checked again."""
        self.load_roster()
        self.refresh()

    def roster_check(self) -> tuple[list, list]:
        """(picked days with no jobs in Working Day, days with jobs that were not picked)"""
        if self.roster is None:
            return [], []
        return self.dates.check_roster(self.roster, date.today())

    def forget_dry_run(self) -> None:
        """A day or setting changed, so the last trial run no longer matches the screen."""
        self.changed = True
        if getattr(self, "save_btn", None) is not None:
            self.save_btn.configure(state="disabled")

    def refresh(self) -> None:
        """Check the picked days against Working Day and summarise each day's jobs."""
        self.forget_dry_run()
        if self.roster is None:
            self.capacity.configure(
                text=f"อ่านหน้า Working Day ไม่ได้: {self.roster_error[:160]}",
                foreground="#B00020")
            return
        missing, not_picked = self.roster_check()
        lines, total = [], 0
        for d in self.dates.horizon:
            crew = self.roster.on(d)
            if d in missing:
                lines.append(f"{d.day} {MONTH_ABBR[d.month - 1]}: ✗ ไม่มีงานในหน้า "
                             f"Working Day")
                continue
            jobs = "/".join(str(n) for n in crew.jobs)
            total += crew.total
            end = crew.end.name if crew.end else "ไม่กลับ depot"
            lines.append(f"{d.day} {MONTH_ABBR[d.month - 1]}: ทีม {jobs} = {crew.total} งาน · "
                         f"{crew.start.name} → {end}")
        if not self.dates.work:
            lines.append("ยังไม่ได้เลือกวันทำงาน")
        else:
            lines.append(f"รวม {len(self.dates.work) - len(missing)} วัน {total:,} งาน")
        if missing:
            lines.append("วันที่มี ✗ วางแผนไม่ได้ — กรอกจำนวนงานในหน้า Working Day "
                         "แล้วกด อ่าน workforce.xlsx ใหม่ หรือเอาวันนั้นออกจากปฏิทิน")
        if not_picked:
            label = "หน้า Working Day มีงานวันที่" if not self.dates.work \
                else "หน้า Working Day มีงานแต่ไม่ได้เลือก:"
            lines.append(f"{label} "
                         + ", ".join(f"{d.day} {MONTH_ABBR[d.month - 1]}"
                                     for d in not_picked[:10])
                         + (" …" if len(not_picked) > 10 else ""))
        self.capacity.configure(text="\n".join(lines),
                                foreground="#B00020" if missing else "#1F4E78")

    def log(self, text: str, clear: bool = False) -> None:
        self.out.configure(state="normal")
        if clear:
            self.out.delete("1.0", "end")
        self.out.insert("end", text)
        self.out.see("end")
        self.out.configure(state="disabled")

    # -- progress --------------------------------------------------------

    def set_stage(self, now: int, until: int, seconds: int, label: str) -> None:
        self.stage = (now, until, seconds, time.monotonic())
        # Never backwards: the bar only moves forward
        self.progress["value"] = max(float(self.progress["value"]), float(now))
        self.stage_label.configure(text=f"{label} …" if now < 100 else label)

    def tick(self) -> None:
        if self.proc is None:
            return
        now, until, seconds, started = self.stage
        target = progress_position(now, until, seconds,
                                   time.monotonic() - started)
        if target > float(self.progress["value"]):
            self.progress["value"] = target
        self.root.after(200, self.tick)

    # -- running ---------------------------------------------------------

    def start(self) -> None:
        if self.proc is not None:
            return

        try:
            speed = float(str(self.speed.get()).strip())
            if not 10 <= speed <= 120:
                raise ValueError
        except (tk.TclError, ValueError):
            messagebox.showerror("ความเร็วไม่ถูกต้อง",
                                 "กรอกความเร็วเฉลี่ยของรถได้ตั้งแต่ 10 ถึง 120 กม./ชม.")
            return

        if self.calendar.mode is not None:
            if self.calendar.finish():
                messagebox.showerror("วันทำงานยังใช้ไม่ได้",
                                     self.dates.work_error())
                return
        error = self.dates.work_error()
        if error:
            messagebox.showerror("วันทำงานยังใช้ไม่ได้", error)
            return
        if self.roster is None:
            messagebox.showerror("อ่านหน้า Working Day ไม่ได้",
                                 f"{self.roster_error[:300]}\n\n"
                                 "ปิดไฟล์ใน Excel แล้วกด อ่าน workforce.xlsx ใหม่")
            return
        missing, _ = self.roster_check()
        if missing:
            days = ", ".join(f"{d.day} {MONTH_ABBR[d.month - 1]}" for d in missing)
            messagebox.showerror(
                "วันทำงานไม่ตรงกับหน้า Working Day",
                f"หน้า Working Day ไม่มีงานในวันที่ {days}\n\n"
                "กรอกจำนวนงานของแต่ละทีมและ depot ของวันนั้นใน workforce.xlsx "
                "แล้วกด อ่าน workforce.xlsx ใหม่ หรือเอาวันนั้นออกจากปฏิทิน")
            return

        first = self.dates.horizon[0]
        if not self.dry.get():
            ok = messagebox.askokcancel(
                "ยืนยันการรันจริง",
                f"แผนของวันที่ {first:%d/%m/%Y} จะถูกเขียนลงหน้า Daily Route "
                f"และประวัติลงหน้า Working History ในไฟล์ workforce.xlsx\n\n"
                f"วันที่เหลือเป็นแผนล่วงหน้า แสดงบนแผนที่ แต่ไม่เขียนลง Excel "
                f"เพราะพรุ่งนี้จะวางแผนใหม่จากข้อมูลล่าสุด\n\n"
                "ปิดไฟล์ใน Excel ให้เรียบร้อยก่อนกดตกลง\n"
                "(ระบบสำรองไฟล์เดิมไว้ในโฟลเดอร์ backups ทุกครั้ง)")
            if not ok:
                return

        cmd = ([sys.executable, "-u", str(PROJECT / "main.py")]
               + self.dates.cli_args()
               + ["--path-metric", self.metric.get(),
                  "--speed-kmh", f"{speed:g}"])
        if self.dry.get():
            cmd.append("--dry-run")
        if self.nomap.get():
            cmd.append("--no-map")

        self.launch(cmd, "dry" if self.dry.get() else "real",
                    f"กำลังวางแผน {len(self.dates.work)} วันทำงาน ปกติใช้เวลา 3–5 นาที")

    def save_dry_run(self) -> None:
        """The save button: write the last trial run to Excel without solving again."""
        if self.proc is not None:
            return
        ok = messagebox.askokcancel(
            "บันทึกผลทดลองคำนวณลง Excel",
            "แผนจากการทดลองคำนวณล่าสุดจะถูกเขียนลงหน้า Daily Route และประวัติลงหน้า "
            "Working History ในไฟล์ workforce.xlsx โดยไม่คำนวณใหม่\n\n"
            "ถ้าไฟล์ถูกแก้ไขหลังทดลองคำนวณ ระบบจะไม่บันทึก และให้ทดลองคำนวณใหม่\n\n"
            "ปิดไฟล์ใน Excel ให้เรียบร้อยก่อนกดตกลง\n"
            "(ระบบสำรองไฟล์เดิมไว้ในโฟลเดอร์ backups ทุกครั้ง)")
        if not ok:
            return
        cmd = [sys.executable, "-u", str(PROJECT / "main.py"), "--write-saved"]
        if self.nomap.get():
            cmd.append("--no-map")
        self.launch(cmd, "save", "กำลังบันทึกผลทดลองคำนวณลง Excel")

    def launch(self, cmd: list[str], kind: str, status: str) -> None:
        """Start main.py as a separate process and show its output line by line."""
        self.kind = kind
        self.changed = False
        self.log("$ " + " ".join(cmd[1:]) + "\n\n", clear=True)
        self.run_btn.configure(state="disabled")
        self.save_btn.configure(state="disabled")
        self.map.disarm()                       # the old map is not this plan's
        self.map_btn.configure(state="disabled", text="เปิดแผนที่")
        self.progress["value"] = 0
        self.set_stage(0, 3, 5, "กำลังเริ่ม")
        self.status.configure(text=status)

        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1",
                   DISPATCH_PROGRESS="1")
        self.proc = subprocess.Popen(
            cmd, cwd=str(PROJECT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1, env=env)
        threading.Thread(target=self.pump, daemon=True).start()
        self.root.after(80, self.drain)
        self.root.after(200, self.tick)

    def pump(self) -> None:
        assert self.proc and self.proc.stdout
        for line in self.proc.stdout:
            self.lines.put(line)
        self.lines.put(None)

    def drain(self) -> None:
        finished = False
        while True:
            try:
                line = self.lines.get_nowait()
            except queue.Empty:
                break
            if line is None:
                finished = True
                break
            marker = parse_progress(line)
            if marker is not None:
                self.set_stage(*marker)
                continue
            self.log(line)
        if finished:
            code = self.proc.wait() if self.proc else 1
            self.proc = None
            self.run_btn.configure(state="normal")
            if code == 0:
                self.progress["value"] = 100
                self.stage_label.configure(text="เสร็จแล้ว")
                if self.kind == "dry":
                    if self.changed:          # days or settings changed during the run
                        where = ("คำนวณเสร็จแล้ว แต่มีการแก้วันหรือค่าตั้งระหว่างคำนวณ "
                                 "จึงบันทึกผลนี้ไม่ได้ — ทดลองคำนวณใหม่อีกครั้ง")
                    else:
                        where = ("คำนวณเสร็จแล้ว ยังไม่ได้บันทึกลง Excel — "
                                 "กด บันทึกผลลง Excel ได้โดยไม่ต้องคำนวณใหม่")
                        self.save_btn.configure(state="normal")
                elif self.kind == "save":
                    where = "บันทึกผลทดลองคำนวณลง sheet Daily Route เรียบร้อย"
                else:
                    where = (f"บันทึกแผนวันที่ {self.dates.horizon[0]:%d/%m} "
                             f"ลง sheet Daily Route เรียบร้อย")
                self.status.configure(
                    text=f"สำเร็จ — {where} · ดูแผนทุกวันในช่วงที่เลือกได้จากแผนที่")
                if self.nomap.get():
                    self.map_btn.configure(state="disabled")
                else:
                    self.map.arm(self.kind == "dry")
                    self.map_btn.configure(state="normal")
            else:
                self.stage_label.configure(text="ไม่สำเร็จ")
                self.status.configure(
                    text=f"ไม่สำเร็จ (รหัส {code}) — อ่านข้อความด้านล่าง "
                         f"หรือถ่ายภาพหน้าจอส่งให้ผู้ดูแลระบบ")
                # Excel busy or the save did not stick: the trial run is still kept, so allow a retry
                if self.kind == "save" and code in (2, 3) and not self.changed:
                    self.save_btn.configure(state="normal")
            return
        self.root.after(80, self.drain)

    # -- the map -----------------------------------------------------------

    def open_map(self) -> None:
        """The map button: serve the page on this computer and open the browser."""
        self.map_btn.configure(state="disabled")
        self.map.open()

    def map_message(self, kind: str, text: str) -> None:
        if kind == "busy":
            self.status.configure(text=text)
            return
        if kind == "ready":
            self.map_btn.configure(state="normal", text="เปิดแผนที่อีกครั้ง")
            self.status.configure(
                text=f"เปิดแผนที่แล้วที่ {text} · หน้าต่างนี้ต้องเปิดค้างไว้"
                     f"ระหว่างดูแผนที่")
            return
        self.map_btn.configure(state="normal" if self.map.ready else "disabled")
        self.status.configure(text=text)

    def close(self) -> None:
        self.map.stop()
        if self.proc is not None:
            if not messagebox.askokcancel("ยังรันไม่เสร็จ",
                                          "ปิดหน้าต่างและหยุดการรันเลยไหม"):
                return
            self.proc.terminate()
        self.root.destroy()


def main() -> int:
    root = tk.Tk()
    try:
        ttk.Style().theme_use("vista" if os.name == "nt" else "aqua")
    except tk.TclError:
        pass
    App(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
