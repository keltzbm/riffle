"""The systemd user timers (Linux): each job is a service, which runs it once, and a timer,
which starts the service at the job's times or every so often, both in ~/.config/systemd/user/.

A timer never starts a second run of its service while one is going: the firing waits for the run
to end, so a run longer than twice its interval is followed at once by the next, where launchd
would wait for its next tick (riffle.ops.schedule). An interval job's first run comes when its timer
is installed, its boot delay being long past. New times rewrite and restart the timer, never the
service, so a sync that's running isn't killed, and removing a job disables its timer, leaves a
running service to finish, and clears the time of its last run that Persistent=true keeps. A
user's timers run only while that user is logged in unless lingering is on (`lingering`).
Everything that touches systemctl and loginctl goes through `run`, so the logic is testable
without them.
"""

import getpass
import re
import sys
from pathlib import Path

from riffle import config
from riffle.ops.schedule import jobs as schedule
from riffle.ops.schedule.jobs import SYNC, Job, Runner, Status

_RUNNING = {"activating", "active", "deactivating"}  # a service's states while its job runs
_CALENDAR = re.compile(r"^OnCalendar=\*-\*-\* (\d{2}):(\d{2}):00$", re.M)


def unit_dir() -> Path:
    return config.config_home() / "systemd" / "user"


def _exec_word(word: str, program: bool = False) -> str:
    """A word of ExecStart=: % doubled, since systemd expands specifiers in every word, and $ in an
    argument, where it expands variables too, though never in the program's path; a backslash
    escaped, and the word quoted when it holds a space or a quote."""
    word = word.replace("\\", "\\\\").replace("%", "%%")
    if not program:
        word = word.replace("$", "$$")
    if any(c.isspace() for c in word) or '"' in word or "'" in word:
        return '"' + word.replace('"', '\\"') + '"'
    return word


def service_text(job: Job, exe: Path, log: Path) -> str:
    """The service that runs the job once, its output appended to the job's log."""
    words = " ".join([_exec_word(str(exe), program=True), *(_exec_word(w) for w in job.args)])
    out = str(log).replace("%", "%%")  # a log path is read for specifiers too
    return "\n".join(
        [
            "[Unit]",
            f"Description=riffle {' '.join(job.args)}",
            "",
            "[Service]",
            "Type=oneshot",
            f"ExecStart={words}",
            f"StandardOutput=append:{out}",
            f"StandardError=append:{out}",
            "",
        ]
    )


def timer_text(job: Job, times: list[tuple[int, int]]) -> str:
    """The timer that starts the job's service: every interval from its last start, or at each of
    the times in this machine's zone, a time missed while the machine was off made up at boot.
    To the second, where systemd would let a timer slip a minute to save wakeups."""
    what = f"riffle {' '.join(job.args)}"
    if job.interval:
        when = f"{what} every {job.interval // 60} minutes"
        rules = [f"OnBootSec={job.interval}s", f"OnUnitActiveSec={job.interval}s"]
    else:
        when = f"{what} at its times"
        rules = [*(f"OnCalendar=*-*-* {schedule.fmt(t)}:00" for t in times), "Persistent=true"]
    timer = ["[Unit]", f"Description={when}", "", "[Timer]", *rules, "AccuracySec=1s", ""]
    return "\n".join([*timer, "[Install]", "WantedBy=timers.target", ""])


def unit_paths(job: Job) -> tuple[Path, Path]:
    """The job's timer and service."""
    folder = unit_dir()
    return folder / f"{job.label}.timer", folder / f"{job.label}.service"


def install(
    times: list[tuple[int, int]], exe: Path | None = None, run: Runner = schedule._run, job: Job = SYNC
) -> Path:
    """Write the job's service and timer, then reload systemd, enable the timer and restart it, so
    new times take effect at once. Returns the timer's path."""
    exe = exe or Path(sys.argv[0]).resolve()
    log = schedule.log_path(job)
    log.parent.mkdir(parents=True, exist_ok=True)
    timer, service = unit_paths(job)
    timer.parent.mkdir(parents=True, exist_ok=True)
    service.write_text(service_text(job, exe, log), encoding="utf-8")
    timer.write_text(timer_text(job, times), encoding="utf-8")
    for verb in (["daemon-reload"], ["enable", timer.name], ["restart", timer.name]):
        result = run(["systemctl", "--user", *verb])
        if result.returncode != 0:
            raise RuntimeError(
                f"wrote {timer} but systemctl --user {verb[0]} failed: {result.stderr.strip()}"
            )
    return timer


def remove(run: Runner = schedule._run, job: Job = SYNC) -> bool:
    """Disable the timer, which stops it and leaves a running service to finish, clear the time of
    its last run, which a timer set again later would otherwise make up at once, then delete both
    units and reload systemd. True if there was a unit to remove; when systemd won't disable a
    timer that's written, the units are left as they are, and RuntimeError says why."""
    timer, service = unit_paths(job)
    existed = timer.exists() or service.exists()
    disabled = run(["systemctl", "--user", "disable", "--now", timer.name])
    if disabled.returncode != 0 and existed:
        raise RuntimeError(f"systemctl --user disable failed: {disabled.stderr.strip()}; {timer} is left")
    run(["systemctl", "--user", "clean", "--what=state", timer.name])  # nothing kept yet: no matter
    timer.unlink(missing_ok=True)
    service.unlink(missing_ok=True)
    run(["systemctl", "--user", "daemon-reload"])
    return existed or disabled.returncode == 0


def _show(run: Runner, unit: str, properties: str) -> dict[str, str]:
    """What `systemctl --user show` says of a unit's properties. A unit systemd doesn't know is
    shown as inactive, so a failure means no user manager to ask: OSError."""
    result = run(["systemctl", "--user", "show", unit, f"--property={properties}"])
    if result.returncode != 0:
        raise OSError(f"systemctl --user show failed: {result.stderr.strip()}")
    return dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)


def status(run: Runner = schedule._run, job: Job = SYNC) -> Status:
    """Whether the job's timer is written and active, its times, and how its service's last run
    ended. systemd clears a service's exit when a run starts and sets it when the run ends, so
    there's none to give while a run is going, nor before the first."""
    timer, service = unit_paths(job)
    ticking = _show(run, timer.name, "ActiveState")
    ran = _show(run, service.name, "ActiveState,ExecMainStatus,ExecMainExitTimestamp")
    text = timer.read_text(encoding="utf-8") if timer.exists() else ""
    return Status(
        installed=timer.exists(),
        loaded=ticking.get("ActiveState") == "active",
        times=sorted((int(h), int(m)) for h, m in _CALENDAR.findall(text)),
        last_exit=ran.get("ExecMainStatus") if ran.get("ExecMainExitTimestamp") else None,
        state="running" if ran.get("ActiveState") in _RUNNING else "not running",
    )


def lingering(run: Runner = schedule._run) -> bool | None:
    """Whether this user's timers run while they're logged out; None when logind can't say."""
    try:
        result = run(["loginctl", "show-user", getpass.getuser(), "--property=Linger"])
    except OSError:  # no loginctl at all
        return None
    if result.returncode != 0:
        return None
    return {"Linger=yes": True, "Linger=no": False}.get(result.stdout.strip())
