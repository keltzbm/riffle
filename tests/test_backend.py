"""Which scheduler a machine has, launchd on macOS, systemd on Linux and none elsewhere, decides
what the schedule commands write; where there's none they refuse before writing anything, and
`riffle status` says so on its jobs line."""

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from riffle import schedule as sched
from riffle import status
from riffle.cli import app

NEEDS_SYSTEMD = "scheduling needs systemd on Linux"
ELSEWHERE = "scheduling runs on macOS (launchd) and Linux (systemd)"
NONE = [("linux", None, NEEDS_SYSTEMD), ("freebsd14", "/usr/local/bin/systemctl", ELSEWHERE)]
NONE_IDS = ["Linux without systemctl", "another system"]
SCHEDULE = [
    [],
    ["show"],
    ["set", "07:00"],
    ["remove"],
    ["trickle"],
    ["trickle", "--remove"],
    ["watch"],
    ["watch", "--remove"],
]
README = Path(__file__).resolve().parent.parent / "README.md"


def on(monkeypatch, platform: str, systemctl: str | None) -> None:
    """The tests' machine as `platform`, with systemctl on PATH at `systemctl` or not at all."""
    monkeypatch.setattr(sys, "platform", platform)
    which = shutil.which
    monkeypatch.setattr(
        shutil, "which", lambda cmd, *a, **k: systemctl if cmd == "systemctl" else which(cmd, *a, **k)
    )


def never(*args, **_):
    raise AssertionError(f"a scheduler asked: {args}")


def unit_dir() -> Path:
    return Path(os.environ["XDG_CONFIG_HOME"]) / "systemd" / "user"


# ---- the choice ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("platform", "systemctl", "expected"),
    [
        ("darwin", None, "launchd"),
        ("darwin", "/opt/local/bin/systemctl", "launchd"),
        ("linux", "/usr/bin/systemctl", "systemd"),
        ("linux", None, None),
        ("freebsd14", "/usr/local/bin/systemctl", None),
        ("win32", None, None),
    ],
)
def test_the_backend_is_the_platform_s_own_scheduler(monkeypatch, platform, systemctl, expected):
    on(monkeypatch, platform, systemctl)
    assert sched.backend() == expected


def test_the_backend_is_read_each_time_it_s_asked(monkeypatch):
    on(monkeypatch, "linux", "/usr/bin/systemctl")
    assert sched.backend() == "systemd"
    on(monkeypatch, "linux", None)
    assert sched.backend() is None


def test_on_linux_the_schedule_writes_units_and_no_plist(tmp_path, monkeypatch):
    on(monkeypatch, "linux", "/usr/bin/systemctl")
    calls = []

    def systemctl(args):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "ActiveState=active\n", "")

    path = sched.install([(7, 0)], exe=tmp_path / "riffle", run=systemctl)
    assert path == unit_dir() / "gg.riffletcg.riffle-sync.timer"
    assert sorted(p.name for p in unit_dir().iterdir()) == [
        "gg.riffletcg.riffle-sync.service",
        "gg.riffletcg.riffle-sync.timer",
    ]
    assert not (Path.home() / "Library").exists()
    assert {call[0] for call in calls} == {"systemctl"}
    st = sched.status(run=systemctl)
    assert (st.installed, st.loaded, st.times) == (True, True, [(7, 0)])
    assert sched.remove(run=systemctl) is True
    assert not path.exists() and list(unit_dir().iterdir()) == []


def test_on_a_mac_the_schedule_is_launchd_s_even_with_systemctl_on_path(tmp_path, monkeypatch):
    on(monkeypatch, "darwin", "/opt/local/bin/systemctl")
    calls = []

    def launchctl(args):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    path = sched.install([(7, 0)], exe=tmp_path / "riffle", run=launchctl)
    assert (
        path == Path.home() / "Library" / "LaunchAgents" / "gg.riffletcg.riffle-sync.plist" and path.exists()
    )
    assert [call[0] for call in calls] == ["launchctl", "launchctl"]
    assert not (Path(os.environ["XDG_CONFIG_HOME"]) / "systemd").exists()


# ---- where there's neither -------------------------------------------------------------


@pytest.mark.parametrize(("platform", "systemctl", "said"), NONE, ids=NONE_IDS)
@pytest.mark.parametrize("args", SCHEDULE, ids=lambda args: " ".join(args) or "bare")
def test_schedule_commands_refuse_where_there_s_no_scheduler_before_writing_anything(
    monkeypatch, plain, platform, systemctl, said, args
):
    on(monkeypatch, platform, systemctl)
    for name in ("install", "status", "remove"):
        monkeypatch.setattr(sched, name, never)
    monkeypatch.setattr(subprocess, "run", never)
    result = CliRunner().invoke(app, ["schedule", *args])
    assert result.exit_code == 2, result.output
    assert said in plain(result.output), result.output
    assert not (Path.home() / "Library").exists() and not unit_dir().exists()


@pytest.mark.parametrize(("platform", "systemctl", "said"), NONE, ids=NONE_IDS)
@pytest.mark.parametrize("args", [["set"], ["trickle", "--bogus"]], ids=["no times", "an unknown option"])
def test_the_scheduler_is_checked_before_a_subcommand_s_arguments(
    monkeypatch, plain, platform, systemctl, said, args
):
    on(monkeypatch, platform, systemctl)
    result = CliRunner().invoke(app, ["schedule", *args])
    assert result.exit_code == 2, result.output
    assert said in plain(result.output)


def test_status_s_jobs_line_asks_systemd_on_linux(monkeypatch):
    on(monkeypatch, "linux", "/usr/bin/systemctl")
    monkeypatch.setattr(sched, "status", lambda job=sched.SYNC: sched.Status(True, True, last_exit="0"))
    monkeypatch.setattr(status, "backup_line", lambda *_, **__: "none")
    result = CliRunner().invoke(app, ["status", "--days", "3"])
    assert result.exit_code == 0, result.output
    assert re.search(r"^jobs\s+9 of 9 loaded; every last run exited 0$", result.output, re.M), result.output


@pytest.mark.parametrize(("platform", "systemctl", "said"), NONE, ids=NONE_IDS)
def test_status_says_there_is_no_scheduler_where_there_s_neither(monkeypatch, platform, systemctl, said):
    on(monkeypatch, platform, systemctl)
    monkeypatch.setattr(sched, "status", never)
    monkeypatch.setattr(status, "backup_line", lambda *_, **__: "none")
    result = CliRunner().invoke(app, ["status", "--days", "3"])
    assert result.exit_code == 0, result.output
    assert re.search(r"^jobs\s+no scheduler on this platform yet$", result.output, re.M), result.output


# ---- the README ------------------------------------------------------------------------


def test_the_readme_schedules_on_macos_and_linux_and_says_how_the_jobs_outlive_a_logout():
    text = README.read_text(encoding="utf-8")
    assert "launchd" in text, "the schedule section doesn't name launchd"
    assert "systemd" in text, "the schedule section doesn't name systemd"
    assert not re.search(r"macOS[ -]only|only on macOS", text), "the schedule section still says macOS only"
    assert "loginctl enable-linger" in text, "no word of lingering, so the jobs stop at logout"
