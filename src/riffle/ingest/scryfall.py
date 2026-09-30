"""Scryfall bulk data: every file Scryfall publishes, kept by its publish time, the set
list, and each day's prices.

Scryfall publishes its bulk files about twice a day (09:05 and 21:05 UTC on 2026-09-28):
default cards (each card in English, or in the one language it was printed in), all cards
(every language), oracle cards, unique artwork, rulings, and Tagger's oracle and art tags.
Each refresh asks the bulk index, one API request, and downloads every file whose publish
time it hasn't seen, from *.scryfall.io, which has no rate limit. A download is read whole,
then kept as served in

    <data_dir>/scryfall/bulk/<type>/<published>.jsonl.gz   <published> its updated_at, in UTC
    <data_dir>/scryfall/bulk/checks.jsonl                  every publish seen, kept or not

unless its contents, uncompressed, are byte for byte those of the newest file kept of its
type: then checks.jsonl records the publish, same_as that file, and nothing else is written.
A download that isn't whole gzip is set aside beside the kept files as
<published>-<fetched>.jsonl.gz.bad, never deleted, and the next refresh downloads the file
again. Only the first broken copy of a publish is set aside: a later one goes in checks.jsonl
(its SHA-256, size and why, not_kept) and is deleted, so a file served broken for days is kept
once, not twice a day. Nothing kept is ever removed.

The newest default-cards file is the bulk file: the card catalog in Postgres is loaded from it
by riffle.ingest.scryfall_catalog. After each new one, one more API request fetches the set
list (parent sets and release dates, which card objects lack) into
<data_dir>/scryfall/sets.json, and into scryfall/sets/<fetched>.json whenever it has changed;
Scryfall gives it no publish time, so it's named by fetch time. scryfall/sets/checks.jsonl
records every fetch. Headers and 429 handling: riffle.net.

Since 2026-07-20 bulk files are gzipped JSON Lines only, linked from
`jsonl_download_uri`. The old `download_uri` (one big JSON array) is gone;
it's still read if present so an older cached file keeps working.

Prices: snapshot_prices() keeps the bulk file's prices for its day in
<data_dir>/scryfall/daily/<day>.jsonl.gz, one line per printing, {"id": ..., "prices": {...}}
exactly as Scryfall gave them (strings, in USD, EUR, and MTGO tix), where <day> is the date
the bulk file was published. The day's first file gives them; the second file's prices stay
in its kept copy. A kept file found cut short or corrupt then is set aside as <name>.bad,
and the next online refresh downloads it again.

Before 0.4.0 Riffle kept only the latest default-cards file, as
<data_dir>/default-cards.jsonl.gz, with its publish time in bulk-meta.json, and deleted the
one before. The first refresh moves that file into scryfall/bulk/default_cards/.
"""

import gzip
import hashlib
import json
import shutil
import time
import zlib
from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path

from riffle import net, runs, times
from riffle.config import data_dir
from riffle.progress import SILENT, Step, Tracker

BULK_INDEX = "https://api.scryfall.com/bulk-data"
SETS = "https://api.scryfall.com/sets"
API_PAUSE = 0.1  # seconds between api.scryfall.com requests, as Scryfall asks: between set list pages
DEFAULT = "default_cards"  # the bulk file the catalog and prices are read from


BAD = ".bad"  # a file set aside as unreadable; kept, never read
STAMP = "%Y-%m-%dT%H%M%SZ"


class CorruptBulk(ValueError):
    """The kept bulk file is cut short or corrupt."""


def meta_path() -> Path:
    """Where Riffle recorded the publish time of the one bulk file it kept before 0.4.0."""
    return data_dir() / "bulk-meta.json"


def _kept_published() -> datetime | None:
    """When Scryfall published the bulk file kept before 0.4.0, from bulk-meta.json, or None
    when that's missing or can't be read."""
    try:
        meta = json.loads(meta_path().read_text())
    except (OSError, ValueError):
        return None
    return _published(meta.get("updated_at")) if isinstance(meta, dict) else None


def _stamp(t: datetime) -> str:
    return t.astimezone(UTC).strftime(STAMP)


def bulk_dir(kind: str = DEFAULT) -> Path:
    return data_dir() / "scryfall" / "bulk" / kind


def checks_path() -> Path:
    return data_dir() / "scryfall" / "bulk" / "checks.jsonl"


def sets_path() -> Path:
    return data_dir() / "scryfall" / "sets.json"


