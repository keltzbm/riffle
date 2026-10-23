"""Settings from $XDG_CONFIG_HOME/riffle/config.toml, with sensible defaults.

    vault = "~/notes"
    notes = "games/tcg"
    database_url = "postgresql+psycopg://tcg@localhost:5432/tcg"
    card_notes = true
    card_images = "cache"
    hidden_folder = ".data"

The vault is the Obsidian vault itself; notes is the folder in it that holds a
folder per game (mtg/, one-piece/, fab/, …), relative to the vault. Riffle reads
decks from mtg/ only for now, and a notes folder without one syncs no decks. A
config that names no notes folder was written when vault named the folder
holding tcg/, and is read that way. No vault is set until the config names one
(riffle init asks): a command that reads the vault then says what to add, and the
sync keeps the day's data but writes no notes.

card_notes puts a note behind every card link, so hovering one shows the card; card_images
is "cache" to keep each picture in the vault (about 100 KB a card), "link" to show
Scryfall's, or "off" for none. A value that can't be read is named at the next sync, and
the default is used.

hidden_folder is the folder in mtg/ whose decks stay out of Obsidian's view: a deck whose
note is in it has its data note and version log there too, in its own _generated/ and
_log/, and links in it get no card notes. Obsidian never indexes a folder whose name starts
with a dot, so a name without one isn't used.

The database URL never holds a password: libpq reads it from ~/.pgpass.

backup_log names a backup job's log, for riffle status to say when the store was last backed
up: a line a step, "2026-10-22T08:31:34Z backup ok" (a UTC time, the step, ok or failed), and
a file "backup.failed" beside it while a run is failing. Without it, Time Machine is asked.

What you own comes from the ManaBox export alone: scan a precon into ManaBox
to count it.
"""

import json
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

OBSOLETE_KEYS = {
    "precons": "sealed precons are no longer counted; scan them into ManaBox and delete this line",
}


def _xdg(var: str, default: str) -> Path:
    return Path(os.environ.get(var) or Path.home() / default)


def config_home() -> Path:
    """Where programs keep their settings: $XDG_CONFIG_HOME, else ~/.config."""
    return _xdg("XDG_CONFIG_HOME", ".config")


def config_path() -> Path:
    return config_home() / "riffle" / "config.toml"


def data_dir() -> Path:
    """Bulk data, daily price snapshots, the collection CSV and sync state.

    Never inside the vault or another synced folder: it's large, and much of it changes daily.
    """
    return _xdg("XDG_DATA_HOME", ".local/share") / "riffle"


DEFAULT_DATABASE_URL = "postgresql+psycopg://tcg@localhost:5432/tcg"
CARD_IMAGES = ("cache", "link", "off")  # kept in the vault, linked from Scryfall, or none
HIDDEN_FOLDER = ".data"  # in mtg/; Obsidian indexes no folder whose name starts with a dot
_VAULT_KEY = re.compile(r"""\s*(?:vault|"vault"|'vault')\s*=""")  # the key, bare or quoted
_TABLE = re.compile(r"\s*\[")  # a table's header: keys after it aren't the config's own

DEFAULT_CONFIG = f"""\
# riffle configuration
# your Obsidian vault, e.g. "~/notes"; riffle init --vault sets it
vault = ""
# the folder in the vault that holds a folder per game (mtg/, one-piece/, …);
# Riffle reads decks from mtg/ only for now
notes = "games/tcg"
# Postgres; the password comes from ~/.pgpass, never from this file
database_url = "{DEFAULT_DATABASE_URL}"
# a note behind every card link, so hovering a card shows it (mtg/_generated/cards/)
card_notes = true
# its pictures: "cache" keeps each in the vault, "link" shows Scryfall's, "off" none
card_images = "cache"
# the folder in mtg/ whose decks keep their data notes and logs inside it, out of Obsidian's
# view: Obsidian indexes no folder whose name starts with a dot
hidden_folder = "{HIDDEN_FOLDER}"
"""


@dataclass
class Config:
    vault: Path | None  # None until the config names one
    notes: Path | None  # the folder holding a folder per game, resolved against the vault
    database_url: str = DEFAULT_DATABASE_URL
    obsolete: dict[str, str] | None = None  # key -> why it's ignored
    old_notes: str | None = None  # what a config without a notes line is read as, and the change it needs
    card_notes: bool = True
    card_images: str = "cache"
    hidden_folder: str = HIDDEN_FOLDER
    unread: list[str] = field(default_factory=list)  # each setting that can't be read, and what's used
    backup_log: Path | None = None  # a backup job's log, for riffle status

    @property
    def mtg_dir(self) -> Path | None:
        return self.notes / "mtg" if self.notes else None

    @property
    def collection_csv(self) -> Path:
        return data_dir() / "collection.csv"

    @property
    def manabox_exports(self) -> Path:
        """Every ManaBox export Riffle has seen, as the file it was (riffle.ingest.manabox.keep)."""
        return data_dir() / "manabox"

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


