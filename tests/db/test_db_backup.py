"""`riffle db backup` and `riffle db restore`: a dated, zstd-compressed pg_dump made inside the
database's own container, read back whole before it gets its name, a week of them kept, and a
restore that replaces the database in one transaction; the backup step that ends each sync; what
`riffle status` says of the dumps; and, against a real Postgres, a dump restored into a scratch
database with the same counts."""

import functools
import hashlib
import json
import os
import re
import shutil
import subprocess
import uuid
from collections import namedtuple
from contextlib import nullcontext
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, insert, make_url, text
from sqlalchemy.exc import OperationalError
from typer.testing import CliRunner

import riffle.cli._common as cli_common
import riffle.cli.sync as cli_sync
from riffle import config, db, runs, times
from riffle.cli import app
from riffle.db import backup, compose, migrate
from riffle.db.models import Card, ExternalId, Format, Legality, MtgCard, MtgPrinting, Printing, Set
from riffle.ingest.remote import scryfall
from riffle.ops import status
from tests.cli.test_sync_failures import NOTE, ONLINE, before_closing, network, online_steps
from tests.conftest import PG_TEST_URL, docker_env
from tests.db.test_db_setup import NO_DOCKER
from tests.ops.test_status import quiet

AT = datetime(2026, 10, 23, 22, 15, 3, tzinfo=UTC)
NAME = "tcg-2026-10-23T221503Z.dump"  # tcg's dump made at AT
URL = "postgresql+psycopg://tcg@localhost:5432/tcg"
UNREACHABLE = "postgresql+psycopg://tcg@127.0.0.1:1/unreachable"  # what every test's config names
PREFIX = ["docker", "compose", "--file", "/repo/compose.yaml", "exec", "-T", "db"]
IDENTIFIER = 7364180862312345678  # a cluster's system identifier, as pg_control_system() gives it
COLUMNS = ("system_identifier", "current_user", "current_database")
DUMP = b"PGDMP\x01custom format, zstd"
PG_DUMP_ERROR = (
    b'pg_dump: error: connection to server on socket "/var/run/postgresql/.s.PGSQL.5432" failed:'
    b' FATAL:  database "tcg" does not exist\n'
)
NOT_AN_ARCHIVE = b"pg_restore: error: input file does not appear to be a valid archive\n"
NOT_HERE = (
    "database_url names a Postgres other than the one this machine runs in Docker ({url});"
    " {do} on the machine that runs it"
)
AS_IT_WAS = "; the database is as it was"


def _line(stderr: bytes) -> str:
    """A program's first line on stderr, as Failed carries it."""
    return stderr.decode().splitlines()[0]


# ---- the server database_url names, and the programs run in the container -------------------


class Server:
    """What the server database_url names answers, in place of riffle.db.engine: its system
    identifier, the user connected and the database opened, to a query naming any of them, as a
    row of those columns in the query's order."""

    def __init__(self, identifier: int = IDENTIFIER, user: str = "tcg", database: str = "tcg") -> None:
        self.answers = dict(zip(COLUMNS, (identifier, user, database), strict=True))
        self.asked: list[str] = []

    def engine(self, url: str | None = None) -> "_Engine":
        return _Engine(self, url or URL)

    def row(self, sql: str) -> tuple:
        self.asked.append(sql)
        fields = [c for _, c in sorted((sql.find(c), c) for c in COLUMNS if c in sql)]
        return namedtuple("Row", fields)(*(self.answers[c] for c in fields))


class _Engine:
    def __init__(self, server: Server, url: str) -> None:
        self.server, self.url = server, make_url(url)

    def connect(self) -> "_Connection":
        return _Connection(self.server)

    begin = connect

    def dispose(self) -> None:
        pass


class _Connection:
    def __init__(self, server: Server) -> None:
        self.server = server

    def __enter__(self) -> "_Connection":
        return self

    def __exit__(self, *exc: object) -> None:
        pass

    def close(self) -> None:
        pass

    def execution_options(self, **_: object) -> "_Connection":
        return self

    def begin(self) -> nullcontext:
        return nullcontext(self)

    def execute(self, statement: object, *_: object, **__: object) -> "_Result":
        return _Result(self.server.row(str(statement)))

    exec_driver_sql = execute

    def scalar(self, statement: object, *_: object, **__: object) -> object:
        return self.execute(statement).scalar_one()


class _Result:
    def __init__(self, row) -> None:
        self.row = row

    def one(self):
        return self.row

    first = fetchone = one

    def all(self):
        return [self.row]

    fetchall = all

    def __iter__(self):
        return iter([self.row])

    def scalar_one(self):
        return self.row[0]

    scalar = scalar_one_or_none = scalar_one

    def mappings(self) -> "_Result":
        return _Result(self.row._asdict())


@pytest.fixture
def server(monkeypatch) -> Server:
    """riffle.db.engine answers from a fake server whose identifier the container's psql gives too."""
    found = Server()
    monkeypatch.setattr(db, "engine", found.engine)
    return found


def _read(stdin, given) -> bytes | None:
    """What a program read on stdin: a file's bytes, the bytes given as input, or None for nothing."""
    if hasattr(stdin, "read"):
        return stdin.read()
    return given if isinstance(given, bytes) else None


