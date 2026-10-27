"""Install, inspect, and remove launchd jobs on macOS."""

import os
import plistlib
import sys
from pathlib import Path

from riffle.ops.schedule import jobs as jobs_mod


def plist_path(job: jobs_mod.Job = jobs_mod.SYNC) -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{job.label}.plist"


def _domain() -> str:
    return f"gui/{os.getuid()}"


def build(times: list[tuple[int, int]], exe: Path, log: Path, job: jobs_mod.Job = jobs_mod.SYNC) -> dict:
    """launchd needs absolute paths: no ~, no PATH lookup."""
    when: dict = (
        {"StartInterval": job.interval}
        if job.interval
        else {"StartCalendarInterval": [{"Hour": h, "Minute": m} for h, m in times]}
    )
    return {
        "Label": job.label,
        "ProgramArguments": [str(exe), *job.args],
        **when,
        "StandardOutPath": str(log),
        "StandardErrorPath": str(log),
    }


def read_times(path: Path) -> list[tuple[int, int]]:
    """Times in an existing plist. Handles the one-dict form older versions wrote."""
    if not path.exists():
        return []
    data = plistlib.loads(path.read_bytes())
    sci = data.get("StartCalendarInterval") or []
    if isinstance(sci, dict):
        sci = [sci]
    return sorted((d.get("Hour", 0), d.get("Minute", 0)) for d in sci)


def parse_print(text: str) -> dict[str, str]:
    """The fields that matter from `launchctl print` — first occurrence of each."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        key, sep, value = line.strip().partition(" = ")
        if sep and key in {"state", "runs", "last exit code", "program"} and key not in out:
            out[key] = value.strip()
    return out


def status(
    run: jobs_mod.Runner = jobs_mod._run, path: Path | None = None, job: jobs_mod.Job = jobs_mod.SYNC
) -> jobs_mod.Status:
    path = path or plist_path(job)
    result = run(["launchctl", "print", f"{_domain()}/{job.label}"])
    info = parse_print(result.stdout) if result.returncode == 0 else {}
    return jobs_mod.Status(
        installed=path.exists(),
        loaded=result.returncode == 0,
        times=read_times(path),
        runs=info.get("runs"),
        last_exit=info.get("last exit code"),
        state=info.get("state"),
        program=info.get("program"),
    )


def install(
    times: list[tuple[int, int]],
    exe: Path | None = None,
    run: jobs_mod.Runner = jobs_mod._run,
    path: Path | None = None,
    job: jobs_mod.Job = jobs_mod.SYNC,
) -> Path:
    """Write the plist and (re)load it, or on Linux the job's systemd units. Replaces any
    existing job. An interval job takes no times."""
    path = path or plist_path(job)
    exe = exe or Path(sys.argv[0]).resolve()
    log = jobs_mod.log_path(job)
    log.parent.mkdir(parents=True, exist_ok=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(plistlib.dumps(build(times, exe, log, job)))
    run(["launchctl", "bootout", f"{_domain()}/{job.label}"])  # fails harmlessly if not loaded
    result = run(["launchctl", "bootstrap", _domain(), str(path)])
    if result.returncode != 0:
        raise RuntimeError(f"wrote {path} but launchctl bootstrap failed: {result.stderr.strip()}")
    return path


def remove(
    run: jobs_mod.Runner = jobs_mod._run, path: Path | None = None, job: jobs_mod.Job = jobs_mod.SYNC
) -> bool:
    """Unload and delete, the plist or on Linux the job's systemd units. True if there was
    anything to remove."""
    path = path or plist_path(job)
    was_loaded = run(["launchctl", "bootout", f"{_domain()}/{job.label}"]).returncode == 0
    existed = path.exists()
    path.unlink(missing_ok=True)
    return was_loaded or existed
