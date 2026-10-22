"""Daily Magic prices from MTGJSON: Card Kingdom, TCGplayer, Mana Pool, Cardmarket, and
Cardhoarder in one file, with the past 90 days to start from; and every file of its card
catalog, each build.

MTGJSON builds its files once a day, about 06:12 UTC by their Last-Modified, and serves them
hours later: the syncs at 13:00 UTC on 2026-09-28 and 2026-09-29 still found the day before's.
Its watch learns when each file goes online from its own checks (riffle.cadence), and asks from
then. It serves them without a key, most beside a .sha256 of themselves:

    https://mtgjson.com/api/v5/Meta.json               the latest build's date and version
    https://mtgjson.com/api/v5/AllPricesToday.json.xz  every printing's prices that day
    https://mtgjson.com/api/v5/AllPrices.json.xz       the same for each of the past 90 days
    https://mtgjson.com/api/v5/AllPrintings.json.xz    every set and card, and the rest of FILES

Every build of each file in FILES is kept (watch: `riffle watch mtgjson`, each asked when
riffle.cadence says its next is due, with the ETag of the last one kept): checked against its
.sha256, unpacked whole, its meta read when it has one, and kept by the run rule (riffle.runs)
under its Last-Modified, each file a list of its own. A file that isn't whole (not xz, not the
.sha256's, not JSON) is set aside once a build, and asked for again. Meta.json doesn't say when
a build is new: it's written seconds before the file (the .sha256 too), so a check in between
would find a new Meta.json beside the old file. A build is kept even for a day an AllPrices file
covers: the two disagree about the same day in some cards.

Each new build of BuildManifest.json, the build's list of its files, is sorted before it's kept
(_sort): each file is kept (FILES, and AllPrices), left out by rule (another form of a kept
file, or LEFT_OUT), or new. A new file is held, set aside as served once a build, and the step
warns until it's given a rule; it warns too for a kept file the manifest no longer lists.

    <data_dir>/mtgjson/lists/<list>/<run>/         each file's lists, by the run rule:
                                                   prices-today, all-printings, and the rest
    <data_dir>/mtgjson/watch/<month>.jsonl         every check; for a file kept, its day (its
                                                   meta's date, or the day it was built), its
                                                   version, and the served file's SHA-256,
                                                   size and ETag (riffle.watching)
    <data_dir>/mtgjson/aside/<name>-<UTC time>.<suffix>  one with no Last-Modified, one that
                                                   isn't whole, a copy of a build kept that
                                                   isn't the one kept, or a new file held,
                                                   named by its Last-Modified
    <data_dir>/mtgjson/daily/<day>.json.xz         AllPricesToday as the sync kept it before
    <data_dir>/mtgjson/90-days/<day>.json.xz       AllPrices: <day> and the 90 days before it

AllPrices is kept as returned, checked against its .sha256 and to decompress whole, and named
by the date inside, by the sync (snapshot): on the first run, and again when a day in its
window has no prices kept (the Mac was off that day), at most once every REFILL days, so a
missed day is still inside the next file's window with two months to spare. A day MTGJSON
never built can't come back that way; a kept AllPrices counts as covering its whole window.
Nothing here reads the files back: the price loader does. Headers, retries, and 429 handling:
riffle.net.
"""

import hashlib
import json
import lzma
import math
import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import IO
from urllib.parse import quote

from riffle import net, runs, times, watching
from riffle.config import data_dir
from riffle.progress import SILENT, Step, Tracker

BASE = "https://mtgjson.com/api/v5"
TODAY = "AllPricesToday.json.xz"
HISTORY = "AllPrices.json.xz"
STORE = "mtgjson"
LIST = "prices-today"  # AllPricesToday in the watch log
WINDOW = 90  # days an AllPrices file counts as covering: its date and the 89 before (it has one more)
REFILL = 30  # days at least between AllPrices downloads that fill a missing day
CHUNK = 1 << 20
MAX_LIST = 1 << 30  # bytes AllPricesToday may unpack to; it's about 53 MB
# Bytes any other file may unpack to. Not learned: it only stops a file that unpacks without end
# being read into memory. The biggest, AllDeckFiles' tar, was about 0.87 GB on 2026-09-29.
MAX_FILE = 4 << 30
META = re.compile(rb'\A\s*\{\s*"meta"\s*:\s*(\{[^{}]*\})')  # the file's first object


