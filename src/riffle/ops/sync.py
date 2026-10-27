"""Coordinate card data, prices, collection pickup, generated notes, and backups."""

from contextlib import ExitStack
from pathlib import Path
from typing import Protocol

from riffle import config, net, vault
from riffle import sync as syncmod
from riffle.ingest.local import manabox, precons
from riffle.ingest.remote import tcgcsv
from riffle.ops import database, disk, ingest
from riffle.progress import Step, Tracker, elapsed


class Output(Protocol):
    """Present completed results between workflow steps."""

    def prices(self, snapshot: tcgcsv.Snapshot) -> None: ...

    def no_collection(self) -> None: ...

    def synced(self, result: syncmod.SyncResult) -> None: ...


class Unavailable(RuntimeError):
    """The catalog or inventory cannot be read; end the run before its backup."""


def run(tracker: Tracker, output: Output, *, offline: bool = True) -> None:
    """Refresh data, pick up the collection, write notes, and back up an online run.

    An online invocation falls back to retained data if no source host answers.
    Its final backup still runs, including when vault validation skips the notes.
    An explicitly offline invocation skips downloads, the disk check, and backup.

    Args:
        tracker: Record progress and failures without ending the remaining steps.
        output: Present price and note results where they occur in the workflow.
        offline: Use retained data only, as ingest resyncs and watch mode do.

    Raises:
        Unavailable: The catalog or inventory cannot be read. Earlier retained
            data stays kept, but the run ends before backup.
    """
    cfg = config.load()
    online = not offline and _online(tracker)
    if online:
        ingest.refresh(tracker)
    price_snapshot = ingest.snapshot_prices(tracker, online=online)
    if price_snapshot is not None:
        output.prices(price_snapshot)
    if online:
        disk.check(tracker)
    ingest.pick_up(tracker, cfg)
    if not cfg.collection_csv.exists():
        output.no_collection()
    _notes(cfg, tracker, output, offline=not online)
    if not offline:
        database.backup(tracker, config.load().database_url, warn_elsewhere=True)


def _notes(initial_cfg: config.Config, tracker: Tracker, output: Output, *, offline: bool) -> None:
    """Validate the vault and collection, then generate notes from one catalog snapshot.

    Invalid vault paths or a rejected export end only this part of the workflow.
    Catalog and inventory errors raise Unavailable instead. A completed result
    includes note failures, which are recorded before the caller presents it.
    """
    if initial_cfg.old_notes:
        tracker.step("vault setting").warn(initial_cfg.old_notes)
    for why in initial_cfg.unread:
        tracker.step("setting").warn(why)
    if initial_cfg.vault is None or initial_cfg.notes is None or initial_cfg.mtg_dir is None:
        tracker.step("vault").fail(config.no_vault())
        return
    vault_root, notes_dir, mtg_dir = initial_cfg.vault, initial_cfg.notes, initial_cfg.mtg_dir
    # A vault with no decks in it yet has nothing to write.
    if not notes_dir.is_dir() and vault_root.is_dir():
        tracker.step("vault").warn(
            f"no notes folder at {notes_dir} yet: Riffle reads MTG decks from its mtg/; "
            f"another folder is notes in {config.config_path()}"
        )
        return
    # Write nothing in a missing or mistyped vault.
    if not notes_dir.is_dir():
        tracker.step("vault").fail(
            f"no notes folder at {notes_dir}; set vault and notes in {config.config_path()}"
        )
        return
    # A user of other games only has no MTG decks.
    if not mtg_dir.is_dir():
        tracker.step("vault").ok(f"no MTG decks in {notes_dir}; Riffle reads MTG decks only for now")
        return
    invalid_export = (
        manabox.why_not(initial_cfg.collection_csv) if initial_cfg.collection_csv.exists() else None
    )
    # Keep the existing notes rather than reporting an empty collection.
    if invalid_export:
        tracker.step("vault").fail(
            f"not written: {config.tilde(initial_cfg.collection_csv)} isn't a ManaBox export "
            f"({invalid_export}); "
            "riffle ingest manabox <export>"
        )
        return
    boxes = precons.load(vault.precon_boxes(mtg_dir))
    for problem in boxes.problems:
        tracker.step("precon boxes").warn(problem)
    picture_steps: list[Step] = []

    def progress(done: int, total: int | None) -> None:
        if not picture_steps:
            picture_steps.append(tracker.step("card pictures", total, "pictures"))
        picture_steps[0].update(done, total)

    from riffle.store import postgres

    # Keep the validated vault paths, but reload settings before catalog access.
    current_cfg = config.load()
    with ExitStack() as stack:
        try:
            catalog = stack.enter_context(postgres.open_catalog())
        except postgres.Unavailable as e:
            raise Unavailable(str(e)) from e
        try:
            inventory = syncmod.inventory(current_cfg.collection_csv, catalog, boxes)
        except manabox.NotExport as e:
            raise Unavailable(
                f"{e}; export the collection from ManaBox again, then: riffle ingest manabox <export>"
            ) from e
        if inventory.corrected:
            tracker.step("precon boxes").ok(_corrected(inventory.corrected, len(boxes.boxes)))
        card_notes = (
            syncmod.CardNotes(
                vault_root, notes_dir, current_cfg.card_images, None if offline else _picture, progress
            )
            if current_cfg.card_notes
            else None
        )
        result = syncmod.run(
            mtg_dir, inventory, catalog, card_notes=card_notes, hidden=current_cfg.hidden_folder
        )
    if picture_steps and result.card_notes:
        picture_steps[0].ok(f"{result.card_notes.fetched:,} fetched")
    for label, why in result.failed:
        tracker.step(label).fail(why)
    output.synced(result)