class Container:
    """A fake runner for the programs run inside the database's container, in place of
    subprocess.run: psql answers the identifier, pg_dump writes the dump to its stdout, pg_restore
    and bash say nothing; any of them fails as told, with that stderr. Records every call's
    arguments, what each program was given on stdin, and, when pg_restore read a dump back,
    whether the .part was still there and the dump not yet named."""

    def __init__(self, identifier: int = IDENTIFIER, dump: bytes = DUMP, fail: dict | None = None) -> None:
        self.identifier, self.dump, self.fail = identifier, dump, fail or {}  # program: (code, stderr)
        self.calls: list[list[str]] = []
        self.stdin: dict[str, object] = {}
        self.given: dict[str, bytes | None] = {}
        self.checked: tuple[bool, bool] | None = None

    def programs(self) -> list[str]:
        return [args[len(PREFIX)] for args in self.calls]

    def __call__(self, args, stdin=None, stdout=None, stderr=None, **kwargs) -> subprocess.CompletedProcess:
        program = args[len(PREFIX)]
        self.calls.append(list(args))
        self.stdin[program] = stdin
        self.given[program] = _read(stdin, kwargs.get("input"))
        if program == "pg_restore" and hasattr(stdin, "name"):
            part = Path(stdin.name)
            self.checked = (part.exists(), part.with_suffix("").exists())
        code, err = self.fail.get(program, (0, b""))
        out = b""
        if code == 0 and program == "psql":
            out = f"{self.identifier}\n".encode()
        if code == 0 and program == "pg_dump":
            if hasattr(stdout, "write"):
                stdout.write(self.dump)
            else:
                out = self.dump
        return subprocess.CompletedProcess(args, code, out, err)


# ---- backup -----------------------------------------------------------------------------------


def test_the_dumps_folder_is_db_backups_under_the_data_folder_and_a_week_of_them_is_kept():
    assert backup.FOLDER == "db-backups" and backup.folder() == config.data_dir() / "db-backups"
    assert backup.KEEP / timedelta(days=1) == 7  # a week


def test_backup_dumps_the_database_in_its_container_and_reads_the_dump_back_before_naming_it(
    tmp_path, server
):
    box = Container()
    where = tmp_path / "db-backups"  # not there yet, as before the first backup
    dump = backup.backup(URL, where, AT, run=box, prefix=PREFIX)
    assert dump == backup.Dump(where / NAME, len(DUMP), 1)
    with pytest.raises(FrozenInstanceError):
        dump.kept = 0
    assert dump.path.read_bytes() == DUMP and sorted(p.name for p in where.iterdir()) == [
        NAME,
        f"{NAME}.sha256",
    ]
    probe, dumped, read_back = box.calls
    assert probe == [
        *PREFIX,
        "psql",
        "--no-psqlrc",
        "--tuples-only",
        "--no-align",
        "--quiet",
        "--username=tcg",
        "--dbname=tcg",
        "--command=SELECT system_identifier FROM pg_control_system()",
    ]
    assert dumped == [
        *PREFIX,
        "pg_dump",
        "--format=custom",
        "--compress=zstd",
        "--username=tcg",
        "--dbname=tcg",
    ]
    assert box.stdin["pg_dump"] is subprocess.DEVNULL
    assert read_back == [*PREFIX, "pg_restore", "--file=/dev/null"]
    assert box.given["pg_restore"] == DUMP and box.checked == (True, False)


def test_backup_prunes_the_database_s_dumps_older_than_a_week_and_counts_those_kept(tmp_path, server):
    old = tmp_path / f"tcg-{runs.name(AT - timedelta(days=8))}.dump"
    recent = tmp_path / f"tcg-{runs.name(AT - timedelta(days=1))}.dump"
    other = tmp_path / f"tcg_test-{runs.name(AT - timedelta(days=30))}.dump"
    for path in (old, recent, other):
        path.write_bytes(b"x")
    dump = backup.backup(URL, tmp_path, AT, run=Container(), prefix=PREFIX)
    assert dump.kept == 2
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted(
        [recent.name, other.name, NAME, f"{NAME}.sha256"]
    )


@pytest.mark.parametrize(
    ("fail", "said"),
    [
        ({"pg_dump": (1, PG_DUMP_ERROR + b"pg_dump: detail: ...\n")}, _line(PG_DUMP_ERROR)),
        ({"pg_dump": (2, b"")}, "exited 2"),
        ({"pg_restore": (1, NOT_AN_ARCHIVE)}, f"{NAME} doesn't read back whole ({_line(NOT_AN_ARCHIVE)})"),
    ],
    ids=["pg_dump fails", "pg_dump fails saying nothing", "the dump doesn't read back whole"],
)
def test_a_dump_that_fails_or_doesn_t_read_back_whole_never_gets_its_name_and_leaves_no_part(
    tmp_path, server, fail, said
):
    box = Container(fail=fail)
    with pytest.raises(backup.Failed) as e:
        backup.backup(URL, tmp_path, AT, run=box, prefix=PREFIX)
    assert str(e.value).endswith(said)
    assert list(tmp_path.iterdir()) == []
    assert box.programs() == ["psql", "pg_dump"] + (["pg_restore"] if "pg_restore" in fail else [])


def test_a_database_url_naming_another_postgres_than_docker_s_raises_not_here_with_its_password_masked(
    tmp_path, server
):
    box = Container(identifier=IDENTIFIER + 1)
    url = "postgresql+psycopg://tcg:secret@db.example.net:5432/tcg"
    with pytest.raises(backup.NotHere) as e:
        backup.backup(url, tmp_path, AT, run=box, prefix=PREFIX)
    shown = "postgresql+psycopg://tcg:***@db.example.net:5432/tcg"
    assert str(e.value) == NOT_HERE.format(url=shown, do="back it up")
    assert box.programs() == ["psql"] and list(tmp_path.iterdir()) == []


def test_a_probe_that_fails_raises_failed_with_docker_s_or_psql_s_line(tmp_path, server):
    box = Container(fail={"psql": (1, b'service "db" is not running\n')})
    with pytest.raises(backup.Failed) as e:
        backup.backup(URL, tmp_path, AT, run=box, prefix=PREFIX)
    assert str(e.value).endswith('service "db" is not running') and box.programs() == ["psql"]


def test_no_docker_raises_not_here_with_compose_s_own_message(tmp_path, server, monkeypatch):
    def no_docker(*args, **kwargs):
        raise FileNotFoundError(NO_DOCKER)

    monkeypatch.setattr(compose, "in_db", no_docker)
    box = Container()
    with pytest.raises(backup.NotHere) as e:
        backup.backup(URL, tmp_path, AT, run=box)
    assert str(e.value) == NO_DOCKER
    dump = tmp_path / NAME
    dump.write_bytes(DUMP)
    with pytest.raises(backup.NotHere) as e:
        backup.restore(URL, dump, run=box)
    assert str(e.value) == NO_DOCKER and box.calls == []


