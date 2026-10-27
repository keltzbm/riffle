"""Handle database setup, migration, backup, and restore commands."""

from collections.abc import Callable
from pathlib import Path
from typing import Any

import typer

from riffle import config, times
from riffle.cli import _common as common
from riffle.progress import Step, Tracker


def _db_engine():
    from riffle import db

    try:
        return db.engine()
    except db.BadURL as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1) from e


def _cant_reach(url: str, e: Exception) -> str:
    from riffle import db

    return f"can't reach Postgres at {db.display(url)}: {db.reason(e)}"


def _unreachable(url: str, e: Exception) -> typer.Exit:
    typer.echo(_cant_reach(url, e), err=True)
    typer.echo("start it with: riffle db up", err=True)
    return typer.Exit(1)


def _schema(eng) -> tuple[str, bool]:
    """The database's schema revision as db status says it, and whether it's the head."""
    from riffle.db import migrate

    current, head = migrate.current(eng), migrate.head()
    if current == head:
        return f"schema     {current} (head)", True
    have = f"{current}, head is {head}" if current else f"empty, head is {head}"
    return f"schema     {have} — run: riffle db upgrade", False


def _in_step(step: Step, url: str, undone: str, act: Callable[[], Any], warn_elsewhere: bool = False) -> Any:
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
        step.fail(f"{undone}: {_cant_reach(url, e)}; start it with: riffle db up")
    except (db.BadURL, backup.Failed, OSError) as e:
        step.fail(f"{undone}: {e}")
    return None


def _backup(tracker: Tracker, warn_elsewhere: bool = False) -> None:
    """The database dumped beside the data (riffle.db.backup), as the step `database backup`. In
    the sync, a database this machine doesn't run in Docker only warns: it's backed up where it
    runs."""
    from riffle.db import backup
    from riffle.watch import watching

    step = tracker.step("database backup")
    url = config.load().database_url
    dump = _in_step(
        step, url, "not backed up", lambda: backup.backup(url, backup.folder(), times.now()), warn_elsewhere
    )
    if dump is None:
        return
    said = f"{config.tilde(dump.path)}, {watching.size(dump.size)}, read back whole; {dump.kept} kept"
    if dump.not_removed:  # made all the same: only the older dumps stay
        step.warn(f"{said}; older dumps not removed: {dump.not_removed}")
    else:
        step.ok(said)


def db_up() -> None:
    """Start the local Postgres (Docker Compose) and wait until it's healthy."""
    from riffle.db import compose

    try:
        code = compose.up()
    except FileNotFoundError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1) from e
    raise typer.Exit(code)


def db_upgrade() -> None:
    """Apply every migration the database doesn't have yet."""
    from sqlalchemy.exc import OperationalError

    from riffle.db import migrate

    eng = _db_engine()
    try:
        before = migrate.current(eng)
        migrate.upgrade(eng)
        after = migrate.current(eng)
    except OperationalError as e:
        raise _unreachable(eng.url.render_as_string(hide_password=False), e) from e
    if before == after:
        typer.echo(f"schema     {after} (head), nothing to apply")
    else:
        typer.echo(f"schema     {before or 'empty'} → {after} (head)")


def db_status() -> None:
    """Server, schema revision, and whether migrations are pending. Exits 1 unless reachable and current."""
    from sqlalchemy import text
    from sqlalchemy.exc import OperationalError

    from riffle import db

    eng = _db_engine()
    url = eng.url.render_as_string(hide_password=False)
    typer.echo(f"database   {db.display(url)}")
    try:
        with eng.connect() as conn:
            version = conn.execute(text("SHOW server_version")).scalar_one()
        line, current = _schema(eng)
    except OperationalError as e:
        raise _unreachable(url, e) from e
    typer.echo(f"server     PostgreSQL {str(version).split()[0]}")
    typer.echo(line)
    if not current:
        raise typer.Exit(1)


def db_backup() -> None:
    """Dump the database beside the data, read the dump back whole, and keep a week of dumps."""
    with common._run("riffle db backup") as tracker:
        _backup(tracker)


def db_restore(
    dump: Path = typer.Argument(..., exists=True, dir_okay=False, help="A dump riffle db backup made"),
    yes: bool = typer.Option(False, "--yes", help="Replace the database without asking"),
) -> None:
    """Replace everything in the database with a dump, in one transaction: if anything fails,
    nothing changes."""
    from sqlalchemy.exc import OperationalError

    from riffle import db
    from riffle.db import backup

    url = config.load().database_url
    if not yes:
        if not common._interactive():
            typer.echo(
                "riffle db restore replaces the database: run it at a terminal to be asked first,"
                " or add --yes",
                err=True,
            )
            raise typer.Exit(2)
        at = backup.made(dump)
        made = f" (made {times.shown(at)})" if at else ""
        typer.confirm(f"Replace everything in {_shown(url)} with {dump.name}{made}?", abort=True)

    def restore() -> bool:
        backup.restore(url, dump)
        return True

    with common._run(f"riffle db restore {dump.name}") as tracker:
        step = tracker.step("database restore")
        if _in_step(step, url, "not restored", restore):
            step.ok(f"{dump.name} into {db.display(url)}")
            try:
                said = _schema(db.engine(url))[0]
            except OperationalError as e:  # restored all the same
                said = f"schema     unknown: {_cant_reach(url, e)}"
            common._echo(said)


def _shown(url: str) -> str:
    """The URL for a question, any password masked; one that can't be read isn't shown."""
    from sqlalchemy.exc import ArgumentError

    from riffle import db

    try:
        return db.display(url)
    except ArgumentError:
        return "the database database_url names"
