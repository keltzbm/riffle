"""The systemd units (Linux), tested without systemd: a fake `systemctl --user` records each call
and the unit files on disk when it came, and a fake `loginctl` answers for lingering."""

import getpass
import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from riffle import times
from riffle.ops import schedule as sched
from riffle.ops.schedule import systemd
from riffle.watch.watching import STORES

SYNC_TIMER = "gg.riffletcg.riffle-sync.timer"
SYNC_SERVICE = "gg.riffletcg.riffle-sync.service"
LOGOUT = "jobs stop when you log out; to keep them: sudo loginctl enable-linger"
RAN = {"ExecMainStatus": "0", "ExecMainExitTimestamp": "Mon 2026-09-28 07:01:20 MDT"}


def never(*args, **_):
    raise AssertionError(f"a real process asked for: {args}")


@pytest.fixture(autouse=True)
def linux(monkeypatch):
    """systemd is Linux's, so the schedule commands run as on a Linux machine with systemctl on
    PATH, wherever the tests do; and nothing here starts a real process."""
    monkeypatch.setattr(sys, "platform", "linux")
    which = shutil.which
    monkeypatch.setattr(
        shutil,
        "which",
        lambda cmd, *a, **k: "/usr/bin/systemctl" if cmd == "systemctl" else which(cmd, *a, **k),
    )
    monkeypatch.setattr(subprocess, "run", never)


def user(*rest: str) -> list[str]:
    return ["systemctl", "--user", *rest]


def units() -> list[str]:
    """The unit files on disk, by name."""
    folder = systemd.unit_dir()
    return sorted(p.name for p in folder.iterdir()) if folder.is_dir() else []


class FakeSystemctl:
    """`systemctl --user`: `calls` keeps each call's arguments and `files` the unit files on disk
    when it came. A timer restarted is active until disabled; `fail` names the verb that exits 1;
    `service` sets what `show` says of any service (never run, by default)."""

    def __init__(self, fail=None, service=None):
        self.calls: list[list[str]] = []
        self.files: list[list[str]] = []
        self.active: set[str] = set()
        self.fail = fail
        self.service = {"ActiveState": "inactive", "ExecMainStatus": "0", "ExecMainExitTimestamp": ""}
        self.service.update(service or {})

    def __call__(self, args):
        assert args[:2] == ["systemctl", "--user"], args
        self.calls.append(list(args))
        self.files.append(units())
        verb = args[2]
        if verb == self.fail:
            return subprocess.CompletedProcess(args, 1, "", "Input/output error")
        if verb in {"daemon-reload", "enable", "clean"}:
            return subprocess.CompletedProcess(args, 0, "", "")
        if verb == "restart":
            self.active.add(args[3])
            return subprocess.CompletedProcess(args, 0, "", "")
        if verb == "disable":
            unit = args[-1]
            self.active.discard(unit)
            if unit in self.files[-1]:
                return subprocess.CompletedProcess(args, 0, "", "")
            return subprocess.CompletedProcess(
                args, 1, "", f"Failed to disable unit: Unit file {unit} does not exist."
            )
        if verb == "show":
            unit, props = args[3], args[4].removeprefix("--property=").split(",")
            timer = {"ActiveState": "active" if unit in self.active else "inactive"}
            values = timer if unit.endswith(".timer") else self.service
            return subprocess.CompletedProcess(
                args, 0, "".join(f"{k}={values.get(k, '')}\n" for k in props), ""
            )
        raise AssertionError(f"unexpected systemctl {verb}")


class FakeLoginctl:
    """`loginctl show-user`, saying `stdout` and exiting `rc`."""

    def __init__(self, stdout="", rc=0):
        self.calls: list[list[str]] = []
        self.stdout, self.rc = stdout, rc

    def __call__(self, args):
        self.calls.append(list(args))
        return subprocess.CompletedProcess(
            args, self.rc, self.stdout, "" if self.rc == 0 else "Failed to get user"
        )


# ---- the unit files ---------------------------------------------------------------

