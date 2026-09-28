"""Daily snapshots of whole store price lists: Card Kingdom's and Mana Pool's.

Card Kingdom publishes everything it sells and buys as JSON lists, without a key:

    https://api.cardkingdom.com/api/v2/pricelist        every single: {"meta": {"created_at",
                                                        "base_url"}, "data": [{"id", "sku",
                                                        "scryfall_id", "url", "name", "variation",
                                                        "edition", "is_foil", "price_retail",
                                                        "qty_retail", "price_buy", "qty_buying",
                                                        "condition_values": {"nm_price", "nm_qty",
                                                        "ex_price", "ex_qty", "vg_price", "vg_qty",
                                                        "g_price", "g_qty"}}]}
    https://api.cardkingdom.com/api/sealed_pricelist    every sealed product: {"meta", "data":
                                                        [{"id", "url", "name", "edition",
                                                        "price_retail", "qty_retail", "price_buy",
                                                        "qty_buying", "ships_internationally"}]}

A single's row is one SKU: a printing in one finish (is_foil is the string "true" or "false";
etched is a variation containing "Etched"), with its Near Mint price and copies in stock
(price_retail, qty_retail), the same for each condition, and what Card Kingdom pays and how
many it wants. Prices are decimal strings in USD. scryfall_id is the printing, but it's
missing on some rows and repeats on others (a double-faced token has one per back), so it
isn't a unique key. The response is sometimes wrapped in <html><head></head><body> ...
</body></html>.

Mana Pool, a US marketplace, publishes its prices the same way, as {"data": [...]}:

    https://manapool.com/api/v1/prices/singles    a row per printing: "scryfall_id",
                                                  "tcgplayer_product_id", "set_code", "number",
                                                  "available_quantity", "price_market" and
                                                  "price_market_foil", and "price_cents" (the
                                                  cheapest copy), "price_cents_nm", and
                                                  "price_cents_lp_plus" (Lightly Played or
                                                  better), each also with "_foil" and "_etched"
    https://manapool.com/api/v1/prices/variants   a row per printing, language, condition, and
                                                  finish: "language_id", "condition_id",
                                                  "finish_id", "low_price", "available_quantity"
    https://manapool.com/api/v1/prices/sealed     a row per sealed product

Its prices are integer cents in USD.

Each list says when the store made it, in its meta, first thing in the body: Card Kingdom's
created_at ("2026-09-27 13:08:38", no zone; read as Pacific time, where Card Kingdom is, until
a list shows otherwise) and Mana Pool's as_of (UTC, "2026-09-27T20:23:47.529Z"). A list's day
is that stamp's date on the store's own clock, never converted, and the day's list is kept
once, as returned but gzipped, its gzip header holding when it was fetched:

    <data_dir>/<store>/daily/<day>/<list>.json.gz

A list isn't asked for while the store's today, on its clock, is kept, and one that turns out
to be for a day already kept isn't kept again. Riffle used to date lists by the Mac's day, so a
list found under a day that isn't its own is moved to its own day, or set aside when that day
has its list. A list with no readable stamp isn't a day's: it's set aside too, named by when it
was fetched, and its step fails so someone looks. Nothing here deletes a list:

    <data_dir>/<store>/aside/<list>-<UTC time>.json.gz

A list with no rows ({"data": null} or []) keeps nothing and is asked for again next run
(riffle.ingest.empties). Once a store gives no answer at all, its other lists fail without
asking. Nothing here reads the lists back: the price loader does, and riffle.ingest.checks
checks every kept list's day and stamp. Headers, retries, and 429 handling: riffle.net.
"""

import gzip
import re
import shutil
import zlib
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo

from riffle import net, times
from riffle.config import data_dir
from riffle.ingest import empties
from riffle.progress import SILENT, Step, Tracker

WRAPPER = (b"<html><head></head><body>", b"</body></html>")  # Card Kingdom's, sometimes
HEAD = 4096  # bytes read at each end of a list
EMPTY = re.compile(rb'"data"\s*:\s*(null|\[\s*\])')
SLACK = timedelta(minutes=5)  # clocks disagree a little: a stamp this far past its fetch is fine
PACIFIC = ZoneInfo("America/Los_Angeles")


@dataclass(frozen=True)
class PriceList:
    store: str  # the folder under data_dir
    name: str  # the file in each day's folder, <name>.json.gz
    label: str  # the progress step
    url: str
    stamp: str  # the key in the list's meta saying when the store made it
    zone: tzinfo  # the store's clock: its day, and its stamp's zone when the stamp names none
    zone_name: str


