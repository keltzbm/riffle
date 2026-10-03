"""What Riffle has kept, at a glance: `riffle status`.

Each source's last days as a strip, one cell a day by the source's own clock, read from what
`riffle check` reads (riffle.ingest.checks), and MTGO's events by their dates: it asks nothing
of any source and keeps nothing. A cell's height is what was kept that day against the busiest
day the row shows, in the braille dots the progress bars fill bottom up: ⣀ ⣤ ⣶ ⣿. A day with
nothing kept, between days that had some, is ✗: a gap. A day with none after the row's last is
· (not yet: a source's day may not be published, and riffle check judges one that's late), and
the days before its first are blank.

Shape carries every meaning and color only repeats it, in the progress bars' purple: one color
for one kind of thing, as the vault's charts have it, so a strip reads in a log, without color,
and for every kind of color vision. Under the strips: the MTGO backlog by month, the scheduled
jobs, the store's size and the disk's free space, and whether Time Machine has a destination.
"""

import math
import os
import re
import shutil
import subprocess
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import typer

from riffle import progress, schedule, times, watching
from riffle.ingest import checks, mtgo
from riffle.runs import zstd

LEVELS = "⣀⣤⣶⣿"  # a day's share of the row's busiest day shown, in quarters
GAP, YET, BEFORE = "✗", "·", " "
PURPLE = progress.GRADIENT[1]  # #A855F7: the progress bars' middle stop
DAYS = 14  # a strip's days unless asked for more or fewer: two weeks
MONTHS_SHOWN = 3  # MTGO's owed months named before the rest are summed
FILES = {"day": "file"}  # GoatBots' and Scryfall's checks name a day's file a day; their rows count files
CAN_T_READ = (OSError, ValueError, KeyError, TypeError, zstd.ZstdError)


@dataclass
class Row:
    label: str
    unit: str = "list"  # what it counts
    days: Counter[str] = field(default_factory=Counter)  # ISO day -> kept that day, every day kept
    problems: int = 0  # riffle check's, for its source
    late: bool = False  # a list late by its own margin, as riffle check judges it
    unreadable: str = ""  # why its source couldn't be read


def source_rows(found: dict[str, Callable[[], checks.Report]] = checks.CHECKS) -> list[Row]:
    """A row for each source riffle check reads, or for each of a source's own rows (tcgcsv's
    prices and products), its problems and lateness on the first; a source whose check can't
    run, a row saying why."""
    out = []
    for name, check in found.items():
        try:
            rep = check()
        except CAN_T_READ as e:
            out.append(Row(name, unreadable=str(e) or type(e).__name__))
            continue
        for n, (row, days) in enumerate(sorted(rep.kept.items()) or [("", Counter[str]())]):
            first = n == 0
            label = f"{rep.source} {row}".strip()
            unit = "game" if row else FILES.get(rep.what, rep.what)  # tcgcsv's rows: each game's day
            out.append(Row(label, unit, days, len(rep.problems) if first else 0, rep.late and first))
    return out


def mtgo_row(kept: Callable[[], Counter[str]] = mtgo.kept_days) -> Row:
    """MTGO's events, by the dates in their names."""
    try:
        return Row("MTGO events", "event", kept())
    except OSError as e:
        return Row("MTGO events", "event", unreadable=str(e))


def window(end: date, n: int) -> list[date]:
    """The n days to end, oldest first."""
    return [end - timedelta(days=n - 1 - k) for k in range(n)]


def cells(row: Row, days: list[date]) -> list[str]:
    """A row's mark for each day: a level of LEVELS when something was kept, GAP for none
    between days kept, YET for none after its last, BEFORE before its first."""
    kept = sorted(day for day, n in row.days.items() if n)
    most = max((row.days.get(d.isoformat(), 0) for d in days), default=0)
    out = []
    for d in days:
        day, n = d.isoformat(), row.days.get(d.isoformat(), 0)
        if n:
            out.append(LEVELS[math.ceil(len(LEVELS) * n / most) - 1])
        elif not kept or day < kept[0]:
            out.append(BEFORE)
        elif day > kept[-1]:
            out.append(YET)
        else:
            out.append(GAP)
    return out


def _mark(cell: str) -> str:
    """A cell as printed: color repeats what its shape says."""
    if cell in LEVELS:
        return typer.style(cell, fg=PURPLE)
    return typer.style(cell, bold=True) if cell == GAP else cell