SYNC_SERVICE_TEXT = """\
[Unit]
Description=riffle sync

[Service]
Type=oneshot
ExecStart=/x/riffle sync
StandardOutput=append:/d/sync.log
StandardError=append:/d/sync.log
"""

SYNC_TIMER_TEXT = """\
[Unit]
Description=riffle sync at its times

[Timer]
OnCalendar=*-*-* 07:00:00
OnCalendar=*-*-* 16:00:00
Persistent=true
AccuracySec=1s

[Install]
WantedBy=timers.target
"""

TRICKLE_SERVICE_TEXT = """\
[Unit]
Description=riffle mtgo trickle

[Service]
Type=oneshot
ExecStart=/x/riffle mtgo trickle
StandardOutput=append:/d/mtgo-trickle.log
StandardError=append:/d/mtgo-trickle.log
"""

TRICKLE_TIMER_TEXT = """\
[Unit]
Description=riffle mtgo trickle every 10 minutes

[Timer]
OnBootSec=600s
OnUnitActiveSec=600s
AccuracySec=1s

[Install]
WantedBy=timers.target
"""


def test_the_units_go_under_the_user_s_systemd_folder(monkeypatch):
    assert systemd.unit_dir() == Path(os.environ["XDG_CONFIG_HOME"]) / "systemd" / "user"
    monkeypatch.delenv("XDG_CONFIG_HOME")
    assert systemd.unit_dir() == Path.home() / ".config" / "systemd" / "user"


def test_the_sync_s_units_run_it_at_each_time_to_the_second_and_catch_up_a_missed_one():
    assert systemd.service_text(sched.SYNC, Path("/x/riffle"), Path("/d/sync.log")) == SYNC_SERVICE_TEXT
    assert systemd.timer_text(sched.SYNC, [(7, 0), (16, 0)]) == SYNC_TIMER_TEXT


def test_an_interval_job_s_units_run_it_every_so_many_seconds_from_boot():
    assert (
        systemd.service_text(sched.TRICKLE, Path("/x/riffle"), Path("/d/mtgo-trickle.log"))
        == TRICKLE_SERVICE_TEXT
    )
    assert systemd.timer_text(sched.TRICKLE, []) == TRICKLE_TIMER_TEXT
    watch = systemd.timer_text(sched.watch_job("manapool"), [])
    assert "[Unit]\nDescription=riffle watch manapool every 5 minutes\n\n" in watch
    assert "[Timer]\nOnBootSec=300s\nOnUnitActiveSec=300s\nAccuracySec=1s\n\n[Install]\n" in watch
    assert "OnCalendar" not in watch and "Persistent" not in watch
    service = systemd.service_text(
        sched.watch_job("manapool"), Path("/x/riffle"), Path("/d/watch-manapool.log")
    )
    assert (
        "Description=riffle watch manapool\n" in service and "ExecStart=/x/riffle watch manapool\n" in service
    )


def test_exec_start_escapes_what_systemd_would_expand_and_quotes_a_path_with_a_space():
    exe, log = Path("/x/my riffle/100%/$v/riffle"), Path("/d/100%/sync.log")
    text = systemd.service_text(sched.SYNC, exe, log)
    assert 'ExecStart="/x/my riffle/100%%/$v/riffle" sync\n' in text  # the program's $ is never expanded
    assert "StandardOutput=append:/d/100%%/sync.log\nStandardError=append:/d/100%%/sync.log\n" in text


# ---- install / status / remove ---------------------------------------------------------


def test_install_writes_both_units_then_reloads_enables_and_starts_the_timer(tmp_path):
    fake = FakeSystemctl()
    path = systemd.install([(7, 0), (16, 0)], exe=tmp_path / "riffle", run=fake)
    assert path == systemd.unit_dir() / SYNC_TIMER
    assert path.read_text() == SYNC_TIMER_TEXT
    log = sched.log_path(sched.SYNC)
    expected = systemd.service_text(sched.SYNC, tmp_path / "riffle", log)
    assert (path.parent / SYNC_SERVICE).read_text() == expected
    assert log.parent.is_dir()  # the log's folder exists before systemd appends to it
    assert fake.calls == [user("daemon-reload"), user("enable", SYNC_TIMER), user("restart", SYNC_TIMER)]
    assert fake.files == [[SYNC_SERVICE, SYNC_TIMER]] * 3  # both on disk before systemd hears of them
    assert fake.active == {SYNC_TIMER}