def sets_dir() -> Path:
    return data_dir() / "scryfall" / "sets"


def _append(log: Path, line: dict) -> None:
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a", encoding="utf-8") as f:
        f.write(json.dumps(line) + "\n")


def _lines(log: Path) -> list[dict]:
    """A check log's lines; one that can't be read is skipped."""
    try:
        text = log.read_text(encoding="utf-8")
    except OSError:
        return []
    out = []
    for raw in text.splitlines():
        try:
            line = json.loads(raw)
        except ValueError:
            continue
        if isinstance(line, dict):
            out.append(line)
    return out


def _digest(path: Path, gzipped: bool | None = None) -> str:
    """The SHA-256 of a file's contents, uncompressed when it's gzip (by its name unless
    gzipped says). Reads it whole, so a file cut short or corrupt raises here."""
    h = hashlib.sha256()
    opener = gzip.open if (path.name.endswith(".gz") if gzipped is None else gzipped) else open
    with opener(path, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def _set_aside(path: Path) -> Path:
    """path renamed to <name>.bad, beside it, or to <stem>-<now>... when that's taken; never
    deleted."""
    dest = path.with_name(path.name + BAD)
    if dest.exists():
        stem, _, rest = path.name.partition(".")
        dest = path.with_name(f"{stem}-{_stamp(times.now())}.{rest}{BAD}")
    path.replace(dest)
    return dest


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


def _kept_sets() -> list[Path]:
    return sorted(sets_dir().glob("*.json"))


def _adopt_sets() -> None:
    """A set list kept before 0.4.0, with no copy in scryfall/sets/ yet, copied there first,
    named by its file time: the time it was fetched."""
    if sets_path().exists() and not _kept_sets():
        when = datetime.fromtimestamp(sets_path().stat().st_mtime, UTC)
        sets_dir().mkdir(parents=True, exist_ok=True)
        shutil.copyfile(sets_path(), sets_dir() / f"{_stamp(when)}.json")


def keep_sets(fetched: datetime) -> str:
    """A copy of sets.json, just fetched, in scryfall/sets/ unless it's the same as the newest
    copy there; either way the fetch goes in the check log. What the step says."""
    log = sets_dir() / "checks.jsonl"
    sha = hashlib.sha256(sets_path().read_bytes()).hexdigest()
    kept = _kept_sets()
    newest = kept[-1] if kept else None
    line = {"fetched": _stamp(fetched), "sha256": sha}
    if newest is not None and hashlib.sha256(newest.read_bytes()).hexdigest() == sha:
        _append(log, line | {"same_as": newest.name})
        return f"unchanged since {times.shown(_from_stamp(newest.name))}"
    dest = sets_dir() / f"{_stamp(fetched)}.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(sets_path(), dest)
    _append(log, line | {"kept": dest.name})
    return "changed, kept" if newest is not None else "kept"


def _published(stamp: object) -> datetime | None:
    """A bulk file's updated_at as a time, or None if it isn't one."""
    if not isinstance(stamp, str) or not stamp:
        return None
    try:
        published = datetime.fromisoformat(stamp)
    except ValueError:
        return None
    return published if published.tzinfo else published.replace(tzinfo=UTC)


def _from_stamp(name: str) -> datetime:
    """The time a kept file's name starts with (2026-09-28T090539Z...)."""
    return datetime.strptime(name[: len("2026-09-28T090539Z")], STAMP).replace(tzinfo=UTC)


def bulk_index() -> list[dict]:
    """Every bulk file Scryfall lists, as the bulk index gives them."""
    body = net.get(BULK_INDEX, accept="application/json")
    if body is None:
        raise RuntimeError(f"Scryfall's bulk index is missing ({BULK_INDEX})")
    entries = json.loads(body).get("data")
    if not isinstance(entries, list) or not all(isinstance(e, dict) and e.get("type") for e in entries):
        raise RuntimeError("Scryfall's bulk index isn't the expected JSON")
    return entries


def kept_files(kind: str = DEFAULT) -> list[Path]:
    """The files kept of a type, oldest first by publish time (a second copy of one publish,
    named <published>-<fetched>, after the first)."""
    files = [p for p in bulk_dir(kind).glob("*.json*") if not p.name.endswith((".part", ".new", BAD))]
    return sorted(files, key=lambda p: p.name.split(".")[0])


def _seen(kind: str, published: datetime) -> bool:
    """Whether this publish is kept, or was checked and found the same as a file still kept.
    A kept file set aside as unreadable doesn't count, so the next refresh fetches it again."""
    stamp = _stamp(published)
    kept = kept_files(kind)
    if any(p.name.startswith(stamp) for p in kept):
        return True
    names = {p.name for p in kept}
    return any(
        line.get("type") == kind and line.get("stamp") == stamp and line.get("same_as") in names
        for line in _lines(checks_path())
    )


def is_current(info: dict) -> bool:
    """Whether the default-cards file Scryfall publishes now is kept, or was checked and found
    the same as one kept. By publish time, not day: Scryfall publishes about twice a day, and
    each file is kept."""
    published = _published(info.get("updated_at"))
    return published is not None and bulk_file() is not None and _seen(DEFAULT, published)


def download_url(info: dict) -> tuple[str, str]:
    """(url, the kept file's suffix): JSONL.gz now, the old JSON array as a fallback."""
    if info.get("jsonl_download_uri"):
        return info["jsonl_download_uri"], ".jsonl.gz"
    if info.get("download_uri"):
        return info["download_uri"], ".json"
    raise RuntimeError(f"Scryfall bulk entry has no download link: {sorted(info)}")


def _kept_digest(kind: str, path: Path) -> str:
    """A kept file's digest, from the check log, else read from the file."""
    for line in reversed(_lines(checks_path())):
        if line.get("type") == kind and line.get("kept") == path.name and line.get("sha256"):
            return str(line["sha256"])
    return _digest(path)


def download(info: dict, progress: net.Progress | None = None) -> tuple[Path, bool]:
    """Download the bulk file info names (a bulk index entry) and keep it, unless its contents
    are the newest kept file's of its type. Returns (the file kept, or that newest one; whether
    it was kept now). A download that isn't whole gzip is set aside and raises."""
    kind = str(info.get("type") or DEFAULT)
    published = _published(info.get("updated_at"))
    if published is None:
        raise RuntimeError(f"Scryfall's {kind} entry has no publish time")
    url, suffix = download_url(info)
    fetched = times.now()
    name = f"{_stamp(published)}{suffix}"
    fresh = bulk_dir(kind) / f"{name}.new"
    if net.download(url, fresh, progress=progress) is None:
        raise RuntimeError(f"Scryfall's {kind} file is missing ({url})")
    with fresh.open("rb") as f:
        gz = f.read(2) == b"\x1f\x8b"
    try:
        if suffix.endswith(".gz") and not gz:
            raise gzip.BadGzipFile("isn't gzip — Scryfall's format may have changed again")
        sha = _digest(fresh, gzipped=suffix.endswith(".gz"))
    except (OSError, EOFError, zlib.error) as e:
        why = f"is cut short or corrupt ({e or type(e).__name__})"
        if suffix.endswith(".gz") and not gz:
            why = str(e)
        earlier = sorted(bulk_dir(kind).glob(f"{_stamp(published)}*{BAD}"))
        if earlier:  # a file served broken for days is kept once, not at every refresh
            line: dict = {"type": kind, "stamp": _stamp(published), "fetched": _stamp(fetched)}
            line |= {"size": fresh.stat().st_size, "served_sha256": runs.file_sha256(fresh), "bad": why}
            fresh.unlink()
            _append(checks_path(), line | {"not_kept": True, "aside": earlier[0].name})
            said = f"a copy of this publish is set aside already as {earlier[0].name}"
            raise RuntimeError(f"the {kind} file {why}; {said}, asked again next run") from e
        aside = fresh.with_name(f"{_stamp(published)}-{_stamp(fetched)}{suffix}{BAD}")
        fresh.replace(aside)
        raise RuntimeError(f"the {kind} file {why}; set aside as {aside.name}, asked again next run") from e
    line = {
        "type": kind,
        "published": info.get("updated_at"),
        "stamp": _stamp(published),
        "fetched": _stamp(fetched),
        "size": fresh.stat().st_size,
        "sha256": sha,
    }
    kept = kept_files(kind)
    if kept and _kept_digest(kind, kept[-1]) == sha:
        fresh.unlink()  # the same contents as a file kept: the check log records this publish
        _append(checks_path(), line | {"same_as": kept[-1].name})
        return kept[-1], False
    dest = bulk_dir(kind) / name
    if dest.exists():  # this publish is kept already and came again different: keep both
        dest = dest.with_name(f"{_stamp(published)}-{_stamp(fetched)}{suffix}")
    fresh.replace(dest)
    _append(checks_path(), line | {"kept": dest.name})
    return dest, True


def _adopt() -> None:
    """Move the bulk file kept before 0.4.0 (<data_dir>/default-cards.*) into
    scryfall/bulk/default_cards/, named by its publish time from bulk-meta.json, or by its file
    time when that can't be read; one set aside as unreadable goes there too, by its file time.
    A name already taken leaves the file where it is."""
    for old in sorted(data_dir().glob("default-cards.*")):
        if old.name.endswith((".part", ".new")):
            continue
        bad = old.name.endswith(BAD)
        suffix = ".jsonl.gz" if ".jsonl.gz" in old.name else ".json"
        mtime = datetime.fromtimestamp(old.stat().st_mtime, UTC)
        when = mtime if bad else (_kept_published() or mtime)
        dest = bulk_dir() / f"{_stamp(when)}{suffix}{BAD if bad else ''}"
        if not dest.exists():
            dest.parent.mkdir(parents=True, exist_ok=True)
            old.replace(dest)


def _when(path: Path) -> str:
    return f"the {times.shown(_from_stamp(path.name))} file"


def _keep_one(info: dict, step: Step, force: bool = False) -> bool:
    """One bulk file on its own step. Whether a file was downloaded."""
    kind = info["type"]
    published = _published(info.get("updated_at"))
    if not force and published is not None and _seen(kind, published):
        step.ok(f"current, the {times.shown(published)} file")
        return False
    path, kept = download(info, progress=step.update)
    size = f"{path.stat().st_size / 1e6:,.1f} MB"
    step.ok(f"kept {_when(path)}, {size}" if kept else f"the same as {_when(path)}; not kept twice")
    return True


def label(kind: str) -> str:
    return f"Scryfall {kind.replace('_', ' ')}"


def refresh(force: bool = False, tracker: Tracker = SILENT) -> None:
    """Download each bulk file Scryfall lists whose publish time hasn't been seen, default
    cards first, then the set list after a new default-cards file; force downloads default
    cards again. A failure is reported on its own step, never raised: the files kept stay,
    and a sync carries on with them."""
    step = tracker.step(label(DEFAULT), unit="bytes")
    try:
        _adopt()
        entries = bulk_index()
    except Exception as e:
        step.fail(str(e) or type(e).__name__)
        return
    default = next((e for e in entries if e["type"] == DEFAULT), None)
    others = [e for e in entries if e["type"] != DEFAULT]
    try:
        if default is None:
            raise RuntimeError(f"no bulk file of type {DEFAULT}")
        new = _keep_one(default, step, force=force)
    except Exception as e:
        step.fail(str(e) or type(e).__name__)
    else:
        if new or not sets_path().exists():
            refresh_sets(tracker)
    for entry in others:
        other = tracker.step(label(entry["type"]), unit="bytes")
        try:
            _keep_one(entry, other)
        except Exception as e:
            other.fail(str(e) or type(e).__name__)


def refresh_sets(tracker: Tracker = SILENT) -> None:
    """Fetch the set list and keep a copy when it's changed. The catalog builds any set the
    list lacks from what cards say, so a failure is reported, never raised: it can't stop a
    sync."""
    step = tracker.step("Scryfall set list")
    try:
        _adopt_sets()
        fetched = times.now()
        sets = read_sets(fetch_sets())
        note = keep_sets(fetched)
    except Exception as e:
        step.fail(str(e))
        return
    step.ok(f"{len(sets):,} sets, {note}")


def prices_dir() -> Path:
    return data_dir() / "scryfall" / "daily"


def bulk_file() -> Path | None:
    """The newest default-cards file kept, or the one kept before 0.4.0 until the first refresh
    moves it."""
    kept = kept_files(DEFAULT)
    if kept:
        return kept[-1]
    old = [p for p in data_dir().glob("default-cards.*") if not p.name.endswith((".part", ".new", BAD))]
    return max(old, key=lambda p: p.stat().st_mtime) if old else None


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
    """When Scryfall published the bulk file: the time its name starts with, or for the file
    kept before 0.4.0 bulk-meta.json's updated_at, else the file's own time."""
    if bulk.parent == bulk_dir():
        return _from_stamp(bulk.name)
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
        aside = _set_aside(bulk)
        raise CorruptBulk(
            f"{bulk.name} is cut short or corrupt ({e or type(e).__name__}); set aside as "
            f"{aside.name}, and downloaded again by the next online sync"
        ) from e
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    tmp.replace(dest)
    return dest, True
