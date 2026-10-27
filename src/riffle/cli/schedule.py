"""Handle scheduled job commands."""

from datetime import datetime, timedelta
from pathlib import Path

import typer

from riffle import times
from riffle.cli import _common as common


def _scheduler() -> None:
    """Stop a schedule command where there's no scheduler, before it writes anything."""
    from riffle.ops import schedule as sched

    if not sched.supported():
        common._echo(sched.unsupported(), err=True)
        raise typer.Exit(2)


def _installed(path: Path) -> None:
    """On Linux, a line for each timer an install command enabled."""
    from riffle.ops import schedule as sched

    if sched.backend() == "systemd":
        typer.echo(f"systemd timer {path.name} enabled")


def _lingering() -> None:
    """On Linux, a line when the user's timers would stop at logout; nothing when they won't, or when
    logind can't say."""
    import getpass

    from riffle.ops import schedule as sched
    from riffle.ops.schedule import systemd

    if sched.backend() == "systemd" and systemd.lingering() is False:
        typer.echo(
            f"jobs stop when you log out; to keep them: sudo loginctl enable-linger {getpass.getuser()}"
        )


def _remove(job) -> bool:
    """sched.remove, or exit 2 saying why the scheduler wouldn't let the job go."""
    from riffle.ops import schedule as sched

    try:
        return sched.remove(job=job)
    except RuntimeError as e:
        raise typer.BadParameter(str(e)) from e


def _ran(st) -> str:
    """How the job's last run went: launchd counts a job's runs, systemd doesn't."""
    from riffle.ops import schedule as sched

    ended = f"last exit {st.last_exit or '—'}   state {st.state or '—'}"
    return f"  {ended}" if sched.backend() == "systemd" else f"  runs       {st.runs or '0'}   {ended}"


def _where(job) -> str:
    """The line naming the file that holds the job: its plist, or on Linux its timer."""
    from riffle.ops import schedule as sched

    if sched.backend() == "systemd":
        from riffle.ops.schedule import systemd

        return f"  units      {systemd.unit_paths(job)[0]}"
    return f"  plist      {sched.plist_path(job)}"


def _show_schedule() -> None:
    from riffle.ops import schedule as sched

    _show_sync()
    typer.echo()
    _show_every(sched.TRICKLE, "trickle", "riffle schedule trickle")
    for job in _watch_jobs():
        typer.echo()
        _show_every(job, f"{job.args[-1]} watch", "riffle schedule watch")


def _watch_jobs() -> list:
    from riffle.ops import schedule as sched
    from riffle.watch import watching

    return [sched.watch_job(store) for store in watching.STORES]


def _show_every(job, what: str, start: str) -> None:
    """A job that runs every few minutes: whether it's loaded, and how its runs went."""
    from riffle.ops import schedule as sched

    try:
        st = sched.status(job=job)
    except OSError as e:  # a scheduler that can't be asked: a user manager out of reach, say
        typer.echo(f"{job.label}\n  loaded     unknown ({e})")
        return
    if not st.installed and not st.loaded:
        typer.echo(f"no {what} job — start one with: {start}")
        return
    first, after, log = _show_runs(job)
    typer.echo(job.label)
    typer.echo(f"  every      {(job.interval or 0) // 60} minutes: riffle {' '.join(job.args)}")
    for line in first:
        typer.echo(line)
    typer.echo(f"  loaded     {'yes' if st.loaded else 'NO — reload with: ' + start}")
    if st.loaded:
        typer.echo(_ran(st))
    for line in after:
        typer.echo(line)
    typer.echo(log)
    typer.echo(_where(job))


def _show_runs(job) -> tuple[list[str], list[str], str]:
    """What a job's log says (riffle.ops.joblogs): its newest run and how it ended, and for a job
    run every few minutes when the next is due; the run it's failing on, until a run passes, and
    the steps that failed in the last 24 hours; and its log line. launchd's own count starts
    again at each reload, so its last exit can be 0 the morning after ten failed runs."""
    from riffle.ops import joblogs, status
    from riffle.ops import schedule as sched

    path = sched.log_path(job)
    try:
        runs = joblogs.read(path)
    except (OSError, UnicodeDecodeError) as e:
        return [], [], f"  log        {path}: {status.unreadable(e)}"
    if not runs:
        return ["  last run   none in the log"], [], f"  log        {path}"
    newest = runs[-1]
    ended = "exit unknown" if newest.exit is None else f"exited {newest.exit} after {newest.took}"
    first = [f"  last run   {_when(newest.started)}: {ended}"]
    if job.interval:
        first.append(f"  next run   about {_when(newest.started + timedelta(seconds=job.interval))}")
    after = []
    failing = joblogs.failing(runs)
    if failing:
        run, n = failing
        which = f"the {joblogs.nth(n)} run in a row, the last" if n > 1 else "the last run,"
        after.append(f"  failing    {which} {_when(run.started)}" + (f": {run.last}" if run.last else ""))
    failed = joblogs.failed_since(runs, times.now() - timedelta(days=1))
    if failed:
        n, (at, what) = len(failed), failed[-1]
        after.append(f"  failed     {n} step{'s' * (n != 1)} in the last 24 h, the last {_when(at)}: {what}")
    return first, after, f"  log        {path}"