def test_an_unreachable_server_raises_sqlalchemy_s_operational_error_unchanged(tmp_path):
    box = Container()
    with pytest.raises(OperationalError):
        backup.backup(UNREACHABLE, tmp_path, AT, run=box, prefix=PREFIX)
    assert box.calls == [] and list(tmp_path.iterdir()) == []


# ---- a dump's name, the folder's dumps, and pruning ------------------------------------------


def test_each_dump_has_its_sha256_beside_it_as_sha256sum_writes_it(tmp_path, server):
    dump = backup.backup(URL, tmp_path, AT, run=Container(), prefix=PREFIX)
    assert backup.checksum(dump.path) == tmp_path / f"{NAME}.sha256"
    assert backup.checksum(dump.path).read_text() == f"{hashlib.sha256(DUMP).hexdigest()}  {NAME}\n"


@pytest.mark.parametrize(
    ("program", "said"),
    [
        ("psql", "the database's container gave no answer in 60 s"),
        ("pg_dump", "pg_dump took longer than 30 minutes"),
        ("pg_restore", f"{NAME} took longer than 30 minutes to read back"),
    ],
)
def test_a_program_that_doesn_t_finish_in_time_fails_the_backup_and_leaves_nothing(
    tmp_path, server, program, said
):
    box = Container()

    def run(args, **kwargs):
        if args[len(PREFIX)] == program:
            raise subprocess.TimeoutExpired(args, kwargs["timeout"])
        return box(args, **kwargs)

    with pytest.raises(backup.Failed) as e:
        backup.backup(URL, tmp_path, AT, run=run, prefix=PREFIX)
    assert str(e.value) == said and list(tmp_path.iterdir()) == []


def test_compose_s_own_warnings_aren_t_taken_for_the_failure(tmp_path, server):
    warned = b'time="2026-10-23T16:15:03-06:00" level=warning msg="The X variable is not set."\n'
    box = Container(fail={"psql": (1, warned + b'service "db" is not running\n')})
    with pytest.raises(backup.Failed) as e:
        backup.backup(URL, tmp_path, AT, run=box, prefix=PREFIX)
    assert str(e.value) == 'service "db" is not running'


def test_a_dump_is_kept_and_says_why_when_older_ones_can_t_be_removed(tmp_path, server, monkeypatch):
    def refused(*args, **kwargs):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(backup, "prune", refused)
    dump = backup.backup(URL, tmp_path, AT, run=Container(), prefix=PREFIX)
    assert dump.path.exists() and dump.kept == 1 and dump.not_removed == "[Errno 13] Permission denied"


def test_a_dump_s_name_says_when_it_was_made_and_nothing_else_s_does():
    assert backup.made(Path(NAME)) == AT
    assert backup.made(Path(f"{NAME}.part")) is None
    assert backup.made(Path("notes.txt")) is None and backup.made(Path("tcg-today.dump")) is None


def test_dumps_are_the_database_s_own_oldest_first_and_nothing_else_in_the_folder(tmp_path):
    names = [f"tcg-{runs.name(AT - timedelta(days=d))}.dump" for d in (2, 0, 1)]
    others = [f"tcg_test-{runs.name(AT)}.dump", f"{NAME}.part", "notes.txt", f"tcg-{runs.name(AT)}.txt"]
    for name in names + others:
        (tmp_path / name).write_bytes(b"")
    (tmp_path / "old").mkdir()
    assert backup.dumps("tcg", tmp_path) == [tmp_path / name for name in sorted(names)]
    assert backup.dumps("tcg", tmp_path / "none") == []


def test_prune_removes_the_database_s_dumps_older_than_a_week_and_keeps_the_rest(tmp_path):
    ago = {"8 days": timedelta(days=8), "6 days": timedelta(days=6), "now": timedelta()}
    paths = {age: tmp_path / f"tcg-{runs.name(AT - before)}.dump" for age, before in ago.items()}
    for path in paths.values():
        path.write_bytes(b"x")
    assert backup.prune("tcg", tmp_path, AT) == [paths["8 days"]]
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted([paths["6 days"].name, paths["now"].name])


def test_prune_never_removes_the_database_s_newest_dump_however_old(tmp_path):
    older = tmp_path / f"tcg-{runs.name(AT - timedelta(days=40))}.dump"
    newest = tmp_path / f"tcg-{runs.name(AT - timedelta(days=30))}.dump"
    for path in (older, newest):
        path.write_bytes(b"x")
    assert backup.prune("tcg", tmp_path, AT) == [older]
    assert [p.name for p in tmp_path.iterdir()] == [newest.name]


def test_prune_removes_the_database_s_part_files_and_touches_nothing_else_in_the_folder(tmp_path):
    old = runs.name(AT - timedelta(days=30))
    stays = [f"tcg_test-{old}.dump", f"tcg_test-{old}.dump.part", "notes.txt", f"tcg-{old}.txt", NAME]
    goes = [f"tcg-{old}.dump.part", f"tcg-{runs.name(AT - timedelta(hours=1))}.dump.part"]
    for name in stays + goes:
        (tmp_path / name).write_bytes(b"x")
    (tmp_path / "old").mkdir()
    assert backup.prune("tcg", tmp_path, AT) == []  # a .part isn't a dump
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted([*stays, "old"])


# ---- restore ----------------------------------------------------------------------------------

SCRIPT_IN_ORDER = (
    "BEGIN;",
    "DROP SCHEMA public CASCADE;",
    "CREATE SCHEMA public AUTHORIZATION pg_database_owner;",
    "GRANT USAGE ON SCHEMA public TO PUBLIC;",
    "COMMENT ON SCHEMA public IS 'standard public schema';",
    "pg_restore --no-owner --no-privileges --file=-",
    "COMMIT;",
)
PSQL_FLAGS = ("--no-psqlrc", "--quiet", "--set=ON_ERROR_STOP=1", '--username="$1"', '--dbname="$2"')


