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
    """Refresh Scryfall bulk data and sets, then load the card catalog."""
    from riffle.ingest.remote import scryfall, scryfall_catalog

    scryfall.refresh(force=force, tracker=tracker)
    scryfall_catalog.update(tracker=tracker, force=force)


def snapshot_prices(tracker: Tracker, online: bool, delay: float = 0.1) -> tcgcsv.Snapshot | None:
    """Snapshot retained Scryfall prices and, when online, collect every price source.

    Each source records its own failure so later sources and note generation can
    continue. An offline call still builds Scryfall prices from the retained bulk
    file, but makes no requests.

    Args:
        tracker: Record each source's progress and outcome.
        online: Allow source downloads after the local Scryfall snapshot.
        delay: Seconds between TCGCSV requests.

    Returns:
        The TCGCSV snapshot for the caller to present, or None if the watch has
        no snapshot, fails, or is skipped offline. Other sources report via tracker.
    """
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
    """Replace the collection durably, then assign its modification time in nanoseconds.

    runs.write flushes the bytes before replacing dest, so readers see a whole
    file. Automatic pickup uses the export's time. Manual ingest uses the later
    of the ingest time and the newest download's time, keeping a future-dated
    download from undoing a manual restore on the next sync.
    """
    runs.write(dest, src.read_bytes())
    os.utime(dest, ns=(made_ns, made_ns))


def keep_collection(cfg: config.Config) -> tuple[manabox.Kept | None, bool]:
    """Retain the current ManaBox collection before anything replaces it.

    Returns:
        The retained export and whether it was newly kept, or (None, False) when
        the collection is absent or is not an export.

    Raises:
        OSError: The collection cannot be read or retained. Callers must leave
            the collection in place when retention fails.
    """
    collection_path = cfg.collection_csv
    if not collection_path.exists():
        return None, False
    if manabox.why_not(collection_path):
        collection_path.open("rb").close()
        return None, False
    return manabox.keep(collection_path, cfg.manabox_exports)


def since(
    was: manabox.Kept | None, now: manabox.Kept, before: Path
) -> tuple[str, list[str], manabox.Change | None]:
    """Compare the collection at before with the retained export now.

    was supplies the previous export's timestamp. Compare the live before file
    rather than was.path, so a changed history copy cannot hide a difference.

    Returns:
        A summary suffix, warnings, and the change. With no previous export,
        return a notice and no change; changed columns produce a warning and
        skip the comparison.
    """
    if was is None:
        return "; nothing to compare", [], None
    previous_time = times.shown(was.at)
    changed_columns = manabox.columns_changed(before, now.path)
    if changed_columns:
        return (
            "",
            [
                f"the export's columns changed since the export of {previous_time}: "
                f"{', '.join(changed_columns)}; no diff"
            ],
            None,
        )
    previous_holdings = manabox.load(before)
    change = manabox.change(previous_holdings, manabox.load(now.path))
    previous_copies = sum(h.quantity for h in previous_holdings)
    warnings = []
    if change.removed * 2 > previous_copies:
        warnings.append(
            f"the export of {times.shown(now.at)} drops {change.removed:,} of {previous_copies:,} copies: "
            "check it's the whole collection (riffle ingest manabox --diff)"
        )
    return f"; since the export of {previous_time}: {change}", warnings, change


def not_kept(e: OSError) -> str:
    return f"can't keep the export: {e}; the collection was not replaced"


def not_written(e: OSError, now: manabox.Kept) -> str:
    return (
        f"can't write the collection: {e}; it's as it was, and the export is kept as manabox/{now.path.name}"
    )


def pick_up(tracker: Tracker, cfg: config.Config) -> None:
    """Retain ManaBox exports and replace the collection with a newer valid download.

    Scan Downloads newest first and warn about invalid exports newer than the
    collection. Retain the current collection and every valid export before
    replacing anything; a retention failure ends the step with no replacement.
    Copy the newest valid candidate only if it is newer than the collection,
    preserving its modification time for the next pickup comparison.
    """
    collection_path = cfg.collection_csv
    collection_mtime = collection_path.stat().st_mtime if collection_path.exists() else None
    exports: list[Path] = []
    candidate = None
    for path in manabox.exports(cfg.downloads):
        newer = collection_mtime is None or path.stat().st_mtime > collection_mtime
        reason = manabox.why_not(path)
        if reason is None:
            if not exports and newer:
                candidate = path
            exports.append(path)
        elif not exports and newer:
            tracker.step("collection").warn(
                f"not picked up: {path.name} in Downloads isn't a ManaBox export ({reason})"
            )
    try:
        previous, previous_kept = keep_collection(cfg)
        retained = {path: manabox.keep(path, cfg.manabox_exports) for path in reversed(exports)}
    except OSError as e:
        tracker.step("collection").fail(not_kept(e))
        return
    selected = retained[candidate][0] if candidate else None
    newly_kept = sorted(
        (
            export
            for export, is_new in [(previous, previous_kept), *retained.values()]
            if export and is_new and export != selected
        ),
        key=lambda export: export.at,
    )
    if newly_kept:
        span = times.shown(newly_kept[0].at) + (
            f" to {times.shown(newly_kept[-1].at)}" if len(newly_kept) > 1 else ""
        )
        tracker.step("collection").ok(
            f"kept {len(newly_kept)} export{'s' * (len(newly_kept) != 1)} in manabox/, {span}"
        )
    if candidate is None or selected is None:
        return
    changes, warnings, _ = since(previous, selected, collection_path)
    try:
        copy_manabox(selected.path, collection_path, candidate.stat().st_mtime_ns)
    except OSError as e:
        tracker.step("collection").fail(not_written(e, selected))
        return
    reason = "newer than the collection" if collection_mtime is not None else "no collection yet"
    retention_status = "kept as" if retained[candidate][1] else "already kept as"
    tracker.step("collection").ok(
        f"picked up {candidate.name} from Downloads ({reason}), "
        f"{retention_status} manabox/{selected.path.name}{changes}"
    )
    for warning in warnings:
        tracker.step("collection").warn(warning)
