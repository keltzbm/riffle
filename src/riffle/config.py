"""Settings from $XDG_CONFIG_HOME/riffle/config.toml, with sensible defaults.

    vault = "~/atelier/library"
    notes = "games/tcg"
    database_url = "postgresql+psycopg://tcg@localhost:5432/tcg"

The vault is the Obsidian vault itself; notes is the folder in it that holds
mtg/, relative to the vault. A config that names no notes folder was written
when vault named the folder holding tcg/, and is read that way.

The database URL never holds a password: libpq reads it from ~/.pgpass.

What you own comes from the ManaBox export alone: scan a precon into ManaBox
to count it.
"""

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

OBSOLETE_KEYS = {
    "precons": "sealed precons are no longer counted; scan them into ManaBox and delete this line",
}


def _xdg(var: str, default: str) -> Path:
    return Path(os.environ.get(var) or Path.home() / default)


def config_path() -> Path:
    return _xdg("XDG_CONFIG_HOME", ".config") / "riffle" / "config.toml"


def data_dir() -> Path:
    """Bulk data, daily price snapshots, the collection CSV and sync state.

    Never inside ~/atelier: that tree is synced.
    """
    return _xdg("XDG_DATA_HOME", ".local/share") / "riffle"


DEFAULT_DATABASE_URL = "postgresql+psycopg://tcg@localhost:5432/tcg"

DEFAULT_CONFIG = f"""\
# riffle configuration
vault = "~/atelier/library"
# the folder in the vault that holds mtg/
notes = "games/tcg"
# Postgres; the password comes from ~/.pgpass, never from this file
database_url = "{DEFAULT_DATABASE_URL}"
"""


@dataclass
class Config:
    vault: Path
    notes: Path  # the folder holding mtg/, resolved against the vault
    database_url: str = DEFAULT_DATABASE_URL
    obsolete: dict[str, str] | None = None  # key -> why it's ignored
    old_notes: str | None = None  # what a config without a notes line is read as, and the change it needs

    @property
    def mtg_dir(self) -> Path:
        return self.notes / "mtg"

    @property
    def collection_csv(self) -> Path:
        return data_dir() / "collection.csv"

    @property
    def arena_list(self) -> Path:
        return data_dir() / "arena-collection.txt"

    @property
    def downloads(self) -> Path:
        return Path.home() / "Downloads"


def _tilde(path: Path) -> str:
    home = Path.home()
    return f"~/{path.relative_to(home).as_posix()}" if path.is_relative_to(home) else path.as_posix()


def _old_notes(vault: Path) -> str:
    """The change that keeps an old config's notes folder, vault/tcg, with vault naming the vault."""
    root = next((d for d in (vault, *vault.parents) if (d / ".obsidian").is_dir()), vault)
    notes = f'add notes = "{(vault / "tcg").relative_to(root).as_posix()}"'
    return notes if root == vault else f'set vault = "{_tilde(root)}" and {notes}'


def load() -> Config:
    path = config_path()
    raw = tomllib.loads(path.read_text()) if path.exists() else tomllib.loads(DEFAULT_CONFIG)
    vault = Path(raw.get("vault", "~/atelier/library")).expanduser()
    old = "notes" not in raw  # written when vault named the folder holding tcg/
    notes = vault / ("tcg" if old else Path(raw["notes"]).expanduser())
    return Config(
        vault=vault,
        notes=notes,
        database_url=raw.get("database_url", DEFAULT_DATABASE_URL),
        obsolete={k: why for k, why in OBSOLETE_KEYS.items() if k in raw} or None,
        old_notes=f"{_tilde(path)} names no notes folder, so it's {_tilde(notes)}: {_old_notes(vault)}"
        if old
        else None,
    )


def write_default() -> Path:
    path = config_path()
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(DEFAULT_CONFIG)
    return path
