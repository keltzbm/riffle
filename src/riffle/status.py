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
and for every kind of color vision. Under the strips: the MTGO backlog by month; the scheduled
jobs, and what their logs say (riffle.joblogs): the sync failing run after run, the steps that
failed in the last 24 hours, the days a job started too few runs; the store's size, the disk's
free space and the days it leaves; and the newest backup, from the backup job's log when the
config names one (backup_log), else whether Time Machine has a destination.
"""

import math
import os
import re
import shutil
import subprocess
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import typer

from riffle import config, disk, joblogs, progress, schedule, times, watching
from riffle.ingest import checks, mtgo
from riffle.runs import zstd

LEVELS = "⣀⣤⣶⣿"  # a day's share of the row's busiest day shown, in quarters
GAP, YET, BEFORE = "✗", "·", " "
PURPLE = progress.GRADIENT[1]  # #A855F7: the progress bars' middle stop
DAYS = 14  # a strip's days unless asked for more or fewer: two weeks
MONTHS_SHOWN = 3  # MTGO's owed months named before the rest are summed
FILES = {"day": "file"}  # GoatBots' and Scryfall's checks name a day's file a day; their rows count files
CAN_T_READ = (OSError, ValueError, KeyError, TypeError, zstd.ZstdError)
BACKUP = re.compile(r"^(\S+) (\S+) (ok|failed)$")  # a backup job's log line: "<UTC time> <step> ok"
DAY = timedelta(days=1)
YEAR = 365  # days of disk left past which the count says nothing


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
    between days kept (the days riffle check names), YET for none after its last, BEFORE
    before its first."""
    kept = sorted(day for day, n in row.days.items() if n)
    gaps = set(checks.missing(row.days))
    most = max((row.days.get(d.isoformat(), 0) for d in days), default=0)
    out = []
    for d in days:
        day, n = d.isoformat(), row.days.get(d.isoformat(), 0)
        if n:
            out.append(LEVELS[math.ceil(len(LEVELS) * n / most) - 1])
        elif day in gaps:
            out.append(GAP)
        elif not kept or day < kept[0]:
            out.append(BEFORE)
        else:
            out.append(YET)
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


def installed_jobs(
    status: Callable[[schedule.Job], schedule.Status] = lambda job: schedule.status(job=job),
) -> list[tuple[schedule.Job, schedule.Status]]:
    """Each job installed or loaded, of the sync, the MTGO trickle and each store's watch, with
    what launchd says of it; OSError when launchctl can't be asked."""
    every = [schedule.SYNC, schedule.TRICKLE, *(schedule.watch_job(store) for store in watching.STORES)]
    found = [(job, status(job)) for job in every]
    return [(job, st) for job, st in found if st.installed or st.loaded]


def job_logs(
    jobs: list[schedule.Job], read: Callable[[Path], list[joblogs.Run]] = joblogs.read
) -> tuple[dict[schedule.Job, list[joblogs.Run]], list[str]]:
    """Each job's runs from its log, and what's said of each log that can't be read."""
    found, unread = {}, []
    for job in jobs:
        path = schedule.log_path(job)
        try:
            found[job] = read(path)
        except (OSError, UnicodeDecodeError) as e:
            unread.append(f"{path.name}: {unreadable(e)}")
    return found, unread


def unreadable(e: OSError | UnicodeDecodeError) -> str:
    """What's said of a job's log that can't be read."""
    why = (e.strerror if isinstance(e, OSError) else None) or str(e)
    return f"unreadable ({why}); failures in the last 24 h unknown"