@dataclass(frozen=True)
class File:
    """One file of MTGJSON's build, kept as a list of its own."""

    list: str  # its name in the watch log, and its folder in lists/
    path: str  # under BASE
    label: str  # its step: MTGJSON <label>
    meta: bool = False  # must start with its meta, as a price file does
    sha256: bool = True  # MTGJSON serves a .sha256 beside it
    most: int = MAX_FILE  # bytes it may unpack to
    manifest: bool = False  # the build's list of its files, each new one sorted before it's kept (_sort)

    @property
    def served(self) -> str:
        """Its name as served, for a copy set aside: AllPrintings.json.xz."""
        return self.path.rsplit("/", 1)[-1]


# Every file of the build that's kept, AllPricesToday first so the rest never hold it up.
# Checked 2026-09-29 against its BuildManifest.json, and since against each new build's (_sort).
FILES = (
    File(LIST, TODAY, "prices today", meta=True, most=MAX_LIST),
    File("all-printings", "AllPrintings.json.xz", "AllPrintings", meta=True),
    File("all-identifiers", "AllIdentifiers.json.xz", "AllIdentifiers", meta=True),
    File("tcgplayer-skus", "TcgplayerSkus.json.xz", "TcgplayerSkus", meta=True),
    File("atomic-cards", "AtomicCards.json.xz", "AtomicCards", meta=True),
    File("all-deck-files", "AllDeckFiles.tar.xz", "AllDeckFiles"),
    File("card-identifiers", "csv/cardIdentifiers.csv.xz", "cardIdentifiers"),
    File("cardmarket-identifiers", "CardmarketIdentifiers.json.xz", "CardmarketIdentifiers", meta=True),
    File("set-list", "SetList.json.xz", "SetList", meta=True),
    File("deck-list", "DeckList.json.xz", "DeckList", meta=True),
    File("keywords", "Keywords.json.xz", "Keywords", meta=True),
    File("card-types", "CardTypes.json.xz", "CardTypes", meta=True),
    File("enum-values", "EnumValues.json.xz", "EnumValues", meta=True),
    File("compiled-list", "CompiledList.json.xz", "CompiledList", meta=True),
    File("meta", "Meta.json.xz", "Meta", meta=True),
    File("build-manifest", "BuildManifest.json", "BuildManifest", meta=True, sha256=False, manifest=True),
)

# The rest of the build is left out on purpose: another form of a kept file (the same name up to
# its first dot, as AllPrintings.sqlite or Meta.json), and each file one of these covers.
LEFT_OUT = re.compile(
    "|".join(
        (
            # each deck's own file, which AllDeckFiles holds
            r"decks/.*",
            # the build as CSV (but cardIdentifiers, the uuid-to-IDs file, kept) or Parquet
            r"(csv|parquet)/.*",
            r"AllPrintings(CSV|Parquet)Files\..*",
            # each set's file (10E.json, CON_.json), which AllPrintings holds: checked 2026-09-30
            r"[A-Z0-9_]+\..*",
            r"AllSetFiles\..*",
            # each format's file, which AllPrintings and AtomicCards hold: checked 2026-09-30
            r"(Legacy|Modern|Pauper|Pioneer|Standard|Vintage)(Atomic)?\..*",
            # listed, but not served: HTTP 404 on 2026-09-29 and 2026-10-21
            r"ReferralMap\..*",
        )
    )
)

