"""Define scheduled jobs and shared scheduling types."""

import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from riffle.config import data_dir
from riffle.watch import cadence


@dataclass(frozen=True)
class Job:
    label: str
    args: tuple[str, ...]  # riffle's arguments
    log: str  # its output, in the data folder
    interval: int | None = None


PREFIX = "gg.riffletcg.riffle"
SYNC = Job(f"{PREFIX}-sync", ("sync",), "sync.log")
TRICKLE = Job(f"{PREFIX}-mtgo", ("mtgo", "trickle"), "mtgo-trickle.log", interval=600)
LABEL = SYNC.label
WATCH_EVERY = int(cadence.EVERY.total_seconds())
_TIME = re.compile(r"^(\d{1,2}):(\d{2})$")

Runner = Callable[[list[str]], subprocess.CompletedProcess]


def watch_job(store: str) -> Job:
    """The job that runs `riffle watch <store>`."""
    return Job(f"{PREFIX}-watch-{store}", ("watch", store), f"watch-{store}.log", interval=WATCH_EVERY)


def _run(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True)


def log_path(job: Job = SYNC) -> Path:
    return data_dir() / job.log


def parse_time(text: str) -> tuple[int, int]:
    """'07:00' or '7:00' -> (7, 0). 24-hour only; '7pm' and '25:00' are errors."""
    m = _TIME.match(text.strip())
    if not m:
        raise ValueError(f"'{text}' isn't a 24-hour HH:MM time (e.g. 07:00, 19:30)")
    hour, minute = int(m[1]), int(m[2])
    if hour > 23 or minute > 59:
        raise ValueError(f"'{text}' is out of range — hours 00–23, minutes 00–59")
    return hour, minute


def parse_times(texts: list[str]) -> list[tuple[int, int]]:
    """Validated, deduplicated, sorted."""
    if not texts:
        raise ValueError("give at least one time, e.g. 07:00")
    return sorted({parse_time(t) for t in texts})


def fmt(t: tuple[int, int]) -> str:
    return f"{t[0]:02d}:{t[1]:02d}"


def next_run(times: list[tuple[int, int]], now: datetime) -> datetime | None:
    """The next wall-clock time any of `times` comes round, after `now`."""
    candidates = []
    for h, m in times:
        t = now.replace(hour=h, minute=m, second=0, microsecond=0)
        candidates.append(t if t > now else t + timedelta(days=1))
    return min(candidates, default=None)


@dataclass
class Status:
    installed: bool
    loaded: bool
    times: list[tuple[int, int]] = field(default_factory=list)
    runs: str | None = None
    last_exit: str | None = None
    state: str | None = None
    program: str | None = None
