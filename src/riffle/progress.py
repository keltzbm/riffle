"""Progress for long-running work, shown while it happens.

Ingest code reports through a Tracker and never draws anything itself: it
opens a step, updates it with a count (or bytes) as work proceeds, and ends it
with ok(note), fail(why), or drop() when there's nothing worth recording.

The CLI picks the display with open_tracker(). On a terminal, Rich draws a line per
running step: a spinner, a bar of braille dots and its percentage (see _bar()), the
count or bytes and speed, and the time so far with how long is left, or how long it
has been waiting. A finished step turns into a permanent line, a green ✔ (or a red
✘) with its note and how long it took, printed in order with everything else the
command says. Anywhere else, notably the scheduled job's sync.log, nothing animates:
a dated line when the run starts, then a timestamped line as each step ends."""

import functools
import math
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import TYPE_CHECKING, Any, Protocol, TextIO

if TYPE_CHECKING:
    from rich.console import Console
    from rich.progress import Progress, Task, TaskID
    from rich.style import Style
    from rich.text import Text

LABEL_WIDTH = 20
BAR_WIDTH = 30  # cells, on a terminal 102 columns wide or more; see bar_width()
REFRESH = 20  # redraws a second
STALL = 10.0  # seconds without progress before a running step says how long it's been waiting


class Step(Protocol):
    def update(self, done: int, total: int | None = None) -> None: ...
    def ok(self, note: str = "") -> None: ...
    def fail(self, why: str) -> None: ...
    def drop(self) -> None: ...


class Tracker(Protocol):
    def step(self, label: str, total: int | None = None, unit: str = "") -> Step: ...


class _Silent:
    """Reports nowhere; the default for code called outside the CLI. Its own step."""

    def step(self, label: str, total: int | None = None, unit: str = "") -> "_Silent":
        return self

    def update(self, done: int, total: int | None = None) -> None:
        pass

    def ok(self, note: str = "") -> None:
        pass

    def fail(self, why: str) -> None:
        pass

    def drop(self) -> None:
        pass


SILENT: Tracker = _Silent()


class Watched:
    """Passes every step on to another tracker and remembers which ones failed, so a
    command can finish its work and still exit non-zero."""

    def __init__(self, inner: Tracker) -> None:
        self.inner = inner
        self.failed: list[str] = []

    def step(self, label: str, total: int | None = None, unit: str = "") -> "_WatchedStep":
        return _WatchedStep(self, label, self.inner.step(label, total, unit))


class _WatchedStep:
    def __init__(self, watched: Watched, label: str, inner: Step) -> None:
        self._watched, self._label, self._inner = watched, label, inner

    def update(self, done: int, total: int | None = None) -> None:
        self._inner.update(done, total)

    def ok(self, note: str = "") -> None:
        self._inner.ok(note)

    def fail(self, why: str) -> None:
        self._watched.failed.append(self._label)
        self._inner.fail(why)

    def drop(self) -> None:
        self._inner.drop()


def elapsed(seconds: float) -> str:
    """8.2s, 1m 54s, 2h 05m."""
    if seconds < 59.95:  # would round to 60.0s
        return f"{seconds:.1f}s"
    minutes, secs = divmod(round(seconds), 60)
    if minutes < 60:
        return f"{minutes}m {secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


# ---- plain lines, for logs and pipes ---------------------------------------------------


class LogTracker:
    """A timestamped line per finished step. Streams are looked up when written,
    so output lands wherever sys.stdout and sys.stderr point at the time."""

    def __init__(
        self,
        out: TextIO | None = None,
        err: TextIO | None = None,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = datetime.now,
    ) -> None:
        self._out, self._err, self.clock, self.now = out, err, clock, now

    def write(self, line: str, error: bool = False) -> None:
        stream = (self._err or sys.stderr) if error else (self._out or sys.stdout)
        print(line, file=stream, flush=True)

    def header(self, title: str) -> None:
        self.write(f"{self.now():%Y-%m-%d %H:%M:%S}  {title}")

    def step(self, label: str, total: int | None = None, unit: str = "") -> "_LogStep":
        return _LogStep(self, label)


