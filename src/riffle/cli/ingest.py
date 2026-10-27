"""Handle local imports and source collection commands."""

import os
import time
from collections.abc import Callable
from functools import partial
from pathlib import Path

import typer

from riffle import config, runs, times
from riffle.cli import _common as common
from riffle.ingest.local import manabox
from riffle.progress import Tracker


def init(
    given: str | None = typer.Option(None, "--vault", help="Your Obsidian vault  [default: asked for]"),
) -> None:
    """Write a config, or name its vault, and show where everything lives. Without --vault, asks for
    the vault when none is set and someone is at the terminal."""
    if given is None and config.load().vault is None and common._interactive():
        given = typer.prompt("Where is your Obsidian vault?")
    folder = None
    if given is not None:
        if not given.strip():  # an unset variable, say: as a path it would be this folder
            raise typer.BadParameter("no folder given")
        try:
            folder = Path(os.path.abspath(Path(given).expanduser()))
        except RuntimeError as e:  # ~name for someone with no home here
            raise typer.BadParameter(f"no folder at {given}") from e
        if not folder.is_dir():
            raise typer.BadParameter(f"no folder at {folder}")
    path = config.write_default()
    if folder is not None:
        try:
            config.set_vault(folder)
        except ValueError as e:
            raise typer.BadParameter(str(e)) from e
    cfg = config.load()
    typer.echo(f"config     {path}")
    typer.echo(f"vault      {cfg.vault or 'none set'}")
    if cfg.notes:
        typer.echo(f"notes      {cfg.notes}")
    typer.echo(f"data       {config.data_dir()}")
    typer.echo(f"database   {cfg.database_url}")
    for key, why in (cfg.obsolete or {}).items():
        typer.echo(f"  ! `{key}` in config is ignored: {why}", err=True)
    if cfg.old_notes:
        typer.echo(f"  ! {cfg.old_notes}", err=True)
    if cfg.vault is None:
        typer.echo(f"  ! {config.no_vault()}", err=True)


def _refresh(tracker: Tracker, force: bool = False) -> None:
    """Scryfall's bulk files and set list, downloaded, and default cards loaded into the card catalog."""
    from riffle.ingest.remote import scryfall, scryfall_catalog

    scryfall.refresh(force=force, tracker=tracker)
    scryfall_catalog.update(tracker=tracker, force=force)


def ingest_scryfall(
    force: bool = typer.Option(False, help="Download and load default cards even if already kept"),
    no_sync: bool = typer.Option(False, "--no-sync", help="Don't resync the vault afterwards"),
) -> None:
    """Download every Scryfall bulk file not kept yet and the set list, and load the cards."""
    from riffle.cli import prices as prices_mod
    from riffle.cli import sync as sync_mod

    with common._run("riffle ingest scryfall") as tracker:
        _refresh(tracker, force=force)
        if no_sync:
            prices_mod._snapshot_prices(tracker, online=False)
        else:
            sync_mod._run_sync(tracker, offline=True)


def _copy_manabox(src: Path, dest: Path, made_ns: int) -> None:
    """Whole or not at all, and on the disk (runs.write): a sync reading the collection meanwhile
    sees the old file or the new one. Its time is `made_ns`, which the next sync's pickup is
    measured against: a sync's pickup gives it the export's, so the next export taken is one made
    after it; an ingest by hand, the ingest's, or the newest ManaBox*.csv's in Downloads when that
    is later (dated by a clock ahead of this Mac's), so an export put back by hand stays the
    collection."""
    runs.write(dest, src.read_bytes())
    os.utime(dest, ns=(made_ns, made_ns))


def _keep_collection(cfg: config.Config) -> tuple[manabox.Kept | None, bool]:
    """The collection kept with the exports before anything replaces it (manabox.keep): what
    it's kept as, and whether it was kept just now; (None, False) when there's none, or it isn't
    an export. One that can't be read can't be kept, so that's OSError, and nothing replaces it."""
    stored = cfg.collection_csv
    if not stored.exists():
        return None, False
    if manabox.why_not(stored):
        stored.open("rb").close()
        return None, False
    return manabox.keep(stored, cfg.manabox_exports)


def _since(
    was: manabox.Kept | None, now: manabox.Kept, before: Path
) -> tuple[str, list[str], manabox.Change | None]:
    """What changed from `before`, the file kept as `was`, to the export kept as `now` that
    replaces it: the end of the line that says so, the warnings, and the change itself. `before`
    is read, not its kept copy, so a kept copy changed since can't hide a change."""
    if was is None:
        return "; nothing to compare", [], None
    when = times.shown(was.at)
    columns = manabox.columns_changed(before, now.path)
    if columns:
        return (
            "",
            [f"the export's columns changed since the export of {when}: {', '.join(columns)}; no diff"],
            None,
        )
    old = manabox.load(before)
    change = manabox.change(old, manabox.load(now.path))
    copies = sum(h.quantity for h in old)
    warnings = []
    if change.removed * 2 > copies:
        warnings.append(
            f"the export of {times.shown(now.at)} drops {change.removed:,} of {copies:,} copies: "
            "check it's the whole collection (riffle ingest manabox --diff)"
        )
    return f"; since the export of {when}: {change}", warnings, change


