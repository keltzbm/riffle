"""The progress displays: timestamped lines off a terminal, Rich's live display on one."""

import io
import itertools
import math
import sys
from datetime import datetime, timedelta, timezone

import pytest
from rich.console import Console
from rich.progress import Progress

from riffle import progress


@pytest.mark.parametrize(
    ("seconds", "text"),
    [
        (0.04, "0.0s"),
        (8.25, "8.2s"),
        (59.96, "1m 00s"),
        (114, "1m 54s"),
        (3599.6, "1h 00m"),
        (7500, "2h 05m"),
    ],
)
def test_elapsed(seconds, text):
    assert progress.elapsed(seconds) == text


def test_silent_tracker_accepts_everything():
    step = progress.SILENT.step("anything", total=3, unit="groups")
    step.update(1)
    step.ok("done")
    step.warn("look")
    step.fail("no")
    step.drop()


def test_log_lines_are_in_utc_and_warnings_and_failures_go_to_stderr():
    out, err = io.StringIO(), io.StringIO()
    ticks = itertools.count(0.0, 8.25)
    at = datetime(2026, 9, 25, 7, 0, 3, tzinfo=timezone(timedelta(hours=-6)))  # 13:00:03 UTC
    log = progress.LogTracker(out, err, clock=lambda: next(ticks), now=lambda: at)
    log.header("riffle sync")
    prices = log.step("Scryfall prices")
    prices.update(5, 10)
    prices.ok("kept 2026-09-24")
    log.step("card catalog").ok()
    log.step("Cardmarket pokemon").warn("empty since 2026-09-21")
    log.step("tcgcsv fab").fail("HTTP 503")
    log.step("quiet").drop()
    assert out.getvalue() == (
        "2026-09-25 13:00:03 UTC  riffle sync\n"
        "13:00:03  Scryfall prices: kept 2026-09-24 (8.2s)\n"
        "13:00:03  card catalog (8.2s)\n"
    )
    assert err.getvalue() == (
        "13:00:03  warning: Cardmarket pokemon: empty since 2026-09-21 (8.2s)\n"
        "13:00:03  ! tcgcsv fab: HTTP 503\n"
    )


def test_a_warning_isnt_a_failure(tracker):
    watched = progress.Watched(tracker)
    watched.step("Cardmarket pokemon").warn("empty since 2026-09-21")
    watched.step("tcgcsv fab").fail("HTTP 503")
    assert watched.failed == ["tcgcsv fab"]
    assert tracker.outcomes()["Cardmarket pokemon"] == ("warn", "empty since 2026-09-21")


def _task(total, done, unit):
    bar = Progress()
    bar.update(bar.add_task("x", total=total, unit=unit), completed=done)
    return bar.tasks[0]


@pytest.mark.parametrize(
    ("total", "done", "unit", "text"),
    [
        (105, 43, "groups", "43/105 groups"),
        (None, 0, "", ""),
        (10_000_000, 2_500_000, "bytes", "2.5/10.0 MB"),
        (None, 2_500_000, "bytes", "2.5 MB"),
    ],
)
def test_amount_shows_counts_or_bytes(total, done, unit, text):
    assert str(progress._amount(_task(total, done, unit))) == text


@pytest.mark.parametrize(
    ("fraction", "cells"),
    [
        (0, [0, 0, 0, 0]),
        (1 / 32, [1, 0, 0, 0]),  # one dot at a time
        (9 / 32, [8, 1, 0, 0]),  # a cell fills before the next begins
        (0.999, [8, 8, 8, 7]),  # full only when done
        (1, [8, 8, 8, 8]),
        (1.5, [8, 8, 8, 8]),
        (-0.5, [0, 0, 0, 0]),
    ],
)
def test_a_bar_fills_one_dot_at_a_time(fraction, cells):
    assert progress.dots(fraction, width=4) == cells


def test_the_gradient_runs_from_violet_to_orchid():
    colors = progress.gradient(5)
    assert (colors[0], colors[2], colors[4]) == progress.GRADIENT


def test_a_bar_glides_toward_its_real_count():
    assert progress.glide(0.0, 1.0, 0.0) == 0.0
    assert progress.glide(0.0, 1.0, progress.GLIDE) == pytest.approx(1 - math.exp(-1))
    twice = progress.glide(progress.glide(0.0, 1.0, 0.1), 1.0, 0.1)
    assert twice == pytest.approx(progress.glide(0.0, 1.0, 0.2))  # the pace doesn't depend on redraws
    assert progress.glide(0.9995, 1.0, 0.01) == 1.0  # it lands exactly
    assert progress.glide(0.8, 0.5, 5.0) == 0.5  # and glides back when a total grows


def test_the_spinner_sweeps_through_the_full_cells_then_rests():
    speed, rest = progress.SWEEP_SPEED, progress.SWEEP_REST
    cells = [progress.sweep((n + 0.5) / speed, 5) for n in range(5 + rest + 2)]
    assert cells == [0, 1, 2, 3, 4] + [None] * rest + [0, 1]
    assert progress.sweep(3.5 / speed, 0) is None  # nothing full yet