Fetch = Callable[[str], bytes | None]  # url -> body, or None for 404
Download = Callable[[str, Path, net.Progress | None], int | None]  # url, dest -> bytes, or None for 404
Watcher = Callable[..., net.Fetched | None]  # net.fetch_new


@dataclass
class Snapshot:
    day: date  # MTGJSON's latest build
    kept: list[str] = field(default_factory=list)  # files kept this run: HISTORY


def _get(url: str) -> bytes | None:
    return net.get(url)


def _download(url: str, dest: Path, progress: net.Progress | None = None) -> int | None:
    return net.download(url, dest, progress=progress)


def daily_dir() -> Path:
    return data_dir() / "mtgjson" / "daily"


def history_dir() -> Path:
    return data_dir() / "mtgjson" / "90-days"


def lists_dir(name: str = LIST) -> Path:
    """Where one file's lists are kept: AllPricesToday's by default."""
    return data_dir() / STORE / "lists" / name


def kept_days(folder: Path) -> list[date]:
    """The days of the <day>.json.xz files in folder, oldest first."""
    days = []
    for path in folder.glob("*.json.xz"):
        try:
            days.append(date.fromisoformat(path.name.removesuffix(".json.xz")))
        except ValueError:
            continue
    return sorted(days)


def covered(daily: list[date], history: list[date]) -> set[date]:
    """Every day a kept file has prices for: each daily file's (or build's), and each AllPrices
    file's 90."""
    days = set(daily)
    for last in history:
        days.update(last - timedelta(n) for n in range(WINDOW))
    return days


def build_day(fetch: Fetch = _get) -> date:
    """The date of MTGJSON's latest build, from Meta.json."""
    body = fetch(f"{BASE}/Meta.json")
    if body is None:
        raise net.FetchError("Meta.json: HTTP 404")
    try:
        return date.fromisoformat(json.loads(body)["data"]["date"])
    except (ValueError, KeyError, TypeError, RecursionError) as e:
        raise net.FetchError("Meta.json: not the expected JSON") from e


def _digest(body: bytes) -> str:
    """The SHA-256 a .sha256 file gives: its first word, which the file's name may follow."""
    return (body.decode("ascii", errors="replace").split() or [""])[0].lower()


def _expected(fetch: Fetch, name: str) -> str:
    """The SHA-256 name's .sha256 gives it."""
    digest = fetch(f"{BASE}/{name}.sha256")
    if digest is None:
        raise net.FetchError(f"{name}.sha256: HTTP 404")
    return _digest(digest)


def _meta(head: bytes, name: str) -> tuple[date, str]:
    """The date and version in a price file's meta, from its first bytes."""
    found = META.match(head)
    try:
        if found is None:
            raise ValueError("no meta first")
        meta = json.loads(found.group(1))
        version = meta["version"]
        if not isinstance(version, str):
            raise TypeError("version")
        return date.fromisoformat(meta["date"]), version
    except (ValueError, KeyError, TypeError) as e:
        raise net.FetchError(f"{name}: not the expected JSON") from e


@contextmanager
def _xz(path: Path, name: str) -> Iterator[IO[bytes]]:
    """An xz file, open to unpack; one that doesn't unpack is a FetchError."""
    try:
        with lzma.open(path) as f:
            yield f
    except lzma.LZMAError as e:
        raise net.FetchError(f"{name}: not xz") from e
    except EOFError as e:
        raise net.FetchError(f"{name}: cut off before its end") from e


def _head(path: Path, name: str) -> bytes:
    """An xz file's first bytes unpacked, once the whole file has been read through: a file
    that doesn't decompress to its end isn't kept."""
    with _xz(path, name) as f:
        head = f.read(512)
        while f.read(CHUNK):
            pass
    return head


