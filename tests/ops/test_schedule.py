"""The launchd schedule, tested without launchd: a fake `launchctl` records calls."""

import plistlib
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from riffle import times
from riffle.ops import schedule as sched


@pytest.fixture(autouse=True)
def mac(monkeypatch):
    """launchd is macOS's, so the schedule commands run as on a Mac wherever the tests do."""
    monkeypatch.setattr(sys, "platform", "darwin")


class FakeLaunchctl:
    def __init__(self, loaded=False, bootstrap_rc=0, print_out=""):
        self.calls = []
        self.loaded = loaded
        self.bootstrap_rc = bootstrap_rc
        self.print_out = print_out

    def __call__(self, args):
        self.calls.append(args[1])
        verb = args[1]
        if verb == "bootout":
            rc, self.loaded = (0 if self.loaded else 3), False
            return subprocess.CompletedProcess(args, rc, "", "")
        if verb == "bootstrap":
            self.loaded = self.bootstrap_rc == 0
            err = "Bootstrap failed: 5: Input/output error"
            return subprocess.CompletedProcess(args, self.bootstrap_rc, "", err)
        if verb == "print":
            if self.loaded:
                return subprocess.CompletedProcess(args, 0, self.print_out, "")
            return subprocess.CompletedProcess(args, 113, "", "Could not find service")
        raise AssertionError(f"unexpected launchctl {verb}")


PRINT = """gui/501/gg.riffletcg.riffle-sync = {
	active count = 0
	path = /Users/pat/Library/LaunchAgents/gg.riffletcg.riffle-sync.plist
	state = not running
	program = /Users/pat/riffle/.venv/bin/riffle
	runs = 3
	last exit code = 0
	event triggers = {
		com.apple.launchd.calendarinterval = {
			state = active
		}
	}
}
"""


# ---- times ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "text, expected",
    [
        ("07:00", (7, 0)),
        ("7:00", (7, 0)),
        ("00:00", (0, 0)),
        ("23:59", (23, 59)),
        ("19:30", (19, 30)),
        (" 08:15 ", (8, 15)),
    ],
)
def test_parse_time_accepts_24_hour(text, expected):
    assert sched.parse_time(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "7pm",
        "7:00 PM",
        "24:00",
        "12:60",
        "25:00",
        "7",
        "07:5",
        "",
        "ab:cd",
        "-1:00",
    ],
)
def test_parse_time_rejects(text):
    with pytest.raises(ValueError):
        sched.parse_time(text)


def test_parse_times_sorts_and_dedupes():
    assert sched.parse_times(["19:30", "07:00", "7:00"]) == [(7, 0), (19, 30)]
    with pytest.raises(ValueError, match="at least one"):
        sched.parse_times([])


@pytest.mark.parametrize(
    "now, expected",
    [
        (datetime(2026, 9, 21, 6, 59), datetime(2026, 9, 21, 7, 0)),  # later today
        (datetime(2026, 9, 21, 7, 0), datetime(2026, 9, 21, 19, 30)),  # exactly at a time: that one's gone
        (datetime(2026, 9, 21, 20, 0), datetime(2026, 9, 22, 7, 0)),  # tomorrow
        (datetime(2026, 12, 31, 23, 0), datetime(2027, 1, 1, 7, 0)),  # across a year
    ],
)
def test_next_run(now, expected):
    assert sched.next_run([(7, 0), (19, 30)], now) == expected


def test_next_run_with_no_times():
    assert sched.next_run([], datetime(2026, 9, 21)) is None


# ---- plist ---------------------------------------------------------------------


def test_build_uses_absolute_paths_and_every_time(tmp_path):
    d = sched.build([(7, 0), (19, 30)], tmp_path / "bin" / "riffle", tmp_path / "sync.log")
    assert d["Label"] == sched.LABEL
    assert d["ProgramArguments"] == [str(tmp_path / "bin" / "riffle"), "sync"]
    assert d["StartCalendarInterval"] == [{"Hour": 7, "Minute": 0}, {"Hour": 19, "Minute": 30}]
    assert d["StandardOutPath"] == d["StandardErrorPath"] == str(tmp_path / "sync.log")
    assert "~" not in plistlib.dumps(d).decode()