CARD_KINGDOM = (
    PriceList(
        "cardkingdom",
        "singles",
        "Card Kingdom singles",
        "https://api.cardkingdom.com/api/v2/pricelist",
        "created_at",
        PACIFIC,
        "Pacific time",
    ),
    PriceList(
        "cardkingdom",
        "sealed",
        "Card Kingdom sealed",
        "https://api.cardkingdom.com/api/sealed_pricelist",
        "created_at",
        PACIFIC,
        "Pacific time",
    ),
)
MANA_POOL = tuple(
    PriceList(
        "manapool",
        name,
        f"Mana Pool {name}",
        f"https://manapool.com/api/v1/prices/{name}",
        "as_of",
        UTC,
        "UTC",
    )
    for name in ("singles", "variants", "sealed")
)

Download = Callable[[str, Path, net.Progress | None], int | None]  # url, dest -> bytes, or None for 404


@dataclass
class Snapshot:
    kept: list[str] = field(default_factory=list)  # labels of the lists kept this run
    skipped: list[str] = field(default_factory=list)  # the day's list was kept already
    empty: list[str] = field(default_factory=list)  # answered with no rows
    failed: list[tuple[str, str]] = field(default_factory=list)  # (label, why)


def _download(url: str, dest: Path, progress: net.Progress | None = None) -> int | None:
    return net.download(url, dest, accept="application/json", progress=progress)


def day_dir(store: str, day: date) -> Path:
    return data_dir() / store / "daily" / day.isoformat()


def target(plist: PriceList, day: date) -> Path:
    return day_dir(plist.store, day) / f"{plist.name}.json.gz"


def _shown(path: Path) -> str:
    return path.relative_to(data_dir()).as_posix()


def _edge(path: Path, size: int, end: bool) -> bytes:
    with path.open("rb") as f:
        if end:
            f.seek(max(0, path.stat().st_size - size))
        return f.read(size)


def check(path: Path, what: str) -> bytes:
    """A FetchError unless the file looks like a whole price list: one JSON object with a
    "data" key near its start, complete to its closing brace. Read at both ends only; a list
    is tens of megabytes. Returns its first bytes, unwrapped."""
    head, tail = _edge(path, HEAD, end=False).lstrip(), _edge(path, HEAD, end=True).rstrip()
    if head.startswith(WRAPPER[0]) and tail.endswith(WRAPPER[1]):
        head, tail = head.removeprefix(WRAPPER[0]).lstrip(), tail.removesuffix(WRAPPER[1]).rstrip()
    if not head.startswith(b"{") or b'"data"' not in head or not tail.endswith(b"}"):
        raise net.FetchError(f"{what}: not the expected JSON")
    return head


def made(head: bytes, plist: PriceList) -> datetime | None:
    """When the store made a list, from its first bytes: the stamp in its meta, on the store's
    clock when the stamp names no zone. None when there's none to read."""
    found = re.search(rb'"%s"\s*:\s*"([^"]{1,64})"' % re.escape(plist.stamp.encode()), head)
    if found is None:
        return None
    try:
        at = datetime.fromisoformat(found.group(1).decode("ascii"))
    except ValueError:  # UnicodeDecodeError is one
        return None
    return at.replace(tzinfo=plist.zone) if at.tzinfo is None else at


def day_of(at: datetime, plist: PriceList) -> date:
    """The day of a list made at `at`: its date on the store's clock."""
    return at.astimezone(plist.zone).date()


def kept_made(path: Path, plist: PriceList) -> datetime | None:
    """When a kept list was made, from its first bytes; None if it can't be read or has no stamp."""
    try:
        with gzip.open(path) as f:
            return made(f.read(HEAD), plist)
    except (OSError, EOFError, zlib.error):
        return None


def fetched(path: Path) -> datetime | None:
    """When a gzipped file Riffle kept was fetched: the time in its gzip header, written as it
    was kept, so it survives a copy. None if it can't be read or holds none."""
    try:
        with gzip.open(path) as f:
            f.read(1)
            stamp = f.mtime
    except (OSError, EOFError, zlib.error):
        return None
    return datetime.fromtimestamp(stamp, UTC) if stamp else None


def filed_right(path: Path, plist: PriceList) -> bool:
    """Whether path holds a list made on the day it's filed under."""
    at = kept_made(path, plist)
    return at is not None and day_of(at, plist).isoformat() == path.parent.name


def _prune(folder: Path) -> None:
    if folder.is_dir() and not any(folder.iterdir()):
        folder.rmdir()


def _aside(plist: PriceList, at: datetime) -> Path:
    """A free name in the store's aside folder for a list fetched at `at`."""
    stem = f"{plist.name}-{at.astimezone(UTC):%Y-%m-%dT%H%M%SZ}"
    folder = data_dir() / plist.store / "aside"
    dest, n = folder / f"{stem}.json.gz", 1
    while dest.exists():
        n += 1
        dest = folder / f"{stem}-{n}.json.gz"
    dest.parent.mkdir(parents=True, exist_ok=True)
    return dest


