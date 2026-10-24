"""Progress markers for the planning window's loading bar.

Prints ``@@PROGRESS|<now>|<until>|<seconds>|<label>`` lines, only when
``DISPATCH_PROGRESS=1`` is set.
"""
from __future__ import annotations

import os
import sys

MARK = "@@PROGRESS"


def enabled() -> bool:
    return os.environ.get("DISPATCH_PROGRESS") == "1"


def report(now: float, label: str, until: float = 0, seconds: float = 0) -> None:
    """Print one marker, if anyone is listening."""
    if not enabled():
        return
    safe = str(label).replace("|", "/").replace("\n", " ")
    sys.stdout.write(
        f"{MARK}|{int(now)}|{int(until)}|{int(seconds)}|{safe}\n"
    )
    sys.stdout.flush()


def parse(line: str) -> tuple[int, int, int, str] | None:
    """(now, until, seconds, label) from a marker line, or None if it is not one.

    A malformed marker is treated as ordinary output rather than an error: a
    loading bar is not worth failing a plan over.
    """
    text = line.strip()
    if not text.startswith(MARK + "|"):
        return None
    parts = text.split("|", 4)
    if len(parts) != 5:
        return None
    try:
        now, until, seconds = int(parts[1]), int(parts[2]), int(parts[3])
    except ValueError:
        return None
    now = max(0, min(100, now))
    until = max(0, min(100, until))
    return now, until, max(0, seconds), parts[4]


def position(now: int, until: int, seconds: int, elapsed: float) -> float:
    """Where the bar should sit ``elapsed`` seconds into a stage (never past 95% of the way)."""
    if until <= now or seconds <= 0:
        return float(now)
    fraction = min(max(elapsed, 0.0) / seconds, 0.95)
    return now + (until - now) * fraction