class _LogStep:
    def __init__(self, tracker: LogTracker, label: str) -> None:
        self._tracker, self._label, self._start = tracker, label, tracker.clock()

    def update(self, done: int, total: int | None = None) -> None:
        pass

    def ok(self, note: str = "") -> None:
        took = elapsed(self._tracker.clock() - self._start)
        what = f"{self._label}: {note}" if note else self._label
        self._tracker.write(f"{self._tracker.now():%H:%M:%S}  {what} ({took})")

    def fail(self, why: str) -> None:
        self._tracker.write(f"{self._tracker.now():%H:%M:%S}  ! {self._label}: {why}", error=True)

    def drop(self) -> None:
        pass


# ---- the live display, for a terminal --------------------------------------------------
# A bar is a row of braille cells over a dotted track, each cell holding 8 dots. It fills
# one dot at a time, each cell's left column bottom up and then its right, in a violet to
# orchid gradient. What it shows glides toward the real count, and a spinner sweeps
# through the filled part, so the bar moves even between updates. A step with no total
# shows a snake crawling along the track instead. Nothing has a background color, and the
# track, count, and times read on light terminals as well as dark ones.

RGB = tuple[int, int, int]
GRADIENT: tuple[RGB, ...] = ((109, 40, 217), (168, 85, 247), (232, 121, 249))  # violet, purple, orchid
ACCENT: RGB = (217, 70, 239)  # fuchsia: the sweeping spinner, and a step that's waiting
TRACK: RGB = (140, 128, 175)  # the dotted track: a lavender gray that shows on light and dark
GLIDE = 0.15  # seconds for a bar to close about two thirds of the gap to its real count
SWEEP_SPEED = 12.0  # cells a second for the spinner sweeping through a bar
SWEEP_REST = 10  # cells' worth of time between sweeps
SPIN_SPEED = 16.0  # the sweeping spinner's frames a second
SNAKE_SPEED = 16.0  # cells a second for the snake on a bar with no total
_DOTS = "⠀⡀⡄⡆⡇⣇⣧⣷⣿"  # a cell with 0 to 8 dots: its left column bottom up, then its right
_RAIL = "⣀"  # an empty cell: a stretch of dotted track
_SPIN = "⣾⣽⣻⢿⡿⣟⣯⣷"  # the sweeping spinner: one dot missing, going around
_SNAKE = "⣿⣷⣧⣇⡇⡆⡄⡀"  # the snake, head to tail
_BESIDE = 72  # columns the rest of a download's line can take: spinner, label, percentage, amount, times


def _amount(task: "Task") -> "Text":
    """The count or bytes column: 43/105 groups, 12.3/148.0 MB  8.1 MB/s."""
    from rich.text import Text

    unit = task.fields.get("unit", "")
    if unit == "bytes":
        text = f"{task.completed / 1e6:,.1f}"
        if task.total:
            text += f"/{task.total / 1e6:,.1f}"
        text += " MB"
        if task.speed:
            text += f"  {task.speed / 1e6:,.1f} MB/s"
    elif task.total is not None:
        text = f"{task.completed:,.0f}/{task.total:,.0f} {unit}".rstrip()
    else:
        text = ""
    return Text(text)


def bar_width(columns: int) -> int:
    """How wide a bar can be on a terminal this many columns wide, leaving room for the
    rest of its line so the line never wraps: BAR_WIDTH cells, down to 8."""
    return max(8, min(BAR_WIDTH, columns - _BESIDE))


def _mix(a: RGB, b: RGB, t: float) -> RGB:
    """The color t of the way from a to b."""
    return (
        round(a[0] + (b[0] - a[0]) * t),
        round(a[1] + (b[1] - a[1]) * t),
        round(a[2] + (b[2] - a[2]) * t),
    )


