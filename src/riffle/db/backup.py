"""`riffle db backup` and `riffle db restore`: the database dumped to a dated file beside the
data, and a dump loaded back in its place.

pg_dump, pg_restore and psql run inside the database's own container (compose.in_db), so they
are always the server's version and no machine needs a Postgres client. They run only when
database_url names that same server, its system identifier read both ways: otherwise a Mac whose
URL names the server would dump, or overwrite, the database it stopped running itself.

A dump's SHA-256 is kept beside it (<dump>.sha256, as sha256sum writes it), and a restore checks
it first: reading a dump back proves it whole when it's made, not that it's unchanged since. Each
program in the container has a time limit, so a Docker that stops answering fails the step rather
than holding the run lock, and every sync after it, for good.
"""

import re
import subprocess
from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from riffle import config, db, runs
from riffle.db import compose

FOLDER = "db-backups"
KEEP = timedelta(days=7)  # a week of dumps here; the server's off-site copy keeps older ones
NAME = re.compile(r"^(?P<database>.+)-(?P<stamp>\d{4}-\d{2}-\d{2}T\d{6}(?:\.\d+)?Z)\.dump$")
SUM = ".sha256"
IDENTITY = "SELECT system_identifier FROM pg_control_system()"
ASIDES = ("NOTICE:", "DETAIL:", "HINT:", "WARNING:", "WARN[")  # stderr lines that aren't the failure
ASKED = 60  # seconds for the container to say which server it runs
LONGEST = 30 * 60  # seconds for a dump, a read-back or a load: far past any yet, short of for good
LOADER = "riffle-restore"  # the load's application_name, by which one left running is stopped

# The schema as Postgres 18 makes it, then the dump as SQL, then COMMIT only once pg_restore has
# finished: a dump that stops partway ends psql's input with no commit, and Postgres rolls the
# whole load back. A lock held elsewhere fails the load after a minute rather than stalling it.
LOAD = f"""set -eo pipefail
{{
  echo 'BEGIN;'
  echo 'SET client_min_messages = warning;'
  echo "SET lock_timeout = '1min';"
  echo 'DROP SCHEMA public CASCADE;'
  echo 'CREATE SCHEMA public AUTHORIZATION pg_database_owner;'
  echo 'GRANT USAGE ON SCHEMA public TO PUBLIC;'
  echo "COMMENT ON SCHEMA public IS 'standard public schema';"
  pg_restore --no-owner --no-privileges --file=-
  echo 'COMMIT;'
}} | PGAPPNAME={LOADER} psql --no-psqlrc --quiet --set=ON_ERROR_STOP=1 --username="$1" --dbname="$2"
"""

Runner = Callable[..., subprocess.CompletedProcess]


class NotHere(Exception):
    """database_url doesn't name the database this machine runs in Docker."""


class Failed(Exception):
    """A program in the database's container failed; the message ends with its own first line."""


@dataclass(frozen=True)
class Dump:
    path: Path
    size: int  # bytes
    kept: int  # the database's dumps in the folder afterwards
    not_removed: str = ""  # why older dumps couldn't be removed, when they couldn't


def folder() -> Path:
    return config.data_dir() / FOLDER


def name(database: str, at: datetime) -> str:
    """The file a dump made at `at` is kept as: tcg-2026-10-23T221503Z.dump."""
    return f"{database}-{runs.name(at.replace(microsecond=0))}.dump"


def made(path: Path) -> datetime | None:
    """When a dump's name says it was made; None for a name that isn't a dump's."""
    found = NAME.match(path.name)
    return runs.parse(found["stamp"]) if found else None


def checksum(dump: Path) -> Path:
    """The file beside a dump that holds its SHA-256."""
    return dump.with_name(dump.name + SUM)


def _of(database: str, filename: str) -> bool:
    """Whether a file name is a dump's, of this database."""
    found = NAME.match(filename)
    return found is not None and found["database"] == database and runs.parse(found["stamp"]) is not None


