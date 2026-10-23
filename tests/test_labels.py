"""Every launchd job is labelled under the project's domain: in the plist names, in what the
schedule commands print, and in status's job names."""

import sys

from typer.testing import CliRunner

from riffle import schedule as sched
from riffle import status, watching
from riffle.cli import app
from tests.test_schedule import PRINT, FakeLaunchctl

PREFIX = "gg.riffletcg.riffle"


def test_every_job_is_labelled_under_the_project_s_domain(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert sched.PREFIX == PREFIX
    assert (sched.SYNC.label, sched.TRICKLE.label, sched.LABEL) == (
        f"{PREFIX}-sync",
        f"{PREFIX}-mtgo",
        f"{PREFIX}-sync",
    )
    assert sched.watch_job("manapool").label == f"{PREFIX}-watch-manapool"
    assert sched.plist_path() == tmp_path / "Library" / "LaunchAgents" / f"{PREFIX}-sync.plist"
    assert sched.build([(7, 0)], tmp_path / "riffle", tmp_path / "sync.log")["Label"] == f"{PREFIX}-sync"


def test_status_names_each_job_by_what_follows_the_prefix():
    lines = status.jobs_lines(lambda job: sched.Status(True, False))
    assert "0 of 9 loaded; sync isn't; mtgo isn't; watch-cardkingdom isn't;" in lines[0]


def _cli(tmp_path, monkeypatch, fake):
    """`riffle` on a Mac with the schedule's launchctl faked; returns each command's output."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(sys, "platform", "darwin")
    install, job_status, remove = sched.install, sched.status, sched.remove
    monkeypatch.setattr(
        sched, "install", lambda times, job=sched.SYNC: install(times, tmp_path / "riffle", fake, job=job)
    )
    monkeypatch.setattr(sched, "status", lambda job=sched.SYNC: job_status(fake, job=job))
    monkeypatch.setattr(sched, "remove", lambda job=sched.SYNC: remove(fake, job=job))
    return lambda *args: CliRunner().invoke(app, list(args)).output


def test_the_schedule_commands_name_the_jobs_by_the_new_labels(tmp_path, monkeypatch):
    run = _cli(tmp_path, monkeypatch, FakeLaunchctl(print_out=PRINT))
    stores = list(watching.STORES)
    agents = tmp_path / "Library" / "LaunchAgents"
    assert f"{PREFIX}-sync\n" in run("schedule", "set", "07:00")
    assert (agents / f"{PREFIX}-sync.plist").exists()
    assert f"{PREFIX}-mtgo\n" in run("schedule", "trickle")
    watched = run("schedule", "watch")
    assert all(f"{PREFIX}-watch-{store}\n" in watched for store in stores)
    assert sorted(p.name for p in agents.iterdir()) == sorted(
        [f"{PREFIX}-sync.plist", f"{PREFIX}-mtgo.plist", *(f"{PREFIX}-watch-{s}.plist" for s in stores)]
    )
    every = run("schedule")
    assert "com.keltzbm" not in every
    assert every.index(f"{PREFIX}-sync") < every.index(f"{PREFIX}-mtgo") < every.index(f"{PREFIX}-watch-")
    assert run("schedule", "remove") == f"removed {PREFIX}-sync\n"
    assert run("schedule", "trickle", "--remove") == f"removed {PREFIX}-mtgo\n"
    assert run("schedule", "watch", "--remove") == "".join(f"removed {PREFIX}-watch-{s}\n" for s in stores)
