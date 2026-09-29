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
    <data_dir>/goatbots/yearly/<year>-short-<UTC time>.zip  a whole year's that came short, fetched then
    <data_dir>/goatbots/yearly/<year>.none         GoatBots had no archive for the year: the day it said so
    <data_dir>/goatbots/card-definitions.zip       the latest definitions

The daily zip is small, so each run fetches it and keeps it when its day is new. The
definitions are fetched again whenever they're older than the newest day kept. An empty
price file ({}) keeps nothing, and empty definitions don't replace the kept ones; either is
asked for again next run, and is a warning after seven runs in a row (riffle.ingest.empties).
The yearly archives are the history before Riffle's own: GoatBots keeps only the last few
years, so every year it still has is kept once, newest first, until a year it has none for,
which is asked for again a week later. The current year's is kept as it stands, and again
whole once the year is over: as the year's only if the whole one runs to Dec 31 and holds
every day of the partial, which stays beside it. One short of that is asked for again next
run, a warning after seven runs in a row, and kept beside the partial when it has a day no
archive of the year kept has, so nothing is lost if the whole one never comes. Nothing here
reads the prices back: the price loader does. Headers, retries, and 429 handling: riffle.net.
"""

import json
import lzma
import re
import zipfile
import zlib
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from riffle import net, times
from riffle.config import data_dir
from riffle.ingest import empties
from riffle.progress import SILENT, Step, Tracker

BASES = ("https://www.goatbots.com/download/prices", "https://www.goatbots.com/download")
LATEST = "price-history.zip"
DEFINITIONS = "card-definitions.zip"
FIRST_YEAR = 2012  # GoatBots began trading; no archive can be older
RETRY_NONE = timedelta(days=7)  # a year GoatBots had no archive for is asked for again this often
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
        if not n:
            empties.report(step, "goatbots/prices", "empty price file")
            return
        empties.clear("goatbots/prices")
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
    """The latest card definitions, replacing the last unless they're empty."""
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
        if not doc:
            empties.report(step, "goatbots/cards", "empty card definitions")
            return
        empties.clear("goatbots/cards")
        fresh.replace(dest)
        snap.kept.append(DEFINITIONS)
        step.ok(_count(len(doc), "card"))
    finally:
        fresh.unlink(missing_ok=True)


def _archive(year: int, fresh: Path, download: Download, step: Step) -> set[date] | None:
    """A year's archive into fresh, checked: a zip whose files are that year's days, each
    intact. Its days, or None when GoatBots has no archive for the year."""
    name = f"price-history-{year}.zip"
    if not _fetch(name, fresh, download, step.update):
        return None
    with _open(fresh, name) as zf:
        days = {day for day in price_days(zf) if day.year == year}
        if not days:
            raise net.FetchError(f"{name}: no price-history-{year}-<month>-<day>.txt in it, only {_held(zf)}")
        try:
            broken = zf.testzip()
        except DAMAGED as e:
            raise net.FetchError(f"{name}: damaged ({type(e).__name__})") from e
        if broken is not None:
            raise net.FetchError(f"{name}: {broken} is damaged")
    return days


def _lacks(year: int, days: set[date], partial: Path) -> list[date]:
    """What a whole year's archive lacks: Dec 31, and any day of the partial one kept."""
    wanted = {date(year, 12, 31)}
    if partial.exists():
        with _open(partial, partial.name) as zf:
            wanted |= {day for day in price_days(zf) if day.year == year}
    return sorted(wanted - days)


def _year_days(year: int) -> set[date]:
    """Every day of year its kept partial and short archives hold."""
    days: set[date] = set()
    for path in yearly_dir().glob(f"{year}-*.zip"):
        with _open(path, path.name) as zf:
            days |= {day for day in price_days(zf) if day.year == year}
    return days


def _short(year: int, days: set[date], lacks: list[date], fresh: Path, snap: Snapshot, step: Step) -> None:
    """A whole year that lacks days isn't the year's: it's asked for again next run, a warning
    after empties.WARN_AFTER runs in a row. When it has a day no kept archive of the year has,
    it's kept beside them, named by its fetch time, so nothing is lost if the whole one never
    comes."""
    shown = ", ".join(day.isoformat() for day in lacks[:3])
    if len(lacks) > 3:
        shown += f", and {_count(len(lacks) - 3, 'more day')}"
    new = days - _year_days(year)
    if new:
        dest = yearly_dir() / f"{year}-short-{times.now().astimezone(UTC):%Y-%m-%dT%H%M%SZ}.zip"
        fresh.replace(dest)
        snap.kept.append(f"yearly/{dest.name}")
        kept = f"kept as {dest.name} for {_count(len(new), 'day')} not kept before"
    else:
        kept = "no day in it not kept before"
    note = f"lacks {shown}; {kept}; the whole year asked again next run"
    seen = empties.record(f"goatbots/{year}", "short")
    if seen.runs < empties.WARN_AFTER:
        step.ok(note)
    else:
        step.warn(f"{note} ({seen.runs} runs in a row, since {seen.first.date()})")


def _none_since(path: Path) -> date:
    """The day GoatBots last had no archive for a year, as its .none file says; the file's own
    day (UTC) for one written before the day was."""
    try:
        return date.fromisoformat(path.read_text(encoding="utf-8").strip())
    except ValueError:
        return datetime.fromtimestamp(path.stat().st_mtime, UTC).date()


def _year(year: int, latest: date, snap: Snapshot, download: Download, step: Step) -> bool:
    """One year's archive as its own step. Whether the walk goes on to older years."""
    whole, partial, none = (yearly_dir() / f"{year}{end}" for end in (".zip", "-partial.zip", ".none"))
    dest = partial if year == latest.year else whole
    fresh = dest.with_name(f"{dest.name}.new")
    try:
        days = _archive(year, fresh, download, step)
        if days is None:
            if year == latest.year:  # early January: nothing archived yet this year
                step.drop()
                return True
            if partial.exists():  # it had one when this year was the current one
                raise net.FetchError(f"price-history-{year}.zip: HTTP 404, though a partial one is kept")
            today = times.today()
            none.write_text(f"{today}\n", encoding="utf-8")
            step.ok(f"none from GoatBots; asked again from {today + RETRY_NONE}")
            return False
        if dest == whole and (lacks := _lacks(year, days, partial)):
            _short(year, days, lacks, fresh, snap, step)
            return True
        fresh.replace(dest)
    finally:
        fresh.unlink(missing_ok=True)
    empties.clear(f"goatbots/{year}")
    none.unlink(missing_ok=True)
    snap.kept.append(f"yearly/{dest.name}")
    step.ok(f"kept {_count(len(days), 'day')}, {dest.stat().st_size / 1e6:,.1f} MB")
    return True


def _years(latest: date, snap: Snapshot, download: Download, tracker: Tracker) -> None:
    """Every yearly archive GoatBots still has and Riffle doesn't, newest first, each its own
    step. A failed one stops the walk; the next run starts again from it. So does a year
    GoatBots had none for, until it's asked for again a week later."""
    for year in range(latest.year, FIRST_YEAR - 1, -1):
        whole, partial, none = (yearly_dir() / f"{year}{end}" for end in (".zip", "-partial.zip", ".none"))
        if whole.exists() or (year == latest.year and partial.exists()):
            continue
        if none.exists() and times.today() < _none_since(none) + RETRY_NONE:
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
    _years(snap.day or today or times.today(), snap, download, tracker)
    return snap
