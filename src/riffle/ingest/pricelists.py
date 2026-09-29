"""Every whole store price list Card Kingdom and Mana Pool publish.

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
a list shows otherwise) and Mana Pool's as_of (UTC, "2026-09-27T20:23:47.529Z").

Card Kingdom makes a new list every few hours and Mana Pool about every half hour, and a list
not caught is gone, so every list is kept (riffle.runs): the run's first whole, twice, and each
later one as a difference against it, under the UTC time the store made it:

    <data_dir>/<store>/lists/<list>/<base's stamp>/...

`riffle prices watch <store>` asks for each of a store's lists once; its launchd job runs every 5
minutes, and the sync does the same. One request a list: it carries the ETag of the list last
kept, so Mana Pool answers 304 when nothing's new, and Card Kingdom, which sends no ETag, is
hung up on once the list's first bytes show its stamp is kept. A list comes gzipped, a seventh
of its size, and is kept as served. Every check goes in <store>/watch.jsonl: when, which list,
what came, and for each list kept its stamp, file, size, and the SHA-256 of the list and of
the file kept, which riffle.ingest.checks checks.

One run at a time a store: a run that finds the store's lock held asks nothing. A list with no
rows ({"data": null} or []) keeps nothing and is asked for again next run
(riffle.ingest.empties). A list with no readable stamp, or one that doesn't read back as
written, is set aside, named by when it was fetched, and its step fails so someone looks:

    <data_dir>/<store>/aside/<list>-<UTC time>.json.gz

Once a store gives no answer at all, its other lists fail without asking. Until 2026-09-29
Riffle kept one list a day, under the day its stamp says on the store's clock; those stay:

    <data_dir>/<store>/daily/<day>/<list>.json.gz

Headers, retries, and 429 handling: riffle.net.
"""

import gzip
import json
import re
import shutil
import zlib
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo

from riffle import locks, net, runs, times
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
    name: str  # its folder under lists/, and its file in each day's folder of daily/
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

STORES = {lists[0].store: lists for lists in (CARD_KINGDOM, MANA_POOL)}

Fetch = Callable[..., net.Fetched | None]  # net.fetch_new


@dataclass
class Watch:
    busy: bool = False  # another run held the store: nothing asked
    kept: list[str] = field(default_factory=list)  # labels of the lists kept this run
    same: list[str] = field(default_factory=list)  # the list asked for was kept already
    empty: list[str] = field(default_factory=list)  # answered with no rows
    failed: list[tuple[str, str]] = field(default_factory=list)  # (label, why)


def lists_dir(plist: PriceList) -> Path:
    return data_dir() / plist.store / "lists" / plist.name


def log_path(store: str) -> Path:
    return data_dir() / store / "watch.jsonl"


def _tags_path(store: str) -> Path:
    return data_dir() / store / "watch-etags.json"


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
    """When a list kept whole in daily/ was made, from its first bytes; None if it can't be
    read or has no stamp."""
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


def _set_aside(fresh: Path, plist: PriceList, at: datetime) -> Path:
    """Keep a fetched list gzipped in the aside folder, whole or not at all."""
    dest = _aside(plist, at)
    part = dest.with_name(dest.name + ".part")
    try:
        with fresh.open("rb") as f, gzip.open(part, "wb") as out:
            shutil.copyfileobj(f, out)
        part.replace(dest)
    finally:
        part.unlink(missing_ok=True)
    return dest


def _size(size: int) -> str:
    """A size to read at a glance: a difference is often a few kilobytes."""
    return f"{size / 1e6:,.1f} MB" if size >= 1e6 else f"{size / 1e3:,.0f} KB"


def _log(store: str, entry: dict) -> None:
    path = log_path(store)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def _load_tags(store: str) -> dict[str, str]:
    try:
        found = json.loads(_tags_path(store).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in found.items() if isinstance(k, str) and isinstance(v, str)}


def _save_tags(store: str, tags: dict[str, str]) -> None:
    path = _tags_path(store)
    part = path.with_name(path.name + ".part")
    part.write_text(json.dumps(tags, indent=1, sort_keys=True), encoding="utf-8")
    part.replace(path)


