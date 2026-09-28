"""Scryfall bulk data: download the default-cards file and the set list, and keep
each day's prices.

Published daily; carries oracle ids, legalities, and prices — including
MTGO tix (Scryfall sources those from Cardhoarder). Every refresh asks the
bulk index, one API request, when Scryfall last published the file; only a
file for a newer day than the one kept is downloaded, from *.scryfall.io,
which has no rate limit, and then one more API request fetches the set list
(<data_dir>/scryfall/sets.json: parent sets and release dates, which card
objects lack). Headers and 429 handling: riffle.net. The card catalog in
Postgres is loaded from these files by riffle.ingest.scryfall_catalog.

Since 2026-07-20 bulk files are gzipped JSON Lines only, linked from
`jsonl_download_uri`. The old `download_uri` (one big JSON array) is gone;
it's still read if present so an older cached file keeps working.

Prices: the bulk file is replaced every day, so its prices would be lost.
snapshot_prices() keeps them: <data_dir>/scryfall/daily/<day>.jsonl.gz holds
one line per printing, {"id": ..., "prices": {...}} exactly as Scryfall gave
them (strings, in USD, EUR, and MTGO tix), where <day> is the bulk file's date.

bulk-meta.json records when Scryfall published the kept file. If it can't be read, the
kept file's day is unknown, so the next online refresh downloads the bulk file again. A
kept file cut short or corrupt is set aside as <name>.bad, for the same reason.
"""

import gzip
import json
import time
import zlib
from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path

from riffle import net
from riffle.config import data_dir
from riffle.progress import SILENT, Tracker

BULK_INDEX = "https://api.scryfall.com/bulk-data"
SETS = "https://api.scryfall.com/sets"
API_PAUSE = 0.1  # seconds between api.scryfall.com requests, as Scryfall asks: between set list pages


BAD = ".bad"  # a bulk file set aside as unreadable; removed by the next download


class CorruptBulk(ValueError):
    """The kept bulk file is cut short or corrupt."""


def meta_path() -> Path:
    return data_dir() / "bulk-meta.json"


def _kept_published() -> datetime | None:
    """When Scryfall published the kept bulk file, from bulk-meta.json, or None when that's
    missing or can't be read."""
    try:
        meta = json.loads(meta_path().read_text())
    except (OSError, ValueError):
        return None
    return _published(meta.get("updated_at")) if isinstance(meta, dict) else None


def sets_path() -> Path:
    return data_dir() / "scryfall" / "sets.json"


def fetch_sets(dest: Path | None = None) -> Path:
    """Scryfall's set list, every page of it, saved as one list object."""
    dest = dest or sets_path()
    found: list[dict] = []
    url: str | None = SETS
    while url:
        body = net.get(url, accept="application/json")
        if body is None:
            raise RuntimeError(f"Scryfall's set list is missing ({url})")
        page = json.loads(body)
        if not isinstance(page.get("data"), list):
            raise RuntimeError("Scryfall's set list isn't the expected JSON")
        found += page["data"]
        url = page.get("next_page") if page.get("has_more") else None
        if url:
            time.sleep(API_PAUSE)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    tmp.write_text(json.dumps({"object": "list", "has_more": False, "data": found}), encoding="utf-8")
    tmp.replace(dest)
    return dest


def read_sets(path: Path | None = None) -> list[dict]:
    """The saved set list; empty if there's none yet."""
    path = path or sets_path()
    return json.loads(path.read_text(encoding="utf-8"))["data"] if path.exists() else []


def remote_info(kind: str = "default_cards") -> dict:
    body = net.get(BULK_INDEX, accept="application/json")
    if body is None:
        raise RuntimeError(f"Scryfall's bulk index is missing ({BULK_INDEX})")
    for entry in json.loads(body)["data"]:
        if entry["type"] == kind:
            return entry
    raise RuntimeError(f"no bulk file of type {kind}")


def _published(stamp: object) -> datetime | None:
    """A bulk file's updated_at as a time, or None if it isn't one."""
    if not isinstance(stamp, str) or not stamp:
        return None
    try:
        published = datetime.fromisoformat(stamp)
    except ValueError:
        return None
    return published if published.tzinfo else published.replace(tzinfo=UTC)


def is_current(info: dict) -> bool:
    """Whether the kept bulk file is for the same day as the one Scryfall publishes now, or a
    later one. Days, not times: the price history keeps one file a day, so a second file the
    same day would add nothing to it. Going by the download's age instead skipped every other
    day for a job run at the same time each day, which starts a few seconds short of 24 hours
    after the last download finished."""
    if bulk_file() is None:
        return False
    kept = _kept_published()
    remote = _published(info.get("updated_at"))
    return kept is not None and remote is not None and kept.date() >= remote.date()


def download_url(info: dict) -> tuple[str, str]:
    """(url, local filename) — JSONL.gz now, the old JSON array as a fallback."""
    if info.get("jsonl_download_uri"):
        return info["jsonl_download_uri"], "default-cards.jsonl.gz"
    if info.get("download_uri"):
        return info["download_uri"], "default-cards.json"
    raise RuntimeError(f"Scryfall bulk entry has no download link: {sorted(info)}")


