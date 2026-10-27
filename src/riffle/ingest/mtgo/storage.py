"""Read and retain MTGO events and raw payloads."""

import gzip
import json
from collections import Counter
from collections.abc import Iterable, Iterator
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path

from riffle import files, trickle
from riffle.config import data_dir
from riffle.ingest.mtgo import pages as pages_mod
from riffle.models.metagame import Event


def store_dir() -> Path:
    return data_dir() / "mtgo"


def event_path(slug: str) -> Path:
    """Where an event is kept: under its year and month, so a command that reads recent
    events lists only their months' folders."""
    parsed = pages_mod.parse_slug(slug)
    if parsed is None:
        return store_dir() / f"{slug}.json"
    return store_dir() / parsed[1][:4] / parsed[1][5:7] / f"{slug}.json"


def stored_path(slug: str) -> Path:
    """The event's file: under its month, or in the store's top folder, where earlier
    builds kept every event until the next trickle run moves it."""
    path, top = event_path(slug), store_dir() / f"{slug}.json"
    return top if not path.exists() and top.exists() else path


def save(event: Event) -> Path:
    """Whole or not at all: a run cut off mid-write leaves no event file."""
    path = event_path(event.slug)
    with files.replacing(path) as part:
        part.write_text(json.dumps(asdict(event), indent=1), encoding="utf-8")
    return path


def _event_files(folder: Path, since: date | None) -> Iterator[Path]:
    """Every event file in the store, skipping the months before since: the top folder's,
    kept there by earlier builds, then each month's. A top-folder copy of an event its month
    holds too is passed over, so no event is read twice; _file_events sets it aside."""
    if not folder.exists():
        return
    for path in folder.glob("*.json"):
        parsed = pages_mod.parse_slug(path.stem)
        if parsed is None or not (folder / parsed[1][:4] / parsed[1][5:7] / path.name).exists():
            yield path
    first = since.isoformat()[:7] if since else ""
    for month in sorted(folder.glob("[0-9][0-9][0-9][0-9]/[0-9][0-9]")):
        if f"{month.parent.name}-{month.name}" >= first:
            yield from month.glob("*.json")


def kept_days(since: date | None = None) -> Counter[str]:
    """Events stored with their decks, by the dates in their names (riffle status). One dated
    before since is counted by its name, unread: a strip shows the days from since, and needs
    the earlier ones only to know where its first day is."""
    found: Counter[str] = Counter()
    first = since.isoformat() if since else ""
    for path in _event_files(store_dir(), None):
        parsed = pages_mod.parse_slug(path.stem)
        if parsed is not None and (parsed[1] < first or _has_decks(path)):
            found[parsed[1]] += 1
    return found


def _file_events() -> int:
    """Events earlier builds kept in the store's top folder, moved under their months. A
    rename: nothing is rewritten, and a file whose name isn't an event's stays. One whose month
    holds it already is set aside beside it as <slug>.json.dup, both copies kept."""
    moved = 0
    for path in sorted(store_dir().glob("*.json")) if store_dir().exists() else ():
        dest, dup = event_path(path.stem), path.with_name(f"{path.name}.dup")
        if pages_mod.parse_slug(path.stem) is None:
            continue
        if dest.exists():
            if not dup.exists():
                path.replace(dup)
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        path.replace(dest)
        moved += 1
    return moved


def load(
    fmt: str | Iterable[str] | None = None,
    since: date | None = None,
    kinds: Iterable[str] | None = None,
    folder: Path | None = None,
) -> list[Event]:
    """Stored events, newest first."""
    folder = folder or store_dir()
    kinds = set(kinds or pages_mod.KINDS)
    fmts = {fmt} if isinstance(fmt, str) else set(fmt or ())
    events = []
    for p in _event_files(folder, since):
        e = Event.from_dict(json.loads(p.read_text(encoding="utf-8")))
        if not e.decks:
            continue
        if fmts and e.format not in fmts:
            continue
        if since and e.date < since.isoformat():
            continue
        if e.kind in kinds:
            events.append(e)
    return sorted(events, key=lambda e: (e.date, e.event_id), reverse=True)


def raw_path(slug: str) -> Path:
    """The page's whole data object, kept apart from the parsed events: load() reads
    every .json under the months' folders."""
    return store_dir() / "raw" / f"{slug}.json.gz"


def save_raw(slug: str, data: dict, url: str, fetched: datetime, size: int) -> Path:
    """Standings, records, player IDs and per-printing rows, as mtgo.com gave them."""
    path = raw_path(slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part")
    payload = {"url": url, "fetched": trickle.stamp(fetched), "bytes": size, "data": data}
    with gzip.open(tmp, "wt", encoding="utf-8") as f:
        json.dump(payload, f)
    tmp.replace(path)
    return path


def is_stored(slug: str) -> bool:
    """Stored with decks. An empty event was saved before its lists were
    published (older versions did this) — it counts as not stored."""
    path = stored_path(slug)
    return path.exists() and _has_decks(path)


def _has_decks(path: Path) -> bool:
    try:
        return bool(json.loads(path.read_text(encoding="utf-8")).get("decks"))
    except (OSError, ValueError):
        return False
