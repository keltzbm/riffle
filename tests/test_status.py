"""`riffle status`: each source's days as a strip, gaps marked, then the backlog, jobs, disk and backup."""

import gzip
import json
import re
import subprocess
import sys
from collections import Counter
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from riffle import schedule, status, trickle
from riffle.cli import app
from riffle.config import config_path, data_dir
from riffle.ingest import checks, mtgo, pricelists

D = [date(2026, 9, 28 + k) for k in range(3)] + [date(2026, 10, k) for k in range(1, 4)]
NOW = datetime(2026, 10, 3, 8, 30, tzinfo=UTC)
GB = 10**9


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


def test_mtgo_s_row_counts_stored_events_by_the_dates_in_their_names():
    store = mtgo.store_dir()
    (store / "2026" / "10").mkdir(parents=True)
    stored = json.dumps({"decks": [{"player": "carol"}]})
    for slug in ("modern-league-2026-10-0111161", "pauper-league-2026-10-0111153"):
        (store / "2026" / "10" / f"{slug}.json").write_text(stored)
    (store / "vintage-league-2026-09-3011185.json").write_text(stored)  # kept by an earlier build
    (store / "2026" / "10" / "notes.json").write_text(stored)  # not an event's name
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
    assert status.jobs_lines(jobs(sync=ran, mtgo=ran)) == ["2 of 2 loaded; every last run exited 0"]
    found = status.jobs_lines(
        jobs(
            sync=schedule.Status(True, True, last_exit="1"),
            mtgo=schedule.Status(True, False),
            watch_tcgcsv=schedule.Status(True, True, last_exit="(never exited)"),
        )
    )
    assert found == ["2 of 3 loaded; mtgo isn't; sync's last run exited 1"]
    assert status.jobs_lines(jobs()) == [
        "none installed (riffle schedule set, riffle schedule trickle, riffle schedule watch)"
    ]


def test_jobs_that_can_t_be_asked_about_say_so():
    def no_launchctl(job: schedule.Job) -> schedule.Status:
        raise FileNotFoundError("launchctl")

    assert status.jobs_lines(no_launchctl) == ["unknown (launchctl)"]


# ---- what the job logs say ----------------------------------------------------------------------

RAN = schedule.Status(True, True, last_exit="0")
POSTGRES = (
    "can't reach Postgres at postgresql+psycopg://tcg@localhost:5432/tcg: connection refused"
    " — start it with: riffle db up"
)
MANAPOOL_FAILED = (
    "2026-10-02 02:08:10 UTC  riffle watch manapool\n"
    "02:17:40  ! Mana Pool variants: HTTP 503\n"
    "1 step failed: Mana Pool variants\n"
    "02:17:41  exited 1 after 9m 31s\n"
    "2026-10-03 05:08:10 UTC  riffle watch manapool\n"
    "05:17:40  ! Mana Pool singles: download interrupted after 25,615 bytes (The read operation timed out)\n"
    "05:17:46  ! Mana Pool sealed: not asked: Mana Pool gave no answer\n"
    "2 steps failed: Mana Pool singles, Mana Pool sealed\n"
    "05:17:46  exited 1 after 9m 36s\n"
)