def download(
    dest_dir: Path | None = None, progress: net.Progress | None = None, info: dict | None = None
) -> tuple[Path, dict]:
    """The bulk file info names (the bulk index's entry, asked for when not given)."""
    dest_dir = dest_dir or data_dir()
    info = info or remote_info()
    url, filename = download_url(info)
    dest = dest_dir / filename
    fresh = dest_dir / (filename + ".new")  # checked before it replaces the last good file
    if net.download(url, fresh, progress=progress) is None:
        raise RuntimeError(f"Scryfall's bulk file is missing ({url})")
    with fresh.open("rb") as f:
        magic = f.read(2)
    if filename.endswith(".gz") and magic != b"\x1f\x8b":
        fresh.unlink()
        raise RuntimeError("downloaded bulk file isn't gzip — Scryfall's format may have changed again")
    fresh.replace(dest)
    for old in dest_dir.glob("default-cards.*"):
        if old != dest and not old.name.endswith((".part", ".new")):  # a set-aside .bad goes too
            old.unlink()
    return dest, info


def refresh(force: bool = False, tracker: Tracker = SILENT) -> None:
    """Download the bulk file when Scryfall has published one for a newer day than the one
    kept (see is_current), then the set list; force downloads it anyway. A failure is reported
    on its step, never raised: the last download stays, and a sync carries on with it."""
    step = tracker.step("Scryfall bulk data", unit="bytes")
    try:
        info = remote_info()
        if not force and is_current(info):
            published = _published(info.get("updated_at"))
            step.ok(f"current, Scryfall {published:%Y-%m-%d}" if published else "current")
            if not sets_path().exists():
                refresh_sets(tracker)
            return
        path, info = download(progress=step.update, info=info)
    except Exception as e:
        step.fail(str(e) or type(e).__name__)
        return
    meta = {"updated_at": info.get("updated_at"), "downloaded_at": datetime.now(UTC).isoformat()}
    tmp = meta_path().with_name(meta_path().name + ".part")
    tmp.write_text(json.dumps(meta))
    tmp.replace(meta_path())
    updated = str(info.get("updated_at") or "?")
    step.ok(f"{path.stat().st_size / 1e6:,.1f} MB, Scryfall {updated[:10]}")
    refresh_sets(tracker)


def refresh_sets(tracker: Tracker = SILENT) -> None:
    """Fetch the set list. The catalog builds any set the list lacks from what cards say, so a
    failure is reported, never raised: it can't stop a sync."""
    step = tracker.step("Scryfall set list")
    try:
        sets = read_sets(fetch_sets())
    except Exception as e:
        step.fail(str(e))
        return
    step.ok(f"{len(sets):,} sets")


def prices_dir() -> Path:
    return data_dir() / "scryfall" / "daily"


def bulk_file(dest_dir: Path | None = None) -> Path | None:
    """The downloaded bulk file, if any."""
    dest_dir = dest_dir or data_dir()
    files = [p for p in dest_dir.glob("default-cards.*") if not p.name.endswith((".part", ".new", BAD))]
    return max(files, key=lambda p: p.stat().st_mtime) if files else None


def cards(bulk: Path) -> Iterator[dict]:
    """Every card object in a bulk file, JSON Lines (gzipped or not) or the old JSON array."""
    if ".jsonl" in bulk.name:
        opener = gzip.open if bulk.name.endswith(".gz") else open
        with opener(bulk, "rt", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    yield json.loads(line)
    else:
        with bulk.open(encoding="utf-8") as f:
            yield from json.load(f)


def bulk_updated_at(bulk: Path) -> datetime:
    """When Scryfall published the bulk file: its updated_at, else the file's own time."""
    return _kept_published() or datetime.fromtimestamp(bulk.stat().st_mtime, UTC)


def bulk_day(bulk: Path) -> date:
    """The day the bulk file's prices are for."""
    return bulk_updated_at(bulk).date()


def snapshot_prices(bulk: Path | None = None) -> tuple[Path, bool]:
    """Keep the bulk file's prices for its day. Returns (file, whether it was written now).
    A bulk file cut short or corrupt is set aside, so the next online refresh downloads it
    again, and CorruptBulk says so."""
    bulk = bulk or bulk_file()
    if bulk is None:
        raise FileNotFoundError("no Scryfall bulk file yet")
    dest = prices_dir() / f"{bulk_day(bulk).isoformat()}.jsonl.gz"
    if dest.exists():
        return dest, False
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    try:
        with gzip.open(tmp, "wt", encoding="utf-8") as out:
            for card in cards(bulk):
                line = json.dumps({"id": card["id"], "prices": card.get("prices")}, separators=(",", ":"))
                out.write(line + "\n")
    except (EOFError, zlib.error, gzip.BadGzipFile) as e:
        tmp.unlink(missing_ok=True)
        bulk.replace(bulk.with_name(bulk.name + BAD))
        raise CorruptBulk(
            f"{bulk.name} is cut short or corrupt ({e or type(e).__name__}); set aside, "
            "and downloaded again by the next online sync"
        ) from e
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    tmp.replace(dest)
    return dest, True