def refile(path: Path, plist: PriceList, seen: tuple[Path, ...] = ()) -> str:
    """Move a list kept under a day that isn't its own to its own day, or set it aside when that
    day has its list or the list's own day can't be read. A list in the way that isn't its day's
    either is moved on first. Says what it did."""
    at = kept_made(path, plist)
    own = target(plist, day_of(at, plist)) if at else None
    old = path.parent.name
    if own == path:
        return f"the list under {old} is its own"
    if own is not None and own.exists() and own not in seen and not filed_right(own, plist):
        refile(own, plist, (*seen, path))
    if own is not None and not own.exists():
        own.parent.mkdir(parents=True, exist_ok=True)
        path.replace(own)
        said = f"moved the list kept under {old} to {own.parent.name}, the day it was made"
    else:
        dest = _aside(plist, fetched(path) or times.now())
        path.replace(dest)
        why = f"{own.parent.name} has its list" if own else f"it has no readable {plist.stamp}"
        said = f"set the list kept under {old} aside as {_shown(dest)}: {why}"
    _prune(path.parent)
    return said


def _gzip(src: Path, dest: Path) -> None:
    """Keep src gzipped at dest, whole or not at all; a failed first list leaves no empty day."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    try:
        with src.open("rb") as f, gzip.open(part, "wb") as out:
            shutil.copyfileobj(f, out)
        part.replace(dest)
    finally:
        part.unlink(missing_ok=True)
        _prune(dest.parent)


def _settle(
    plist: PriceList, fresh: Path, head: bytes, clock: Callable[[], datetime], tracker: Tracker, step: Step
) -> str:
    """Keep a downloaded list under its own day, unless that day is kept; end step saying so.
    Returns "kept", "have" or "empty"."""
    what = plist.url.rsplit("/", 1)[-1]
    key, now = f"{plist.store}/{plist.name}", clock()
    if EMPTY.search(head):
        empties.report(step, key, "empty list", now)
        return "empty"
    empties.clear(key)
    at = made(head, plist)
    if at is None:
        dest = _aside(plist, now)
        _gzip(fresh, dest)
        raise net.FetchError(f"{what}: no {plist.stamp} in it; kept aside as {_shown(dest)}, not as a day")
    day, today = day_of(at, plist), day_of(now, plist)
    dest = target(plist, day)
    if dest.exists():
        if filed_right(dest, plist):
            later = f"; the {today} list isn't out yet" if day < today else ""
            step.ok(f"already have {day}, made {times.shown(at)}{later}")
            return "have"
        tracker.step(plist.label).ok(refile(dest, plist))
    _gzip(fresh, dest)
    note = f"kept {day}, made {times.shown(at)}, {dest.stat().st_size / 1e6:,.1f} MB"
    if at - now > SLACK:
        step.warn(f"{note}; it says it was made after it was fetched, so its clock isn't {plist.zone_name}")
    else:
        step.ok(note)
    return "kept"


def snapshot(
    lists: tuple[PriceList, ...],
    download: Download = _download,
    tracker: Tracker = SILENT,
    clock: Callable[[], datetime] = times.now,
) -> Snapshot:
    """Keep each list's day not kept yet, each its own step. A list that fails keeps nothing and
    is retried the next run; after one gets no answer at all, the rest fail without asking."""
    snap = Snapshot()
    answering = True
    for plist in lists:
        today = day_of(clock(), plist)
        kept = target(plist, today)
        if kept.exists():
            if filed_right(kept, plist):
                snap.skipped.append(plist.label)
                tracker.step(plist.label).ok(f"already have {today}")
                continue
            tracker.step(plist.label).ok(refile(kept, plist))
        if not answering:
            why = "not asked: no answer to the list before"
            snap.failed.append((plist.label, why))
            tracker.step(plist.label).fail(why)
            continue
        step = tracker.step(plist.label, unit="bytes")
        fresh = data_dir() / plist.store / "daily" / f"{plist.name}.json.new"
        what = plist.url.rsplit("/", 1)[-1]
        try:
            fresh.parent.mkdir(parents=True, exist_ok=True)
            if download(plist.url, fresh, step.update) is None:
                raise net.FetchError(f"{what}: HTTP 404")
            got = _settle(plist, fresh, check(fresh, what), clock, tracker, step)
        except (net.FetchError, OSError) as e:
            answering = not isinstance(e, net.NoAnswer)
            snap.failed.append((plist.label, str(e)))
            step.fail(str(e))
        else:
            {"kept": snap.kept, "have": snap.skipped, "empty": snap.empty}[got].append(plist.label)
        finally:
            fresh.unlink(missing_ok=True)
    return snap
