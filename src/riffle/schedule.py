"""The launchd jobs (macOS): `riffle sync` at set times each day, the MTGO trickle
every 10 minutes, and a watch for each store every 5 minutes.

A store's watch is its own job because launchd never starts a job while that job is
still running: it skips the firing. One job for every store would stop checking them
all while any one of them fetched.

One label per job: installing a job again replaces it, it never adds a second
one. Times are 24-hour HH:MM. Everything that touches launchd goes through `run`
so the logic is testable without it.
"""

import os
import plistlib
import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from riffle.config import data_dir


@dataclass(frozen=True)
class Job:
    label: str
    args: tuple[str, ...]  # riffle's arguments
    log: str  # its output, in the data folder
    interval: int | None = None  # seconds between runs; None: at set times of day


PREFIX = "com.keltzbm.riffle"  # every job's label starts with it
SYNC = Job(f"{PREFIX}-sync", ("sync",), "sync.log")
TRICKLE = Job(f"{PREFIX}-mtgo", ("mtgo", "trickle"), "mtgo-trickle.log", interval=600)
LABEL = SYNC.label
WATCH_EVERY = 300  # seconds: a Mana Pool list lasts about 30 minutes, so each gets about six tries
_TIME = re.compile(r"^(\d{1,2}):(\d{2})$")

Runner = Callable[[list[str]], subprocess.CompletedProcess]


def watch_job(store: str) -> Job:
    """The job that runs `riffle watch <store>`."""
    return Job(f"{PREFIX}-watch-{store}", ("watch", store), f"watch-{store}.log", interval=WATCH_EVERY)


def _run(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True)


def plist_path(job: Job = SYNC) -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{job.label}.plist"


def log_path(job: Job = SYNC) -> Path:
    return data_dir() / job.log


def _domain() -> str:
    return f"gui/{os.getuid()}"


# ---- times ---------------------------------------------------------------------


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


# ---- the plist -----------------------------------------------------------------


def build(times: list[tuple[int, int]], exe: Path, log: Path, job: Job = SYNC) -> dict:
    """launchd needs absolute paths: no ~, no PATH lookup."""
    when: dict = (
        {"StartInterval": job.interval}
        if job.interval
        else {"StartCalendarInterval": [{"Hour": h, "Minute": m} for h, m in times]}
    )
    return {
        "Label": job.label,
        "ProgramArguments": [str(exe), *job.args],
        **when,
        "StandardOutPath": str(log),
        "StandardErrorPath": str(log),
    }


def read_times(path: Path) -> list[tuple[int, int]]:
    """Times in an existing plist. Handles the one-dict form older versions wrote."""
    if not path.exists():
        return []
    data = plistlib.loads(path.read_bytes())
    sci = data.get("StartCalendarInterval") or []
    if isinstance(sci, dict):
        sci = [sci]
    return sorted((d.get("Hour", 0), d.get("Minute", 0)) for d in sci)


def parse_print(text: str) -> dict[str, str]:
    """The fields that matter from `launchctl print` — first occurrence of each."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        key, sep, value = line.strip().partition(" = ")
        if sep and key in {"state", "runs", "last exit code", "program"} and key not in out:
            out[key] = value.strip()
    return out


# ---- actions -------------------------------------------------------------------


@dataclass
class Status:
    installed: bool
    loaded: bool
    times: list[tuple[int, int]] = field(default_factory=list)
    runs: str | None = None
    last_exit: str | None = None
    state: str | None = None
    program: str | None = None


def status(run: Runner = _run, path: Path | None = None, job: Job = SYNC) -> Status:
    path = path or plist_path(job)
    result = run(["launchctl", "print", f"{_domain()}/{job.label}"])
    info = parse_print(result.stdout) if result.returncode == 0 else {}
    return Status(
        installed=path.exists(),
        loaded=result.returncode == 0,
        times=read_times(path),
        runs=info.get("runs"),
        last_exit=info.get("last exit code"),
        state=info.get("state"),
        program=info.get("program"),
    )


def install(
    times: list[tuple[int, int]],
    exe: Path | None = None,
    run: Runner = _run,
    path: Path | None = None,
    job: Job = SYNC,
) -> Path:
    """Write the plist and (re)load it. Replaces any existing job. An interval job
    takes no times."""
    path = path or plist_path(job)
    exe = exe or Path(sys.argv[0]).resolve()
    log = log_path(job)
    log.parent.mkdir(parents=True, exist_ok=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(plistlib.dumps(build(times, exe, log, job)))
    run(["launchctl", "bootout", f"{_domain()}/{job.label}"])  # fails harmlessly if not loaded
    result = run(["launchctl", "bootstrap", _domain(), str(path)])
    if result.returncode != 0:
        raise RuntimeError(f"wrote {path} but launchctl bootstrap failed: {result.stderr.strip()}")
    return path


def remove(run: Runner = _run, path: Path | None = None, job: Job = SYNC) -> bool:
    """Unload and delete. True if there was anything to remove."""
    path = path or plist_path(job)
    was_loaded = run(["launchctl", "bootout", f"{_domain()}/{job.label}"]).returncode == 0
    existed = path.exists()
    path.unlink(missing_ok=True)
    return was_loaded or existed
