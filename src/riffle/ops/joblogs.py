"""What the scheduled jobs' logs say about their runs: when each began, how it ended, the steps
that failed, and how many runs each job started a day.

Each run writes a header (progress.LogTracker.header), `2026-10-19 01:26:10 UTC  riffle watch
manapool`; a line a step, `01:35:40  ! Mana Pool singles: …` for one that failed, its time UTC
and its date the header's; and since 0069 a closing line, `01:35:46  exited 1 after 9m 36s`. A
run without one was cut off, is still running, or ran before 0069: how it ended isn't known.
A line with no time (a summary, the error a run exits on, a traceback) belongs to the run above
it. Headers written before 2026-09-29 have no zone: they're the Mac's time.

A line's date is its header's, carried past each midnight: a line more than twelve hours earlier
than the one above it is the next day's. A run the Mac froze asleep for days goes on writing
after it wakes, and its lines are dated as if no day had passed: only a date on every line would
tell, and its failure still shows, since the run's exit says it.
"""

import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

HEADER = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})( UTC)?  \S")
TIMED = re.compile(r"^(\d{2}):(\d{2}):(\d{2})  (.*)$")
CLOSING = re.compile(r"^exited (-?\d+) after (.+)$")
ENOUGH = 0.9  # of the runs a job owes a day: fewer, and it fired too rarely that day
DAY = timedelta(days=1)
HALF_DAY = timedelta(hours=12)


@dataclass
class Run:
    started: datetime  # its header's time, UTC
    exit: int | None = None  # from its closing line; None without one
    took: str = ""  # the closing line's duration, as written: "12m 07s"
    last: str = ""  # its last line besides its header and closing line: why it failed, if it did
    failed: list[tuple[datetime, str]] = field(default_factory=list)  # each failed step: when, and what


def _header(found: re.Match[str]) -> datetime:
    at = datetime.fromisoformat(found[1])
    return at.replace(tzinfo=UTC) if found[2] else at.astimezone().astimezone(UTC)


def _on(before: datetime, found: re.Match[str]) -> datetime:
    """A line's time, on the day of the line before it; on the next day if it's more than twelve
    hours earlier, midnight having passed, where less is the clock set back."""
    at = before.replace(hour=int(found[1]), minute=int(found[2]), second=int(found[3]), microsecond=0)
    return at + DAY if before - at > HALF_DAY else at


def read(path: Path) -> list[Run]:
    """The runs in a job's log, oldest first; none when there's no log."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    runs: list[Run] = []
    at = datetime.min.replace(tzinfo=UTC)  # the time of the line before
    for line in text.splitlines():
        head = HEADER.match(line)
        if head:
            runs.append(Run(_header(head)))
            at = runs[-1].started
            continue
        if not runs or not line.strip():
            continue
        run, timed = runs[-1], TIMED.match(line)
        if timed:
            at = _on(at, timed)
            closing = CLOSING.match(timed[4])
            if closing:
                run.exit, run.took = int(closing[1]), closing[2]
                continue
            if timed[4].startswith("! "):
                run.failed.append((at, timed[4][2:]))
        run.last = line.strip()
    return runs


def failing(runs: list[Run]) -> tuple[Run, int] | None:
    """The newest run that says how it ended, if it failed, and how many such runs in a row,
    ending with it, failed. A run that doesn't say is passed over."""
    ended = [run for run in runs if run.exit is not None]
    n = 0
    for run in reversed(ended):
        if run.exit == 0:
            break
        n += 1
    return (ended[-1], n) if n else None


def failed_since(runs: list[Run], since: datetime) -> list[tuple[datetime, str]]:
    """Every failed step at or after since, oldest first."""
    return [(at, what) for run in runs for at, what in run.failed if at >= since]


def started(runs: list[Run]) -> Counter[str]:
    """How many runs started on each UTC day."""
    return Counter(run.started.date().isoformat() for run in runs)


def short_days(
    runs: list[Run], every: timedelta, today: date, since: date | None = None
) -> list[tuple[str, int, int]]:
    """Each whole UTC day, after the log's first and before today (and from since), on which a
    job run every `every` started fewer than ENOUGH of the runs it owes a day: the day, the runs
    started, and the runs owed."""
    if not runs:
        return []
    owed, count = DAY // every, started(runs)
    day = runs[0].started.date() + DAY
    if since and since > day:
        day = since
    out = []
    while day < today:
        n = count.get(day.isoformat(), 0)
        if n < ENOUGH * owed:
            out.append((day.isoformat(), n, owed))
        day += DAY
    return out


def nth(n: int) -> str:
    """1st, 2nd, 3rd, 4th, 11th, 12th, 13th, 21st."""
    last = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{last}"
