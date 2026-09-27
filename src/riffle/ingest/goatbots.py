"""MTGO prices from GoatBots, a large MTGO bot chain beside Cardhoarder (whose prices Scryfall
and MTGJSON carry): each day's, and its yearly archives for the years before.

GoatBots publishes its average sell prices once a day (5:30 AM Central European Time) as
zipped JSON for anyone's own project, asking only that a website showing them link to
goatbots.com:

    https://www.goatbots.com/download/prices/price-history.zip         the latest day
    https://www.goatbots.com/download/prices/price-history-<year>.zip  each day of that year
    https://www.goatbots.com/download/prices/card-definitions.zip      the cards it prices

The layout below is the one the open-source clients that read these files expect; nothing
here has seen a real file, so a zip that doesn't match fails its step and says what it held.
Older clients fetch the same names from /download/; that's tried when /download/prices/
answers 404. A price file, price-history-<day>.txt, maps each MTGO catalog ID (a foil has
its own, like Scryfall's mtgo_id and mtgo_foil_id) to its price in tix: {"348": 419.99}.
card-definitions.txt maps the same IDs to {"name", "cardset", "rarity", "foil"}. Riffle keeps
the zips as returned:

    <data_dir>/goatbots/daily/<day>.zip            the price zip for <day>
    <data_dir>/goatbots/yearly/<year>.zip          a year's archive, fetched once the year was over
    <data_dir>/goatbots/yearly/<year>-partial.zip  this year's, as it stood when first kept
    <data_dir>/goatbots/yearly/<year>.none         GoatBots had no archive for the year
    <data_dir>/goatbots/card-definitions.zip       the latest definitions

The daily zip is small, so each run fetches it and keeps it when its day is new. The
definitions are fetched again whenever they're older than the newest day kept. The yearly
archives are the history before Riffle's own: GoatBots keeps only the last few years, so
every year it still has is kept once, newest first, until a year it has none for, which is
noted so it isn't asked for again. The current year's is kept as it stands, and again whole
once the year is over. Nothing here reads the prices back: the price loader does. Headers,
retries, and 429 handling: riffle.net.
"""

import json
import lzma
import re
import zipfile
import zlib
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from riffle import net
from riffle.config import data_dir
from riffle.progress import SILENT, Step, Tracker

BASES = ("https://www.goatbots.com/download/prices", "https://www.goatbots.com/download")
LATEST = "price-history.zip"
DEFINITIONS = "card-definitions.zip"
FIRST_YEAR = 2012  # GoatBots began trading; no archive can be older
MAX_ENTRY = 256 << 20  # bytes a file in a zip may unpack to; a price file is about 2 MB
PRICE_FILE = re.compile(r"price-history-(\d{4}-\d{2}-\d{2})\.txt")
# What reading a corrupt or unexpected zip raises: bad structure, bad data, an entry cut
# short, a compression method or encryption zipfile can't read (NotImplementedError,
# RuntimeError), a bad name or size (ValueError).
DAMAGED = (
    zipfile.BadZipFile,
    zlib.error,
    lzma.LZMAError,
    EOFError,
    NotImplementedError,
    RuntimeError,
    ValueError,
)

Download = Callable[[str, Path, net.Progress | None], int | None]  # url, dest -> bytes, or None for 404


@dataclass
class Snapshot:
    day: date | None = None  # the latest day GoatBots published, if its zip could be read
    kept: list[str] = field(default_factory=list)  # files kept this run, relative to goatbots/


def _download(url: str, dest: Path, progress: net.Progress | None = None) -> int | None:
    return net.download(url, dest, accept="application/zip", progress=progress)


def _count(n: int, noun: str) -> str:
    return f"{n:,} {noun}" if n == 1 else f"{n:,} {noun}s"


def goatbots_dir() -> Path:
    return data_dir() / "goatbots"


def daily_dir() -> Path:
    return goatbots_dir() / "daily"


def yearly_dir() -> Path:
    return goatbots_dir() / "yearly"