def test_prune_removes_a_dump_s_sha256_with_it_and_one_whose_dump_is_gone(tmp_path):
    old = tmp_path / f"tcg-{runs.name(AT - timedelta(days=8))}.dump"
    new = tmp_path / NAME
    taken = tmp_path / f"tcg-{runs.name(AT - timedelta(days=2))}.dump"  # its dump removed by hand
    other = tmp_path / f"tcg_test-{runs.name(AT - timedelta(days=30))}.dump"
    for path in (old, new, other):
        path.write_bytes(b"x")
    for path in (old, new, taken, other):
        backup.checksum(path).write_text("")
    assert backup.prune("tcg", tmp_path, AT) == [old]
    left = [new.name, f"{new.name}.sha256", other.name, f"{other.name}.sha256"]
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted(left)


@pytest.fixture
def dump(tmp_path) -> Path:
    path = tmp_path / NAME
    path.write_bytes(DUMP)
    return path


def test_restore_reads_the_dump_back_before_loading_and_loads_nothing_when_that_fails(dump, server):
    box = Container(fail={"pg_restore": (1, NOT_AN_ARCHIVE)})
    with pytest.raises(backup.Failed) as e:
        backup.restore(URL, dump, run=box, prefix=PREFIX)
    assert str(e.value) == f"{NAME} doesn't read back whole ({_line(NOT_AN_ARCHIVE)}){AS_IT_WAS}"
    assert box.programs() == ["psql", "pg_restore"]
    assert box.calls[1] == [*PREFIX, "pg_restore", "--file=/dev/null"] and box.given["pg_restore"] == DUMP


def test_restore_loads_the_dump_in_one_transaction_as_the_user_and_into_the_database_the_server_names(
    dump, server
):
    box = Container()
    # no database in the URL: the server says which one it opened, and as whom
    assert backup.restore("postgresql+psycopg://tcg@localhost:5432", dump, run=box, prefix=PREFIX) is None
    assert box.programs() == ["psql", "pg_restore", "bash"]
    *_v, script, name, user, database = box.calls[2]
    assert box.calls[2][: len(PREFIX) + 2] == [*PREFIX, "bash", "-c"]
    assert (name, user, database) == ("riffle-restore", "tcg", "tcg") and box.given["bash"] == DUMP
    assert "set -eo pipefail" in script and all(flag in script for flag in PSQL_FLAGS)
    places = [script.index(line) for line in SCRIPT_IN_ORDER]
    assert places == sorted(places)


@pytest.mark.parametrize(
    ("fail", "said"),
    [
        ((3, b'ERROR:  relation "cards" already exists\nLINE 1: CREATE TABLE cards (\n'), _line),
        ((137, b""), lambda _: "exited 137"),
    ],
    ids=["an error", "killed saying nothing"],
)
def test_a_failed_load_raises_failed_ending_the_database_is_as_it_was(dump, server, fail, said):
    box = Container(fail={"bash": fail})
    with pytest.raises(backup.Failed) as e:
        backup.restore(URL, dump, run=box, prefix=PREFIX)
    assert str(e.value) == f"{said(fail[1])}{AS_IT_WAS}" and box.programs() == ["psql", "pg_restore", "bash"]


def test_restore_refuses_a_database_url_naming_another_postgres_than_docker_s(dump, server):
    box = Container(identifier=IDENTIFIER + 1)
    with pytest.raises(backup.NotHere) as e:
        backup.restore("postgresql+psycopg://tcg:secret@reef:5432/tcg", dump, run=box, prefix=PREFIX)
    assert str(e.value) == NOT_HERE.format(url="postgresql+psycopg://tcg:***@reef:5432/tcg", do="restore it")
    assert box.programs() == ["psql"]


# ---- the commands -----------------------------------------------------------------------------


def test_a_restore_refuses_a_dump_that_doesn_t_match_its_sha256_and_loads_nothing(dump, server):
    backup.checksum(dump).write_text(f"{'0' * 64}  {NAME}\n")
    box = Container()
    with pytest.raises(backup.Failed) as e:
        backup.restore(URL, dump, run=box, prefix=PREFIX)
    changed = f"{NAME} has changed since it was made: its SHA-256 isn't the one in {NAME}.sha256"
    assert str(e.value) == changed + AS_IT_WAS and box.programs() == ["psql"]


def test_a_restore_goes_on_when_the_dump_matches_its_sha256(dump, server):
    backup.checksum(dump).write_text(f"{hashlib.sha256(DUMP).hexdigest()}  {NAME}\n")
    box = Container()
    backup.restore(URL, dump, run=box, prefix=PREFIX)
    assert box.programs() == ["psql", "pg_restore", "bash"]


def test_the_load_names_itself_and_waits_a_minute_at_most_for_a_lock():
    assert f"PGAPPNAME={backup.LOADER} psql " in backup.LOAD
    assert backup.LOAD.index("SET lock_timeout = '1min';") < backup.LOAD.index("DROP SCHEMA public CASCADE;")


@pytest.mark.parametrize(
    ("stop", "raised", "said"),
    [
        (KeyboardInterrupt(), KeyboardInterrupt, ""),
        (
            subprocess.TimeoutExpired(["bash"], 1800),
            backup.Failed,
            f"the load took longer than 30 minutes{AS_IT_WAS}",
        ),
    ],
    ids=["Ctrl-C", "the time limit"],
)
def test_a_load_cut_short_is_ended_on_the_server_so_that_it_rolls_back(dump, server, stop, raised, said):
    box = Container()

    def run(args, **kwargs):
        if args[len(PREFIX)] == "bash":
            raise stop
        return box(args, **kwargs)

    with pytest.raises(raised) as e:
        backup.restore(URL, dump, run=run, prefix=PREFIX)
    assert str(e.value) == said
    [ended] = [sql for sql in server.asked if "pg_terminate_backend" in sql]
    assert "WHERE application_name = :app" in ended