def _copies(n: int) -> str:
    return f"{n:,} cop{'y' if n == 1 else 'ies'}"


def _corrected(correction: precons.Corrected, boxes: int) -> str:
    corrected_copies, unmatched_copies = (
        sum(f.copies for f in correction.fixes),
        sum(s.copies for s in correction.short),
    )
    summary = f"{boxes} registered; {_copies(corrected_copies)} read as a box's printing"
    if unmatched_copies:
        summary += (
            f"; {_copies(unmatched_copies)} the boxes hold not recorded as theirs (riffle check collection)"
        )
    return summary


def _picture(url: str, dest: Path) -> int | None:
    """Download a card picture and return its byte count, or None if missing."""
    return net.download(url, dest, accept="image/*", timeout=30)


NETWORK_WAIT = 120.0


def _hosts() -> list[str]:
    """List distinct source hosts in probe order, with Scryfall first."""
    from urllib.parse import urlparse

    from riffle.ingest.remote import cardmarket, goatbots, mtgjson, pricelists, scryfall, tcgcsv

    urls = [scryfall.BULK_INDEX, mtgjson.BASE, goatbots.BASES[0], cardmarket.BASE, tcgcsv.BASE]
    urls += [plist.url for plist in pricelists.CARD_KINGDOM + pricelists.MANA_POOL]
    return list(dict.fromkeys(host for url in urls if (host := urlparse(url).hostname)))


def _online(tracker: Tracker) -> bool:
    """Wait for a source host to accept a connection.

    A scheduled sync can start before the network is ready. If no host answers
    within NETWORK_WAIT, fail the network step and let the caller sync offline.
    One reachable host is enough; each source reports its own download failures.
    """
    step = tracker.step("network")
    waited = net.wait_online(*_hosts(), timeout=NETWORK_WAIT)
    if waited is None:
        step.fail(f"no connection after {elapsed(NETWORK_WAIT)}; syncing offline")
        return False
    if waited < 1:
        step.drop()
    else:
        step.ok(f"up after {elapsed(waited)}")
    return True
