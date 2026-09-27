"""Daily snapshots of whole store price lists: Card Kingdom's.

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

A store's list is its prices right now, with no date of its own to go by (Card Kingdom's
created_at has no time zone), so Riffle keeps one a day, named by the Mac's date, as returned
but gzipped:

    <data_dir>/<store>/daily/<day>/<list>.json.gz

A list kept today isn't asked for again, and once a store gives no answer at all, its
other lists fail without asking. Nothing here reads the lists back: the price loader does.
Headers, retries, and 429 handling: riffle.net.
"""

import gzip
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from riffle import net
from riffle.config import data_dir
from riffle.progress import SILENT, Step, Tracker

WRAPPER = (b"<html><head></head><body>", b"</body></html>")  # Card Kingdom's, sometimes


@dataclass(frozen=True)
class PriceList:
    store: str  # the folder under data_dir
    name: str  # the file in each day's folder, <name>.json.gz
    label: str  # the progress step
    url: str


CARD_KINGDOM = (
    PriceList(
        "cardkingdom", "singles", "Card Kingdom singles", "https://api.cardkingdom.com/api/v2/pricelist"
    ),
    PriceList(
        "cardkingdom", "sealed", "Card Kingdom sealed", "https://api.cardkingdom.com/api/sealed_pricelist"
    ),
)

Download = Callable[[str, Path, net.Progress | None], int | None]  # url, dest -> bytes, or None for 404


@dataclass
class Snapshot:
    day: date
    kept: list[str] = field(default_factory=list)  # labels of the lists kept this run
    skipped: list[str] = field(default_factory=list)  # already kept today
    failed: list[tuple[str, str]] = field(default_factory=list)  # (label, why)


def _download(url: str, dest: Path, progress: net.Progress | None = None) -> int | None:
    return net.download(url, dest, accept="application/json", progress=progress)


def day_dir(store: str, day: date) -> Path:
    return data_dir() / store / "daily" / day.isoformat()


def target(plist: PriceList, day: date) -> Path:
    return day_dir(plist.store, day) / f"{plist.name}.json.gz"


def _edge(path: Path, size: int, end: bool) -> bytes:
    with path.open("rb") as f:
        if end:
            f.seek(max(0, path.stat().st_size - size))
        return f.read(size)


def check(path: Path, what: str) -> None:
    """A FetchError unless the file looks like a whole price list: one JSON object with a
    "data" key near its start, complete to its closing brace. Read at both ends only; a list
    is tens of megabytes."""
    head, tail = _edge(path, 4096, end=False).lstrip(), _edge(path, 4096, end=True).rstrip()
    if head.startswith(WRAPPER[0]) and tail.endswith(WRAPPER[1]):
        head, tail = head.removeprefix(WRAPPER[0]).lstrip(), tail.removesuffix(WRAPPER[1]).rstrip()
    if not head.startswith(b"{") or b'"data"' not in head or not tail.endswith(b"}"):
        raise net.FetchError(f"{what}: not the expected JSON")


def _keep(plist: PriceList, day: date, download: Download, step: Step) -> Path:
    """Download one list, check it, and keep it gzipped for the day. The download waits
    beside the day folders, which is made only for a list that's kept."""
    dest = target(plist, day)
    fresh = dest.parent.parent / f"{plist.name}.json.new"
    part = dest.with_name(dest.name + ".part")
    fresh.parent.mkdir(parents=True, exist_ok=True)
    try:
        if download(plist.url, fresh, step.update) is None:
            raise net.FetchError(f"{plist.url.rsplit('/', 1)[-1]}: HTTP 404")
        check(fresh, plist.url.rsplit("/", 1)[-1])
        dest.parent.mkdir(exist_ok=True)
        with fresh.open("rb") as src, gzip.open(part, "wb") as out:
            shutil.copyfileobj(src, out)
        part.replace(dest)
        return dest
    finally:
        fresh.unlink(missing_ok=True)
        part.unlink(missing_ok=True)
        if dest.parent.is_dir() and not any(dest.parent.iterdir()):
            dest.parent.rmdir()  # a failed first list leaves no empty day behind


def snapshot(
    lists: tuple[PriceList, ...],
    download: Download = _download,
    tracker: Tracker = SILENT,
    today: date | None = None,
) -> Snapshot:
    """Keep today's copy of each list not kept yet, each its own step. A list that fails
    keeps nothing and is retried the next run; after one gets no answer at all, the rest
    fail without asking."""
    snap = Snapshot(day=today or date.today())
    answering = True
    for plist in lists:
        if target(plist, snap.day).exists():
            snap.skipped.append(plist.label)
            tracker.step(plist.label).ok(f"already have {snap.day}")
            continue
        if not answering:
            why = "not asked: no answer to the list before"
            snap.failed.append((plist.label, why))
            tracker.step(plist.label).fail(why)
            continue
        step = tracker.step(plist.label, unit="bytes")
        try:
            dest = _keep(plist, snap.day, download, step)
        except (net.FetchError, OSError) as e:
            answering = not isinstance(e, net.NoAnswer)
            snap.failed.append((plist.label, str(e)))
            step.fail(str(e))
        else:
            snap.kept.append(plist.label)
            step.ok(f"kept {snap.day}, {dest.stat().st_size / 1e6:,.1f} MB")
    return snap
