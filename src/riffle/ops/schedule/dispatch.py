"""Select the scheduler backend for this platform."""

import shutil
import sys
from pathlib import Path

from riffle.ops.schedule import jobs, launchd, systemd


def backend() -> str | None:
    """The scheduler this machine has: launchd on macOS, systemd on Linux where systemctl is on
    PATH, none elsewhere."""
    if sys.platform == "darwin":
        return "launchd"
    if sys.platform == "linux" and shutil.which("systemctl"):
        return "systemd"
    return None


def supported() -> bool:
    """Whether this machine has a scheduler to run the jobs."""
    return backend() is not None


def unsupported() -> str:
    """What the schedule commands say where there's no scheduler."""
    if sys.platform == "linux":
        return "scheduling needs systemd on Linux"
    return "scheduling runs on macOS (launchd) and Linux (systemd)"


def status(run: jobs.Runner = jobs._run, path: Path | None = None, job: jobs.Job = jobs.SYNC) -> jobs.Status:
    if backend() == "systemd":
        return systemd.status(run=run, job=job)
    return launchd.status(run=run, job=job, path=path)


def install(
    times: list[tuple[int, int]],
    exe: Path | None = None,
    run: jobs.Runner = jobs._run,
    path: Path | None = None,
    job: jobs.Job = jobs.SYNC,
) -> Path:
    if backend() == "systemd":
        return systemd.install(times, exe=exe, run=run, job=job)
    return launchd.install(times, exe=exe, run=run, job=job, path=path)


def remove(run: jobs.Runner = jobs._run, path: Path | None = None, job: jobs.Job = jobs.SYNC) -> bool:
    if backend() == "systemd":
        return systemd.remove(run=run, job=job)
    return launchd.remove(run=run, job=job, path=path)
