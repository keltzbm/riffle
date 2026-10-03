"""`riffle status`: each source's days as a strip, gaps marked, then the backlog, jobs, disk and backup."""

import re
import subprocess
from collections import Counter
from datetime import UTC, date, datetime

from typer.testing import CliRunner

from riffle import schedule, status, trickle
from riffle.cli import app
from riffle.config import data_dir
from riffle.ingest import checks, mtgo

D = [date(2026, 9, 28 + k) for k in range(3)] + [date(2026, 10, k) for k in range(1, 4)]
NOW = datetime(2026, 10, 3, 8, 30, tzinfo=UTC)


def days(**kept: int) -> Counter[str]:
    """kept by day of the window: days(d1=1) is one kept on D[1]."""
    return Counter({D[int(k[1:])].isoformat(): n for k, n in kept.items()})


def test_a_strip_marks_what_was_kept_the_gaps_not_yet_and_before():
    row = status.Row("Mana Pool", days=days(d1=1, d2=4, d4=2))
    assert status.cells(row, D) == [" ", "⣀", "⣿", "✗", "⣤", "·"]
    assert status.cells(status.Row("tcgcsv products"), D) == [" "] * 6  # nothing kept yet


def test_a_day_s_height_is_against_the_busiest_day_shown():
    row = status.Row("Card Kingdom", days=days(d0=8, d1=7, d2=5, d3=3, d4=1) + Counter({"2026-01-01": 99}))
    assert status.cells(row, D) == ["⣿", "⣿", "⣶", "⣤", "⣀", "·"]  # a day not shown sets nothing


def report(source: str, what: str = "list", problems: int = 0, late: bool = False, **rows: Counter[str]):
    def check() -> checks.Report:
        rep = checks.Report(source, what, problems=["x"] * problems, late=late)
        for row, kept in rows.items():
            rep.kept[row.replace("_", "")] = kept
        return rep

    return check


def test_a_row_for_each_source_riffle_check_reads_or_each_of_its_own():
    def broken() -> checks.Report:
        raise OSError("disk gone")

    found = status.source_rows(
        {
            "Mana Pool": report("Mana Pool", problems=2, _=days(d0=3)),
            "tcgcsv": report("tcgcsv", "day", late=True, prices=days(d0=91), products=days(d1=88)),
            "GoatBots": report("GoatBots", "day"),
            "Scryfall": broken,
        }
    )
    assert [(r.label, r.unit, r.problems, r.late, r.unreadable) for r in found] == [
        ("Mana Pool", "list", 2, False, ""),
        ("tcgcsv prices", "game", 0, True, ""),  # the source's problems and lateness on its first row
        ("tcgcsv products", "game", 0, False, ""),
        ("GoatBots", "file", 0, False, ""),  # nothing kept: a row all the same
        ("Scryfall", "list", 0, False, "disk gone"),
    ]
    assert found[1].days == days(d0=91)


def test_mtgo_s_row_counts_events_by_the_dates_in_their_names():
    store = mtgo.store_dir()
    (store / "2026" / "10").mkdir(parents=True)
    for slug in ("modern-league-2026-10-0111161", "pauper-league-2026-10-0111153"):
        (store / "2026" / "10" / f"{slug}.json").write_text("{}")
    (store / "vintage-league-2026-09-3011185.json").write_text("{}")  # kept by an earlier build
    (store / "2026" / "10" / "notes.json").write_text("{}")  # not an event's name
    row = status.mtgo_row()
    assert (row.label, row.unit, row.days) == (
        "MTGO events",
        "event",
        Counter({"2026-10-01": 2, "2026-09-30": 1}),
    )


def test_mtgo_s_row_says_why_its_events_can_t_be_read():
    def unreadable() -> Counter[str]:
        raise PermissionError("not allowed")

    assert status.mtgo_row(unreadable).unreadable == "not allowed"


def test_the_strips_with_their_months_counts_and_legend():
    rows = [
        status.Row("Card Kingdom", days=days(d0=2, d1=2, d5=1), problems=1, late=True),
        status.Row("MTGO events", "event", days(d5=1)),
        status.Row("Scryfall", unreadable="disk gone"),
    ]
    printed = status.strips(rows, D)
    assert "\x1b[38;2;168;85;247m⣿\x1b[0m" in printed[2] and "\x1b[1m✗\x1b[0m" in printed[2]  # #A855F7; bold
    assert [re.sub(r"\x1b\[[0-9;]*m", "", line) for line in printed] == [
        "              Sep      Oct",
        "              28 29 30  1  2  3 ",
        "Card Kingdom   ⣿  ⣿  ✗  ✗  ✗  ⣤   5 lists; 1 wrong (riffle check); late (riffle check)",
        "MTGO events                   ⣿   1 event",
        "Scryfall      unreadable (disk gone)",
        "  ⣀ ⣤ ⣶ ⣿  kept that day, against the busiest day its row shows",
        "  ✗  none kept, between days that were   ·  none yet   blank: before the first",
    ]


def owed(*days_owed: str) -> dict[str, trickle.Owed]:
    return {
        f"event-{n}": trickle.Owed(day=day, found="2026-10-03T00:00:00+00:00")
        for n, day in enumerate(days_owed)
    }