def _one(plist: PriceList, fetch: Fetch, tags: dict[str, str], now: datetime, step: Step) -> dict:
    """Ask for one list and keep it if it's new; end step saying what came. The log entry."""
    what = plist.url.rsplit("/", 1)[-1]
    folder = lists_dir(plist)
    have = runs.kept(folder)

    def known(head: bytes) -> bool:
        at = made(head, plist)
        return at is not None and runs.name(at) in have

    fresh = folder.parent / f"{plist.name}.new"
    try:
        fresh.parent.mkdir(parents=True, exist_ok=True)
        got = fetch(
            plist.url,
            fresh,
            known,
            etag=tags.get(plist.name),
            accept="application/json",
            progress=step.update,
        )
        if got is None:
            raise net.FetchError(f"{what}: HTTP 404")
        if got.status == "unchanged":
            step.ok("no new list since the last one kept")
            return {"result": "unchanged"}
        if got.status == "known":
            at = made(got.head, plist)
            assert at is not None  # known() found it
            if got.etag:
                tags[plist.name] = got.etag
            step.ok(f"have the list made {times.shown(at)}")
            return {"result": "known", "made": runs.name(at)}
        return _keep(plist, fresh, got, tags, now, step)
    finally:
        fresh.unlink(missing_ok=True)


def _keep(
    plist: PriceList, fresh: Path, got: net.Fetched, tags: dict[str, str], now: datetime, step: Step
) -> dict:
    """Keep a list fetched whole, as a difference or a new run's base."""
    what = plist.url.rsplit("/", 1)[-1]
    head = check(fresh, what)
    key = f"{plist.store}/{plist.name}"
    if EMPTY.search(head):
        empties.report(step, key, "empty list", now)
        return {"result": "empty"}
    empties.clear(key)
    at = made(head, plist)
    if at is None:
        dest = _set_aside(fresh, plist, now)
        raise net.FetchError(f"{what}: no {plist.stamp} in it; kept aside as {_shown(dest)}")
    try:
        kept = runs.keep(lists_dir(plist), at, fresh.read_bytes())
    except runs.Unverified as e:
        dest = _set_aside(fresh, plist, now)
        raise runs.Unverified(f"{e}; the list is kept aside as {_shown(dest)}") from e
    if got.etag:
        tags[plist.name] = got.etag
    how = (
        f"a new run, {_size(kept.stored // 2)} kept twice"
        if kept.kind == "base"
        else f"a difference of {_size(kept.stored)}"
    )
    note = f"kept the list made {times.shown(at)}, {_size(kept.size)}: {how}"
    if at - now > SLACK:
        step.warn(f"{note}; it says it was made after it was fetched, so its clock isn't {plist.zone_name}")
    elif kept.notes:
        step.warn(f"{note}; {'; '.join(kept.notes)}")
    else:
        step.ok(note)
    return {
        "result": "kept",
        "made": kept.stamp,
        "kind": kept.kind,
        "file": _shown(kept.path),
        "size": kept.size,
        "stored": kept.stored,
        "sha256": kept.sha256,
        "file_sha256": kept.file_sha256,
    }


def watch(
    lists: tuple[PriceList, ...],
    fetch: Fetch = net.fetch_new,
    tracker: Tracker = SILENT,
    clock: Callable[[], datetime] = times.now,
) -> Watch:
    """Ask for each of a store's lists once and keep each new one, each its own step. A list
    that fails keeps nothing and is asked for again next run; after one gets no answer at all,
    the rest fail without asking. A run that finds the store's lock held asks nothing."""
    store = lists[0].store
    res = Watch()
    with locks.held(data_dir() / store / "watch.lock") as mine:
        if not mine:
            res.busy = True
            tracker.step(f"{lists[0].label.rsplit(' ', 1)[0]} lists").ok("another run is asking for them")
            return res
        tags = _load_tags(store)
        answering = True
        for plist in lists:
            now = clock()
            entry: dict = {"at": runs.name(now.replace(microsecond=0)), "list": plist.name}
            if not answering:
                why = "not asked: no answer to the list before"
                res.failed.append((plist.label, why))
                tracker.step(plist.label).fail(why)
                _log(store, entry | {"result": "failed", "why": why})
                continue
            step = tracker.step(plist.label, unit="bytes")
            try:
                entry |= _one(plist, fetch, tags, now, step)
            except (net.FetchError, OSError) as e:
                answering = not isinstance(e, net.NoAnswer)
                res.failed.append((plist.label, str(e)))
                step.fail(str(e))
                entry |= {"result": "failed", "why": str(e)}
            else:
                sort = {"kept": res.kept, "empty": res.empty}.get(entry["result"], res.same)
                sort.append(plist.label)
            _log(store, entry)
        _save_tags(store, tags)
    return res