def dumps(database: str, where: Path) -> list[Path]:
    """The database's dumps in the folder, oldest first."""
    if not where.is_dir():
        return []
    found = [path for path in where.iterdir() if _of(database, path.name) and path.is_file()]
    return sorted(found, key=lambda path: made(path) or datetime.min.replace(tzinfo=UTC))


def _aside(line: str) -> bool:
    """A notice or a warning, not the failure: Postgres's, or Compose's, which come as
    time="…" level=warning msg="…" when its stderr isn't a terminal."""
    return line.startswith(ASIDES) or (line.startswith("time=") and "level=warning" in line)


def _line(result: subprocess.CompletedProcess) -> str:
    """A failed program's first line on stderr that isn't a notice or a warning, or its exit
    code when it said nothing."""
    lines = [s.strip() for s in (result.stderr or b"").decode(errors="replace").splitlines() if s.strip()]
    said = [s for s in lines if not _aside(s)] or lines
    return said[0] if said else f"exited {result.returncode}"


def _inside(prefix: Sequence[str] | None) -> list[str]:
    if prefix is not None:
        return list(prefix)
    try:
        return compose.in_db()
    except FileNotFoundError as e:
        raise NotHere(str(e)) from e


def _server(url: str) -> tuple[str, str, str]:
    """The server's system identifier, the role and the database, as database_url connects."""
    from sqlalchemy import text

    eng = db.engine(url)
    try:
        with eng.connect() as conn:
            row = conn.execute(text(f"SELECT ({IDENTITY})::text, current_user, current_database()")).one()
    finally:
        eng.dispose()
    return str(row[0]), str(row[1]), str(row[2])