ELSEWHERE = NOT_HERE.format(url="postgresql+psycopg://tcg@reef:5432/tcg", do="back it up")
ELSEWHERE_TO_RESTORE = NOT_HERE.format(url="postgresql+psycopg://tcg@reef:5432/tcg", do="restore it")
UNREACHABLE_SAID = (
    rf"not {{done}}: can't reach Postgres at {re.escape(UNREACHABLE)}: .+; start it with: riffle db up"
)


def _holder() -> str:
    """Who holds the run lock, by the command written in it."""
    return json.loads(cli_common._lock_path().read_text(encoding="utf-8"))["command"]


@pytest.fixture
def backed_up(monkeypatch) -> SimpleNamespace:
    """backup.backup as the commands call it, answering as told (a Dump, or an error raised): records
    each call's URL, folder and time, the run lock's holder at the time, and its turn in `sequence`
    among whatever else is recorded there."""
    made = SimpleNamespace(calls=[], answer=None, held=[], sequence=[])

    def fake(url, where, at, run=subprocess.run, prefix=None):
        made.calls.append((url, where, at))
        made.held.append(_holder())
        made.sequence.append("backup")
        if isinstance(made.answer, Exception):
            raise made.answer
        return made.answer

    monkeypatch.setattr(backup, "backup", fake)
    return made


@pytest.fixture
def restored(monkeypatch) -> SimpleNamespace:
    """backup.restore as the command calls it, raising the error told: records each call's URL and
    dump, and the run lock's holder at the time."""
    made = SimpleNamespace(calls=[], answer=None, held=[])

    def fake(url, dump, run=subprocess.run, prefix=None):
        made.calls.append((url, dump))
        made.held.append(_holder())
        if isinstance(made.answer, Exception):
            raise made.answer

    monkeypatch.setattr(backup, "restore", fake)
    return made


def _args(command: str, dump: Path) -> list[str]:
    return ["db", "backup"] if command == "backup" else ["db", "restore", str(dump), "--yes"]


def test_db_backup_says_the_dump_its_size_that_it_read_back_whole_and_how_many_are_kept(backed_up):
    path = backup.folder() / NAME
    backed_up.answer = backup.Dump(path, 41_200_000, 14)
    result = CliRunner().invoke(app, ["db", "backup"])
    assert result.exit_code == 0, result.output
    assert config.tilde(path).startswith("~/")
    assert f"database backup: {config.tilde(path)}, 41.2 MB, read back whole; 14 kept" in result.output
    [(url, where, at)] = backed_up.calls
    assert (url, where) == (UNREACHABLE, backup.folder()) and abs(times.now() - at) < timedelta(minutes=1)
    assert backed_up.held == ["riffle db backup"]


@pytest.mark.parametrize(
    ("error", "said"),
    [
        (backup.NotHere(ELSEWHERE), ELSEWHERE),
        (backup.Failed(_line(PG_DUMP_ERROR)), _line(PG_DUMP_ERROR)),
        (OSError(28, "No space left on device"), "[Errno 28] No space left on device"),
    ],
    ids=["NotHere", "Failed", "OSError"],
)
def test_db_backup_fails_its_step_and_exits_1_when_the_dump_can_t_be_made(backed_up, error, said):
    backed_up.answer = error
    result = CliRunner().invoke(app, ["db", "backup"])
    assert result.exit_code == 1, result.output
    assert f"! database backup: not backed up: {said}" in result.output
    assert before_closing(result.output) == "1 step failed: database backup"


@pytest.mark.parametrize(
    ("command", "step", "done"),
    [("backup", "database backup", "backed up"), ("restore", "database restore", "restored")],
)
def test_db_backup_and_restore_say_how_to_start_an_unreachable_server(dump, monkeypatch, command, step, done):
    monkeypatch.setattr(compose, "in_db", lambda *args, **kwargs: PREFIX)
    result = CliRunner().invoke(app, _args(command, dump))
    assert result.exit_code == 1, result.output
    assert re.search(rf"! {step}: {UNREACHABLE_SAID.format(done=done)}", result.output), result.output
    assert before_closing(result.output) == f"1 step failed: {step}"


@pytest.mark.parametrize(
    ("command", "step", "done"),
    [("backup", "database backup", "backed up"), ("restore", "database restore", "restored")],
)
def test_db_backup_and_restore_name_the_config_when_database_url_can_t_be_used(dump, command, step, done):
    config.config_path().write_text('database_url = "not a url"\n', encoding="utf-8")
    result = CliRunner().invoke(app, _args(command, dump))
    assert result.exit_code == 1, result.output
    assert f"! {step}: not {done}: database_url in {config.config_path()} can't be used: " in result.output
    assert before_closing(result.output) == f"1 step failed: {step}"


def test_db_restore_needs_a_dump_that_exists(tmp_path, restored, plain):
    result = CliRunner().invoke(app, ["db", "restore", str(tmp_path / "none.dump"), "--yes"])
    assert result.exit_code == 2
    assert "does not exist" in plain(result.output) and restored.calls == []


def test_db_restore_without_yes_and_no_terminal_says_how_to_be_asked_and_runs_nothing(
    dump, restored, monkeypatch
):
    monkeypatch.setattr(cli_common, "_interactive", lambda: False)
    result = CliRunner().invoke(app, ["db", "restore", str(dump)])
    assert result.exit_code == 2, result.output
    said = "riffle db restore replaces the database: run it at a terminal to be asked first, or add --yes"
    assert said in result.stderr and "database restore" not in result.output
    assert restored.calls == []