def no_vault() -> str:
    """What a command that reads the vault says when the config names none."""
    return f'no vault set: set vault = "~/your-vault" in {tilde(config_path())}, or run riffle init'


def load() -> Config:
    path = config_path()
    raw = tomllib.loads(path.read_text(encoding="utf-8")) if path.exists() else tomllib.loads(DEFAULT_CONFIG)
    unread = []
    named = raw.get("vault", "")
    if not isinstance(named, str):
        unread.append(f"vault in {tilde(path)} is {named!r}, not a folder; no vault is used")
        named = ""
    try:
        vault = Path(named).expanduser() if named.strip() else None
    except RuntimeError:  # ~name for someone with no home here
        unread.append(f"vault in {tilde(path)} is {named!r}, a home no one has here; no vault is used")
        vault = None
    old = "notes" not in raw  # written when vault named the folder holding tcg/
    notes = vault / ("tcg" if old else Path(raw["notes"]).expanduser()) if vault else None
    card_notes = raw.get("card_notes", True)
    if not isinstance(card_notes, bool):
        unread.append(f"card_notes in {tilde(path)} is {card_notes!r}, not true or false; true is used")
        card_notes = True
    images = raw.get("card_images", "cache")
    if images not in CARD_IMAGES:
        choices = ", ".join(f'"{x}"' for x in CARD_IMAGES)
        unread.append(f'card_images in {tilde(path)} is {images!r}, not one of {choices}; "cache" is used')
        images = "cache"
    backup = raw.get("backup_log", "")
    if not isinstance(backup, str):
        unread.append(f"backup_log in {tilde(path)} is {backup!r}, not a path; none is used")
        backup = ""
    hidden = raw.get("hidden_folder", HIDDEN_FOLDER)
    used = f'"{HIDDEN_FOLDER}" is used'
    if not isinstance(hidden, str) or hidden in ("", ".", "..") or "/" in hidden:
        shown = f'"{hidden}"' if isinstance(hidden, str) else repr(hidden)
        unread.append(f"hidden_folder in {tilde(path)} is {shown}, not a folder's name; {used}")
        hidden = HIDDEN_FOLDER
    elif not hidden.startswith("."):
        unread.append(
            f'hidden_folder in {tilde(path)} is "{hidden}", which doesn\'t start with a dot, '
            f"so Obsidian would index it; {used}"
        )
        hidden = HIDDEN_FOLDER
    return Config(
        vault=vault,
        notes=notes,
        database_url=raw.get("database_url", DEFAULT_DATABASE_URL),
        obsolete={k: why for k, why in OBSOLETE_KEYS.items() if k in raw} or None,
        old_notes=f"{tilde(path)} names no notes folder, so it's {tilde(notes)}: {_old_notes(vault)}"
        if old and vault and notes
        else None,
        card_notes=card_notes,
        card_images=images,
        hidden_folder=hidden,
        unread=unread,
        backup_log=Path(backup).expanduser() if backup else None,
    )


def write_default() -> Path:
    path = config_path()
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(DEFAULT_CONFIG, encoding="utf-8")
    return path


def set_vault(folder: Path) -> None:
    """Name `folder` as the vault, by ~ when it's under home: the config's own vault line, the one
    before any table, is rewritten, or one added at the top, and every other line is left as it
    was. A config that wouldn't then read back with that vault is left alone: ValueError."""
    path = config_path()
    named = json.dumps(tilde(folder), ensure_ascii=False)  # JSON's escapes are TOML's
    with path.open(encoding="utf-8", newline="") as f:  # each line's own ending, \r\n or \n
        lines = f.read().splitlines(keepends=True)
    top = next((n for n, line in enumerate(lines) if _TABLE.match(line)), len(lines))
    at = next((n for n in range(top) if _VAULT_KEY.match(lines[n])), None)
    near = lines[at if at is not None else 0] if lines else "\n"
    ending = near[len(near.rstrip("\r\n")) :] or "\n"  # the replaced line's, or the first line's
    line = f"vault = {named}{ending}"
    if at is None:
        lines.insert(0, line)
    else:
        lines[at] = line
    text = "".join(lines)
    try:
        read = tomllib.loads(text).get("vault")
    except tomllib.TOMLDecodeError:
        read = None
    if read != tilde(folder):
        raise ValueError(f"{tilde(path)} can't take a vault line as it is: set vault = {named} in it by hand")
    with path.open("w", encoding="utf-8", newline="") as f:
        f.write(text)