def _fetch(name: str, dest: Path, download: Download, progress: net.Progress | None) -> bool:
    """name into dest from the first base that has it. False when none does."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    return any(download(f"{base}/{name}", dest, progress) is not None for base in BASES)


def _open(path: Path, name: str) -> zipfile.ZipFile:
    try:
        return zipfile.ZipFile(path)
    except DAMAGED as e:
        raise net.FetchError(f"{name}: not a zip") from e


def _held(zf: zipfile.ZipFile) -> str:
    """What a zip holds, for a message: its first few names."""
    names = zf.namelist()
    shown = ", ".join(names[:3]) + (f", and {len(names) - 3:,} more" if len(names) > 3 else "")
    return shown or "nothing"


def _json(zf: zipfile.ZipFile, entry: str, name: str) -> object:
    """A file in the zip, parsed; a FetchError if it's too big to be what it claims, is
    damaged, or isn't JSON."""
    size = zf.getinfo(entry).file_size
    if size > MAX_ENTRY:
        raise net.FetchError(f"{name}: {entry} unpacks to {size:,} bytes, too big to be what it claims")
    try:
        body = zf.read(entry)
    except DAMAGED as e:
        raise net.FetchError(f"{name}: {entry} is damaged") from e
    try:
        return json.loads(body)
    except (ValueError, RecursionError) as e:
        raise net.FetchError(f"{name}: {entry} isn't the expected JSON") from e


def price_days(zf: zipfile.ZipFile) -> dict[date, str]:
    """Day -> the price file for it, from the names in a price zip."""
    days = {}
    for entry in zf.namelist():
        found = PRICE_FILE.fullmatch(entry.rsplit("/", 1)[-1])
        if found:
            try:
                days[date.fromisoformat(found.group(1))] = entry
            except ValueError:
                continue
    return days


def _prices(path: Path) -> tuple[date, int]:
    """The day of a latest-prices zip and how many prices it has, checked: a price file of
    MTGO IDs and prices in tix (the latest, if it holds more than one)."""
    with _open(path, LATEST) as zf:
        days = price_days(zf)
        if not days:
            raise net.FetchError(f"{LATEST}: no price-history-<day>.txt in it, only {_held(zf)}")
        day = max(days)
        doc = _json(zf, days[day], LATEST)
    if not isinstance(doc, dict) or not all(
        isinstance(v, int | float) and not isinstance(v, bool) for v in doc.values()
    ):
        raise net.FetchError(f"{LATEST}: {days[day]} isn't MTGO IDs and prices")
    return day, len(doc)


def _latest(snap: Snapshot, download: Download, step: Step) -> None:
    """The latest day's prices, kept when the day is new."""
    fresh = daily_dir() / f"{LATEST}.new"
    try:
        if not _fetch(LATEST, fresh, download, step.update):
            raise net.FetchError(f"{LATEST}: HTTP 404")
        day, n = _prices(fresh)
        snap.day = day
        dest = daily_dir() / f"{day.isoformat()}.zip"
        if dest.exists():
            step.ok(f"already have {day}")
            return
        fresh.replace(dest)
        snap.kept.append(f"daily/{dest.name}")
        step.ok(f"kept {day}, {_count(n, 'price')}")
    finally:
        fresh.unlink(missing_ok=True)


def definitions_due() -> bool:
    """Whether the card definitions are missing or older than the newest day kept."""
    defs = goatbots_dir() / DEFINITIONS
    days = sorted(daily_dir().glob("*.zip"))
    if not defs.exists():
        return bool(days)
    return bool(days) and defs.stat().st_mtime < max(p.stat().st_mtime for p in days)


def _definitions(snap: Snapshot, download: Download, step: Step) -> None:
    """The latest card definitions, replacing the last."""
    dest = goatbots_dir() / DEFINITIONS
    fresh = goatbots_dir() / f"{DEFINITIONS}.new"
    try:
        if not _fetch(DEFINITIONS, fresh, download, step.update):
            raise net.FetchError(f"{DEFINITIONS}: HTTP 404")
        entry = "card-definitions.txt"
        with _open(fresh, DEFINITIONS) as zf:
            if entry not in zf.namelist():
                raise net.FetchError(f"{DEFINITIONS}: no {entry} in it, only {_held(zf)}")
            doc = _json(zf, entry, DEFINITIONS)
        if not isinstance(doc, dict) or not all(isinstance(v, dict) and "name" in v for v in doc.values()):
            raise net.FetchError(f"{DEFINITIONS}: {entry} isn't MTGO IDs and cards")
        fresh.replace(dest)
        snap.kept.append(DEFINITIONS)
        step.ok(_count(len(doc), "card"))
    finally:
        fresh.unlink(missing_ok=True)


