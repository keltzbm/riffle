"""Database setup that needs no server: config, migration scripts, `db up`, `db status` errors."""

import subprocess

import pytest
from typer.testing import CliRunner

from riffle import config, db
from riffle.cli import app
from riffle.db import compose, migrate


def test_database_url_defaults_to_local_postgres_without_a_password(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert config.load().database_url == "postgresql+psycopg://tcg@localhost:5432/tcg"
    config.write_default()
    assert config.load().database_url == config.DEFAULT_DATABASE_URL  # the written file says the same


def test_database_url_from_config(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    config.write_default().write_text('database_url = "postgresql+psycopg://tcg@server:5432/tcg"\n')
    assert config.load().database_url == "postgresql+psycopg://tcg@server:5432/tcg"


def test_display_masks_a_password():
    assert db.display("postgresql+psycopg://tcg:secret@host/tcg") == "postgresql+psycopg://tcg:***@host/tcg"
    assert db.display("postgresql+psycopg://tcg@host/tcg") == "postgresql+psycopg://tcg@host/tcg"


def test_migrations_form_one_line_from_the_first():
    """One head, one base, and every revision after the first builds on exactly one other."""
    revs = list(migrate.scripts().walk_revisions())
    assert migrate.head() == revs[0].revision
    assert [r.down_revision for r in revs][-1] is None
    assert all(isinstance(r.down_revision, str) for r in revs[:-1])


def test_migration_scripts_ship_inside_the_package():
    assert migrate.SCRIPTS.parent.name == "db" and (migrate.SCRIPTS / "env.py").exists()


def _fake_run(calls):
    def run(args):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0)

    return run


def test_db_up_runs_compose_on_the_repo_file():
    calls: list[list[str]] = []
    assert compose.up(run=_fake_run(calls), which=lambda _: "/usr/bin/docker") == 0
    assert calls == [
        ["docker", "compose", "--file", str(compose.REPO / "compose.yaml"), "up", "--detach", "--wait"]
    ]


def test_db_up_explains_what_is_missing(tmp_path):
    with pytest.raises(FileNotFoundError, match="no compose.yaml"):
        compose.up(repo=tmp_path, run=_fake_run([]), which=lambda _: "/usr/bin/docker")
    with pytest.raises(FileNotFoundError, match="OrbStack"):
        compose.up(run=_fake_run([]), which=lambda _: None)


@pytest.mark.parametrize("command", ["status", "upgrade"])
def test_db_commands_say_how_to_start_an_unreachable_server(tmp_path, monkeypatch, command):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    config.write_default().write_text('database_url = "postgresql+psycopg://tcg@127.0.0.1:1/tcg"\n')
    result = CliRunner().invoke(app, ["db", command])
    assert result.exit_code == 1
    assert "can't reach Postgres at postgresql+psycopg://tcg@127.0.0.1:1/tcg" in result.output
    assert "riffle db up" in result.output


def test_a_database_url_that_cant_be_used_names_the_config_not_the_value(tmp_path, monkeypatch, tracker):
    from riffle.ingest import scryfall_catalog
    from riffle.store import postgres

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    path = config.write_default()
    path.write_text('database_url = "tcg:secret@localhost"\n')
    with pytest.raises(db.BadURL, match=f"database_url in {path} can't be used") as e:
        db.engine()
    assert "secret" not in str(e.value)
    assert scryfall_catalog.update(tracker=tracker) is None  # reported on its step, not raised
    assert tracker.outcomes()["card catalog"] == ("fail", str(e.value))
    with pytest.raises(postgres.Unavailable, match="can't be used"), postgres.open_catalog():
        pass
    result = CliRunner().invoke(app, ["db", "status"])
    assert result.exit_code == 1 and "can't be used" in result.output