def _same_server(
    url: str, prefix: Sequence[str] | None, run: Runner, doing: str
) -> tuple[list[str], str, str]:
    """The start of a command in the database's container, and the role and database database_url
    connects as, once the server it names is the one in the container. The URL is asked first, so
    a server that can't be reached says so wherever docker is or isn't."""
    ident, user, database = _server(url)
    inside = _inside(prefix)
    try:
        probe = run(
            [
                *inside,
                *("psql", "--no-psqlrc", "--tuples-only", "--no-align", "--quiet"),
                f"--username={user}",
                f"--dbname={database}",
                f"--command={IDENTITY}",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=ASKED,
        )
    except subprocess.TimeoutExpired:
        raise Failed(f"the database's container gave no answer in {ASKED} s") from None
    if probe.returncode:
        raise Failed(_line(probe))
    if probe.stdout.decode(errors="replace").strip() != ident:
        raise NotHere(
            f"database_url names a Postgres other than the one this machine runs in Docker"
            f" ({db.display(url)}); {doing} on the machine that runs it"
        )
    return inside, user, database


def _read_back(path: Path, inside: list[str], run: Runner, shown: str) -> None:
    """Every byte of a dump read and decompressed, as a restore would, into nothing."""
    with path.open("rb") as f:
        try:
            back = run(
                [*inside, "pg_restore", "--file=/dev/null"],
                stdin=f,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                timeout=LONGEST,
            )
        except subprocess.TimeoutExpired:
            raise Failed(f"{shown} took longer than {LONGEST // 60} minutes to read back") from None
    if back.returncode:
        raise Failed(f"{shown} doesn't read back whole ({_line(back)})")


def backup(
    url: str, where: Path, at: datetime, run: Runner = subprocess.run, prefix: Sequence[str] | None = None
) -> Dump:
    """Dumps the database database_url names, reads the dump back whole before giving it its
    name, keeps its SHA-256 beside it, then prunes the folder."""
    inside, user, database = _same_server(url, prefix, run, "back it up")
    dest = where / name(database, at)
    with runs.writing(dest) as f:
        try:
            dumped = run(
                [
                    *inside,
                    "pg_dump",
                    "--format=custom",
                    "--compress=zstd",
                    f"--username={user}",
                    f"--dbname={database}",
                ],
                stdin=subprocess.DEVNULL,
                stdout=f,
                stderr=subprocess.PIPE,
                timeout=LONGEST,
            )
        except subprocess.TimeoutExpired:
            raise Failed(f"pg_dump took longer than {LONGEST // 60} minutes") from None
        if dumped.returncode:
            raise Failed(_line(dumped))
        f.flush()
        _read_back(Path(f.name), inside, run, dest.name)
    runs.write(checksum(dest), f"{runs.file_sha256(dest)}  {dest.name}\n".encode())
    try:
        prune(database, where, at)
    except OSError as e:  # the dump is made all the same
        return Dump(dest, dest.stat().st_size, len(dumps(database, where)), str(e))
    return Dump(dest, dest.stat().st_size, len(dumps(database, where)))


def prune(database: str, where: Path, now: datetime, keep: timedelta = KEEP) -> list[Path]:
    """Removes the database's dumps made more than `keep` before now, never its newest, with
    their checksums, and what a run that died left: a .part, and a checksum whose dump is gone
    (the run lock means none is being written). Returns the dumps removed."""
    found = dumps(database, where)
    gone = [path for path in found[:-1] if (made(path) or now) < now - keep]
    for path in gone:
        path.unlink()
        checksum(path).unlink(missing_ok=True)
    for part in where.glob("*.dump.part"):
        if _of(database, part.name.removesuffix(".part")):
            part.unlink(missing_ok=True)
    for sums in where.glob(f"*.dump{SUM}"):
        dump = sums.with_name(sums.name.removesuffix(SUM))
        if _of(database, dump.name) and not dump.exists():
            sums.unlink(missing_ok=True)
    return gone


def _unchanged(dump: Path) -> None:
    """Failed when the SHA-256 kept beside the dump, if there is one, isn't the dump's own."""
    sums = checksum(dump)
    if not sums.exists():
        return
    said = sums.read_text(encoding="utf-8").split()
    if not said or said[0] != runs.file_sha256(dump):
        raise Failed(f"{dump.name} has changed since it was made: its SHA-256 isn't the one in {sums.name}")


def restore(url: str, dump: Path, run: Runner = subprocess.run, prefix: Sequence[str] | None = None) -> None:
    """Replaces everything in the database database_url names with the dump, in one transaction,
    once the dump matches the SHA-256 beside it, if any, and reads back whole."""
    inside, user, database = _same_server(url, prefix, run, "restore it")
    try:
        _unchanged(dump)
        _read_back(dump, inside, run, dump.name)
    except Failed as e:
        raise Failed(f"{e}; the database is as it was") from None
    try:
        load(user, database, dump, run, inside)
    except BaseException:
        _stop_loading(url)
        raise


def _stop_loading(url: str) -> None:
    """Ends a load still running in the container, so that its transaction rolls back: a Ctrl-C
    or a time limit stops only docker's client, and the load would go on, and might commit,
    without it. What it can't do here goes unsaid: the error being raised is the one to see."""
    from sqlalchemy import text

    with suppress(Exception):
        eng = db.engine(url)
        try:
            with eng.connect() as conn:
                conn.execute(
                    text(
                        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE application_name = :app"
                    ),
                    {"app": LOADER},
                )
        finally:
            eng.dispose()


def load(
    user: str, database: str, dump: Path, run: Runner = subprocess.run, prefix: Sequence[str] | None = None
) -> None:
    """The dump loaded in place of everything in public, in one transaction (LOAD)."""
    inside = _inside(prefix)
    with dump.open("rb") as f:
        try:
            loaded = run(
                [*inside, "bash", "-c", LOAD, LOADER, user, database],
                stdin=f,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                timeout=LONGEST,
            )
        except subprocess.TimeoutExpired:
            raise Failed(
                f"the load took longer than {LONGEST // 60} minutes; the database is as it was"
            ) from None
    if loaded.returncode:
        raise Failed(f"{_line(loaded)}; the database is as it was")