def _archive(year: int, dest: Path, download: Download, step: Step) -> int | None:
    """A year's archive into dest, checked: a zip whose files are that year's days, each intact.
    How many days it has, or None when GoatBots has no archive for the year."""
    name = f"price-history-{year}.zip"
    fresh = dest.with_name(f"{dest.name}.new")
    try:
        if not _fetch(name, fresh, download, step.update):
            return None
        with _open(fresh, name) as zf:
            days = [day for day in price_days(zf) if day.year == year]
            if not days:
                raise net.FetchError(
                    f"{name}: no price-history-{year}-<month>-<day>.txt in it, only {_held(zf)}"
                )
            try:
                broken = zf.testzip()
            except DAMAGED as e:
                raise net.FetchError(f"{name}: damaged ({type(e).__name__})") from e
            if broken is not None:
                raise net.FetchError(f"{name}: {broken} is damaged")
        fresh.replace(dest)
        return len(days)
    finally:
        fresh.unlink(missing_ok=True)


def _year(year: int, latest: date, snap: Snapshot, download: Download, step: Step) -> bool:
    """One year's archive as its own step. Whether the walk goes on to older years."""
    whole, partial = yearly_dir() / f"{year}.zip", yearly_dir() / f"{year}-partial.zip"
    dest = partial if year == latest.year else whole
    days = _archive(year, dest, download, step)
    if days is None:
        if year == latest.year:  # early January: nothing archived yet this year
            step.drop()
            return True
        if partial.exists():  # it had one when this year was the current one
            raise net.FetchError(f"price-history-{year}.zip: HTTP 404, though a partial one is kept")
        (yearly_dir() / f"{year}.none").touch()
        step.ok("none from GoatBots; not asked again")
        return False
    if dest == whole:
        partial.unlink(missing_ok=True)
    snap.kept.append(f"yearly/{dest.name}")
    step.ok(f"kept {_count(days, 'day')}, {dest.stat().st_size / 1e6:,.1f} MB")
    return True


def _years(latest: date, snap: Snapshot, download: Download, tracker: Tracker) -> None:
    """Every yearly archive GoatBots still has and Riffle doesn't, newest first, each its own
    step. A failed one stops the walk; the next run starts again from it."""
    for year in range(latest.year, FIRST_YEAR - 1, -1):
        whole, partial = yearly_dir() / f"{year}.zip", yearly_dir() / f"{year}-partial.zip"
        if whole.exists() or (year == latest.year and partial.exists()):
            continue
        if (yearly_dir() / f"{year}.none").exists():
            return
        step = tracker.step(f"GoatBots {year}", unit="bytes")
        try:
            if not _year(year, latest, snap, download, step):
                return
        except (net.FetchError, OSError) as e:
            step.fail(str(e))
            return


def snapshot(
    download: Download = _download, tracker: Tracker = SILENT, today: date | None = None
) -> Snapshot:
    """Keep GoatBots' latest prices, its card definitions after a new day or when missing or
    behind, and every yearly archive not yet kept. Each is a step on the tracker; a failure is
    reported on its step and keeps nothing, so the next run tries again. today stands in for
    the latest day when GoatBots' zip can't be read."""
    snap = Snapshot()
    step = tracker.step("GoatBots prices", unit="bytes")
    try:
        _latest(snap, download, step)
    except (net.FetchError, OSError) as e:
        step.fail(str(e))
    try:
        due = definitions_due()
    except OSError:
        due = True
    if due:
        cards = tracker.step("GoatBots cards", unit="bytes")
        try:
            _definitions(snap, download, cards)
        except (net.FetchError, OSError) as e:
            cards.fail(str(e))
    _years(snap.day or today or date.today(), snap, download, tracker)
    return snap