@functools.cache
def gradient(width: int = BAR_WIDTH) -> tuple[RGB, ...]:
    """Each cell's color: GRADIENT's stops spread evenly from the first cell to the last."""
    spans = len(GRADIENT) - 1
    colors = []
    for i in range(width):
        x = i * spans / (width - 1) if width > 1 else 0.0
        k = min(int(x), spans - 1)
        colors.append(_mix(GRADIENT[k], GRADIENT[k + 1], x - k))
    return tuple(colors)


def dots(fraction: float, width: int = BAR_WIDTH) -> list[int]:
    """Each cell's dots, 0 to 8, for a bar `fraction` done: full cells, then the leading
    cell's dots, then empty track."""
    n = int(min(max(fraction, 0.0), 1.0) * width * 8)
    return [min(8, max(0, n - 8 * i)) for i in range(width)]


def glide(shown: float, target: float, seconds: float) -> float:
    """Where a bar showing `shown` should be `seconds` later: on its way to `target`,
    quick while the gap is wide and slowing as it closes."""
    shown += (target - shown) * (1 - math.exp(-seconds / GLIDE))
    return target if abs(target - shown) < 0.001 else shown


def sweep(seconds: float, full: int) -> int | None:
    """The full cell the sweeping spinner is on, `seconds` in, or None while it rests
    between sweeps or no cell is full yet."""
    cell = int(seconds * SWEEP_SPEED) % (full + SWEEP_REST)
    return cell if cell < full else None


def snake(seconds: float, width: int = BAR_WIDTH) -> list[int | None]:
    """For each cell, how far behind the snake's head it is, 0 for the head itself, or
    None where the snake isn't."""
    head = int(seconds * SNAKE_SPEED) % (width + len(_SNAKE))
    return [head - i if 0 <= head - i < len(_SNAKE) else None for i in range(width)]


@functools.lru_cache(maxsize=512)
def _ink(rgb: RGB, bold: bool = False) -> "Style":
    from rich.color import Color
    from rich.style import Style

    return Style(color=Color.from_rgb(*rgb), bold=bold)


def _bar(seconds: float, fraction: float | None, width: int = BAR_WIDTH) -> "Text":
    """A bar `fraction` done and its percentage, or with no fraction the snake, `seconds`
    into its animation."""
    from rich.text import Text

    colors, bar = gradient(width), Text()
    if fraction is None:
        for rgb, behind in zip(colors, snake(seconds, width), strict=True):
            if behind is None:
                bar.append(_RAIL, _ink(TRACK))
            else:
                bar.append(_SNAKE[behind], _ink(_mix(rgb, TRACK, behind / len(_SNAKE))))
        return bar
    cells = dots(fraction, width)
    spinner = sweep(seconds, sum(1 for n in cells if n == 8))
    for i, (rgb, n) in enumerate(zip(colors, cells, strict=True)):
        if i == spinner:
            bar.append(_SPIN[int(seconds * SPIN_SPEED) % len(_SPIN)], _ink(ACCENT))
        else:
            bar.append(_DOTS[n] if n else _RAIL, _ink(rgb if n else TRACK))
    edge = colors[max(sum(1 for n in cells if n) - 1, 0)]
    bar.append(f" {fraction:4.0%}", _ink(edge, bold=True))
    return bar


class _Glides:
    """Each running bar's shown fraction, gliding toward its real one on the step's
    clock, so the pace doesn't depend on how often the display redraws."""

    def __init__(self) -> None:
        self._shown: dict[int, tuple[float, float]] = {}

    def __call__(self, task: "Task") -> float:
        target = min(task.completed / task.total, 1.0) if task.total else 1.0
        now = task.get_time()
        shown, then = self._shown.get(task.id, (0.0, now))
        shown = glide(shown, target, now - then)
        self._shown[task.id] = (shown, now)
        return shown