def short_runs(
    logs: dict[schedule.Job, list[joblogs.Run]], today: date, since: date | None = None
) -> list[str]:
    """Each whole day on which jobs run every few minutes started too few runs (riffle.joblogs):
    how many of them did, and the one that started the fewest against what it owed. Most at once
    is the Mac asleep; one alone, its runs taking longer than its interval."""
    every = [job for job in logs if job.interval]
    short: dict[str, list[tuple[float, str, int, int]]] = {}
    for job in every:
        gap = timedelta(seconds=job.interval or 0)
        for day, n, owed in joblogs.short_days(logs[job], gap, today, since):
            short.setdefault(day, []).append((n / owed, _name(job), n, owed))
    out = []
    for day, found in sorted(short.items()):
        _v, name, n, owed = min(found)
        out.append(
            f"{len(found)} of {len(every)} jobs started too few runs on {day},"
            f" the fewest {name}, {n} of {owed}"
        )
    return out


def _sync_failing(runs: list[joblogs.Run]) -> str | None:
    """The sync's last run, said until a run exits 0, when it failed."""
    found = joblogs.failing(runs)
    if found is None:
        return None
    run, n = found
    said = f"the sync's last run failed {times.shown(run.started)}"
    said += f", the {joblogs.nth(n)} in a row" if n > 1 else ""
    return said + (f": {run.last}" if run.last else "")


def _steps_failed(logs: dict[schedule.Job, list[joblogs.Run]], now: datetime) -> list[str]:
    """How many steps failed in the last 24 hours, every job's counted, and the last."""
    failed = sorted(
        (
            (at, _name(job), what)
            for job, runs in logs.items()
            for at, what in joblogs.failed_since(runs, now - DAY)
        ),
        key=lambda found: found[0],
    )
    if not failed:
        return []
    at, name, what = failed[-1]
    n = len(failed)
    return [f"{n} step{'s' * (n != 1)} failed in the last 24 h, the last {times.shown(at)} in {name}: {what}"]


def jobs_lines(
    status: Callable[[schedule.Job], schedule.Status] = lambda job: schedule.status(job=job),
    read: Callable[[Path], list[joblogs.Run]] = joblogs.read,
    now: datetime | None = None,
    days: int = DAYS,
) -> list[str]:
    """Each scheduled job: whether it's loaded, and any whose last run didn't exit 0, by launchd,
    with the sync's failed run from its log until a run passes (launchd's count starts again at
    each reload, so its last exit can be 0 the morning after ten failures). Then a line each for
    the steps that failed in the last 24 hours, each day of the last `days` the jobs fired too
    rarely, and each log that can't be read."""
    try:
        installed = installed_jobs(status)
    except OSError as e:
        return [f"unknown ({e})"]
    if not installed:
        return ["none installed (riffle schedule set, riffle schedule trickle, riffle schedule watch)"]
    now = now or times.now()
    logs, unread = job_logs([job for job, _ in installed], read)
    sync = _sync_failing(logs.get(schedule.SYNC, []))
    said = [f"{sum(st.loaded for _, st in installed)} of {len(installed)} loaded"]
    said += [f"{_name(job)} isn't" for job, st in installed if not st.loaded]
    failed = [sync] if sync else []
    failed += [
        f"{_name(job)}'s last run exited {st.last_exit}"
        for job, st in installed
        if st.loaded
        and st.last_exit not in (None, "0")
        and not str(st.last_exit).startswith("(")
        and not (sync and job == schedule.SYNC)
    ]
    said += failed or ["every last run exited 0"]
    today = now.astimezone(UTC).date()
    return [
        "; ".join(said),
        *_steps_failed(logs, now),
        *short_runs(logs, today, today - timedelta(days=days - 1)),
        *unread,
    ]


def _size(n: float) -> str:
    """Bytes to read at a glance, in GB past a gigabyte."""
    return f"{n / 1e9:,.1f} GB" if n >= 1e9 else watching.size(int(n))