@pytest.mark.parametrize(
    ("name", "made", "answer"),
    [(NAME, " (made 2026-10-23 22:15 UTC)", "\n"), ("notes.dump", "", "n\n")],
    ids=["a dated dump, the default answer", "an undated file, answering no"],
)
def test_db_restore_at_a_terminal_asks_first_and_no_runs_nothing(
    tmp_path, restored, monkeypatch, name, made, answer
):
    path = tmp_path / name
    path.write_bytes(DUMP)
    monkeypatch.setattr(cli_common, "_interactive", lambda: True)
    result = CliRunner().invoke(app, ["db", "restore", str(path)], input=answer)
    assert result.exit_code == 1, result.output
    assert f"Replace everything in {UNREACHABLE} with {name}{made}? [y/N]: " in result.output
    assert "Aborted" in result.output and restored.calls == []


def test_db_restore_at_a_terminal_answering_yes_goes_on(dump, restored, monkeypatch):
    monkeypatch.setattr(cli_common, "_interactive", lambda: True)
    monkeypatch.setattr(migrate, "current", lambda engine: migrate.head())
    result = CliRunner().invoke(app, ["db", "restore", str(dump)], input="y\n")
    assert result.exit_code == 0, result.output
    assert restored.calls == [(UNREACHABLE, dump)]


def test_db_restore_asks_about_a_database_url_it_can_t_read_without_showing_it(dump, restored, monkeypatch):
    config.config_path().write_text('database_url = "not a url"\n', encoding="utf-8")
    monkeypatch.setattr(cli_common, "_interactive", lambda: True)
    result = CliRunner().invoke(app, ["db", "restore", str(dump)], input="n\n")
    assert result.exit_code == 1, result.output
    asked = f"Replace everything in the database database_url names with {NAME} (made 2026-10-23 22:15 UTC)?"
    assert asked in result.output and "Aborted" in result.output and restored.calls == []


@pytest.mark.parametrize("older", [False, True], ids=["at head", "an older schema"])
def test_db_restore_with_yes_restores_and_says_the_schema_the_dump_carried(
    dump, restored, monkeypatch, older
):
    head = migrate.head()
    monkeypatch.setattr(migrate, "current", lambda engine: "0001" if older else head)
    result = CliRunner().invoke(app, ["db", "restore", str(dump), "--yes"])
    assert result.exit_code == 0, result.output
    assert "Replace everything" not in result.output
    done = f"database restore: {NAME} into {UNREACHABLE}"
    schema = (
        f"schema     0001, head is {head} — run: riffle db upgrade" if older else f"schema     {head} (head)"
    )
    assert done in result.output and schema in result.output, result.output
    assert result.output.index(done) < result.output.index(schema)
    assert restored.calls == [(UNREACHABLE, dump)]
    [held] = restored.held
    assert held.startswith("riffle db restore")


@pytest.mark.parametrize(
    ("error", "said"),
    [
        (backup.NotHere(ELSEWHERE_TO_RESTORE), ELSEWHERE_TO_RESTORE),
        (
            backup.Failed(f"{NAME} doesn't read back whole ({_line(NOT_AN_ARCHIVE)}){AS_IT_WAS}"),
            f"{NAME} doesn't read back whole ({_line(NOT_AN_ARCHIVE)}){AS_IT_WAS}",
        ),
        (OSError(13, "Permission denied"), "[Errno 13] Permission denied"),
    ],
    ids=["NotHere", "Failed", "OSError"],
)
def test_db_restore_fails_its_step_and_exits_1_when_the_restore_can_t_be_made(dump, restored, error, said):
    restored.answer = error
    result = CliRunner().invoke(app, ["db", "restore", str(dump), "--yes"])
    assert result.exit_code == 1, result.output
    assert f"! database restore: not restored: {said}" in result.output
    assert before_closing(result.output) == "1 step failed: database restore"


def test_db_backup_warns_and_exits_0_when_older_dumps_can_t_be_removed(backed_up):
    path = backup.folder() / NAME
    backed_up.answer = backup.Dump(path, 41_200_000, 15, "[Errno 13] Permission denied")
    result = CliRunner().invoke(app, ["db", "backup"])
    assert result.exit_code == 0, result.output
    said = (
        f"{config.tilde(path)}, 41.2 MB, read back whole; 15 kept;"
        " older dumps not removed: [Errno 13] Permission denied"
    )
    assert f"warning: database backup: {said}" in result.output and "step failed" not in result.output


def test_db_restore_says_the_schema_is_unknown_when_the_server_goes_away_after(dump, restored, monkeypatch):
    def gone(engine):
        raise OperationalError("SELECT", {}, Exception("server closed the connection unexpectedly"))

    monkeypatch.setattr(migrate, "current", gone)
    result = CliRunner().invoke(app, ["db", "restore", str(dump), "--yes"])
    assert result.exit_code == 0, result.output
    unknown = f"schema     unknown: can't reach Postgres at {UNREACHABLE}: server closed the connection"
    assert unknown in result.output


# ---- the sync's last step ---------------------------------------------------------------------


@pytest.fixture
def deck(opened, monkeypatch) -> Path:
    """One deck note in the vault every test's config names, the catalog in memory and a price day
    kept, so a sync has its vault half to do; returns the deck notes' mtg/."""
    mtg = Path.home() / "vault" / "games" / "tcg" / "mtg"
    (mtg / "modern").mkdir(parents=True)
    (mtg / "modern" / "burn.md").write_text(NOTE)
    monkeypatch.setattr(scryfall, "snapshot_prices", lambda: (Path("/d/2026-09-26.jsonl.gz"), False))
    return mtg


def test_a_sync_ends_with_the_database_backup_step_after_everything_else_it_does(
    deck, backed_up, monkeypatch
):
    network(monkeypatch, up_after=0)
    calls: list[str] = []
    online_steps(monkeypatch, calls)
    backed_up.sequence = calls
    path = backup.folder() / NAME
    backed_up.answer = backup.Dump(path, 41_200_000, 14)
    result = CliRunner().invoke(app, ["sync"])
    assert result.exit_code == 0, result.output
    assert calls == [*ONLINE, "backup"]
    said = f"database backup: {config.tilde(path)}, 41.2 MB, read back whole; 14 kept"
    assert said in result.output and result.output.index("decks ·") < result.output.index(said)
    assert (deck / "_generated" / "burn-data.md").exists()
    [(url, where, _v)] = backed_up.calls
    assert (url, where) == (UNREACHABLE, backup.folder()) and backed_up.held == ["riffle sync"]