def test_new_times_rewrite_and_restart_the_timer_never_the_service(tmp_path):
    fake = FakeSystemctl()
    systemd.install([(7, 0)], exe=tmp_path / "riffle", run=fake)
    path = systemd.install([(6, 0), (18, 0)], exe=tmp_path / "riffle", run=fake)  # set again = replace
    assert "OnCalendar=*-*-* 06:00:00\nOnCalendar=*-*-* 18:00:00\n" in path.read_text()
    assert "07:00" not in path.read_text()
    assert fake.calls[3:] == [user("daemon-reload"), user("enable", SYNC_TIMER), user("restart", SYNC_TIMER)]
    assert not any(arg.endswith(".service") for call in fake.calls for arg in call)  # a running sync lives
    assert systemd.status(run=fake).times == [(6, 0), (18, 0)]


@pytest.mark.parametrize("verb", ["daemon-reload", "enable", "restart"])
def test_install_says_which_systemctl_step_failed_after_writing_the_units(tmp_path, verb):
    timer = systemd.unit_dir() / SYNC_TIMER
    with pytest.raises(RuntimeError) as caught:
        systemd.install([(7, 0)], exe=tmp_path / "riffle", run=FakeSystemctl(fail=verb))
    assert str(caught.value) == f"wrote {timer} but systemctl --user {verb} failed: Input/output error"
    assert timer.exists() and (timer.parent / SYNC_SERVICE).exists()  # written, just not loaded


def test_status_asks_the_timer_whether_it_s_active_and_the_service_how_its_last_run_went(tmp_path):
    fake = FakeSystemctl(
        service={
            "ActiveState": "failed",
            "ExecMainStatus": "1",
            "ExecMainExitTimestamp": "Fri 2026-10-23 07:01:21 MDT",
        }
    )
    systemd.install([(7, 0), (16, 0)], exe=tmp_path / "riffle", run=fake)
    fake.calls.clear()
    st = systemd.status(run=fake)
    assert fake.calls == [
        user("show", SYNC_TIMER, "--property=ActiveState"),
        user("show", SYNC_SERVICE, "--property=ActiveState,ExecMainStatus,ExecMainExitTimestamp"),
    ]
    assert (st.installed, st.loaded, st.times) == (True, True, [(7, 0), (16, 0)])
    assert (st.last_exit, st.state) == ("1", "not running")
    assert st.runs is None  # systemd keeps no count of a unit's runs


def test_a_job_never_run_has_no_last_exit(tmp_path):
    fake = FakeSystemctl()  # ExecMainStatus=0 with no start time: systemd's default, not a run's result
    systemd.install([], exe=tmp_path / "riffle", run=fake, job=sched.TRICKLE)
    st = systemd.status(run=fake, job=sched.TRICKLE)
    assert (st.installed, st.loaded, st.times, st.last_exit, st.state) == (
        True,
        True,
        [],
        None,
        "not running",
    )


@pytest.mark.parametrize(
    ("active", "state"),
    [
        ("activating", "running"),
        ("active", "running"),
        ("deactivating", "running"),
        ("inactive", "not running"),
        ("failed", "not running"),
    ],
)
def test_the_service_s_state_is_running_while_systemd_has_it_up(tmp_path, active, state):
    fake = FakeSystemctl(service={"ActiveState": active, **RAN})
    systemd.install([(7, 0)], exe=tmp_path / "riffle", run=fake)
    assert systemd.status(run=fake).state == state