def trickling(owed_now: dict[str, trickle.Owed], months: dict[str, dict]):
    return lambda: mtgo.TrickleStatus(NOW, trickle.Pace(), None, [], [], owed_now, months, False)


def test_mtgo_s_backlog_by_month_newest_first():
    found = owed("2026-10-01", "2026-09-30", "2026-09-29", "2026-08-01", "2026-07-01", "2025-01-01")
    assert status.owed_lines(trickling(found, {"2020-10": {}, "2026-10": {}})) == [
        "6 events owed: 2026-10 1, 2026-09 2, 2026-08 1, older 2",
        "indexes read back to 2020-10",
    ]
    assert status.owed_lines(trickling({}, {})) == ["0 events owed"]


def test_mtgo_s_backlog_that_can_t_be_read_says_so():
    def unreadable() -> mtgo.TrickleStatus:
        raise OSError("no request log")

    assert status.owed_lines(unreadable) == ["unknown (no request log)"]


def jobs(**states: schedule.Status):
    """A status for each job, by its name: watch-tcgcsv, sync, mtgo; the rest not installed."""
    return lambda job: states.get(
        job.label.removeprefix(f"{schedule.PREFIX}-").replace("-", "_"), schedule.Status(False, False)
    )


def test_each_job_loaded_and_how_its_last_run_ended():
    ran = schedule.Status(True, True, last_exit="0")
    assert status.jobs_line(jobs(sync=ran, mtgo=ran)) == "2 of 2 loaded; every last run exited 0"
    found = status.jobs_line(
        jobs(
            sync=schedule.Status(True, True, last_exit="1"),
            mtgo=schedule.Status(True, False),
            watch_tcgcsv=schedule.Status(True, True, last_exit="(never exited)"),
        )
    )
    assert found == "2 of 3 loaded; mtgo isn't; sync's last run exited 1"
    assert status.jobs_line(jobs()) == (
        "none installed (riffle schedule set, riffle schedule trickle, riffle schedule watch)"
    )


def test_jobs_that_can_t_be_asked_about_say_so():
    def no_launchctl(job: schedule.Job) -> schedule.Status:
        raise FileNotFoundError("launchctl")

    assert status.jobs_line(no_launchctl) == "unknown (launchctl)"


class Usage:
    def __init__(self, free: int):
        self.free = free


def test_the_store_s_size_and_the_disk_s_free_space(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "one").write_bytes(b"x" * 1500)
    (tmp_path / "two").write_bytes(b"x" * 500)
    assert (
        status.store_line(tmp_path, lambda _: Usage(472_700_000_000)) == f"2 KB in {tmp_path}; 472.7 GB free"
    )


def test_a_store_that_can_t_be_measured_says_so(tmp_path):
    def gone(_):
        raise FileNotFoundError("no such folder")

    assert status.store_line(tmp_path, gone) == "unknown (no such folder)"


def tmutil(stdout: str = "", stderr: str = ""):
    return lambda args, **_: subprocess.CompletedProcess(args, 0, stdout, stderr)


def test_whether_time_machine_has_a_destination():
    none = tmutil(stderr="tmutil: No destinations configured.\n")
    assert status.backup_line(none) == "none: Time Machine has no destination, so the store is on one disk"
    one = tmutil(
        "====================================================\nName          : Backup Disk\nKind : Local\n"
    )
    assert status.backup_line(one) == "Time Machine to Backup Disk"
    assert status.backup_line(tmutil("something else")) == "unknown: tmutil said nothing it could read"

    def missing(args, **_):
        raise FileNotFoundError("tmutil")

    assert status.backup_line(missing) == "unknown: no Time Machine here to ask"


def quiet(monkeypatch):
    """The jobs and backup lines without asking this machine."""
    monkeypatch.setattr(status, "jobs_line", lambda: "9 of 9 loaded; every last run exited 0")
    monkeypatch.setattr(status, "backup_line", lambda: "Time Machine to Backup Disk")
    monkeypatch.setattr(status.times, "now", lambda: NOW)


def test_the_command_over_an_empty_store(monkeypatch):
    quiet(monkeypatch)
    result = CliRunner().invoke(app, ["status", "--days", "3"])
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert lines[0] == "riffle status · 2026-10-03 08:30 UTC · the last 3 days, each source by its own clock"
    assert lines[3] == "               1  2  3 "
    assert lines[4] == "Card Kingdom             0 lists"
    assert "MTGO events              0 events" in lines
    assert lines[-4:] == [
        "MTGO          0 events owed",
        "jobs          9 of 9 loaded; every last run exited 0",
        f"store         nothing kept yet in {data_dir()}",
        "backup        Time Machine to Backup Disk",
    ]


def test_a_source_that_can_t_be_read_fails_the_command(monkeypatch):
    quiet(monkeypatch)

    def broken() -> checks.Report:
        raise ValueError("not JSON")

    monkeypatch.setitem(checks.CHECKS, "Scryfall", broken)
    result = CliRunner().invoke(app, ["status"])
    assert result.exit_code == 1
    assert "Scryfall      unreadable (not JSON)" in result.output.splitlines()
    assert "the last 14 days" in result.output


def test_a_store_with_nothing_kept_yet(tmp_path):
    assert status.store_line(tmp_path / "riffle") == f"nothing kept yet in {tmp_path / 'riffle'}"