def test_read_times_handles_old_single_dict_and_missing(tmp_path):
    p = tmp_path / "x.plist"
    assert sched.read_times(p) == []
    p.write_bytes(plistlib.dumps({"StartCalendarInterval": {"Hour": 7, "Minute": 0}}))  # what v0.2.0 wrote
    assert sched.read_times(p) == [(7, 0)]
    p.write_bytes(plistlib.dumps({"StartCalendarInterval": [{"Hour": 19, "Minute": 5}, {"Hour": 6}]}))
    assert sched.read_times(p) == [(6, 0), (19, 5)]


def test_parse_print_takes_the_job_state_not_the_trigger_state():
    info = sched.parse_print(PRINT)
    assert info == {
        "state": "not running",
        "program": "/Users/pat/riffle/.venv/bin/riffle",
        "runs": "3",
        "last exit code": "0",
    }


# ---- install / status / remove ------------------------------------------------


def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    return tmp_path / "LaunchAgents" / f"{sched.LABEL}.plist"


def test_install_writes_loads_and_replaces(tmp_path, monkeypatch):
    path = _env(tmp_path, monkeypatch)
    lc = FakeLaunchctl(print_out=PRINT)
    sched.install([(7, 0)], exe=tmp_path / "riffle", run=lc, path=path)
    assert lc.calls == ["bootout", "bootstrap"] and lc.loaded
    assert (tmp_path / "data" / "riffle").is_dir()  # log dir exists before launchd needs it

    sched.install([(6, 0), (18, 0)], exe=tmp_path / "riffle", run=lc, path=path)  # set again = replace
    assert sched.read_times(path) == [(6, 0), (18, 0)]
    assert lc.calls[-2:] == ["bootout", "bootstrap"]

    st = sched.status(run=lc, path=path)
    assert (st.installed, st.loaded, st.times) == (True, True, [(6, 0), (18, 0)])
    assert (st.runs, st.last_exit) == ("3", "0")


def test_install_reports_bootstrap_failure(tmp_path, monkeypatch):
    path = _env(tmp_path, monkeypatch)
    with pytest.raises(RuntimeError, match="bootstrap failed"):
        sched.install([(7, 0)], exe=tmp_path / "riffle", run=FakeLaunchctl(bootstrap_rc=5), path=path)
    assert path.exists()  # written, just not loaded


def test_status_when_written_but_never_loaded(tmp_path, monkeypatch):
    """The exact v0.2.0 bug: a plist on disk that launchd never heard of."""
    path = _env(tmp_path, monkeypatch)
    path.parent.mkdir(parents=True)
    path.write_bytes(plistlib.dumps(sched.build([(7, 0)], tmp_path / "riffle", tmp_path / "log")))
    st = sched.status(run=FakeLaunchctl(loaded=False), path=path)
    assert st.installed and not st.loaded and st.runs is None


def test_remove(tmp_path, monkeypatch):
    path = _env(tmp_path, monkeypatch)
    lc = FakeLaunchctl()
    sched.install([(7, 0)], exe=tmp_path / "riffle", run=lc, path=path)
    assert sched.remove(run=lc, path=path) is True
    assert not path.exists() and not lc.loaded
    assert sched.remove(run=lc, path=path) is False  # nothing left: says so, doesn't crash


# ---- the trickle job -------------------------------------------------------------


def test_the_trickle_job_runs_every_ten_minutes(tmp_path):
    d = sched.build([], tmp_path / "riffle", tmp_path / "mtgo-trickle.log", sched.TRICKLE)
    assert (d["Label"], d["StartInterval"]) == ("gg.riffletcg.riffle-mtgo", 600)
    assert d["ProgramArguments"] == [str(tmp_path / "riffle"), "mtgo", "trickle"]
    assert "StartCalendarInterval" not in d


