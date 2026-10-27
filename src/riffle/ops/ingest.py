"""Coordinate source refreshes, price snapshots, and collection pickup."""

import os
from collections.abc import Callable
from functools import partial
from pathlib import Path

from riffle import config, runs, times
from riffle.ingest.local import manabox
from riffle.ingest.remote import tcgcsv
from riffle.progress import Tracker, contained, failure


def refresh(tracker: Tracker, force: bool = False) -> None:
    """Scryfall's bulk files and set list, downloaded, and default cards loaded into the card catalog."""
    from riffle.ingest.remote import scryfall, scryfall_catalog

    scryfall.refresh(force=force, tracker=tracker)
    scryfall_catalog.update(tracker=tracker, force=force)


def snapshot_prices(tracker: Tracker, online: bool, delay: float = 0.1) -> tcgcsv.Snapshot | None:
    """Today's price snapshot. Problems are reported, never raised: a sync must finish without it."""
    from riffle.ingest.remote import cardmarket, goatbots, mtgjson, pricelists, scryfall, tcgcsv

    step = tracker.step("Scryfall prices")
    try:
        path, written = scryfall.snapshot_prices()
    except FileNotFoundError:
        step.fail("no bulk file yet — run: riffle ingest scryfall")
    except (ValueError, KeyError) as e:
        step.fail(str(e))
    except Exception as e:
        step.fail(failure(e))
    else:
        day = path.name.split(".")[0]
        if written:
            step.ok(f"kept {day}")
        elif online:
            step.ok(f"already have {day}")
        else:
            # Offline resyncs should not repeat "already have".
            step.drop()
    if not online:
        return None
    # Each source in turn; whatever one raises fails only its own steps (see failure).
    sources: list[tuple[str, Callable[..., object]]] = [
        ("MTGJSON prices", partial(mtgjson.watch, always=True)),
        ("MTGJSON 90 days", mtgjson.snapshot),
        ("GoatBots prices", partial(goatbots.watch, always=True)),
        ("GoatBots years", goatbots.snapshot),
        ("Cardmarket prices", partial(cardmarket.watch, always=True)),
        ("Card Kingdom prices", partial(pricelists.watch, pricelists.CARD_KINGDOM)),
        ("Mana Pool prices", partial(pricelists.watch, pricelists.MANA_POOL)),
    ]
    for label, snapshot in sources:
        with contained(tracker, label, failure) as scope:
            snapshot(tracker=scope)
    with contained(tracker, "tcgcsv prices", failure) as scope:
        return tcgcsv.watch(delay=delay, tracker=scope, always=True).snap
    return None


def copy_manabox(src: Path, dest: Path, made_ns: int) -> None:
    """Whole or not at all, and on the disk (runs.write): a sync reading the collection meanwhile
    sees the old file or the new one. Its time is `made_ns`, which the next sync's pickup is
    measured against: a sync's pickup gives it the export's, so the next export taken is one made
    after it; an ingest by hand, the ingest's, or the newest ManaBox*.csv's in Downloads when that
    is later (dated by a clock ahead of this Mac's), so an export put back by hand stays the
    collection."""
    runs.write(dest, src.read_bytes())
    os.utime(dest, ns=(made_ns, made_ns))


def keep_collection(cfg: config.Config) -> tuple[manabox.Kept | None, bool]:
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


def since(
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


def not_kept(e: OSError) -> str:
    return f"can't keep the export: {e}; the collection was not replaced"


def not_written(e: OSError, now: manabox.Kept) -> str:
    return (
        f"can't write the collection: {e}; it's as it was, and the export is kept as manabox/{now.path.name}"
    )


def pick_up(tracker: Tracker, cfg: config.Config) -> None:
    """Every ManaBox export Riffle sees kept with the others, the collection and each export in
    Downloads; then the newest export in Downloads made the collection, when it's newer than the
    collection. A ManaBox*.csv newer still that isn't an export is named, and left where it is."""
    stored = cfg.collection_csv
    modified = stored.stat().st_mtime if stored.exists() else None
    found: list[Path] = []
    pick = None
    for path in manabox.exports(cfg.downloads):
        newer = modified is None or path.stat().st_mtime > modified
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
        was, seeded = keep_collection(cfg)
        kept = {path: manabox.keep(path, cfg.manabox_exports) for path in reversed(found)}
    except OSError as e:
        tracker.step("collection").fail(not_kept(e))
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
    said, warnings, _ = since(was, taken, stored)
    try:
        copy_manabox(taken.path, stored, pick.stat().st_mtime_ns)
    except OSError as e:
        tracker.step("collection").fail(not_written(e, taken))
        return
    why = "newer than the collection" if modified is not None else "no collection yet"
    as_ = "kept as" if kept[pick][1] else "already kept as"
    tracker.step("collection").ok(
        f"picked up {pick.name} from Downloads ({why}), {as_} manabox/{taken.path.name}{said}"
    )
    for w in warnings:
        tracker.step("collection").warn(w)