def test_status_when_written_but_never_loaded():
    """Unit files on disk that systemd never heard of: installed, not loaded, their times read."""
    folder = systemd.unit_dir()
    folder.mkdir(parents=True)
    (folder / SYNC_TIMER).write_text(SYNC_TIMER_TEXT)
    (folder / SYNC_SERVICE).write_text(SYNC_SERVICE_TEXT)
    st = systemd.status(run=FakeSystemctl())
    assert st.installed and not st.loaded and st.times == [(7, 0), (16, 0)] and st.last_exit is None


def test_status_of_a_job_with_no_units():
    st = systemd.status(run=FakeSystemctl())
    assert (st.installed, st.loaded, st.times) == (False, False, [])


def test_remove_disables_the_timer_while_its_files_are_there_then_deletes_both_and_reloads(tmp_path):
    fake = FakeSystemctl()
    path = systemd.install([(7, 0)], exe=tmp_path / "riffle", run=fake)
    fake.calls.clear()
    fake.files.clear()
    assert systemd.remove(run=fake) is True
    assert fake.calls == [
        user("disable", "--now", SYNC_TIMER),
        user("clean", "--what=state", SYNC_TIMER),  # Persistent=true's last run, gone with the timer
        user("daemon-reload"),
    ]
    assert fake.files == [[SYNC_SERVICE, SYNC_TIMER], [SYNC_SERVICE, SYNC_TIMER], []]
    assert not path.exists() and not (path.parent / SYNC_SERVICE).exists() and not fake.active
    assert not any(arg.endswith(".service") for call in fake.calls for arg in call)  # a running job finishes
    assert systemd.remove(run=fake) is False  # nothing left: says so, doesn't crash


# ---- lingering --------------------------------------------------------------------------


def test_lingering_asks_logind_about_this_user():
    run = FakeLoginctl("Linger=yes\n")
    assert systemd.lingering(run=run) is True
    assert run.calls == [["loginctl", "show-user", getpass.getuser(), "--property=Linger"]]
    assert systemd.lingering(run=FakeLoginctl("Linger=no\n")) is False


@pytest.mark.parametrize(
    ("stdout", "rc"),
    [("Linger=maybe\n", 0), ("", 0), ("Linger=yes\n", 1)],
    ids=["an unknown value", "nothing", "a failed call"],
)
def test_lingering_is_unknown_when_logind_gives_no_clear_answer(stdout, rc):
    assert systemd.lingering(run=FakeLoginctl(stdout, rc)) is None


# ---- the schedule commands on Linux -----------------------------------------------------


def _cli(tmp_path, monkeypatch, fake, linger=True):
    """`riffle` with the schedule's systemctl faked and logind's answer about lingering set;
    returns each command's output."""
    from typer.testing import CliRunner

    from riffle.cli import app

    install, status, remove = sched.install, sched.status, sched.remove
    monkeypatch.setattr(
        sched, "install", lambda times, job=sched.SYNC: install(times, tmp_path / "riffle", fake, job=job)
    )
    monkeypatch.setattr(sched, "status", lambda job=sched.SYNC: status(fake, job=job))
    monkeypatch.setattr(sched, "remove", lambda job=sched.SYNC: remove(fake, job=job))
    monkeypatch.setattr(systemd, "lingering", lambda run=None: linger)
    return lambda *args: CliRunner().invoke(app, list(args)).output


def test_schedule_set_enables_the_sync_timer_and_shows_the_schedule_in_this_machine_s_time(
    tmp_path, monkeypatch, denver
):
    fake = FakeSystemctl(service=RAN)
    run = _cli(tmp_path, monkeypatch, fake)
    monkeypatch.setattr(times, "now", lambda: datetime(2026, 9, 28, 9, 41, tzinfo=UTC))  # 03:41 in Denver
    shown = run("schedule", "set", "07:00", "16:00")
    assert shown.startswith(f"systemd timer {SYNC_TIMER} enabled\n")
    assert (
        "gg.riffletcg.riffle-sync\n"
        "  times      07:00, 16:00  (24-hour, daily, this machine's time: MDT)\n"
        "  last run   none in the log\n"
        "  next run   Mon 2026-09-28 07:00 MDT\n"
        "  loaded     yes\n"
        "  last exit 0   state not running\n"
        f"  log        {sched.log_path(sched.SYNC)}\n"
        f"  units      {systemd.unit_dir() / SYNC_TIMER}\n"
    ) in shown
    assert "plist" not in shown and "  runs " not in shown and LOGOUT not in shown
    assert fake.active == {SYNC_TIMER} and units() == [SYNC_SERVICE, SYNC_TIMER]
    assert not (Path.home() / "Library").exists()  # no plist on Linux