def store_line(
    root: Path,
    usage: Callable[[Path], Any] = shutil.disk_usage,
    history: Callable[[], list[tuple[datetime, int]]] = disk.history,
    now: Callable[[], datetime] = lambda: times.now(),
) -> str:
    """The store's size, the free space on its disk, and the days that leaves at this week's
    rate (riffle.disk): the disk's rate, not the store's, since anything on the Mac fills it."""
    if not root.exists():
        return f"nothing kept yet in {root}"
    try:
        size = sum(
            (Path(folder) / name).lstat().st_size for folder, _, names in os.walk(root) for name in names
        )
        free = usage(root).free
    except OSError as e:
        return f"unknown ({e})"
    said = f"{_size(size)} in {root}; {_size(free)} free"
    fell = disk.rate(history(), now(), free)
    if fell is None:
        return f"{said}; days left: not enough readings yet"
    if fell <= 0:
        return f"{said}, not falling this week"
    left = free / fell
    if left >= YEAR:
        return f"{said}, over a year at this week's rate"
    warned = f" (the sync warns under {disk.WARN_DAYS})" if left < disk.WARN_DAYS else ""
    return f"{said}, about {left:,.0f} days at this week's rate{warned}"


def _backups(lines: list[str]) -> list[tuple[datetime, str, str]]:
    """A backup job's log lines that can be read: when, the step, and ok or failed."""
    out = []
    for line in lines:
        found = BACKUP.match(line.strip())
        if not found:
            continue
        try:
            out.append((datetime.fromisoformat(found[1]), found[2], found[3]))
        except ValueError:
            continue
    return out


def backup_line(
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run, log: Path | None = None
) -> str:
    """When the store was last backed up, from the backup job's log when the config names one
    (backup_log): the newest backup that went well and the newest check, or since when it's
    failed while backup.failed stands beside the log. Without one, whether Time Machine has a
    destination."""
    if log is None:
        return _time_machine(run)
    try:
        lines = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
        failed = log.with_name("backup.failed")
        failing = failed.read_text(encoding="utf-8").strip() if failed.exists() else None
    except (OSError, UnicodeDecodeError) as e:
        return f"unknown ({e})"
    found, shown = _backups(lines), config.tilde(log)
    ok = [at for at, step, result in found if (step, result) == ("backup", "ok")]
    if failing is not None:
        since = [at for at, _, result in found if result == "failed" and (not ok or at > ok[-1])]
        said = f"failed since {times.shown(since[0])}" if since else "failed"
        return f"{said}{f': {failing}' if failing else ''} ({shown})"
    if not ok:
        return f"none found at {shown}"
    checked = [at for at, step, result in found if (step, result) == ("check", "ok")]
    said = f"last backup ok {times.shown(ok[-1])}"
    said += f"; last check ok {times.shown(checked[-1])}" if checked else ""
    return f"{said} ({shown})"


def _time_machine(run: Callable[..., subprocess.CompletedProcess]) -> str:
    """Whether Time Machine has a destination."""
    try:
        got = run(["tmutil", "destinationinfo"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return "unknown: no Time Machine here to ask"
    if "No destinations configured" in got.stdout + got.stderr:
        return "none: Time Machine has no destination, so the store is on one disk"
    names = re.findall(r"^Name\s*:\s*(.+?)\s*$", got.stdout, re.MULTILINE)
    return f"Time Machine to {', '.join(names)}" if names else "unknown: tmutil said nothing it could read"


def _backup_log() -> Path | None:
    """The backup job's log the config names; none if the config can't be read."""
    try:
        return config.load().backup_log
    except (OSError, ValueError):
        return None


def report(now: datetime, n: int, root: Path) -> tuple[list[str], bool]:
    """riffle status's lines, and whether every source could be read."""
    days = window(now.date(), n)
    rows = [*source_rows(), mtgo_row(lambda: mtgo.kept_days(since=days[0]))]
    lines = [f"riffle status · {times.shown(now)} · the last {n} days, each source by its own clock", ""]
    lines += strips(rows, days)
    width = max(len(row.label) for row in rows) + 2
    lines.append("")
    for label, said in (
        ("MTGO", owed_lines()),
        ("jobs", jobs_lines(now=now, days=n)),
        ("store", [store_line(root, now=lambda: now)]),
        ("backup", [backup_line(log=_backup_log())]),
    ):
        lines += [f"{label if n == 0 else '':<{width}}{line}" for n, line in enumerate(said)]
    return lines, not any(row.unreadable for row in rows)
