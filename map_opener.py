"""The "เปิดแผนที่" button: serve the map on 127.0.0.1 and open it in the browser.

OpenStreetMap tiles need a Referer header, which a page opened from disk does not send.
"""
from __future__ import annotations

import queue
import re
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path

#: Which file each kind of run draws.
PUBLISHED_MAP = "route_map.html"
DRY_RUN_MAP = "route_map_dryrun.html"

_URL = re.compile(r"http://localhost:\d+/\S+")


def page_for(dry_run: bool) -> str:
    """The map file the run that just finished has drawn."""
    return DRY_RUN_MAP if dry_run else PUBLISHED_MAP


def url_in(line: str) -> str | None:
    """The address ``serve_map.py`` prints, or None for any other line."""
    found = _URL.search(line or "")
    return found.group(0) if found else None


class MapOpener:
    """Starts the map server on demand and reports back.

    ``on_message(kind, text)`` gets "busy", "ready" (text is the address) or
    "error" (text is the reason, in Thai).
    """

    def __init__(self, project: Path, schedule, on_message,
                 spawn=subprocess.Popen, browser=webbrowser.open) -> None:
        self.project = Path(project)
        self.schedule = schedule
        self.on_message = on_message
        self.spawn = spawn
        self.browser = browser
        self.page: str | None = None      # the map this run drew
        self.url: str | None = None       # where it is being served
        self.proc = None
        self.lines: queue.Queue = queue.Queue()

    # -- what the window tells it ----------------------------------------

    def arm(self, dry_run: bool) -> None:
        """A run has finished and drawn a map. Any older server is dropped."""
        self.stop()
        self.page = page_for(dry_run)

    def disarm(self) -> None:
        """No map to show -- a run has started, or it was told not to draw one."""
        self.stop()
        self.page = None

    @property
    def ready(self) -> bool:
        return self.page is not None

    def open(self) -> None:
        """The button. Opens the browser again if the server is already up."""
        if self.url:
            self.browser(self.url)
            self.on_message("ready", self.url)
            return
        if self.page is None:
            self.on_message("error", "ยังไม่มีแผนที่ ให้กดวางแผนก่อน")
            return
        path = self.project / "mapdata" / self.page
        if not path.exists():
            self.on_message(
                "error", f"ไม่พบไฟล์แผนที่ {self.page} ในโฟลเดอร์ mapdata")
            return
        self.on_message("busy", "กำลังเปิดแผนที่ ...")
        self.proc = self.spawn(
            [sys.executable, "-u", str(self.project / "tools" / "serve_map.py"),
             "--file", self.page, "--no-browser"],
            cwd=str(self.project), stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, encoding="utf-8",
            errors="replace", bufsize=1,
        )
        threading.Thread(target=self._read, daemon=True).start()
        self.schedule(150, self.poll)

    def stop(self) -> None:
        """Close the server, if one is running. Called when the window closes."""
        if self.proc is not None:
            try:
                self.proc.terminate()
            except Exception:                      # already gone
                pass
        self.proc, self.url = None, None
        self.lines = queue.Queue()

    # -- the two halves of waiting for the address -----------------------

    def _read(self) -> None:
        """Background: hand every line to the window's thread."""
        proc = self.proc
        if proc is None or proc.stdout is None:
            return
        for line in proc.stdout:
            self.lines.put(line)
        self.lines.put(None)

    def poll(self) -> None:
        """On the window's thread: look for the address, then open it."""
        while True:
            try:
                line = self.lines.get_nowait()
            except queue.Empty:
                break
            if line is None:
                if self.url is None:
                    self.on_message(
                        "error",
                        "เปิดแผนที่ไม่สำเร็จ ลองกดไฟล์ map.command แทน")
                    self.proc = None
                return
            found = url_in(line)
            if found and self.url is None:
                self.url = found
                self.browser(found)
                self.on_message("ready", found)
        self.schedule(150, self.poll)
