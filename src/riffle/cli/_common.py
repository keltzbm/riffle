"""Share terminal handling, catalog access, and command completion."""

import sys
from collections.abc import Callable, Iterable, Iterator
from contextlib import ExitStack, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer

import riffle.ingest.mtgo.pages as ingest_mtgo_pages
from riffle import config, vault
from riffle import sync as syncmod
from riffle.ingest.local import manabox, precons
from riffle.models import Deck
from riffle.progress import LogTracker, Tracker, Watched, elapsed, open_tracker
from riffle.store import Catalog


def _offer(values: Iterable[str], incomplete: str) -> list[str]:
    return [v for v in values if v.startswith(incomplete)]


def _deck_names() -> list[str]:
    """What `riffle decks` lists. Nothing when the vault can't be read, rather than a
    traceback in the middle of the prompt."""
    try:
        mtg = config.load().mtg_dir
        return [d.slug for d in vault.decks(mtg)] if mtg else []
    except (OSError, ValueError):
        return []


def _complete_deck(incomplete: str) -> list[str]:
    return _offer(_deck_names(), incomplete)


def _complete_deck_or_all(incomplete: str) -> list[str]:
    return _offer([*_deck_names(), "all"], incomplete)


def _complete_legal_format(incomplete: str) -> list[str]:
    from riffle.analysis.legality import FORMAT_RULES

    return _offer(FORMAT_RULES, incomplete)


def _complete_mtgo_format(incomplete: str) -> list[str]:

    return _offer([*ingest_mtgo_pages.FORMATS, "all"], incomplete)


def _complete_store(incomplete: str) -> list[str]:
    from riffle.watch import watching

    return _offer(list(watching.STORES), incomplete)


def _complete_kind(incomplete: str) -> list[str]:

    return _offer(ingest_mtgo_pages.KINDS, incomplete)


def _choices(*values: str) -> Callable[[str], list[str]]:
    """Completion from a fixed list."""

    def complete(incomplete: str) -> list[str]:
        return _offer(values, incomplete)

    return complete


DeckRef = Annotated[
    str,
    typer.Argument(help="Deck note slug (aesi-lands) or a path to .md/.txt", autocompletion=_complete_deck),
]


DecksRef = Annotated[
    str,
    typer.Argument(
        help="Deck note slug (aesi-lands), a path to .md/.txt, or all", autocompletion=_complete_deck_or_all
    ),
]


def _echo(message: str = "", err: bool = False) -> None:
    """typer.echo through whatever sys.stdout or sys.stderr is at the moment. While a command's
    steps are showing, those are Rich's stand-ins, which print a line above the running steps;
    typer.echo on its own finds the terminal underneath them and writes onto the display's last
    line, leaving a stale progress line behind. So anything printed inside _tracked goes
    through here."""
    typer.echo(message, file=sys.stderr if err else sys.stdout)


@contextmanager
def _catalog() -> Iterator[Catalog]:
    """The card catalog for the rest of the block, or exit 1 saying what to run."""
    from riffle.store import postgres

    with ExitStack() as stack:
        try:
            cat = stack.enter_context(postgres.open_catalog())
        except postgres.Unavailable as e:
            _echo(str(e), err=True)
            raise typer.Exit(1) from e
        yield cat


@contextmanager
def _tracked(title: str) -> Iterator[Tracker]:
    """A command's steps. A failed step doesn't stop the command: it finishes its work, then
    names what failed and exits 1, so the scheduled job's last exit shows it. A run written as
    plain lines, a scheduled job's, ends with a closing line saying how it exited, whatever
    ended it (riffle.ops.joblogs reads it back). Inside the block, print with _echo, not typer.echo."""
    log: LogTracker | None = None
    code = 1
    try:
        with open_tracker(title) as tracker:
            log = tracker if isinstance(tracker, LogTracker) else None
            watched = Watched(tracker)
            yield watched
        if watched.failed:
            n = len(watched.failed)
            typer.echo(f"{n} step{'s' * (n != 1)} failed: {', '.join(watched.failed)}", err=True)
            raise typer.Exit(1)
        code = 0
    except typer.Exit as e:
        code = e.exit_code
        raise
    finally:
        if log is not None:
            log.footer(code)


def _lock_path() -> Path:
    return config.data_dir() / "sync.lock"


def _holder(path: Path) -> str:
    """Who holds the run lock, from what they wrote in it, for the waiting message."""
    import json

    try:
        held = json.loads(path.read_text(encoding="utf-8"))
        since = datetime.fromisoformat(held["since"]).astimezone()
        return f"{held['command']} (pid {held['pid']}), running since {since:%H:%M %Z}"
    except (OSError, ValueError, KeyError, TypeError):
        return "another riffle run"


@contextmanager
def _one_at_a_time(tracker: Tracker, command: str) -> Iterator[None]:
    """One run at a time writes prices, the card catalog and the vault. Runs share download
    names (.part, .new), the price log and sync-state.json, so a second run waits for the
    first to finish, saying whose turn it is. The trickle has its own lock."""
    import fcntl
    import json
    import os
    import time

    path = _lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            _echo(f"waiting for {_holder(path)} to finish")
            step, start = tracker.step("waiting for its turn"), time.monotonic()
            fcntl.flock(f, fcntl.LOCK_EX)
            step.ok(f"after {elapsed(time.monotonic() - start)}")
        f.seek(0)
        f.truncate()
        f.write(json.dumps({"command": command, "pid": os.getpid(), "since": datetime.now(UTC).isoformat()}))
        f.flush()
        yield


@contextmanager
def _run(title: str) -> Iterator[Tracker]:
    """_tracked, holding the run lock (see _one_at_a_time) for the whole command."""
    with _tracked(title) as tracker, _one_at_a_time(tracker, title):
        yield tracker


def _inventory(cfg: config.Config, cat: Catalog, boxes: precons.Boxes | None = None) -> syncmod.Inventory:
    """The collection, its misread printings corrected by the precon boxes given, or else by those
    the last sync read; exit 1 if it isn't a ManaBox export."""
    try:
        return syncmod.inventory(cfg.collection_csv, cat, precons.cached() if boxes is None else boxes)
    except manabox.NotExport as e:
        _echo(
            f"{e}; export the collection from ManaBox again, then: riffle ingest manabox <export>", err=True
        )
        raise typer.Exit(1) from e


@contextmanager
def _setup(boxes: precons.Boxes | None = None) -> Iterator[tuple[config.Config, Catalog, syncmod.Inventory]]:
    """The config, the catalog and the collection (_inventory)."""
    cfg = config.load()
    with _catalog() as cat:
        yield cfg, cat, _inventory(cfg, cat, boxes)


def _vault(cfg: config.Config) -> tuple[Path, Path, Path]:
    """The vault, its notes folder, and the deck notes' mtg/ in that. A command that reads the vault
    asks here before it opens the database, so with none set it stops, saying what to set."""
    if cfg.vault is None or cfg.notes is None or cfg.mtg_dir is None:
        raise typer.BadParameter(config.no_vault())
    return cfg.vault, cfg.notes, cfg.mtg_dir


def _mtg_dir(cfg: config.Config) -> Path:
    """The deck notes' folder, as _vault finds it."""
    return _vault(cfg)[2]


def _deck(cfg: config.Config, ref: str) -> Deck:
    """A deck by the path to its file, which needs no vault, or by its slug among the vault's deck
    notes (_mtg_dir)."""
    found = vault.from_file(ref)
    return found if found is not None else vault.find(_mtg_dir(cfg), ref)


def _interactive() -> bool:
    """Whether someone can answer a question: stdin is a terminal."""
    return sys.stdin.isatty()