def test_the_trickle_job_installs_and_goes_beside_the_sync_job(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    fake = FakeLaunchctl()
    path = sched.install([], exe=tmp_path / "riffle", run=fake, job=sched.TRICKLE)
    assert path == tmp_path / "Library" / "LaunchAgents" / "gg.riffletcg.riffle-mtgo.plist"
    assert plistlib.loads(path.read_bytes())["StartInterval"] == 600
    assert sched.log_path(sched.TRICKLE).name == "mtgo-trickle.log"
    assert sched.status(run=fake, job=sched.TRICKLE).loaded
    assert sched.remove(run=fake, job=sched.TRICKLE) and not path.exists()


def _cli(tmp_path, monkeypatch, fake):
    """`riffle` with the schedule's launchctl faked; returns each command's output."""
    from typer.testing import CliRunner

    from riffle.cli import app

    monkeypatch.setenv("HOME", str(tmp_path))
    install, status, remove = sched.install, sched.status, sched.remove
    monkeypatch.setattr(
        sched, "install", lambda times, job: install(times, tmp_path / "riffle", fake, job=job)
    )
    monkeypatch.setattr(sched, "status", lambda job=sched.SYNC: status(fake, job=job))
    monkeypatch.setattr(sched, "remove", lambda job=sched.SYNC: remove(fake, job=job))
    return lambda *args: CliRunner().invoke(app, list(args)).output


def test_the_schedule_commands_show_and_manage_the_trickle_job(tmp_path, monkeypatch):
    run = _cli(tmp_path, monkeypatch, FakeLaunchctl(print_out=PRINT))
    shown = run("schedule", "trickle")
    assert "every      10 minutes: riffle mtgo trickle" in shown and "loaded     yes" in shown
    both = run("schedule")
    assert both.index("gg.riffletcg.riffle-sync") < both.index(
        "gg.riffletcg.riffle-mtgo"
    )  # the fake loads any label
    assert run("schedule", "trickle", "--remove") == "removed gg.riffletcg.riffle-mtgo\n"
    assert "no trickle job — start one with: riffle schedule trickle" in run("schedule")
    assert run("schedule", "trickle", "--remove") == "no trickle job to remove\n"


def test_the_sync_job_shows_its_times_and_next_run_with_the_mac_s_zone(tmp_path, monkeypatch, denver):
    from datetime import UTC

    from typer.testing import CliRunner

    from riffle import times
    from riffle.cli import app

    monkeypatch.setenv("HOME", str(tmp_path))
    fake = FakeLaunchctl(print_out=PRINT)
    sched.install([(7, 0), (16, 0)], exe=tmp_path / "riffle", run=fake)
    status = sched.status
    monkeypatch.setattr(sched, "status", lambda job=sched.SYNC: status(fake, job=job))
    monkeypatch.setattr(times, "now", lambda: datetime(2026, 9, 28, 9, 41, tzinfo=UTC))  # 03:41 in Denver
    shown = CliRunner().invoke(app, ["schedule", "show"]).output
    assert "  times      07:00, 16:00  (24-hour, daily, the Mac's time: MDT)" in shown
    assert "  next run   Mon 2026-09-28 07:00 MDT" in shown


# ---- the watch jobs, one per store ------------------------------------------------


def test_a_store_s_watch_job_runs_every_five_minutes(tmp_path):
    job = sched.watch_job("manapool")
    d = sched.build([], tmp_path / "riffle", tmp_path / "watch-manapool.log", job)
    assert (d["Label"], d["StartInterval"]) == ("gg.riffletcg.riffle-watch-manapool", 300)
    assert d["ProgramArguments"] == [str(tmp_path / "riffle"), "watch", "manapool"]
    assert sched.log_path(job).name == "watch-manapool.log"


def test_the_schedule_commands_install_show_and_remove_a_watch_job_per_store(tmp_path, monkeypatch):
    run = _cli(tmp_path, monkeypatch, FakeLaunchctl(print_out=PRINT))
    stores = ["cardkingdom", "manapool", "cardmarket", "tcgcsv", "mtgjson", "goatbots", "scryfall"]
    labels = [f"gg.riffletcg.riffle-watch-{store}" for store in stores]
    shown = run("schedule", "watch")
    assert [shown.index(label) for label in labels] == sorted(shown.index(label) for label in labels)
    assert "every      5 minutes: riffle watch tcgcsv" in shown and "loaded     yes" in shown
    agents = tmp_path / "Library" / "LaunchAgents"
    assert sorted(p.name for p in agents.iterdir()) == sorted(f"{label}.plist" for label in labels)
    every = run("schedule")
    assert (
        every.index("riffle-mtgo")
        < every.index("riffle-watch-cardkingdom")
        < every.index("riffle-watch-tcgcsv")
        < every.index("riffle-watch-goatbots")
        < every.index("riffle-watch-scryfall")
    )
    assert run("schedule", "watch", "--remove") == "".join(f"removed {label}\n" for label in labels)
    assert "no cardkingdom watch job — start one with: riffle schedule watch" in run("schedule")
    assert run("schedule", "watch", "--remove") == "".join(f"no {label} to remove\n" for label in labels)


def test_a_watch_job_launchd_won_t_load_is_an_error(tmp_path, monkeypatch, plain):
    run = _cli(tmp_path, monkeypatch, FakeLaunchctl(bootstrap_rc=5))
    said = "but launchctl bootstrap failed: Bootstrap failed: 5: Input/output error"
    assert said in plain(run("schedule", "watch"))


# ---- what each job's log says -------------------------------------------------------------

POSTGRES = (
    "can't reach Postgres at postgresql+psycopg://tcg@localhost:5432/tcg: connection refused"
    " — start it with: riffle db up"
)
WATCH_RUN = "2026-10-17 22:02:03 UTC  riffle watch manapool\n22:14:10  exited 1 after 12m 07s\n"
MANAPOOL_FAILED = (
    "2026-10-18 02:08:10 UTC  riffle watch manapool\n"
    "02:17:40  ! Mana Pool variants: HTTP 503\n"
    "1 step failed: Mana Pool variants\n"
    "02:17:41  exited 1 after 9m 31s\n"
    "2026-10-19 06:08:10 UTC  riffle watch manapool\n"
    "06:17:40  ! Mana Pool singles: download interrupted after 25,615 bytes (The read operation timed out)\n"
    "06:17:46  ! Mana Pool sealed: not asked: Mana Pool gave no answer\n"
    "2 steps failed: Mana Pool singles, Mana Pool sealed\n"
    "06:17:46  exited 1 after 9m 36s\n"
)


def job_log(job: sched.Job, text: str) -> Path:
    path = sched.log_path(job)
    path.parent.mkdir(parents=True, exist_ok=True)
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


def test_schedule_show_says_how_each_job_s_newest_run_ended_and_when_the_next_is_due(
    tmp_path, monkeypatch, denver
):
    run = _cli(tmp_path, monkeypatch, FakeLaunchctl(print_out=PRINT))
    run("schedule", "watch")
    job_log(sched.watch_job("manapool"), WATCH_RUN)
    job_log(sched.watch_job("tcgcsv"), "2026-10-17 22:02:03 UTC  riffle watch tcgcsv\n")  # killed, or running
    shown = run("schedule", "show")
    assert (
        "  last run   Sat 2026-10-17 16:02 MDT: exited 1 after 12m 07s\n"
        "  next run   about Sat 2026-10-17 16:07 MDT\n"
    ) in shown
    assert (
        "  last run   Sat 2026-10-17 16:02 MDT: exit unknown\n  next run   about Sat 2026-10-17 16:07 MDT\n"
    ) in shown
    assert "  last run   none in the log\n  loaded     yes\n" in shown  # no run yet: no next run either


def sync_show(tmp_path, monkeypatch, fake):
    """The sync job installed at 07:00 and 16:00 with launchctl faked; `riffle schedule show`'s output."""
    from typer.testing import CliRunner

    from riffle.cli import app

    monkeypatch.setenv("HOME", str(tmp_path))
    sched.install([(7, 0), (16, 0)], exe=tmp_path / "riffle", run=fake)
    status = sched.status
    monkeypatch.setattr(sched, "status", lambda job=sched.SYNC: status(fake, job=job))
    return lambda: CliRunner().invoke(app, ["schedule", "show"]).output


def test_schedule_show_names_the_sync_failing_run_after_run_until_a_run_passes(tmp_path, monkeypatch, denver):
    show = sync_show(tmp_path, monkeypatch, FakeLaunchctl(print_out=PRINT))
    job_log(sched.SYNC, "".join(sync_run(k) for k in range(10)))
    shown = show()
    assert "  last run   Sat 2026-10-17 16:02 MDT: exited 1 after 1.2s\n" in shown
    assert f"  failing    the 10th run in a row, the last Sat 2026-10-17 16:02 MDT: {POSTGRES}\n" in shown
    job_log(sched.SYNC, sync_run(9))
    assert f"  failing    the last run, Sat 2026-10-17 16:02 MDT: {POSTGRES}\n" in show()
    job_log(sched.SYNC, "".join(sync_run(k) for k in range(10)) + sync_run(10, 0))
    assert "failing" not in show()


def test_schedule_show_counts_each_job_s_steps_failed_in_the_last_24_hours(tmp_path, monkeypatch, denver):
    run = _cli(tmp_path, monkeypatch, FakeLaunchctl(print_out=PRINT))
    monkeypatch.setattr(times, "now", lambda: datetime(2026, 10, 19, 8, 30, tzinfo=UTC))
    run("schedule", "watch")
    job_log(sched.watch_job("manapool"), MANAPOOL_FAILED)
    job_log(
        sched.watch_job("tcgcsv"),
        "2026-10-19 02:00:10 UTC  riffle watch tcgcsv\n02:05:00  ! tcgcsv fab: HTTP 503\n"
        "1 step failed: tcgcsv fab\n02:05:01  exited 1 after 4m 51s\n",
    )
    shown = run("schedule", "show")
    assert (
        "  failed     2 steps in the last 24 h, the last Mon 2026-10-19 00:17 MDT: Mana Pool sealed:"
        " not asked: Mana Pool gave no answer\n"
    ) in shown
    assert (
        "  failed     1 step in the last 24 h, the last Sun 2026-10-18 20:05 MDT: tcgcsv fab: HTTP 503\n"
        in shown
    )


def test_schedule_show_says_when_a_job_s_log_can_t_be_read_and_still_exits_0(
    tmp_path, monkeypatch, unreadable
):
    from typer.testing import CliRunner

    from riffle.cli import app

    _cli(tmp_path, monkeypatch, FakeLaunchctl(print_out=PRINT))("schedule", "watch")
    path = unreadable(job_log(sched.watch_job("manapool"), WATCH_RUN))
    result = CliRunner().invoke(app, ["schedule", "show"])
    assert result.exit_code == 0, result.output
    assert (
        f"  log        {path}: unreadable (Permission denied); failures in the last 24 h unknown\n"
        in result.output
    )
    assert f"  log        {path}\n" not in result.output


def test_schedule_show_says_what_the_log_says_of_a_job_launchd_hasn_t_loaded(tmp_path, monkeypatch, denver):
    fake = FakeLaunchctl(print_out=PRINT)
    show = sync_show(tmp_path, monkeypatch, fake)
    sched.install([], exe=tmp_path / "riffle", run=fake, job=sched.watch_job("manapool"))
    job_log(sched.SYNC, sync_run(0) + sync_run(1))
    job_log(sched.watch_job("manapool"), WATCH_RUN)
    fake.loaded = False
    shown = show()
    assert (
        "  loaded     NO — reload with: riffle schedule set 07:00 16:00\n"
        f"  failing    the 2nd run in a row, the last Tue 2026-10-13 16:02 MDT: {POSTGRES}\n"
    ) in shown
    assert (
        "  loaded     NO — reload with: riffle schedule watch\n"
        "  failing    the last run, Sat 2026-10-17 16:02 MDT\n"
    ) in shown