def _keep(name: str, folder: Path, fetch: Fetch, download: Download, step: Step) -> tuple[date, bool]:
    """Download name, check it against its .sha256, and keep it in folder as <day>.json.xz,
    <day> being the date inside; a copy that fails is set aside once its bytes
    (riffle.watching.broken_logged: the sync keeps no ETag). Returns that day, and whether the file is new."""
    url = f"{BASE}/{name}"
    expected = _expected(fetch, name)
    folder.mkdir(parents=True, exist_ok=True)
    fresh = folder / f"{name}.new"  # checked before it's kept
    try:
        if download(url, fresh, step.update) is None:
            raise net.FetchError(f"{name}: HTTP 404")
        try:
            if runs.file_sha256(fresh) != expected:
                raise net.FetchError(f"{name} doesn't match its .sha256; MTGJSON may be mid-update")
            day, _ = _meta(_head(fresh, name), name)
        except net.FetchError as e:
            raise watching.broken_logged(STORE, name, None, fresh, name, times.now(), str(e)) from e
        dest = folder / f"{day.isoformat()}.json.xz"
        if dest.exists():
            return day, False
        fresh.replace(dest)
        return day, True
    finally:
        fresh.unlink(missing_ok=True)


def _mb(path: Path) -> str:
    return f"{path.stat().st_size / 1e6:,.1f} MB"


def snapshot(
    fetch: Fetch = _get, download: Download = _download, tracker: Tracker = SILENT
) -> Snapshot | None:
    """Keep AllPrices when a day in its window has no prices kept (see REFILL): a step on the
    tracker only when it's asked for, or when Meta.json, which says MTGJSON's latest day, can't
    be read (None then). A failure is reported on its step and keeps nothing, so the next run
    tries again."""
    step = tracker.step("MTGJSON 90 days", unit="bytes")
    try:
        day = build_day(fetch)
    except net.FetchError as e:
        step.fail(str(e))
        return None
    snap = Snapshot(day=day)
    history = kept_days(history_dir())
    have = covered(kept_days(daily_dir()) + watching.days(STORE, LIST), history)
    missing = [day - timedelta(n) for n in range(1, WINDOW) if day - timedelta(n) not in have]
    if not missing or (history and (day - history[-1]).days < REFILL):
        step.drop()
        return snap
    try:
        last, new = _keep(HISTORY, history_dir(), fetch, download, step)
    except (net.FetchError, OSError) as e:
        step.fail(str(e))
    else:
        if new:
            snap.kept.append(HISTORY)
            step.ok(f"kept the 90 days to {last}, {_mb(history_dir() / f'{last.isoformat()}.json.xz')}")
        else:
            step.ok(f"already have the 90 days to {last}")
    return snap


def _unknown(head: bytes) -> bool:
    """A build's first bytes are xz: whether it's kept shows only in its Last-Modified."""
    return False


def _unpacked(file: File, fresh: Path) -> bytes:
    """A file as its list is kept: unpacked whole when it's xz, up to its most."""
    if not file.path.endswith(".xz"):
        return fresh.read_bytes()
    with _xz(fresh, file.path) as f:
        data = f.read(file.most + 1)
    if len(data) > file.most:
        raise net.FetchError(
            f"{file.path}: unpacks to more than {file.most:,} bytes, too big to be what it claims"
        )
    return data


def _checked(file: File, fresh: Path, got: net.Fetched, get: Fetch, now: datetime) -> bytes:
    """A file fetched whole, checked: against its .sha256, unpacked to its end, and its meta
    read when it has one (JSON starting with its meta). Its list; watching.Broken when it isn't
    what it should be, set aside once per build."""
    publish = runs.name(got.modified) if got.modified else got.etag
    why = ""
    if file.sha256 and runs.file_sha256(fresh) != _expected(get, file.path):
        why = f"{file.path} doesn't match its .sha256; MTGJSON may be mid-update"
    else:
        try:
            data = _unpacked(file, fresh)
            if file.path.endswith(".json"):
                json.loads(data)
            if file.meta:
                _meta(data[:512], file.path)
            return data
        except net.FetchError as e:
            why = str(e)
        except (ValueError, RecursionError):
            why = f"{file.path}: not the expected JSON"
    raise watching.broken(STORE, file.list, publish, fresh, file.served, now, why)