def test_the_snake_crawls_along_the_track():
    speed = progress.SNAKE_SPEED
    assert progress.snake(0.5 / speed, width=10) == [0] + [None] * 9  # its head coming in
    assert progress.snake(3.5 / speed, width=10)[:5] == [3, 2, 1, 0, None]
    assert progress.snake(12.5 / speed, width=10) == [None] * 5 + [7, 6, 5, 4, 3]  # its tail going out


def test_a_bar_is_its_dots_on_the_track_then_its_percentage():
    text = progress._bar(20.5 / progress.SWEEP_SPEED, 0.5)  # the spinner is resting
    assert text.plain == "⣿" * 15 + "⣀" * 15 + "  50%"
    assert [span.style.color.triplet for span in text.spans[:15]] == list(progress.gradient()[:15])
    assert {span.style.color.triplet for span in text.spans[15:30]} == {progress.TRACK}
    percentage = text.spans[-1].style
    assert percentage.bold and percentage.color.triplet == progress.gradient()[14]  # the leading edge's color


def test_a_spinner_sweeps_through_the_filled_part():
    text = progress._bar(3.5 / progress.SWEEP_SPEED, 0.5)
    assert text.plain[:3] == "⣿⣿⣿" and text.plain[3] in progress._SPIN
    assert text.spans[3].style.color.triplet == progress.ACCENT


def test_a_bar_with_no_total_shows_the_snake():
    text = progress._bar(3.5 / progress.SNAKE_SPEED, None)
    assert text.plain == "⣇⣧⣷⣿" + "⣀" * 26  # no percentage
    assert text.spans[3].style.color.triplet == progress.gradient()[3]  # the head, in full color


def _clocked(width=120):
    """A live display on a clock the test moves by hand."""
    buf, now = io.StringIO(), [100.0]
    live = progress.LiveTracker(Console(file=buf, force_terminal=True, width=width, color_system=None))

    def clock():
        return now[0]

    live.progress.get_time = clock
    return live, now, buf


def test_the_live_bar_glides_to_its_count():
    live, now, buf = _clocked()
    live.step("tcgcsv mtg", total=8, unit="groups").update(4)
    live.progress.make_tasks_table(live.progress.tasks)  # the first redraw starts from empty
    now[0] += 5.5
    live.console.print(live.progress.make_tasks_table(live.progress.tasks))
    line = buf.getvalue()
    assert "tcgcsv mtg" in line and "⣿" * 15 + "⣀" * 15 + "  50%" in line
    assert "4/8 groups" in line and "0:05" in line


def test_a_step_says_how_long_it_has_been_waiting():
    live, now, _ = _clocked()
    step = live.step("mtgo modern", total=10, unit="events")

    def times():
        return progress._times(live.progress.tasks[0]).plain

    now[0] += 2
    step.update(1)
    now[0] += 3
    step.update(2)
    assert times() == "0:05 · 0:24 left"
    now[0] += 11
    step.update(2)  # no progress: still waiting
    assert times() == "0:16 · waiting 0:11"
    step.update(3)
    assert "waiting" not in times()


def test_bars_narrow_so_a_line_never_wraps():
    assert [progress.bar_width(columns) for columns in (120, 106, 90, 80, 40)] == [30, 30, 14, 8, 8]
    live, now, buf = _clocked(width=80)
    download = live.step("scryfall bulk data", total=84_000_000, unit="bytes")
    events = live.step("mtgo modern", total=24, unit="events")
    for step_by, got, done in ((2.0, 10_000_000, 10), (3.0, 31_900_000, 17)):
        now[0] += step_by
        download.update(got)
        events.update(done)
    live.console.print(live.progress.make_tasks_table(live.progress.tasks))
    lines = buf.getvalue().splitlines()
    assert len(lines) == 2 and "MB/s" in lines[0] and "left" in lines[0]  # speed and time left still fit


def test_a_long_label_ends_in_an_ellipsis_instead_of_wrapping():
    live, _, buf = _clocked()
    live.step("tcgcsv riftbound-league-of-legends-trading-card-game", total=3, unit="groups").update(1)
    live.console.print(live.progress.make_tasks_table(live.progress.tasks))
    (line,) = buf.getvalue().splitlines()
    assert "tcgcsv riftbound-league…" in line and "1/3 groups" in line


def test_times_add_what_is_left_once_the_pace_is_known():
    now = [0.0]
    bar = Progress(get_time=lambda: now[0])
    task_id = bar.add_task("x", total=10)
    assert progress._times(bar.tasks[0]).plain == "0:00"
    now[0] = 2.0
    bar.update(task_id, completed=2)
    now[0] = 5.0
    bar.update(task_id, completed=5, moved=5.0)
    assert progress._times(bar.tasks[0]).plain == "0:05 · 0:05 left"


