"""Handle synchronization and watching the vault."""

from pathlib import Path

import typer

from riffle import config, net, times, vault
from riffle import sync as syncmod
from riffle.cli import _common as common
from riffle.ingest.local import manabox, precons
from riffle.ops import disk
from riffle.progress import Step, Tracker, elapsed, failure


def _some(names: list[str], most: int = 10) -> str:
    """The names, or the first ten and how many more: the sync after a vault moves its lists
    removes thousands of notes."""
    more = f" and {len(names) - most:,} more" if len(names) > most else ""
    return ", ".join(names[:most]) + more


def _run_sync(tracker: Tracker, offline: bool = True) -> None:
    """Everything the data affects: collection pickup, prices, generated notes, logs."""
    from riffle.cli import ingest as ingest_mod
    from riffle.cli import prices as prices_mod

    cfg0 = config.load()
    if not offline:
        offline = not _online(tracker)
    if not offline:
        ingest_mod._refresh(tracker)
    prices_mod._snapshot_prices(tracker, online=not offline)
    if not offline:
        disk.check(tracker)
    _pick_up(tracker, cfg0)
    if not cfg0.collection_csv.exists():
        common._echo(
            "no collection yet — export from ManaBox to ~/Downloads, or: riffle ingest manabox <csv>",
            err=True,
        )
    if cfg0.old_notes:
        tracker.step("vault setting").warn(cfg0.old_notes)
    for why in cfg0.unread:
        tracker.step("setting").warn(why)
    try:
        root, notes, mtg = common._vault(cfg0)
    except typer.BadParameter as e:  # the day's data is kept all the same
        tracker.step("vault").fail(str(e))
        return
    if not notes.is_dir() and root.is_dir():  # a vault with no decks in it yet: nothing to write
        tracker.step("vault").warn(
            f"no notes folder at {notes} yet: Riffle reads MTG decks from its mtg/; "
            f"another folder is notes in {config.config_path()}"
        )
        return
    if not notes.is_dir():  # a missing or mistyped vault: nothing is written there
        tracker.step("vault").fail(
            f"no notes folder at {notes}; set vault and notes in {config.config_path()}"
        )
        return
    if not mtg.is_dir():  # a user of other games only: not a fault
        tracker.step("vault").ok(f"no MTG decks in {notes}; Riffle reads MTG decks only for now")
        return
    not_export = manabox.why_not(cfg0.collection_csv) if cfg0.collection_csv.exists() else None
    if not_export:  # the notes as they were beat notes that say nothing is owned
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

    with common._setup(boxes) as (cfg, cat, inv):
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
    common._echo(
        f"{len(res.decks)} decks · {res.changed_notes} notes updated · "
        f"versions changed: {', '.join(res.versions) or 'none'}"
    )
    if res.moved:
        common._echo(f"moved version logs beside their lists: {_some(res.moved)}")
    if res.card_notes:
        c = res.card_notes
        common._echo(f"card notes: {c.cards:,} ({c.written:,} written, {c.removed:,} removed)")
    elif res.cards_removed:
        common._echo(f"card notes: off ({res.cards_removed:,} removed)")
    if res.removed:
        common._echo(f"removed stale generated notes: {_some(res.removed)}")
    for w in res.warnings:
        common._echo(f"  ! {w}", err=True)


def _pick_up(tracker: Tracker, cfg: config.Config) -> None:
    """Every ManaBox export Riffle sees kept with the others, the collection and each export in
    Downloads; then the newest export in Downloads made the collection, when it's newer than the
    collection. A ManaBox*.csv newer still that isn't an export is named, and left where it is."""
    from riffle.cli import ingest as ingest_mod

    stored = cfg.collection_csv
    since = stored.stat().st_mtime if stored.exists() else None
    found: list[Path] = []
    pick = None
    for path in manabox.exports(cfg.downloads):
        newer = since is None or path.stat().st_mtime > since
        why = manabox.why_not(path)
        if why is None:
            if not found and newer:
                pick = path
            found.append(path)
        elif not found and newer:
            tracker.step("collection").warn(
                f"not picked up: {path.name} in Downloads isn't a ManaBox export ({why})"
            )
    try:
        was, seeded = ingest_mod._keep_collection(cfg)
        kept = {path: manabox.keep(path, cfg.manabox_exports) for path in reversed(found)}
    except OSError as e:
        tracker.step("collection").fail(ingest_mod._not_kept(e))
        return
    taken = kept[pick][0] if pick else None
    fresh = sorted(
        (k for k, new in [(was, seeded), *kept.values()] if k and new and k != taken), key=lambda k: k.at
    )
    if fresh:
        span = times.shown(fresh[0].at) + (f" to {times.shown(fresh[-1].at)}" if len(fresh) > 1 else "")
        tracker.step("collection").ok(
            f"kept {len(fresh)} export{'s' * (len(fresh) != 1)} in manabox/, {span}"
        )
    if pick is None or taken is None:
        return
    said, warnings, _ = ingest_mod._since(was, taken, stored)
    try:
        ingest_mod._copy_manabox(taken.path, stored, pick.stat().st_mtime_ns)
    except OSError as e:
        tracker.step("collection").fail(ingest_mod._not_written(e, taken))
        return
    why = "newer than the collection" if since is not None else "no collection yet"
    as_ = "kept as" if kept[pick][1] else "already kept as"
    tracker.step("collection").ok(
        f"picked up {pick.name} from Downloads ({why}), {as_} manabox/{taken.path.name}{said}"
    )
    for w in warnings:
        tracker.step("collection").warn(w)


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
    from riffle.cli import db as db_mod

    if watch:
        _watch_vault(interval)
        return
    with common._run("riffle sync") as tracker:
        _run_sync(tracker, offline=offline)
        if not offline:
            db_mod._backup(tracker, warn_elsewhere=True)


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
