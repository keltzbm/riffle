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
    """Receive results that the caller presents between workflow steps."""

    def prices(self, snapshot: tcgcsv.Snapshot) -> None: ...

    def no_collection(self) -> None: ...

    def synced(self, result: syncmod.SyncResult) -> None: ...


class Unavailable(RuntimeError):
    """The catalog or inventory cannot be read; end the run before its backup."""


def run(tracker: Tracker, output: Output, *, offline: bool = True) -> None:
    """Sync from retained data or refresh online, then back up an online invocation."""
    cfg = config.load()
    online = not offline and _online(tracker)
    if online:
        ingest.refresh(tracker)
    snap = ingest.snapshot_prices(tracker, online=online)
    if snap is not None:
        output.prices(snap)
    if online:
        disk.check(tracker)
    ingest.pick_up(tracker, cfg)
    if not cfg.collection_csv.exists():
        output.no_collection()
    _notes(cfg, tracker, output, offline=not online)
    if not offline:
        database.backup(tracker, config.load().database_url, warn_elsewhere=True)


def _notes(cfg0: config.Config, tracker: Tracker, output: Output, *, offline: bool) -> None:
    """Validate the vault and collection, then generate notes from one catalog snapshot."""
    if cfg0.old_notes:
        tracker.step("vault setting").warn(cfg0.old_notes)
    for why in cfg0.unread:
        tracker.step("setting").warn(why)
    if cfg0.vault is None or cfg0.notes is None or cfg0.mtg_dir is None:
        tracker.step("vault").fail(config.no_vault())
        return
    root, notes, mtg = cfg0.vault, cfg0.notes, cfg0.mtg_dir
    # A vault with no decks in it yet has nothing to write.
    if not notes.is_dir() and root.is_dir():
        tracker.step("vault").warn(
            f"no notes folder at {notes} yet: Riffle reads MTG decks from its mtg/; "
            f"another folder is notes in {config.config_path()}"
        )
        return
    # Write nothing in a missing or mistyped vault.
    if not notes.is_dir():
        tracker.step("vault").fail(
            f"no notes folder at {notes}; set vault and notes in {config.config_path()}"
        )
        return
    # A user of other games only has no MTG decks.
    if not mtg.is_dir():
        tracker.step("vault").ok(f"no MTG decks in {notes}; Riffle reads MTG decks only for now")
        return
    not_export = manabox.why_not(cfg0.collection_csv) if cfg0.collection_csv.exists() else None
    # Keep the existing notes rather than reporting an empty collection.
    if not_export:
        tracker.step("vault").fail(
            f"not written: {config.tilde(cfg0.collection_csv)} isn't a ManaBox export ({not_export}); "
            "riffle ingest manabox <export>"
        )
        return
    boxes = precons.load(vault.precon_boxes(mtg))
    for problem in boxes.problems:
        tracker.step("precon boxes").warn(problem)
    pictures: list[Step] = []

    def progress(done: int, total: int | None) -> None:
        if not pictures:
            pictures.append(tracker.step("card pictures", total, "pictures"))
        pictures[0].update(done, total)

    from riffle.store import postgres

    cfg = config.load()
    with ExitStack() as stack:
        try:
            cat = stack.enter_context(postgres.open_catalog())
        except postgres.Unavailable as e:
            raise Unavailable(str(e)) from e
        try:
            inv = syncmod.inventory(cfg.collection_csv, cat, boxes)
        except manabox.NotExport as e:
            raise Unavailable(
                f"{e}; export the collection from ManaBox again, then: riffle ingest manabox <export>"
            ) from e
        if inv.corrected:
            tracker.step("precon boxes").ok(_corrected(inv.corrected, len(boxes.boxes)))
        card_notes = (
            syncmod.CardNotes(root, notes, cfg.card_images, None if offline else _picture, progress)
            if cfg.card_notes
            else None
        )
        res = syncmod.run(mtg, inv, cat, card_notes=card_notes, hidden=cfg.hidden_folder)
    if pictures and res.card_notes:
        pictures[0].ok(f"{res.card_notes.fetched:,} fetched")
    for label, why in res.failed:
        tracker.step(label).fail(why)
    output.synced(res)


def _copies(n: int) -> str:
    return f"{n:,} cop{'y' if n == 1 else 'ies'}"


def _corrected(c: precons.Corrected, boxes: int) -> str:
    fixed, short = sum(f.copies for f in c.fixes), sum(s.copies for s in c.short)
    said = f"{boxes} registered; {_copies(fixed)} read as a box's printing"
    if short:
        said += f"; {_copies(short)} the boxes hold not recorded as theirs (riffle check collection)"
    return said


def _picture(url: str, dest: Path) -> int | None:
    """A card's picture from Scryfall's image server, written whole to dest."""
    return net.download(url, dest, accept="image/*", timeout=30)


NETWORK_WAIT = 120.0


def _hosts() -> list[str]:
    """Every host an online sync asks, Scryfall's first."""
    from urllib.parse import urlparse

    from riffle.ingest.remote import cardmarket, goatbots, mtgjson, pricelists, scryfall, tcgcsv

    urls = [scryfall.BULK_INDEX, mtgjson.BASE, goatbots.BASES[0], cardmarket.BASE, tcgcsv.BASE]
    urls += [plist.url for plist in pricelists.CARD_KINGDOM + pricelists.MANA_POOL]
    return list(dict.fromkeys(host for url in urls if (host := urlparse(url).hostname)))


def _online(tracker: Tracker) -> bool:
    """Whether any source can be reached, after waiting up to NETWORK_WAIT for one: the
    scheduled job runs as the Mac wakes, before the network is back. Without any the sync goes
    offline. One source that's down (Scryfall's name not resolving, say) fails its own steps
    and costs the others nothing."""
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
