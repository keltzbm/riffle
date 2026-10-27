"""Handle synchronization and watching the vault."""

from pathlib import Path

import typer

from riffle import config, times, vault
from riffle import sync as syncmod
from riffle.cli import _common as common
from riffle.cli import prices
from riffle.ingest.local import manabox
from riffle.ingest.remote import tcgcsv
from riffle.ops import sync as workflow
from riffle.progress import Tracker, failure


def _some(names: list[str], most: int = 10) -> str:
    """The names, or the first ten and how many more: the sync after a vault moves its lists
    removes thousands of notes."""
    more = f" and {len(names) - most:,} more" if len(names) > most else ""
    return ", ".join(names[:most]) + more


class _Output:
    def prices(self, snapshot: tcgcsv.Snapshot) -> None:
        prices._summary(snapshot)

    def no_collection(self) -> None:
        common._echo(
            "no collection yet — export from ManaBox to ~/Downloads, or: riffle ingest manabox <csv>",
            err=True,
        )

    def synced(self, result: syncmod.SyncResult) -> None:
        common._echo(
            f"{len(result.decks)} decks · {result.changed_notes} notes updated · "
            f"versions changed: {', '.join(result.versions) or 'none'}"
        )
        if result.moved:
            common._echo(f"moved version logs beside their lists: {_some(result.moved)}")
        if result.card_notes:
            c = result.card_notes
            common._echo(f"card notes: {c.cards:,} ({c.written:,} written, {c.removed:,} removed)")
        elif result.cards_removed:
            common._echo(f"card notes: off ({result.cards_removed:,} removed)")
        if result.removed:
            common._echo(f"removed stale generated notes: {_some(result.removed)}")
        for w in result.warnings:
            common._echo(f"  ! {w}", err=True)


def _run_sync(tracker: Tracker, offline: bool = True) -> None:
    """Run the workflow and translate a missing catalog or inventory to a command failure."""
    try:
        workflow.run(tracker, _Output(), offline=offline)
    except workflow.Unavailable as e:
        common._echo(str(e), err=True)
        raise typer.Exit(1) from e


def _resync() -> None:
    """An offline sync, for commands that change local data."""
    with common._run("riffle sync") as tracker:
        _run_sync(tracker, offline=True)


def sync_cmd(
    offline: bool = typer.Option(False, help="Skip the Scryfall refresh and price downloads"),
    watch: bool = typer.Option(
        False, "--watch", help="Fetch nothing: resync whenever a deck note is saved or a ManaBox export lands"
    ),
    interval: float = typer.Option(5.0, help="Seconds between checks, with --watch"),
) -> None:
    """Refresh card data, keep today's prices, pick up a ManaBox export, rewrite _generated/, append _log/.
    With --watch, resync from what's kept at every change instead, until Ctrl-C."""
    if watch:
        _watch_vault(interval)
        return
    with common._run("riffle sync") as tracker:
        _run_sync(tracker, offline=offline)


def _watched(cfg: config.Config) -> dict[str, float]:
    """Everything whose change should trigger a resync, with its mtime."""
    paths = [p for p in vault.deck_notes(common._mtg_dir(cfg))]
    paths += [cfg.collection_csv, cfg.arena_list, config.config_path()]
    newest = manabox.newest_export(cfg.downloads)
    if newest:
        paths.append(newest)
    return {str(p): p.stat().st_mtime for p in paths if p.exists()}


def _resync_and_keep_watching() -> None:
    """A failed resync is reported, and watching goes on: the next save may fix it."""
    try:
        _resync()
    except typer.Exit:
        typer.echo("resync failed; still watching", err=True)
    except Exception as e:
        typer.echo(f"resync failed: {failure(e)}; still watching", err=True)


def _watch_vault(interval: float) -> None:
    """Resync whenever a deck note is saved or a new ManaBox export lands. Ctrl-C to stop."""
    import time

    cfg = config.load()
    typer.echo(f"watching {common._mtg_dir(cfg)} and ~/Downloads — Ctrl-C to stop")
    _resync_and_keep_watching()
    seen = _watched(cfg)
    try:
        while True:
            time.sleep(interval)
            now = _watched(cfg)
            if now != seen:
                changed = sorted(
                    Path(p).name for p in set(now) ^ set(seen) | {p for p in now if seen.get(p) != now[p]}
                )
                typer.echo(f"\n{times.local(times.now(), '%H:%M:%S')} changed: {', '.join(changed)}")
                _resync_and_keep_watching()
                seen = _watched(cfg)
    except KeyboardInterrupt:
        typer.echo("\nstopped")
