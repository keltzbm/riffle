"""Postgres, the system of record from v0.4.0 on.

The URL comes from the `database_url` config key and carries no password:
libpq reads it from ~/.pgpass, so psql and psycopg connect the same way,
the launchd job included.
"""

from sqlalchemy import Engine, create_engine, make_url
from sqlalchemy.exc import ArgumentError

from riffle import config

CONNECT_TIMEOUT = 5  # seconds: a stopped or unreachable server fails fast instead of hanging


class BadURL(ValueError):
    """database_url isn't a URL SQLAlchemy can use."""


def engine(url: str | None = None) -> Engine:
    """An engine for url, by default the configured database_url. BadURL when it can't be
    used, naming the config file to fix: the value itself isn't shown, in case it holds a
    password after all."""
    try:
        return create_engine(
            url or config.load().database_url, connect_args={"connect_timeout": CONNECT_TIMEOUT}
        )
    except ArgumentError as e:
        raise BadURL(f"database_url in {config.config_path()} can't be used: {e}") from e


def display(url: str) -> str:
    """The URL for messages, any password masked."""
    return make_url(url).render_as_string(hide_password=True)


def reason(e: BaseException) -> str:
    """An error's own first line: the driver's message for a database error."""
    lines = str(getattr(e, "orig", None) or e).strip().splitlines()
    return lines[0] if lines else type(e).__name__