def _count(n: int, noun: str) -> str:
    return f"{n:,} {noun}" if n == 1 else f"{n:,} {noun}s"


def _stem(path: str) -> str:
    """A path up to the first dot of its name: csv/cardIdentifiers.csv.xz is csv/cardIdentifiers."""
    folder, slash, name = path.rpartition("/")
    return folder + slash + name.partition(".")[0]


def _form(forms: list[str], files: dict) -> str:
    """Which form of a new file to hold: its .json.xz, else its smallest .xz, else its smallest,
    by its size in the manifest, then by name."""

    def size(path: str) -> tuple[float, str]:
        entry = files[path]
        n = entry.get("size_bytes") if isinstance(entry, dict) else None
        return (n if isinstance(n, int) else math.inf, path)

    xz = [path for path in forms if path.endswith(".xz")]
    return min([path for path in xz if path.endswith(".json.xz")] or xz or forms, key=size)


def _hold(path: str, fetch: Watcher, get: Fetch, fresh: Path, now: datetime, step: Step) -> tuple[str, bool]:
    """Download a new file and set it aside as served, under its Last-Modified (or now), checked
    against its .sha256 when MTGJSON serves one: where it went, and whether it was checked. One
    that can't be held raises net.FetchError, or OSError, and keeps nothing. The name is the
    manifest's, so it's quoted to be asked whatever it holds."""
    url = f"{BASE}/{quote(path)}"
    try:
        got = fetch(url, fresh, _unknown, progress=step.update)
        if got is None:
            raise net.FetchError("HTTP 404")
        try:
            digest = get(f"{url}.sha256")
        except net.FetchError as e:
            raise type(e)(f"its .sha256: {e}") from e
        if digest is not None and runs.file_sha256(fresh) != _digest(digest):
            raise net.FetchError("it doesn't match its .sha256")
        where = watching.set_aside(STORE, fresh, path.rsplit("/", 1)[-1], got.modified or now)
        return where, digest is not None
    finally:
        fresh.unlink(missing_ok=True)


def _sort(
    data: bytes, fetch: Watcher, get: Fetch, fresh: Path, now: datetime, step: Step
) -> tuple[str, dict, bool]:
    """Sort the files a new build's manifest lists: kept (FILES, and AllPrices, which the sync
    keeps), left out (another form of a kept file, or LEFT_OUT), or new, each new one held
    (_hold) until MTGJSON gives no answer. What the step says of them, the log entry's facts,
    and whether all is as it was."""
    files = json.loads(data).get("files")
    if not isinstance(files, dict):
        return "its list of files can't be read, so nothing was sorted", {}, False
    kept = {file.path for file in FILES} | {HISTORY}
    stems = {_stem(path) for path in kept}
    new: dict[str, list[str]] = {}
    for path in files:
        if path not in kept and _stem(path) not in stems and not LEFT_OUT.fullmatch(path):
            new.setdefault(_stem(path), []).append(path)
    # the manifest never lists itself
    unlisted = sorted(kept.difference(files, (file.path for file in FILES if file.manifest)))
    said = [f"{_count(len(files), 'file')}, {len(kept.intersection(files))} kept"]
    facts: dict = {}
    if unlisted:
        said.append(f"it no longer lists {_count(len(unlisted), 'file')} Riffle keeps: {', '.join(unlisted)}")
        facts["unlisted"] = unlisted
    if new:
        unruled = sorted(_form(forms, files) for forms in new.values())
        held: dict[str, str] = {}
        not_held: dict[str, str] = {}
        each = []
        silent = False
        for path in unruled:
            if silent:
                why = "not asked once MTGJSON gave no answer"
            else:
                try:
                    where, checked = _hold(path, fetch, get, fresh, now, step)
                except (net.FetchError, OSError) as e:
                    silent, why = isinstance(e, net.NoAnswer), str(e)
                else:
                    held[path] = where
                    unchecked = "" if checked else " with no .sha256 to check it by"
                    each.append(f"{path}, held as {where}{unchecked}")
                    continue
            not_held[path] = why
            each.append(f"{path}, not held: {why}, asked again next build")
        said.append(f"MTGJSON serves {_count(len(each), 'file')} Riffle has no rule for: {'; '.join(each)}")
        facts["unruled"] = unruled
        if held:
            facts["held"] = held
        if not_held:
            facts["not_held"] = not_held
    if not facts:
        return f"{said[0]}, the rest left out by rule", {}, True
    return "; ".join(said), facts, False