def test_an_offline_sync_and_a_resync_make_no_backup(deck, backed_up):
    result = CliRunner().invoke(app, ["sync", "--offline"])
    assert result.exit_code == 0, result.output
    assert "database backup" not in result.output and backed_up.calls == []
    cli_sync._resync()
    assert backed_up.calls == []


def test_a_database_this_machine_doesn_t_run_is_a_warning_in_the_sync_not_a_failure(
    deck, backed_up, monkeypatch
):
    network(monkeypatch, up_after=0)
    online_steps(monkeypatch, [])
    backed_up.answer = backup.NotHere(ELSEWHERE)
    result = CliRunner().invoke(app, ["sync"])
    assert result.exit_code == 0, result.output
    assert f"warning: database backup: not backed up: {ELSEWHERE}" in result.output
    assert "step failed" not in result.output


@pytest.mark.parametrize(
    ("error", "said"),
    [
        (backup.Failed(_line(PG_DUMP_ERROR)), _line(PG_DUMP_ERROR)),
        (OSError(28, "No space left on device"), "[Errno 28] No space left on device"),
    ],
    ids=["Failed", "OSError"],
)
def test_any_other_backup_failure_fails_the_sync_s_step_after_its_other_work(
    deck, backed_up, monkeypatch, error, said
):
    network(monkeypatch, up_after=0)
    calls: list[str] = []
    online_steps(monkeypatch, calls)
    backed_up.sequence = calls
    backed_up.answer = error
    result = CliRunner().invoke(app, ["sync"])
    assert result.exit_code == 1, result.output
    assert calls == [*ONLINE, "backup"]
    assert f"! database backup: not backed up: {said}" in result.output
    assert "1 decks · 2 notes updated" in result.output and (deck / "_generated" / "burn-data.md").exists()
    assert before_closing(result.output) == "1 step failed: database backup"


def test_an_unreachable_database_fails_the_sync_s_backup_step(deck, monkeypatch):
    monkeypatch.setattr(compose, "in_db", lambda *args, **kwargs: PREFIX)
    network(monkeypatch, up_after=0)
    online_steps(monkeypatch, [])
    result = CliRunner().invoke(app, ["sync"])
    assert result.exit_code == 1, result.output
    assert re.search(rf"! database backup: {UNREACHABLE_SAID.format(done='backed up')}", result.output)
    assert before_closing(result.output) == "1 step failed: database backup"


# ---- riffle status ----------------------------------------------------------------------------


def test_the_db_backup_line_names_the_newest_dump_its_size_and_how_many_the_folder_holds(on_a_terminal):
    folder = backup.folder()
    folder.mkdir(parents=True)
    sizes = {
        NAME: 41_200_000,
        f"tcg-{runs.name(AT - timedelta(days=2))}.dump": 40_000_000,
        f"tcg_test-{runs.name(AT - timedelta(hours=1))}.dump": 1_000,
    }
    for name, size in sizes.items():
        with (folder / name).open("wb") as f:
            f.truncate(size)
    (folder / f"tcg-{runs.name(AT + timedelta(hours=1))}.dump.part").write_bytes(b"half")  # not a dump
    (folder / "notes.txt").write_text("")
    assert (
        status.db_backup_line(folder) == f"newest 2026-10-23 16:15 MDT, 41.2 MB; 3 in {config.tilde(folder)}"
    )


@pytest.mark.parametrize("there", [False, True], ids=["no folder", "a folder with no dump"])
def test_the_db_backup_line_says_none_yet_and_what_makes_one(there):
    folder = backup.folder()
    if there:
        folder.mkdir(parents=True)
        (folder / f"{NAME}.part").write_bytes(b"")
    assert status.db_backup_line(folder) == "none yet: riffle db backup makes one, and each sync after it"


def test_riffle_status_shows_the_db_backup_line_under_its_label_after_backup(monkeypatch):
    quiet(monkeypatch)
    result = CliRunner().invoke(app, ["status", "--days", "3"])
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert lines[-2] == "backup        Time Machine to Backup Disk"
    assert lines[-1] == "db backup     none yet: riffle db backup makes one, and each sync after it"


# ---- done when: a dump restores into a scratch database with the same counts ------------------

TABLES = (
    "games",
    "formats",
    "sets",
    "cards",
    "printings",
    "legalities",
    "external_ids",
    "mtg_cards",
    "mtg_printings",
)
SCRATCH = ("tcg_backup_test", "tcg_restore_test")


def _fill(conn) -> None:
    """A row in every table a migrated database has, beyond the games the migration seeds."""
    card_id, set_id, printing_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    conn.execute(insert(Format).values(game_id="mtg", format="commander", name="Commander"))
    conn.execute(
        insert(Set).values(set_id=set_id, game_id="mtg", code="m3c", name="Modern Horizons 3 Commander")
    )
    conn.execute(insert(Card).values(card_id=card_id, game_id="mtg", name="Sol Ring", type_line="Artifact"))
    conn.execute(
        insert(Printing).values(
            printing_id=printing_id, game_id="mtg", card_id=card_id, set_id=set_id, collector_number="283"
        )
    )
    conn.execute(insert(Legality).values(game_id="mtg", format="commander", card_id=card_id, status="legal"))
    conn.execute(
        insert(ExternalId).values(
            source="scryfall", external_id="s-sol", game_id="mtg", printing_id=printing_id, last_seen=AT
        )
    )
    conn.execute(
        insert(MtgCard).values(
            card_id=card_id,
            mana_cost="{1}",
            mana_value=1,
            colors=[],
            color_identity=[],
            keywords=[],
            layout="normal",
            reserved=False,
        )
    )
    conn.execute(
        insert(MtgPrinting).values(printing_id=printing_id, finishes=["nonfoil"], promo=False, digital=False)
    )


