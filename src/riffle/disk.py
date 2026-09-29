"""Free space where Riffle keeps its data, and how fast it's going.

Riffle keeps everything it fetches and deletes nothing, so its data folder only grows: about
1.4 GB a day once every Scryfall file is kept. Each online sync records the disk's free space
in <data_dir>/disk.jsonl, and warns when it's under WARN_BELOW, with the days left at the rate
free space fell over the last week.
"""

import json
import shutil
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from riffle import times
from riffle.config import data_dir
from riffle.progress import Tracker

WARN_BELOW = 50 * 10**9  # bytes
WEEK = timedelta(days=7)
DAY = timedelta(days=1)


def log_path() -> Path:
    return data_dir() / "disk.jsonl"


def _history() -> list[tuple[datetime, int]]:
    """Every free-space reading recorded, oldest first; a line that can't be read is skipped."""
    try:
        text = log_path().read_text(encoding="utf-8")
    except OSError:
        return []
    out = []
    for raw in text.splitlines():
        try:
            line = json.loads(raw)
            out.append((datetime.fromisoformat(line["at"]), int(line["free"])))
        except (ValueError, KeyError, TypeError):
            continue
    return out


def rate(history: list[tuple[datetime, int]], at: datetime, free: int) -> float | None:
    """Bytes a day free space fell since the oldest reading of the last week, if that's at
    least a day old and free space fell."""
    past = [(t, f) for t, f in history if at - WEEK <= t <= at - DAY]
    if not past:
        return None
    then, was = past[0]
    fell = (was - free) / ((at - then) / DAY)
    return fell if fell > 0 else None


def check(tracker: Tracker, usage: Callable[[Path], Any] = shutil.disk_usage) -> None:
    """Record the disk's free space, and warn on a step of its own when it's low. A disk that
    can't be read or written fails the step, never the sync."""
    at = times.now()
    history = _history()
    try:
        folder = data_dir()
        folder.mkdir(parents=True, exist_ok=True)
        free = int(usage(folder).free)
        with log_path().open("a", encoding="utf-8") as f:
            f.write(json.dumps({"at": at.isoformat(), "free": free}) + "\n")
    except OSError as e:
        tracker.step("disk").fail(f"free space unknown ({e})")
        return
    if free >= WARN_BELOW:
        return
    note = f"{free / 1e9:,.0f} GB free, under {WARN_BELOW / 1e9:,.0f} GB"
    fell = rate(history, at, free)
    if fell:
        note += f"; about {free / fell:,.0f} days left at {fell / 1e9:,.1f} GB a day"
    tracker.step("disk").warn(note)
