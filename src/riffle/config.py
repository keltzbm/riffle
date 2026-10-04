"""Settings from $XDG_CONFIG_HOME/riffle/config.toml, with sensible defaults.

    vault = "~/atelier/library"
    notes = "games/tcg"
    database_url = "postgresql+psycopg://tcg@localhost:5432/tcg"
    card_notes = true
    card_images = "cache"

The vault is the Obsidian vault itself; notes is the folder in it that holds a
folder per game (mtg/, one-piece/, fab/, …), relative to the vault. Riffle reads
decks from mtg/ only for now, and a notes folder without one syncs no decks. A
config that names no notes folder was written when vault named the folder
holding tcg/, and is read that way.

card_notes puts a note behind every card link, so hovering one shows the card; card_images
is "cache" to keep each picture in the vault (about 100 KB a card), "link" to show
Scryfall's, or "off" for none. A value that can't be read is named at the next sync, and
the default is used.

The database URL never holds a password: libpq reads it from ~/.pgpass.

What you own comes from the ManaBox export alone: scan a precon into ManaBox
to count it.
"""

import os
import tomllib
from dataclasses import dataclass, field
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
CARD_IMAGES = ("cache", "link", "off")  # kept in the vault, linked from Scryfall, or none

DEFAULT_CONFIG = f"""\
# riffle configuration
vault = "~/atelier/library"
# the folder in the vault that holds a folder per game (mtg/, one-piece/, …);
# Riffle reads decks from mtg/ only for now
notes = "games/tcg"
# Postgres; the password comes from ~/.pgpass, never from this file
database_url = "{DEFAULT_DATABASE_URL}"
# a note behind every card link, so hovering a card shows it (mtg/_generated/cards/)
card_notes = true
# its pictures: "cache" keeps each in the vault, "link" shows Scryfall's, "off" none
card_images = "cache"
"""


@dataclass
class Config:
    vault: Path
    notes: Path  # the folder holding a folder per game, resolved against the vault
    database_url: str = DEFAULT_DATABASE_URL
    obsolete: dict[str, str] | None = None  # key -> why it's ignored
    old_notes: str | None = None  # what a config without a notes line is read as, and the change it needs
    card_notes: bool = True
    card_images: str = "cache"
    unread: list[str] = field(default_factory=list)  # each setting that can't be read, and what's used

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


def tilde(path: Path) -> str:
    home = Path.home()
    return f"~/{path.relative_to(home).as_posix()}" if path.is_relative_to(home) else path.as_posix()


def _old_notes(vault: Path) -> str:
    """The change that keeps an old config's notes folder, vault/tcg, with vault naming the vault."""
    root = next((d for d in (vault, *vault.parents) if (d / ".obsidian").is_dir()), vault)
    notes = f'add notes = "{(vault / "tcg").relative_to(root).as_posix()}"'
    return notes if root == vault else f'set vault = "{tilde(root)}" and {notes}'


def load() -> Config:
    path = config_path()
    raw = tomllib.loads(path.read_text()) if path.exists() else tomllib.loads(DEFAULT_CONFIG)
    vault = Path(raw.get("vault", "~/atelier/library")).expanduser()
    old = "notes" not in raw  # written when vault named the folder holding tcg/
    notes = vault / ("tcg" if old else Path(raw["notes"]).expanduser())
    unread = []
    card_notes = raw.get("card_notes", True)
    if not isinstance(card_notes, bool):
        unread.append(f"card_notes in {tilde(path)} is {card_notes!r}, not true or false; true is used")
        card_notes = True
    images = raw.get("card_images", "cache")
    if images not in CARD_IMAGES:
        choices = ", ".join(f'"{x}"' for x in CARD_IMAGES)
        unread.append(f'card_images in {tilde(path)} is {images!r}, not one of {choices}; "cache" is used')
        images = "cache"
    return Config(
        vault=vault,
        notes=notes,
        database_url=raw.get("database_url", DEFAULT_DATABASE_URL),
        obsolete={k: why for k, why in OBSOLETE_KEYS.items() if k in raw} or None,
        old_notes=f"{tilde(path)} names no notes folder, so it's {tilde(notes)}: {_old_notes(vault)}"
        if old
        else None,
        card_notes=card_notes,
        card_images=images,
        unread=unread,
    )


def write_default() -> Path:
    path = config_path()
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(DEFAULT_CONFIG)
    return path