def _not_kept(e: OSError) -> str:
    return f"can't keep the export: {e}; the collection was not replaced"


def _not_written(e: OSError, now: manabox.Kept) -> str:
    return (
        f"can't write the collection: {e}; it's as it was, and the export is kept as manabox/{now.path.name}"
    )


def _said(line: str, warnings: list[str], change: manabox.Change | None) -> None:
    """An ingest's line, then each copy that changed when there's a change to list, then the
    warnings."""
    if line:
        typer.echo(line)
    for each in change.lines if change else ():
        typer.echo(f"  {each}")
    for w in warnings:
        common._echo(w, err=True)


def ingest_manabox(
    csv_path: Path = typer.Argument(None, help="Default: newest ManaBox*.csv in ~/Downloads"),
    no_sync: bool = typer.Option(False, "--no-sync", help="Don't resync the vault afterwards"),
    diff: bool = typer.Option(False, "--diff", help="List every copy that changed, a line each"),
) -> None:
    """Keep a ManaBox collection export with the others, and make it the collection. One kept
    already (they're in ~/.local/share/riffle/manabox) puts that export back."""
    from riffle.cli import sync as sync_mod

    cfg = config.load()
    src = csv_path or manabox.newest_export(cfg.downloads)
    if src is None:
        raise typer.BadParameter("no ManaBox export in ~/Downloads — pass a path")
    src = src.expanduser()
    why = manabox.why_not(src)
    if why:
        raise typer.BadParameter(f"{src.name} isn't a ManaBox export: {why}")
    try:
        was, _ = _keep_collection(cfg)
        now, new = manabox.keep(src, cfg.manabox_exports)
    except OSError as e:
        common._echo(_not_kept(e), err=True)
        raise typer.Exit(1) from e
    if now == was:
        typer.echo(f"{src.name} is the collection already (manabox/{now.path.name}); nothing changed")
        if diff:
            earlier = [k for k in manabox.kept(cfg.manabox_exports) if k.at < now.at]
            if not earlier:
                typer.echo("the first export kept, nothing to compare")
                return
            said, warnings, change = _since(earlier[-1], now, earlier[-1].path)
            _said(said.removeprefix("; "), warnings, change)
        return
    said, warnings, change = _since(was, now, cfg.collection_csv)
    try:
        downloads = (p.stat().st_mtime_ns for p in manabox.exports(cfg.downloads))
        _copy_manabox(now.path, cfg.collection_csv, max([time.time_ns(), *downloads]))
    except OSError as e:
        common._echo(_not_written(e, now), err=True)
        raise typer.Exit(1) from e
    took = (
        f"{src.name} is the collection now, kept as manabox/{now.path.name}"
        if new
        else f"restored the export of {times.shown(now.at)} (manabox/{now.path.name})"
    )
    _said(took + said, warnings, change if diff else None)
    if not no_sync:
        sync_mod._resync()


def ingest_arena(path: Path) -> None:
    """Load an Arena collection export (text list or CSV with name + count)."""
    from riffle.ingest.local import arena

    holdings = arena.load(path.expanduser())
    dest = config.load().arena_list
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text("".join(f"{h.quantity} {h.name}\n" for h in holdings), encoding="utf-8")
    typer.echo(f"{len(holdings)} Arena cards -> {dest}")


def watch_cmd(
    store: str = typer.Argument(
        ..., help="The store whose lists to keep", autocompletion=common._complete_store
    ),
) -> None:
    """Keep every new list a store publishes: each of its lists asked for once, a new one kept
    whole or as a difference against its run's first (Scryfall's files as served). The jobs
    (riffle schedule watch) run this every 5 minutes; tcgcsv, Cardmarket, MTGJSON, GoatBots
    and Scryfall ask only when their next list is due, as learned from their own lists and
    checks."""
    from riffle.watch import watching

    if store not in watching.STORES:
        stores = ", ".join(watching.STORES)
        raise typer.BadParameter(f"'{store}' has no lists to watch; the stores: {stores}")
    with common._tracked(f"riffle watch {store}") as tracker:  # a failed step exits 1 on leaving
        _watcher(store)(tracker=tracker)


def _watcher(store: str) -> Callable[..., object]:
    """A store's watch, one of watching.STORES."""
    from riffle.ingest.remote import cardmarket, goatbots, mtgjson, pricelists, scryfall, tcgcsv

    watchers: dict[str, Callable[..., object]] = {
        "cardkingdom": partial(pricelists.watch, pricelists.CARD_KINGDOM),
        "manapool": partial(pricelists.watch, pricelists.MANA_POOL),
        "cardmarket": cardmarket.watch,
        "tcgcsv": tcgcsv.watch,
        "mtgjson": mtgjson.watch,
        "goatbots": goatbots.watch,
        "scryfall": scryfall.watch,
    }
    return watchers[store]