def _file(file: File, fetch: Watcher, get: Fetch, tags: dict[str, str], now: datetime, step: Step) -> dict:
    """Ask for one file of the build once, with the ETag of the last one kept, and keep its list
    if it's new; end step saying what came. The log entry."""
    folder = lists_dir(file.list)
    fresh = folder.parent / f"{file.list}.new"
    try:
        fresh.parent.mkdir(parents=True, exist_ok=True)
        got = fetch(f"{BASE}/{file.path}", fresh, _unknown, etag=tags.get(file.list), progress=step.update)
        if got is None:
            raise net.FetchError(f"{file.path}: HTTP 404")
        if got.status == "unchanged":
            step.ok("no new build since the last one kept")
            return {"result": "unchanged"}
        served = watching.served(fresh, got)
        data = _checked(file, fresh, got, get, now)
        if got.modified is None:
            why = f"{file.path}: no Last-Modified, so no time it was built"
            raise watching.broken(STORE, file.list, got.etag, fresh, file.served, now, why)
        day, version = got.modified.date(), None  # a file with no meta: the day it was built, UTC
        if META.match(data[:512]):
            day, version = _meta(data[:512], file.path)
        facts = {"day": day.isoformat()} | ({"version": version} if version else {}) | served
        stamp, built = runs.name(got.modified), times.shown(got.modified)
        if stamp in runs.kept(folder):
            if got.etag:
                tags[file.list] = got.etag
            sha256 = hashlib.sha256(data).hexdigest()
            more, said = watching.again(STORE, folder, stamp, sha256, fresh, file.served, now)
            note = f"have the build made {built} ({day}); {said}"
            if more["same"]:
                step.ok(note)
            else:
                step.warn(note)
            return {"result": "known", "made": stamp} | more | facts
        # A new manifest is sorted before it's kept: a run cut off while it holds a new file leaves
        # the build to be asked for, and sorted, again.
        sort = _sort(data, fetch, get, folder.parent / "held.new", now, step) if file.manifest else None
        kept = runs.keep(folder, got.modified, data)
        if got.etag:
            tags[file.list] = got.etag
        note = f"kept the list built {built} ({day}), {watching.size(len(data))}: {watching.how(kept)}"
        notes = [note, *kept.notes]
        well = not kept.notes
        if sort is not None:
            said, found, fine = sort
            notes.append(said)
            facts |= found
            well = well and fine
        if well:
            step.ok("; ".join(notes))
        else:
            step.warn("; ".join(notes))
        return watching.record(kept) | facts
    finally:
        fresh.unlink(missing_ok=True)


def watch(
    fetch: Watcher = net.fetch_new,
    get: Fetch = _get,
    tracker: Tracker = SILENT,
    clock: Callable[[], datetime] = times.now,
    always: bool = False,
) -> watching.Watch:
    """Ask for each file of the build (FILES) when riffle.cadence says its next is due, or
    always (the sync), and keep each that's new (riffle.watching.many)."""

    def listed(file: File) -> watching.Listed:
        def ask(tags: dict[str, str], now: datetime, step: Step) -> dict:
            return _file(file, fetch, get, tags, now, step)

        return watching.Listed(file.list, f"MTGJSON {file.label}", lists_dir(file.list), ask)

    return watching.many(STORE, "MTGJSON", [listed(file) for file in FILES], tracker, clock, always)