def job_log(job: schedule.Job, text: str) -> Path:
    path = schedule.log_path(job)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def sync_run(k: int, code: int | None = 1) -> str:
    """Run k of the sync's twice-daily runs from 2026-09-23 13:02:03 UTC, as its log has it: the
    header, the Postgres error unless it exited 0, and the closing line unless code is None."""
    started = datetime(2026, 9, 23, 13, 2, 3, tzinfo=UTC) + timedelta(days=k // 2, hours=9 * (k % 2))
    lines = [f"{started:%Y-%m-%d %H:%M:%S} UTC  riffle sync"]
    if code != 0:
        lines.append(POSTGRES)
    if code is not None:
        lines.append(f"{started + timedelta(seconds=1):%H:%M:%S}  exited {code} after 1.2s")
    return "".join(f"{line}\n" for line in lines)


def runs_every(title: str, counts: dict[date, int], every: timedelta = timedelta(minutes=5)) -> str:
    """A job's log with a run every `every` from midnight UTC, as many a day as counts says, each
    exiting 0 at once."""
    out = []
    for day, n in counts.items():
        for k in range(n):
            started = datetime.combine(day, time(), UTC) + k * every
            out.append(f"{started:%Y-%m-%d %H:%M:%S} UTC  {title}\n{started:%H:%M:%S}  exited 0 after 0.4s\n")
    return "".join(out)


def test_the_sync_s_last_failed_run_is_named_until_a_run_passes(on_a_terminal):
    job_log(schedule.SYNC, "".join(sync_run(k) for k in range(10)))
    failing = schedule.Status(True, True, last_exit="1")
    assert status.jobs_lines(jobs(sync=failing), now=NOW) == [
        f"1 of 1 loaded; the sync's last run failed 2026-09-27 16:02 MDT, the 10th in a row: {POSTGRES}"
    ]
    job_log(schedule.SYNC, "".join(sync_run(k) for k in range(10)) + sync_run(10, None))
    # a run killed changes nothing
    assert "the 10th in a row" in status.jobs_lines(jobs(sync=failing), now=NOW)[0]
    job_log(schedule.SYNC, sync_run(9))
    assert status.jobs_lines(jobs(sync=failing), now=NOW) == [
        f"1 of 1 loaded; the sync's last run failed 2026-09-27 16:02 MDT: {POSTGRES}"
    ]
    job_log(schedule.SYNC, "".join(sync_run(k) for k in range(10)) + sync_run(10, 0))
    assert status.jobs_lines(jobs(sync=RAN), now=NOW) == ["1 of 1 loaded; every last run exited 0"]


def test_the_steps_that_failed_in_the_last_24_hours_are_counted_and_the_last_named():
    job_log(schedule.watch_job("manapool"), MANAPOOL_FAILED)
    job_log(
        schedule.watch_job("tcgcsv"),
        "2026-10-02 12:00:10 UTC  riffle watch tcgcsv\n12:05:00  ! tcgcsv fab: HTTP 503\n"
        "1 step failed: tcgcsv fab\n12:05:01  exited 1 after 4m 51s\n",
    )
    assert status.jobs_lines(jobs(watch_manapool=RAN, watch_tcgcsv=RAN), now=NOW) == [
        "2 of 2 loaded; every last run exited 0",
        "3 steps failed in the last 24 h, the last 2026-10-03 05:17 UTC in watch-manapool:"
        " Mana Pool sealed: not asked: Mana Pool gave no answer",
    ]
    job_log(schedule.watch_job("manapool"), MANAPOOL_FAILED.split("2026-10-03")[0])  # the newer run gone
    assert status.jobs_lines(jobs(watch_manapool=RAN), now=NOW) == ["1 of 1 loaded; every last run exited 0"]


S29, S30, O1, O2 = date(2026, 9, 29), date(2026, 9, 30), date(2026, 10, 1), date(2026, 10, 2)
INTERVAL_JOBS = {"mtgo": RAN, "watch_manapool": RAN, "watch_tcgcsv": RAN}


def test_a_whole_day_on_which_a_job_started_too_few_runs_is_named_with_the_fewest():
    job_log(schedule.watch_job("manapool"), runs_every("riffle watch manapool", {S30: 3, O1: 30, O2: 288}))
    job_log(schedule.watch_job("tcgcsv"), runs_every("riffle watch tcgcsv", {S30: 3, O1: 100, O2: 288}))
    assert status.jobs_lines(jobs(**INTERVAL_JOBS), now=NOW) == [
        "3 of 3 loaded; every last run exited 0",
        "2 of 3 jobs started too few runs on 2026-10-01, the fewest watch-manapool, 30 of 288",
    ]


def test_nine_tenths_of_the_runs_owed_is_enough():
    job_log(schedule.watch_job("manapool"), runs_every("riffle watch manapool", {S30: 3, O1: 285, O2: 288}))
    assert status.jobs_lines(jobs(**INTERVAL_JOBS), now=NOW) == ["3 of 3 loaded; every last run exited 0"]


def test_a_day_with_no_run_at_all_counts_as_0_and_each_short_day_is_named_oldest_first():
    job_log(schedule.watch_job("manapool"), runs_every("riffle watch manapool", {S29: 3, O1: 30, O2: 288}))
    assert status.jobs_lines(jobs(**INTERVAL_JOBS), now=NOW) == [
        "3 of 3 loaded; every last run exited 0",
        "1 of 3 jobs started too few runs on 2026-09-30, the fewest watch-manapool, 0 of 288",
        "1 of 3 jobs started too few runs on 2026-10-01, the fewest watch-manapool, 30 of 288",
    ]


def test_a_job_log_that_can_t_be_read_is_named_and_the_rest_still_said(unreadable):
    unreadable(job_log(schedule.watch_job("manapool"), MANAPOOL_FAILED))
    assert status.jobs_lines(jobs(watch_manapool=RAN, mtgo=schedule.Status(True, False)), now=NOW) == [
        "1 of 2 loaded; mtgo isn't; every last run exited 0",
        "watch-manapool.log: unreadable (Permission denied); failures in the last 24 h unknown",
    ]


class Usage:
    def __init__(self, free: int):
        self.free = free


def test_the_store_s_size_and_the_disk_s_free_space(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "one").write_bytes(b"x" * 1500)
    (tmp_path / "two").write_bytes(b"x" * 500)
    assert (
        status.store_line(tmp_path, lambda _: Usage(472_700_000_000))
        == f"2 KB in {tmp_path}; 472.7 GB free; days left: not enough readings yet"
    )


def test_a_store_that_can_t_be_measured_says_so(tmp_path):
    def gone(_):
        raise FileNotFoundError("no such folder")

    assert status.store_line(tmp_path, gone) == "unknown (no such folder)"


@pytest.mark.parametrize(
    ("history", "tail"),
    [
        (
            [(NOW - timedelta(days=7), 440 * GB), (NOW - timedelta(days=2), 340 * GB)],
            ", about 15 days at this week's rate",
        ),
        (
            [(NOW - timedelta(days=7), 486 * GB)],
            ", about 11 days at this week's rate (the sync warns under 14)",
        ),
        ([(NOW - timedelta(days=3), 280 * GB)], ", not falling this week"),
        ([(NOW - timedelta(days=7), 301 * GB)], ", over a year at this week's rate"),
        ([(NOW - timedelta(hours=1), 301 * GB)], "; days left: not enough readings yet"),
        ([], "; days left: not enough readings yet"),
    ],
)
def test_the_days_of_disk_left_at_this_week_s_rate(tmp_path, history, tail):
    (tmp_path / "one").write_bytes(b"x" * 2000)
    line = status.store_line(tmp_path, lambda _: Usage(300 * GB), history=lambda: history, now=lambda: NOW)
    assert line == f"2 KB in {tmp_path}; 300.0 GB free{tail}"


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
    assert status.backup_line(one, log=None) == "Time Machine to Backup Disk"  # no backup_log set


def never(args, **_):
    raise AssertionError("tmutil asked")


def backup_log(home: Path, monkeypatch, lines: str | None, failed: str | None = None) -> Path:
    """The backup job's log under the home folder, and backup.failed beside it when given."""
    monkeypatch.setenv("HOME", str(home))
    path = home / ".local" / "state" / "backup" / "backup.log"
    path.parent.mkdir(parents=True)
    if lines is not None:
        path.write_text(lines)
    if failed is not None:
        (path.parent / "backup.failed").write_text(failed)
    return path


def test_the_last_backup_and_check_that_went_well_from_the_backup_log(tmp_path, monkeypatch):
    log = backup_log(
        tmp_path,
        monkeypatch,
        "2026-09-28T08:33:10Z check ok\n2026-10-01T08:31:02Z backup ok\n2026-10-02T08:31:34Z backup ok\n",
    )
    assert status.backup_line(never, log=log) == (
        "last backup ok 2026-10-02 08:31 UTC; last check ok 2026-09-28 08:33 UTC"
        " (~/.local/state/backup/backup.log)"
    )
    log.write_text("2026-10-02T08:31:34Z backup ok\n")
    assert (
        status.backup_line(never, log=log)
        == "last backup ok 2026-10-02 08:31 UTC (~/.local/state/backup/backup.log)"
    )


@pytest.mark.parametrize(
    ("lines", "failed", "said"),
    [
        (
            "2026-10-11T08:31:00Z backup ok\n2026-10-12T21:40:12Z backup failed\n"
            "2026-10-13T21:40:30Z backup failed\n",
            "backup\n",
            "failed since 2026-10-12 21:40 UTC: backup",
        ),
        (
            "2026-10-12T21:40:12Z prune failed\n2026-10-13T21:40:30Z prune failed\n",
            "prune",
            "failed since 2026-10-12 21:40 UTC: prune",
        ),
        (
            "2026-10-11T08:31:00Z backup ok\n2026-10-12T21:40:12Z backup failed\n",
            "",
            "failed since 2026-10-12 21:40 UTC",
        ),
        ("2026-10-11T08:31:00Z backup ok\n", "backup\n", "failed: backup"),
    ],
)
def test_a_backup_failing_since_the_last_that_went_well(tmp_path, monkeypatch, lines, failed, said):
    log = backup_log(tmp_path, monkeypatch, lines, failed)
    assert status.backup_line(never, log=log) == f"{said} (~/.local/state/backup/backup.log)"


@pytest.mark.parametrize("lines", [None, "", "2026-10-12T21:40:12Z backup failed\n"])
def test_no_backup_found_without_a_log_or_a_backup_that_went_well(tmp_path, monkeypatch, lines):
    log = backup_log(tmp_path, monkeypatch, lines)
    assert status.backup_line(never, log=log) == "none found at ~/.local/state/backup/backup.log"


def test_a_backup_log_that_can_t_be_read_says_so(tmp_path, monkeypatch, unreadable):
    log = unreadable(backup_log(tmp_path, monkeypatch, "2026-10-02T08:31:34Z backup ok\n"))
    assert re.fullmatch(r"unknown \(.*Permission denied.*\)", status.backup_line(never, log=log))


def test_backup_log_lines_that_aren_t_a_step_s_are_passed_over(tmp_path, monkeypatch):
    log = backup_log(
        tmp_path,
        monkeypatch,
        "repository opened\n2026-13-45T08:31:34Z backup ok\n2026-10-02T08:31:34Z backup ok\n",
    )
    assert status.backup_line(never, log=log) == (
        "last backup ok 2026-10-02 08:31 UTC (~/.local/state/backup/backup.log)"
    )


def test_a_config_that_can_t_be_read_names_no_backup_log():
    config_path().write_text("backup_log = [\n")
    assert status._backup_log() is None


def quiet(monkeypatch):
    """The jobs and backup lines without asking this machine, the jobs' as on a Mac."""
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(status, "jobs_lines", lambda *_, **__: ["9 of 9 loaded; every last run exited 0"])
    monkeypatch.setattr(status, "backup_line", lambda *_, **__: "Time Machine to Backup Disk")
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


def kept_singles(day: str) -> None:
    """Card Kingdom's singles kept whole under a day: made at 13:08 Pacific, fetched 43 minutes on."""
    made = datetime.fromisoformat(f"{day}T13:08:38").replace(tzinfo=pricelists.PACIFIC)
    path = data_dir() / "cardkingdom" / "daily" / day / "singles.json.gz"
    path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps({"meta": {"created_at": f"{day} 13:08:38"}, "data": [{"id": 1}]}).encode()
    path.write_bytes(gzip.compress(body, mtime=int((made + timedelta(minutes=43)).timestamp())))


def test_the_command_says_what_each_source_the_jobs_and_the_store_hold(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(sys, "platform", "darwin")  # the jobs as launchd has them, on any runner
    monkeypatch.setattr(status.times, "now", lambda: NOW)
    monkeypatch.setattr(
        schedule, "status", lambda job=schedule.SYNC: schedule.Status(True, True, runs="3", last_exit="0")
    )
    measured = status.store_line
    monkeypatch.setattr(
        status, "store_line", lambda root, **kw: measured(root, usage=lambda _: Usage(300 * GB), **kw)
    )
    kept_singles("2026-10-01")
    kept_singles("2026-10-03")
    job_log(schedule.SYNC, sync_run(10, 0))
    job_log(schedule.watch_job("manapool"), MANAPOOL_FAILED)
    (data_dir() / "disk.jsonl").write_text(
        "".join(
            json.dumps({"at": at.isoformat(), "free": free}) + "\n"
            for at, free in ((NOW - timedelta(days=7), 440 * GB), (NOW - timedelta(days=2), 340 * GB))
        )
    )
    # a file of a known size, so the store's size is known: the logs and lists beside it come
    # to about 800 bytes
    (data_dir() / "pad").write_bytes(b"x" * 99_000)
    with config_path().open("a") as f:
        f.write('backup_log = "~/.local/state/backup/backup.log"\n')
    backup_log(tmp_path, monkeypatch, "2026-10-02T08:31:34Z backup ok\n")
    result = CliRunner().invoke(app, ["status", "--days", "3"])
    assert result.exit_code == 0, result.output
    lines = re.sub(r"\x1b\[[0-9;]*m", "", result.output).splitlines()
    assert "Card Kingdom   ⣿  ✗  ⣿   2 lists" in lines
    assert lines[-4:] == [
        "jobs          9 of 9 loaded; every last run exited 0",
        "              2 steps failed in the last 24 h, the last 2026-10-03 05:17 UTC in watch-manapool:"
        " Mana Pool sealed: not asked: Mana Pool gave no answer",
        f"store         100 KB in {data_dir()}; 300.0 GB free, about 15 days at this week's rate",
        "backup        last backup ok 2026-10-02 08:31 UTC (~/.local/state/backup/backup.log)",
    ]