@pytest.mark.parametrize("args", [["set", "07:00"], ["trickle"], ["watch"]], ids=lambda args: args[0])
def test_the_jobs_are_installed_with_a_warning_when_they_would_stop_at_logout(tmp_path, monkeypatch, args):
    fake = FakeSystemctl()
    shown = _cli(tmp_path, monkeypatch, fake, linger=False)("schedule", *args)
    assert shown.count(f"{LOGOUT} {getpass.getuser()}\n") == 1
    assert fake.active and units()  # installed all the same


@pytest.mark.parametrize("linger", [True, None], ids=["on", "unknown"])
def test_nothing_is_said_of_lingering_when_it_s_on_or_can_t_be_told(tmp_path, monkeypatch, linger):
    shown = _cli(tmp_path, monkeypatch, FakeSystemctl(), linger=linger)("schedule", "set", "07:00")
    assert "log out" not in shown and shown.startswith(f"systemd timer {SYNC_TIMER} enabled\n")


def test_schedule_remove_takes_the_sync_s_units_away(tmp_path, monkeypatch):
    run = _cli(tmp_path, monkeypatch, FakeSystemctl())
    run("schedule", "set", "07:00")
    assert run("schedule", "remove") == "removed gg.riffletcg.riffle-sync\n"
    assert units() == []
    assert run("schedule", "remove") == "no schedule to remove\n"
    assert "no schedule — set one with: riffle schedule set 07:00" in run("schedule", "show")


def test_the_schedule_commands_show_and_manage_the_trickle_job(tmp_path, monkeypatch):
    run = _cli(tmp_path, monkeypatch, FakeSystemctl())
    timer = systemd.unit_dir() / "gg.riffletcg.riffle-mtgo.timer"
    shown = run("schedule", "trickle")
    assert shown.startswith("systemd timer gg.riffletcg.riffle-mtgo.timer enabled\n")
    assert (
        "gg.riffletcg.riffle-mtgo\n"
        "  every      10 minutes: riffle mtgo trickle\n"
        "  last run   none in the log\n"
        "  loaded     yes\n"
        "  last exit —   state not running\n"
        f"  log        {sched.log_path(sched.TRICKLE)}\n"
        f"  units      {timer}\n"
    ) in shown
    assert timer.read_text() == TRICKLE_TIMER_TEXT
    assert run("schedule", "trickle", "--remove") == "removed gg.riffletcg.riffle-mtgo\n"
    assert "no trickle job — start one with: riffle schedule trickle" in run("schedule")
    assert run("schedule", "trickle", "--remove") == "no trickle job to remove\n"


def test_the_schedule_commands_install_show_and_remove_a_watch_job_per_store(tmp_path, monkeypatch):
    failed = {
        "ActiveState": "failed",
        "ExecMainStatus": "1",
        "ExecMainExitTimestamp": "Sat 2026-10-17 16:02:09 MDT",
    }
    run = _cli(tmp_path, monkeypatch, FakeSystemctl(service=failed))
    labels = [f"gg.riffletcg.riffle-watch-{store}" for store in STORES]
    shown = run("schedule", "watch")
    assert shown.startswith("".join(f"systemd timer {label}.timer enabled\n" for label in labels))
    for store, label in zip(STORES, labels, strict=True):
        assert f"{label}\n  every      5 minutes: riffle watch {store}\n" in shown
        assert f"  units      {systemd.unit_dir() / label}.timer\n" in shown
    assert shown.count("  loaded     yes\n  last exit 1   state not running\n") == len(labels)
    assert units() == sorted(f"{label}.{kind}" for label in labels for kind in ("service", "timer"))
    every = run("schedule")
    assert [every.index(label) for label in labels] == sorted(every.index(label) for label in labels)
    assert "no schedule — set one with" in every and "no trickle job" in every
    assert run("schedule", "watch", "--remove") == "".join(f"removed {label}\n" for label in labels)
    assert units() == []
    assert "no cardkingdom watch job — start one with: riffle schedule watch" in run("schedule")
    assert run("schedule", "watch", "--remove") == "".join(f"no {label} to remove\n" for label in labels)