def _counts(engine) -> tuple[dict[str, int], str | None]:
    """Each table of public with how many rows it holds, and the schema's revision."""
    with engine.connect() as conn:
        tables = conn.execute(
            text("SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename")
        ).scalars()
        found = {
            table: conn.execute(text(f'SELECT count(*) FROM "{table}"')).scalar_one() for table in tables
        }
    return found, migrate.current(engine)


@pytest.fixture
def container(pg_engine) -> list[str]:
    """The start of a command run in the test server's own container, found as the one publishing
    the test database's port; skips without docker or the container, fails instead under
    RIFFLE_REQUIRE_POSTGRES."""
    port = make_url(PG_TEST_URL).port or 5432
    why, found = None, []
    if shutil.which("docker") is None:
        why = "docker isn't on PATH"
    else:
        listed = subprocess.run(
            ["docker", "ps", "--filter", f"publish={port}", "--format", "{{.ID}}"],
            capture_output=True,
            text=True,
            env=docker_env(),
            timeout=60,
        )
        found = listed.stdout.split()
        if listed.returncode != 0:
            why = f"docker ps failed: {listed.stderr.strip()}"
        elif not found:
            why = f"no container publishes port {port}"
    if why is not None:
        if os.environ.get("RIFFLE_REQUIRE_POSTGRES"):
            pytest.fail(why)
        pytest.skip(why)
    return ["docker", "exec", "-i", found[0]]


@pytest.fixture
def docker_run():
    """subprocess.run with the user's own Docker config, which every test's temporary HOME hides."""
    return functools.partial(subprocess.run, env=docker_env())


@pytest.fixture
def scratch(pg_engine):
    """Two scratch databases on the test server, each migrated to head with a row in every table,
    as (url, engine): the source, and a target that also holds a second format and a table the
    source lacks. Both are dropped afterwards; the pg_engine fixture has seen to the server."""
    url = make_url(PG_TEST_URL)
    admin = create_engine(
        url.set(database="postgres"), isolation_level="AUTOCOMMIT", connect_args={"connect_timeout": 3}
    )
    engines = []
    try:
        with admin.connect() as conn:
            for name in SCRATCH:
                conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
                conn.execute(text(f'CREATE DATABASE "{name}"'))
        engines = [create_engine(url.set(database=name)) for name in SCRATCH]
        source, target = engines
        for engine in engines:
            migrate.upgrade(engine)
            with engine.begin() as conn:
                _fill(conn)
        with target.begin() as conn:
            conn.execute(insert(Format).values(game_id="mtg", format="modern", name="Modern"))
            conn.execute(text("CREATE TABLE extra (n integer)"))
            conn.execute(text("INSERT INTO extra VALUES (1)"))
        yield [
            (url.set(database=name).render_as_string(hide_password=False), e)
            for name, e in zip(SCRATCH, engines, strict=True)
        ]
    finally:
        for engine in engines:
            engine.dispose()
        with admin.connect() as conn:
            for name in SCRATCH:
                conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


@pytest.mark.postgres
def test_a_dump_restores_into_a_scratch_database_with_the_same_counts_and_revision(
    tmp_path, scratch, container, docker_run
):
    (source_url, source), (target_url, target) = scratch
    dump = backup.backup(source_url, tmp_path / "db-backups", AT, run=docker_run, prefix=container)
    assert dump.path == tmp_path / "db-backups" / f"{SCRATCH[0]}-{runs.name(AT)}.dump" and dump.kept == 1
    assert dump.size == dump.path.stat().st_size > 0
    counts, revision = _counts(source)
    assert set(TABLES) <= set(counts) and all(counts[table] > 0 for table in TABLES)
    assert revision == migrate.head() and _counts(target) != (counts, revision)
    assert backup.restore(target_url, dump.path, run=docker_run, prefix=container) is None
    assert _counts(target) == (counts, revision)


@pytest.mark.postgres
def test_a_restore_over_a_database_with_an_extra_table_leaves_only_the_dump_s_tables(
    tmp_path, scratch, container, docker_run
):
    (source_url, _v), (target_url, target) = scratch
    assert "extra" in _counts(target)[0]
    dump = backup.backup(source_url, tmp_path, AT, run=docker_run, prefix=container)
    backup.restore(target_url, dump.path, run=docker_run, prefix=container)
    counts, _v = _counts(target)
    assert "extra" not in counts and set(TABLES) <= set(counts)


@pytest.mark.postgres
def test_a_load_from_a_file_that_isn_t_a_dump_fails_and_leaves_the_target_as_it_was(
    tmp_path, scratch, container, docker_run
):
    _v, (_v, target) = scratch
    before = _counts(target)
    not_a_dump = tmp_path / "notes.dump"
    not_a_dump.write_bytes(b"not a dump at all\n")
    with pytest.raises(backup.Failed) as e:
        backup.load(make_url(PG_TEST_URL).username, SCRATCH[1], not_a_dump, docker_run, container)
    assert str(e.value).endswith(AS_IT_WAS)
    assert _counts(target) == before


@pytest.mark.postgres
def test_a_dump_psql_can_t_load_leaves_the_target_as_it_was(tmp_path, scratch, container, docker_run):
    (source_url, source), (target_url, target) = scratch
    for engine in (source, target):
        with engine.begin() as conn:
            conn.execute(text("CREATE SCHEMA aside"))
    with source.begin() as conn:
        conn.execute(text("CREATE TABLE aside.notes (n integer)"))
    before = _counts(target)
    dump = backup.backup(source_url, tmp_path, AT, run=docker_run, prefix=container)
    with pytest.raises(backup.Failed) as e:
        backup.restore(target_url, dump.path, run=docker_run, prefix=container)
    assert 'schema "aside" already exists' in str(e.value) and str(e.value).endswith(AS_IT_WAS)
    assert _counts(target) == before
