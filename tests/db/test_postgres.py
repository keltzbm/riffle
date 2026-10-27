"""The schema in a real Postgres: migrations, seeds, constraints."""

import pytest
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import insert, inspect, select
from sqlalchemy.exc import IntegrityError
from typer.testing import CliRunner

from riffle import config, db
from riffle.cli import app
from riffle.db import migrate
from riffle.db.models import Base, Game

pytestmark = pytest.mark.postgres


def test_database_is_at_head(pg_engine):
    assert migrate.current(pg_engine) == migrate.head()


def test_models_match_migrations(pg_engine):
    """What the migrations built is exactly what the models define."""
    with pg_engine.connect() as conn:
        assert compare_metadata(MigrationContext.configure(conn), Base.metadata) == []


def test_games_are_seeded(pg):
    rows = pg.execute(select(Game.game_id, Game.name, Game.tcgplayer_category).order_by(Game.game_id)).all()
    assert [tuple(r) for r in rows] == [
        ("fab", "Flesh and Blood", 62),
        ("mtg", "Magic: The Gathering", 1),
        ("op", "One Piece Card Game", None),
    ]


@pytest.mark.parametrize(
    "row",
    [
        {"game_id": "mtg", "name": "Duplicate code", "tcgplayer_category": None},
        {"game_id": "xx", "name": "Duplicate category", "tcgplayer_category": 1},
        {"game_id": "xx", "name": None, "tcgplayer_category": None},
    ],
    ids=["game_id is the key", "one game per TCGplayer category", "name is required"],
)
def test_games_constraints(pg, row):
    with pytest.raises(IntegrityError):
        pg.execute(insert(Game).values(**row))


def test_several_games_may_lack_a_category(pg):
    pg.execute(insert(Game).values(game_id="xx", name="Another game", tcgplayer_category=None))
    uncategorized = select(Game.game_id).where(Game.tcgplayer_category.is_(None)).order_by(Game.game_id)
    assert pg.execute(uncategorized).scalars().all() == ["op", "xx"]


def test_downgrade_to_empty_and_back(pg_engine):
    migrate.downgrade(pg_engine, "base")
    assert migrate.current(pg_engine) is None
    assert inspect(pg_engine).get_table_names() == ["alembic_version"]
    migrate.upgrade(pg_engine)
    assert migrate.current(pg_engine) == migrate.head()
    with pg_engine.connect() as conn:
        codes = conn.execute(select(Game.game_id).order_by(Game.game_id)).scalars().all()
    assert codes == ["fab", "mtg", "op"]


def test_db_status_names_the_server_and_a_schema_at_head(pg_engine):
    url = pg_engine.url.render_as_string(hide_password=False)
    config.config_path().write_text(f'database_url = "{url}"\n', encoding="utf-8")
    result = CliRunner().invoke(app, ["db", "status"])
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert lines[0] == f"database   {db.display(url)}" and lines[1].startswith("server     PostgreSQL 18")
    assert lines[2] == f"schema     {migrate.head()} (head)"