@pytest.mark.parametrize(("seconds", "text"), [(0, "0:00"), (65.9, "1:05"), (3723, "1:02:03")])
def test_clock(seconds, text):
    assert progress._clock(seconds) == text


def test_live_display_turns_finished_steps_into_lines():
    buf = io.StringIO()
    console = Console(file=buf, force_terminal=True, width=100, color_system=None)
    with progress.LiveTracker(console) as live:
        bulk = live.step("Scryfall bulk data", unit="bytes")
        bulk.update(5_000_000, 10_000_000)
        bulk.ok("148.2 MB, Scryfall 2026-09-24")
        live.step("tcgcsv fab", unit="groups").fail("HTTP 503")
        live.step("Cardmarket pokemon").warn("empty since 2026-09-21")
        live.step("Scryfall prices").drop()
        assert live.progress.tasks == []  # nothing left running
    text = buf.getvalue()
    assert "✔ Scryfall bulk data" in text and "148.2 MB, Scryfall 2026-09-24" in text
    assert "✘ tcgcsv fab" in text and "HTTP 503" in text
    assert "! Cardmarket pokemon" in text and "empty since 2026-09-21" in text


def test_a_finished_step_leaves_no_bar_for_later_lines_to_bring_back():
    """A line printed right after a step ended redrew the display as it last was, the finished
    step's bar included, and the next line printed around the display landed on that bar."""
    buf = io.StringIO()
    console = Console(file=buf, force_terminal=True, width=100, color_system=None)
    with progress.LiveTracker(console) as live:
        step = live.step("mtgo modern", total=16, unit="events")
        step.update(15)
        live.progress.refresh()  # drawn at 15 of 16
        step.fail("5 new, 2 not published yet, 9 empty")
        console.print("5 new events · 76 already stored")
    after = buf.getvalue().split("✘ mtgo modern", 1)[1]
    assert "15/16" not in after


def test_lines_the_cli_prints_during_the_steps_go_above_them(capsys):
    """typer.echo finds the terminal under Rich's stand-in for stdout, so a line it printed
    went around the display, onto its last line. The CLI's _echo goes through the stand-in."""
    from riffle.cli import _echo

    buf = io.StringIO()
    console = Console(file=buf, force_terminal=True, width=100, color_system=None)
    with progress.LiveTracker(console) as live:
        live.step("mtgo modern", total=16, unit="events").update(3)
        _echo("5 new events · 76 already stored")
        _echo("  ! modern-league-2026-09-2410983: HTTP 503", err=True)
    out = buf.getvalue()
    assert "5 new events · 76 already stored" in out and "! modern-league-2026-09-2410983: HTTP 503" in out
    assert capsys.readouterr() == ("", "")  # nothing went around the display


class _Terminal(io.StringIO):
    def isatty(self) -> bool:
        return True


def test_open_tracker_is_live_on_a_terminal(monkeypatch):
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setattr(sys, "stdout", _Terminal())
    with progress.open_tracker("riffle sync") as tracker:
        assert isinstance(tracker, progress.LiveTracker)


def test_open_tracker_writes_plain_lines_otherwise(capsys):
    with progress.open_tracker("riffle sync") as tracker:
        assert isinstance(tracker, progress.LogTracker)
        tracker.step("card catalog").ok("118,389 printings")
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].endswith(" UTC  riffle sync") and len(lines[0]) == len(
        "2026-09-25 07:00:03 UTC  riffle sync"
    )
    assert "  card catalog: 118,389 printings (" in lines[1]


# ---- contained ------------------------------------------------------------------------------


def test_an_error_fails_the_steps_still_running_and_goes_no_further(tracker):
    with progress.contained(tracker, "MTGJSON prices", lambda e: f"broke: {e}") as scope:
        scope.step("MTGJSON today").ok("kept")
        scope.step("MTGJSON 90 days", unit="bytes").update(10, 100)
        raise ZeroDivisionError("x")
    assert tracker.outcomes() == {"MTGJSON today": ("ok", "kept"), "MTGJSON 90 days": ("fail", "broke: x")}


def test_an_error_before_any_step_fails_one_named_by_the_label(tracker):
    with progress.contained(tracker, "GoatBots prices", str):
        raise OSError("disk full")
    assert tracker.outcomes() == {"GoatBots prices": ("fail", "disk full")}


def test_work_that_ends_well_adds_no_step(tracker):
    with progress.contained(tracker, "tcgcsv prices", str) as scope:
        scope.step("tcgcsv mtg").fail("HTTP 503")
        scope.step("tcgcsv fab").drop()
        scope.step("tcgcsv op").warn("empty")
        assert scope.running == []
    assert tracker.outcomes() == {
        "tcgcsv mtg": ("fail", "HTTP 503"),
        "tcgcsv fab": ("drop",),
        "tcgcsv op": ("warn", "empty"),
    }


def test_ctrl_c_still_stops_everything(tracker):
    with pytest.raises(KeyboardInterrupt), progress.contained(tracker, "x", str):
        raise KeyboardInterrupt
    assert tracker.outcomes() == {}
