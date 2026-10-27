"""Track database backups and report database operation failures."""

from collections.abc import Callable

from riffle import config, times
from riffle.progress import Step, Tracker


def cant_reach(url: str, e: Exception) -> str:
    from riffle import db

    return f"can't reach Postgres at {db.display(url)}: {db.reason(e)}"


def in_step[T](
    step: Step, url: str, undone: str, act: Callable[[], T], warn_elsewhere: bool = False
) -> T | None:
    """act()'s result, or None once its error has ended the step, saying what wasn't done and
    why; a database this machine doesn't run in Docker only warns when warn_elsewhere."""
    from sqlalchemy.exc import OperationalError

    from riffle import db
    from riffle.db import backup

    try:
        return act()
    except backup.NotHere as e:
        (step.warn if warn_elsewhere else step.fail)(f"{undone}: {e}")
    except OperationalError as e:
        step.fail(f"{undone}: {cant_reach(url, e)}; start it with: riffle db up")
    except (db.BadURL, backup.Failed, OSError) as e:
        step.fail(f"{undone}: {e}")
    return None


def backup(tracker: Tracker, url: str, warn_elsewhere: bool = False) -> None:
    """The database dumped beside the data (riffle.db.backup), as the step `database backup`. In
    the sync, a database this machine doesn't run in Docker only warns: it's backed up where it
    runs."""
    from riffle.db import backup
    from riffle.watch import watching

    step = tracker.step("database backup")
    dump = in_step(
        step, url, "not backed up", lambda: backup.backup(url, backup.folder(), times.now()), warn_elsewhere
    )
    if dump is None:
        return
    said = f"{config.tilde(dump.path)}, {watching.size(dump.size)}, read back whole; {dump.kept} kept"
    # The new dump is kept even when removing an older dump fails.
    if dump.not_removed:
        step.warn(f"{said}; older dumps not removed: {dump.not_removed}")
    else:
        step.ok(said)