def _clock(seconds: float) -> str:
    """0:42, 12:05, 1:02:03."""
    minutes, secs = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def _times(task: "Task") -> "Text":
    """The time so far, then about how long is left once the pace is known, or instead,
    once STALL seconds pass without progress, how long the step has been waiting."""
    from rich.text import Text

    now = task.get_time()
    text, idle = Text(_clock(task.elapsed or 0.0)), now - task.fields.get("moved", now)
    if idle >= STALL:
        text.append(f" · waiting {_clock(idle)}", style=_ink(ACCENT))
    elif (left := None if task.finished else task.time_remaining) is not None:
        text.append(f" · {_clock(left)} left", style="dim")
    return text


class LiveTracker:
    """Running steps redrawn in place; finished steps printed as permanent lines.

    Everything else printed while it's open (typer.echo included) appears above
    the running steps, because Rich redirects stdout and stderr meanwhile.
    """

    def __init__(self, console: "Console") -> None:
        from rich.progress import Progress as RichProgress
        from rich.progress import ProgressColumn, SpinnerColumn, TextColumn
        from rich.table import Column

        glides = _Glides()

        class Bar(ProgressColumn):
            def render(self, task: "Task") -> "Text":
                fraction = None if task.total is None else glides(task)
                return _bar(task.get_time(), fraction, bar_width(console.width))

        class Amount(ProgressColumn):
            def render(self, task: "Task") -> "Text":
                return _amount(task)

        class Times(ProgressColumn):
            def render(self, task: "Task") -> "Text":
                return _times(task)

        self.console = console
        self.progress: Progress = RichProgress(
            SpinnerColumn(style=_ink(GRADIENT[1])),
            TextColumn("{task.description}", markup=False, table_column=Column(width=LABEL_WIDTH)),
            Bar(),
            Amount(),
            Times(),
            console=console,
            refresh_per_second=REFRESH,
        )

    def __enter__(self) -> "LiveTracker":
        self.progress.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.progress.stop()

    def step(self, label: str, total: int | None = None, unit: str = "") -> "_LiveStep":
        task = self.progress.add_task(label, total=total, unit=unit, moved=self.progress.get_time())
        return _LiveStep(self, task, label)

    def record(self, mark: str, style: str, label: str, note: str, took: str | None) -> None:
        """A finished step's permanent line: mark, label, note, and the time at the right edge."""
        from rich.table import Table
        from rich.text import Text

        line = Table.grid(expand=True, padding=(0, 1))
        line.add_column(width=1)
        line.add_column(width=LABEL_WIDTH, no_wrap=True)
        line.add_column(ratio=1)
        line.add_column(justify="right")
        line.add_row(
            Text(mark, style=f"bold {style}"),
            Text(label, style="bold"),
            Text(note, style=style if style == "red" else ""),
            Text(took or "", style="dim"),
        )
        self.console.print(line)


class _LiveStep:
    def __init__(self, tracker: LiveTracker, task: "TaskID", label: str) -> None:
        self._tracker, self._task, self._label = tracker, task, label
        self._start, self._done = time.monotonic(), 0

    def update(self, done: int, total: int | None = None) -> None:
        progress = self._tracker.progress
        if done == self._done:
            progress.update(self._task, completed=done, total=total)
        else:
            self._done = done
            progress.update(self._task, completed=done, total=total, moved=progress.get_time())

    def _end(self) -> str:
        self._tracker.progress.remove_task(self._task)
        return elapsed(time.monotonic() - self._start)

    def ok(self, note: str = "") -> None:
        self._tracker.record("✔", "green", self._label, note, self._end())

    def fail(self, why: str) -> None:
        self._tracker.record("✘", "red", self._label, why, self._end())

    def drop(self) -> None:
        self._tracker.progress.remove_task(self._task)


# ---- choosing -----------------------------------------------------------------------------


@contextmanager
def open_tracker(title: str) -> Iterator[Tracker]:
    """The live display on a terminal, plain lines anywhere else. title names the run
    in the plain header (the scheduled job's log gets one line per run with the date)."""
    if sys.stdout.isatty():
        from rich.console import Console

        console = Console()
        if console.is_terminal and not console.is_dumb_terminal:
            with LiveTracker(console) as live:
                yield live
            return
    log = LogTracker()
    log.header(title)
    yield log
