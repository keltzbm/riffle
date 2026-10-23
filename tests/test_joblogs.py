"""The job logs read back: each run's header and closing line, its failed steps, how many runs in
a row failed, and the days a job started too few runs."""

from collections import Counter
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

import pytest

from riffle import joblogs

POSTGRES = (
    "can't reach Postgres at postgresql+psycopg://tcg@localhost:5432/tcg: connection refused"
    " — start it with: riffle db up"
)
MANAPOOL = (
    "2026-10-19 01:26:10 UTC  riffle watch manapool\n"
    "01:35:40  ! Mana Pool singles: download interrupted after 25,615 bytes (The read operation timed out)\n"
    "01:35:46  ! Mana Pool variants: no answer after 3 tries ([Errno 8] nodename nor servname provided,"
    " or not known)\n"
    "01:35:46  ! Mana Pool sealed: not asked: Mana Pool gave no answer\n"
    "3 steps failed: Mana Pool singles, Mana Pool variants, Mana Pool sealed\n"
    "01:35:46  exited 1 after 9m 36s\n"
    "2026-10-19 01:40:02 UTC  riffle watch manapool\n"
    "01:40:11  exited 0 after 9.1s\n"
)


def write(folder: Path, text: str) -> Path:
    path = folder / "sync.log"
    path.write_text(text)
    return path


def sync_run(k: int, code: int | None = 1) -> str:
    """Run k of the sync's twice-daily runs from 2026-10-13 13:02:03 UTC, as its log has it: the
    header, the Postgres error unless it exited 0, and the closing line unless code is None."""
    started = datetime(2026, 10, 13, 13, 2, 3, tzinfo=UTC) + timedelta(days=k // 2, hours=9 * (k % 2))
    lines = [f"{started:%Y-%m-%d %H:%M:%S} UTC  riffle sync"]
    if code != 0:
        lines.append(POSTGRES)
    if code is not None:
        lines.append(f"{started + timedelta(seconds=1):%H:%M:%S}  exited {code} after 1.2s")
    return "".join(f"{line}\n" for line in lines)


def at(day: date, *parts: int) -> datetime:
    return datetime.combine(day, time(*parts), UTC)


def runs_on(counts: dict[date, int], every: timedelta = timedelta(minutes=5)) -> list[joblogs.Run]:
    """A run every `every` from midnight UTC, as many a day as counts says."""
    return [joblogs.Run(at(day) + k * every) for day, n in counts.items() for k in range(n)]


# ---- reading a log ----------------------------------------------------------------------------


def test_read_gives_each_run_its_start_exit_duration_last_line_and_failed_steps(tmp_path):
    first = datetime(2026, 10, 19, 1, 26, 10, tzinfo=UTC)
    assert joblogs.read(write(tmp_path, MANAPOOL)) == [
        joblogs.Run(
            first,
            exit=1,
            took="9m 36s",
            last="3 steps failed: Mana Pool singles, Mana Pool variants, Mana Pool sealed",
            failed=[
                (
                    first.replace(hour=1, minute=35, second=40),
                    "Mana Pool singles: download interrupted after 25,615 bytes"
                    " (The read operation timed out)",
                ),
                (
                    first.replace(hour=1, minute=35, second=46),
                    "Mana Pool variants: no answer after 3 tries ([Errno 8] nodename nor servname provided,"
                    " or not known)",
                ),
                (
                    first.replace(hour=1, minute=35, second=46),
                    "Mana Pool sealed: not asked: Mana Pool gave no answer",
                ),
            ],
        ),
        joblogs.Run(datetime(2026, 10, 19, 1, 40, 2, tzinfo=UTC), exit=0, took="9.1s"),
    ]


def test_a_run_that_exits_on_an_error_keeps_the_error_as_its_last_line(tmp_path):
    (run,) = joblogs.read(write(tmp_path, sync_run(9)))
    assert (run.started, run.exit, run.took, run.last) == (
        datetime(2026, 10, 17, 22, 2, 3, tzinfo=UTC),
        1,
        "1.2s",
        POSTGRES,
    )


def test_a_run_with_no_closing_line_has_no_exit(tmp_path):
    (run,) = joblogs.read(write(tmp_path, sync_run(0, None)))
    assert (run.exit, run.took, run.last) == (None, "", POSTGRES)


def test_read_gives_no_runs_for_a_log_that_doesn_t_exist(tmp_path):
    assert joblogs.read(tmp_path / "watch-manapool.log") == []


def test_lines_before_the_first_header_are_skipped(tmp_path):
    stray = "Traceback (most recent call last):\n  File \"riffle\", line 1\nKeyError: 'x'\n"
    assert [run.started for run in joblogs.read(write(tmp_path, stray + sync_run(0, 0)))] == [
        datetime(2026, 10, 13, 13, 2, 3, tzinfo=UTC)
    ]


def test_a_log_that_can_t_be_read_raises_for_the_caller_to_say_so(tmp_path, unreadable):
    with pytest.raises(PermissionError):
        joblogs.read(unreadable(write(tmp_path, sync_run(0))))


def test_a_log_that_isn_t_utf_8_raises_for_the_caller_to_say_so(tmp_path):
    path = tmp_path / "sync.log"
    path.write_bytes(b"2026-10-13 13:02:03 UTC  riffle sync\n\xff\xfe\n")
    with pytest.raises(UnicodeDecodeError):
        joblogs.read(path)


def test_a_failed_step_after_midnight_is_dated_the_next_day(tmp_path):
    text = (
        "2026-10-16 23:58:10 UTC  riffle watch manapool\n"
        "23:59:50  Mana Pool singles: kept 2026-10-16T235803Z (8.2s)\n"
        "00:01:05  ! Mana Pool sealed: not asked: Mana Pool gave no answer\n"
        "1 step failed: Mana Pool sealed\n"
        "00:01:06  exited 1 after 2m 56s\n"
    )
    (run,) = joblogs.read(write(tmp_path, text))
    assert run.failed == [
        (datetime(2026, 10, 17, 0, 1, 5, tzinfo=UTC), "Mana Pool sealed: not asked: Mana Pool gave no answer")
    ]
    assert run.exit == 1


def test_a_line_a_second_earlier_than_the_one_above_is_the_clock_set_back_not_the_next_day(tmp_path):
    text = (
        "2026-10-19 13:02:03 UTC  riffle watch manapool\n"
        "13:02:10  Mana Pool singles: kept 2026-10-19T130103Z (6.1s)\n"
        "13:02:09  ! Mana Pool sealed: HTTP 503\n"
        "1 step failed: Mana Pool sealed\n"
        "13:02:11  exited 1 after 8.0s\n"
    )
    (run,) = joblogs.read(write(tmp_path, text))
    assert run.failed == [(datetime(2026, 10, 19, 13, 2, 9, tzinfo=UTC), "Mana Pool sealed: HTTP 503")]


def test_a_traceback_after_the_closing_line_is_the_run_s_last_line_and_leaves_its_exit(tmp_path):
    text = (
        "2026-10-19 13:02:03 UTC  riffle watch manapool\n"
        "13:02:05  exited 1 after 2.0s\n"
        "Traceback (most recent call last):\n"
        '  File "riffle/cli.py", line 402, in watch_cmd\n'
        "ValueError: boom\n"
    )
    (run,) = joblogs.read(write(tmp_path, text))
    assert (run.exit, run.last) == (1, "ValueError: boom")


def test_a_header_without_a_zone_is_the_mac_s_local_time(tmp_path, denver):
    (run,) = joblogs.read(write(tmp_path, "2026-09-26 07:04:08  riffle sync\n"))
    assert run.started == datetime(2026, 9, 26, 13, 4, 8, tzinfo=UTC)


# ---- judging the runs ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("codes", "expected"),
    [
        ([1] * 10, (9, 10)),
        ([1] * 10 + [0], None),
        ([1] * 10 + [None], (9, 10)),
        ([0, 1], (1, 1)),
        ([1], (0, 1)),
        ([None], None),
        ([], None),
    ],
)
def test_failing_is_the_newest_closed_run_that_exited_other_than_0_and_how_many_in_a_row(
    tmp_path, codes, expected
):
    runs = joblogs.read(write(tmp_path, "".join(sync_run(k, code) for k, code in enumerate(codes))))
    found = joblogs.failing(runs)
    assert found == (None if expected is None else (runs[expected[0]], expected[1]))


