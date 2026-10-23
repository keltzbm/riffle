"""On Linux without systemctl the schedule commands refuse before they write anything, status says
there's no scheduler yet, and db up names Docker for each platform."""

import re
import shutil
import subprocess
import sys

import pytest
from typer.testing import CliRunner

from riffle import schedule as sched
from riffle import status
from riffle.cli import app
from riffle.db import compose

NEEDS_SYSTEMD = "scheduling needs systemd on Linux"
DOCKER = (
    "docker isn't on PATH: install Docker Engine (Linux) or OrbStack or Docker Desktop (macOS), and start it"
)
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


def never(*_, **__):
    raise AssertionError("a scheduler asked")


def linux_without_systemctl(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    which = shutil.which
    monkeypatch.setattr(
        shutil, "which", lambda cmd, *a, **k: None if cmd == "systemctl" else which(cmd, *a, **k)
    )


@pytest.mark.parametrize("args", SCHEDULE, ids=lambda args: " ".join(args) or "bare")
def test_schedule_commands_refuse_on_linux_without_systemctl(tmp_path, monkeypatch, plain, args):
    monkeypatch.setenv("HOME", str(tmp_path))
    linux_without_systemctl(monkeypatch)
    for name in ("install", "status", "remove"):
        monkeypatch.setattr(sched, name, never)
    result = CliRunner().invoke(app, ["schedule", *args])
    assert result.exit_code == 2, result.output
    assert NEEDS_SYSTEMD in plain(result.output), result.output
    assert not (tmp_path / "Library").exists()


def test_status_says_there_is_no_scheduler_on_linux_without_systemctl(monkeypatch):
    linux_without_systemctl(monkeypatch)

    def no_launchctl(job=sched.SYNC):  # what asking launchd would come to on Linux
        raise FileNotFoundError("launchctl")

    monkeypatch.setattr(sched, "status", no_launchctl)
    monkeypatch.setattr(status, "backup_line", lambda *_, **__: "none")
    result = CliRunner().invoke(app, ["status", "--days", "3"])
    assert result.exit_code == 0, result.output
    assert re.search(r"^jobs\s+no scheduler on this platform yet$", result.output, re.M), result.output
    assert "unknown (launchctl)" not in result.output


def test_db_up_names_docker_for_linux_and_macos_when_none_is_on_path():
    with pytest.raises(FileNotFoundError) as caught:
        compose.up(run=lambda args: subprocess.CompletedProcess(args, 0), which=lambda _: None)
    assert str(caught.value) == DOCKER


@pytest.mark.parametrize("args", [["set"], ["trickle", "--bogus"]], ids=["no times", "an unknown option"])
def test_the_platform_is_checked_before_a_subcommand_s_arguments(monkeypatch, plain, args):
    linux_without_systemctl(monkeypatch)
    result = CliRunner().invoke(app, ["schedule", *args])
    assert result.exit_code == 2, result.output
    assert NEEDS_SYSTEMD in plain(result.output)