def _when(t: datetime) -> str:
    """A time as schedule show prints it: this machine's, as its scheduler runs the jobs by it."""
    return times.local(t, "%a %Y-%m-%d %H:%M")


def _show_sync() -> None:
    from riffle.ops import schedule as sched

    try:
        st = sched.status()
    except OSError as e:  # a scheduler that can't be asked: a user manager out of reach, say
        typer.echo(f"{sched.LABEL}\n  loaded     unknown ({e})")
        return
    if not st.installed and not st.loaded:
        typer.echo("no schedule — set one with: riffle schedule set 07:00")
        return
    now = times.now().astimezone()  # the scheduler runs the job by this machine's clock
    at = ", ".join(sched.fmt(t) for t in st.times) or (
        "(none in the timer)" if sched.backend() == "systemd" else "(none in plist)"
    )
    nxt = sched.next_run(st.times, now.replace(tzinfo=None))
    first, after, log = _show_runs(sched.SYNC)
    typer.echo(f"{sched.LABEL}")
    whose = "this machine's" if sched.backend() == "systemd" else "the Mac's"
    typer.echo(f"  times      {at}  (24-hour, daily, {whose} time: {now.tzname()})")
    for line in first:
        typer.echo(line)
    typer.echo(f"  next run   {_when(nxt.astimezone())}" if nxt else "  next run   —")
    reload = "riffle schedule set " + " ".join(sched.fmt(t) for t in st.times)
    typer.echo(f"  loaded     {'yes' if st.loaded else 'NO — reload with: ' + reload}")
    if st.loaded:
        typer.echo(_ran(st))
    for line in after:
        typer.echo(line)
    typer.echo(log)
    typer.echo(_where(sched.SYNC))


def schedule_main(ctx: typer.Context) -> None:
    """Show every job (times, next run, last result). Subcommands: set, remove, trickle, watch."""
    _scheduler()  # before a subcommand's arguments are read, so a wrong one still hears why
    if ctx.invoked_subcommand is None:
        _show_schedule()


def schedule_show() -> None:
    """Times, next run, whether each job is loaded, and how its last run went."""
    _show_schedule()


def schedule_set(
    times: list[str] = typer.Argument(..., help="24-hour HH:MM times, e.g. 07:00 19:30"),
) -> None:
    """Run `riffle sync` daily at these times. Replaces any existing schedule."""
    from riffle.ops import schedule as sched

    try:
        parsed = sched.parse_times(times)
        _installed(sched.install(parsed))
    except (ValueError, RuntimeError) as e:
        raise typer.BadParameter(str(e)) from e
    _lingering()
    _show_schedule()


def schedule_remove() -> None:
    """Unload and delete the daily sync job."""
    from riffle.ops import schedule as sched

    typer.echo(f"removed {sched.LABEL}" if _remove(sched.SYNC) else "no schedule to remove")


def schedule_trickle(
    remove: bool = typer.Option(False, "--remove", help="Unload and delete the trickle job instead"),
) -> None:
    """Run `riffle mtgo trickle` every 10 minutes. Replaces any existing trickle job."""
    from riffle.ops import schedule as sched

    if remove:
        removed = _remove(sched.TRICKLE)
        typer.echo(f"removed {sched.TRICKLE.label}" if removed else "no trickle job to remove")
        return
    try:
        _installed(sched.install([], job=sched.TRICKLE))
    except RuntimeError as e:
        raise typer.BadParameter(str(e)) from e
    _lingering()
    _show_every(sched.TRICKLE, "trickle", "riffle schedule trickle")


def schedule_watch(
    remove: bool = typer.Option(False, "--remove", help="Unload and delete the watch jobs instead"),
) -> None:
    """Run `riffle watch <store>` every 5 minutes for each store, each its own job, so a long
    fetch holds up only its own store. Replaces any existing watch jobs."""
    from riffle.ops import schedule as sched

    jobs = _watch_jobs()
    if remove:
        for job in jobs:
            typer.echo(f"removed {job.label}" if _remove(job) else f"no {job.label} to remove")
        return
    try:
        for job in jobs:
            _installed(sched.install([], job=job))
    except RuntimeError as e:
        raise typer.BadParameter(str(e)) from e
    _lingering()
    for n, job in enumerate(jobs):
        if n:
            typer.echo()
        _show_every(job, f"{job.args[-1]} watch", "riffle schedule watch")