def strips(rows: list[Row], days: list[date]) -> list[str]:
    """The month and day lines, a line for each row, and the legend."""
    width = max(len(row.label) for row in rows) + 2
    months = [" "] * (3 * len(days))
    for k, d in enumerate(days):
        if k == 0 or d.day == 1:
            name = d.strftime("%b")
            months[3 * k : 3 * k + len(name)] = name
    out = [" " * width + "".join(months).rstrip(), " " * width + "".join(f"{d.day:>2} " for d in days)]
    for row in rows:
        if row.unreadable:
            out.append(f"{row.label:<{width}}unreadable ({row.unreadable})")
            continue
        shown = sum(row.days.get(d.isoformat(), 0) for d in days)
        said = [f"{shown:,} {row.unit}{'s' * (shown != 1)}"]
        said += [f"{row.problems:,} wrong (riffle check)"] if row.problems else []
        said += ["late (riffle check)"] if row.late else []
        strip = "".join(f" {_mark(c)} " for c in cells(row, days))
        out.append(f"{row.label:<{width}}{strip}  {'; '.join(said)}")
    out.append(
        f"  {' '.join(_mark(c) for c in LEVELS)}  kept that day, against the busiest day its row shows"
    )
    out.append(
        f"  {_mark(GAP)}  none kept, between days that were   {YET}  none yet   blank: before the first"
    )
    return out


def owed_lines(status: Callable[[], mtgo.TrickleStatus] = mtgo.status) -> list[str]:
    """MTGO's owed events by the month they're in, newest first; and how far back its indexes
    are read."""
    try:
        st = status()
    except OSError as e:
        return [f"unknown ({e})"]
    months = sorted(Counter(o.day[:7] for o in st.owed.values()).items(), reverse=True)
    named = [f"{month} {n:,}" for month, n in months[:MONTHS_SHOWN]]
    if len(months) > MONTHS_SHOWN:
        named.append(f"older {sum(n for _, n in months[MONTHS_SHOWN:]):,}")
    said = [f"{len(st.owed):,} events owed" + (f": {', '.join(named)}" if named else "")]
    return said + ([f"indexes read back to {min(st.months)}"] if st.months else [])


def _name(job: schedule.Job) -> str:
    return job.label.removeprefix(f"{schedule.PREFIX}-")


def jobs_line(
    status: Callable[[schedule.Job], schedule.Status] = lambda job: schedule.status(job=job),
) -> str:
    """Each scheduled job: whether it's loaded, and any whose last run didn't exit 0."""
    every = [schedule.SYNC, schedule.TRICKLE, *(schedule.watch_job(store) for store in watching.STORES)]
    try:
        found = [(job, status(job)) for job in every]
    except OSError as e:
        return f"unknown ({e})"
    installed = [(job, st) for job, st in found if st.installed or st.loaded]
    if not installed:
        return "none installed (riffle schedule set, riffle schedule trickle, riffle schedule watch)"
    said = [f"{sum(st.loaded for _, st in installed)} of {len(installed)} loaded"]
    said += [f"{_name(job)} isn't" for job, st in installed if not st.loaded]
    failed = [
        f"{_name(job)}'s last run exited {st.last_exit}"
        for job, st in installed
        if st.loaded and st.last_exit not in (None, "0") and not str(st.last_exit).startswith("(")
    ]
    return "; ".join(said + (failed or ["every last run exited 0"]))


def _size(n: float) -> str:
    """Bytes to read at a glance, in GB past a gigabyte."""
    return f"{n / 1e9:,.1f} GB" if n >= 1e9 else watching.size(int(n))


def store_line(root: Path, usage: Callable[[Path], Any] = shutil.disk_usage) -> str:
    """The store's size, and the free space on its disk."""
    if not root.exists():
        return f"nothing kept yet in {root}"
    try:
        size = sum(
            (Path(folder) / name).lstat().st_size for folder, _, names in os.walk(root) for name in names
        )
        free = usage(root).free
    except OSError as e:
        return f"unknown ({e})"
    return f"{_size(size)} in {root}; {_size(free)} free"


def backup_line(run: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> str:
    """Whether Time Machine has a destination: the store's only backup until store-backup."""
    try:
        got = run(["tmutil", "destinationinfo"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return "unknown: no Time Machine here to ask"
    if "No destinations configured" in got.stdout + got.stderr:
        return "none: Time Machine has no destination, so the store is on one disk"
    names = re.findall(r"^Name\s*:\s*(.+?)\s*$", got.stdout, re.MULTILINE)
    return f"Time Machine to {', '.join(names)}" if names else "unknown: tmutil said nothing it could read"


def report(now: datetime, n: int, root: Path) -> tuple[list[str], bool]:
    """riffle status's lines, and whether every source could be read."""
    rows = [*source_rows(), mtgo_row()]
    days = window(now.date(), n)
    lines = [f"riffle status · {times.shown(now)} · the last {n} days, each source by its own clock", ""]
    lines += strips(rows, days)
    width = max(len(row.label) for row in rows) + 2
    lines.append("")
    for label, said in (
        ("MTGO", owed_lines()),
        ("jobs", [jobs_line()]),
        ("store", [store_line(root)]),
        ("backup", [backup_line()]),
    ):
        lines += [f"{label if n == 0 else '':<{width}}{line}" for n, line in enumerate(said)]
    return lines, not any(row.unreadable for row in rows)