def test_a_timer_systemd_won_t_enable_is_an_error(tmp_path, monkeypatch, plain):
    from typer.testing import CliRunner

    from riffle.cli import app

    _cli(tmp_path, monkeypatch, FakeSystemctl(fail="enable"))
    result = CliRunner().invoke(app, ["schedule", "set", "07:00"])
    assert result.exit_code == 2, result.output
    said = plain(result.output)
    assert "wrote " in said and "but systemctl --user enable failed: Input/output error" in said


def no_bus(args):  # no user manager to talk to: systemctl --user exits 1
    return subprocess.CompletedProcess(args, 1, "", "Failed to connect to bus")


def test_a_user_manager_that_can_t_be_asked_is_unknown_not_unloaded(tmp_path, monkeypatch):
    systemd.install([(7, 0)], exe=tmp_path / "riffle", run=FakeSystemctl())
    with pytest.raises(OSError, match="systemctl --user show failed: Failed to connect to bus"):
        systemd.status(run=no_bus)
    shown = _cli(tmp_path, monkeypatch, no_bus)("schedule", "show")
    assert (
        "gg.riffletcg.riffle-sync\n  loaded     unknown (systemctl --user show failed: Failed to connect"
        in shown
    )
    assert "NO — reload" not in shown


def test_a_running_job_has_no_last_exit_until_its_run_ends(tmp_path):
    fake = FakeSystemctl(
        service={"ActiveState": "activating", "ExecMainStatus": "0", "ExecMainExitTimestamp": ""}
    )
    systemd.install([], exe=tmp_path / "riffle", run=fake, job=sched.TRICKLE)
    st = systemd.status(run=fake, job=sched.TRICKLE)
    assert (st.last_exit, st.state) == (None, "running")


def test_a_dollar_in_an_argument_is_doubled_for_systemd():
    job = sched.Job("x", ("watch", "a$b"), "x.log", interval=300)
    assert "ExecStart=/x/riffle watch a$$b\n" in systemd.service_text(
        job, Path("/x/riffle"), Path("/d/x.log")
    )


def test_a_timer_systemd_won_t_disable_is_left_and_the_command_says_why(tmp_path, monkeypatch, plain):
    from typer.testing import CliRunner

    from riffle.cli import app

    path = systemd.install([(7, 0)], exe=tmp_path / "riffle", run=FakeSystemctl())
    with pytest.raises(RuntimeError, match="systemctl --user disable failed: Input/output error"):
        systemd.remove(run=FakeSystemctl(fail="disable"))
    assert path.exists() and (path.parent / SYNC_SERVICE).exists()
    _cli(tmp_path, monkeypatch, FakeSystemctl(fail="disable"))
    result = CliRunner().invoke(app, ["schedule", "remove"])
    assert result.exit_code == 2 and "systemctl --user disable failed" in plain(result.output)
    assert path.exists()


def test_a_timer_still_ticking_without_its_files_is_removed(tmp_path):
    fake = FakeSystemctl()
    path = systemd.install([(7, 0)], exe=tmp_path / "riffle", run=fake)
    path.unlink()
    (path.parent / SYNC_SERVICE).unlink()  # deleted by hand, systemd not told

    def ticking(args):  # the timer is still loaded, so systemd disables it all the same
        return subprocess.CompletedProcess(args, 0, "", "")

    assert systemd.remove(run=ticking) is True


def test_lingering_is_unknown_without_loginctl():
    def missing(args):
        raise FileNotFoundError("loginctl")

    assert systemd.lingering(run=missing) is None