def test_failed_since_gives_every_failed_step_at_or_after_since_oldest_first():
    now = datetime(2026, 10, 19, 8, 30, tzinfo=UTC)
    since = now - timedelta(hours=24)
    runs = [
        joblogs.Run(
            now - timedelta(hours=30), exit=1, failed=[(now - timedelta(hours=30), "tcgcsv fab: HTTP 503")]
        ),
        joblogs.Run(since, exit=1, failed=[(since, "Mana Pool variants: HTTP 503")]),
        joblogs.Run(
            now - timedelta(hours=3),
            exit=1,
            failed=[
                (now - timedelta(hours=3), "Mana Pool singles: download interrupted"),
                (
                    now - timedelta(hours=3, seconds=-6),
                    "Mana Pool sealed: not asked: Mana Pool gave no answer",
                ),
            ],
        ),
        joblogs.Run(now - timedelta(hours=1), exit=0),
    ]
    assert joblogs.failed_since(runs, since) == [
        (since, "Mana Pool variants: HTTP 503"),
        (now - timedelta(hours=3), "Mana Pool singles: download interrupted"),
        (now - timedelta(hours=3, seconds=-6), "Mana Pool sealed: not asked: Mana Pool gave no answer"),
    ]


def test_started_counts_the_runs_on_each_utc_day():
    runs = [
        joblogs.Run(datetime(2026, 10, 16, 23, 58, tzinfo=UTC)),
        joblogs.Run(datetime(2026, 10, 17, 0, 3, tzinfo=UTC)),
        joblogs.Run(datetime(2026, 10, 17, 0, 8, tzinfo=UTC)),
    ]
    assert joblogs.started(runs) == Counter({"2026-10-16": 1, "2026-10-17": 2})
    assert joblogs.started([]) == Counter()


D13, D14, D15, D16, D17, D18 = (date(2026, 10, k) for k in range(13, 19))


@pytest.mark.parametrize(
    ("counts", "today", "since", "expected"),
    [
        ({D15: 3, D16: 30, D17: 288}, D18, None, [("2026-10-16", 30, 288)]),
        ({D15: 3, D16: 285, D17: 288}, D18, None, []),
        ({D14: 3, D16: 30}, D17, None, [("2026-10-15", 0, 288), ("2026-10-16", 30, 288)]),
        ({D15: 3, D16: 288, D17: 2}, D17, None, []),
        ({D13: 3, D14: 30, D15: 30, D16: 288}, D17, D15, [("2026-10-15", 30, 288)]),
        ({}, D18, None, []),
    ],
)
def test_short_days_are_the_whole_days_under_nine_tenths_of_the_runs_owed(counts, today, since, expected):
    assert joblogs.short_days(runs_on(counts), timedelta(minutes=5), today, since) == expected


def test_a_job_owes_a_day_as_many_runs_as_its_interval_fits_in_one():
    runs = runs_on({D15: 3, D16: 100}, timedelta(minutes=10))
    assert joblogs.short_days(runs, timedelta(minutes=10), D17) == [("2026-10-16", 100, 144)]
