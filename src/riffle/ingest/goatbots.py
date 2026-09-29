"""MTGO prices from GoatBots, a large MTGO bot chain beside Cardhoarder (whose prices Scryfall
and MTGJSON carry): each day's, and its yearly archives for the years before.

GoatBots publishes its average sell prices once a day as zipped JSON for anyone's own
project, asking only that a website showing them link to goatbots.com. The zip of
2026-09-28's prices was made 03:15:20 UTC on 2026-09-29 (its Last-Modified); the price file
inside says 05:15:20, Central European local time with no zone.

    https://www.goatbots.com/download/prices/price-history.zip         the latest day
    https://www.goatbots.com/download/prices/price-history-<year>.zip  each day of that year
    https://www.goatbots.com/download/prices/card-definitions.zip      the cards it prices

A zip that doesn't hold what's described below fails its step and says what it held.
Older clients fetch the same names from /download/; that's tried when /download/prices/
answers 404. A price file, price-history-<day>.txt, maps each MTGO catalog ID (a foil has
its own, like Scryfall's mtgo_id and mtgo_foil_id) to its price in tix: {"348": 419.99}.
card-definitions.txt maps the same IDs to {"name", "cardset", "rarity", "foil"}.

Every day's price file is kept (watch: `riffle watch goatbots`, asked when riffle.cadence
says the next is due, with the ETag of the last one kept): the file's own bytes, by the run
rule (riffle.runs), under the zip's Last-Modified. A zip holding more than one price file
has never been seen: the newest is kept as the list, and the zip is set aside whole. The
rest are kept as returned, by the sync (snapshot):

    <data_dir>/goatbots/lists/prices/<run>/        the price files, by the run rule
    <data_dir>/goatbots/watch.jsonl                every check; for a list kept, its day, the
                                                   price file's name and time, and the zip's
                                                   SHA-256, size and ETag (riffle.watching)
    <data_dir>/goatbots/aside/price-history-<UTC time>.zip  a zip with more than one price
                                                   file, none, or no Last-Modified
    <data_dir>/goatbots/daily/<day>.zip            the price zip for <day>, as the sync kept it before
    <data_dir>/goatbots/yearly/<year>.zip          a year's archive, fetched once the year was over
    <data_dir>/goatbots/yearly/<year>-partial.zip  this year's, as it stood when first kept
    <data_dir>/goatbots/yearly/<year>-short-<UTC time>.zip  a whole year's that came short, fetched then
    <data_dir>/goatbots/yearly/<year>.none         GoatBots had no archive for the year: the day it said so
    <data_dir>/goatbots/card-definitions.zip       the latest definitions

The definitions are fetched again whenever they're older than the newest list kept. An empty
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

from riffle import net, runs, times, watching
from riffle.config import data_dir
from riffle.ingest import empties
from riffle.progress import SILENT, Step, Tracker

BASES = ("https://www.goatbots.com/download/prices", "https://www.goatbots.com/download")
LATEST = "price-history.zip"
STORE = "goatbots"
LIST = "prices"  # the day's price file in the watch log
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
Watcher = Callable[..., net.Fetched | None]  # net.fetch_new


@dataclass
class Snapshot:
    day: date | None = None  # the newest day whose prices are kept
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


def lists_dir() -> Path:
    return goatbots_dir() / "lists" / LIST


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


def _read(zf: zipfile.ZipFile, entry: str, name: str) -> bytes:
    """A file in the zip; a FetchError if it's too big to be what it claims, or is damaged."""
    size = zf.getinfo(entry).file_size
    if size > MAX_ENTRY:
        raise net.FetchError(f"{name}: {entry} unpacks to {size:,} bytes, too big to be what it claims")
    try:
        return zf.read(entry)
    except DAMAGED as e:
        raise net.FetchError(f"{name}: {entry} is damaged") from e


def _json(zf: zipfile.ZipFile, entry: str, name: str) -> object:
    """A file in the zip, parsed; a FetchError if it can't be read (_read) or isn't JSON."""
    return _parsed(_read(zf, entry, name), entry, name)


def _parsed(body: bytes, entry: str, name: str) -> object:
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


def _prices(body: bytes, entry: str) -> int:
    """How many prices a price file has, checked: MTGO IDs and prices in tix."""
    doc = _parsed(body, entry, LATEST)
    if not isinstance(doc, dict) or not all(
        isinstance(v, int | float) and not isinstance(v, bool) for v in doc.values()
    ):
        raise net.FetchError(f"{LATEST}: {entry} isn't MTGO IDs and prices")
    return len(doc)


def definitions_due() -> bool:
    """Whether the card definitions are missing or older than the newest list kept: in a run,
    or a daily zip kept before."""
    defs = goatbots_dir() / DEFINITIONS
    lists = [*daily_dir().glob("*.zip"), *runs.kept(lists_dir()).values()]
    if not defs.exists():
        return bool(lists)
    return bool(lists) and defs.stat().st_mtime < max(p.stat().st_mtime for p in lists)


def newest_day() -> date | None:
    """The newest day whose prices are kept: in a run, or a daily zip kept before."""
    days = watching.days(STORE, LIST)
    for path in daily_dir().glob("*.zip"):
        try:
            days.append(date.fromisoformat(path.stem))
        except ValueError:
            continue
    return max(days, default=None)


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
    """Keep GoatBots' card definitions after a new list or when missing or behind, and every
    yearly archive not yet kept. Each is a step on the tracker; a failure is reported on its step
    and keeps nothing, so the next run tries again. today stands in for the newest day when no
    day is kept."""
    snap = Snapshot(day=newest_day())
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


class NoPriceFile(net.FetchError):
    """A price zip with no price file in it."""


def _newest(path: Path) -> tuple[dict[date, str], bytes, str]:
    """The price files in a latest-prices zip by day, and the newest one's bytes and time (as
    the zip says it: Central European local time, no zone)."""
    with _open(path, LATEST) as zf:
        days = price_days(zf)
        if not days:
            raise NoPriceFile(f"{LATEST}: no price-history-<day>.txt in it, only {_held(zf)}")
        entry = days[max(days)]
        return days, _read(zf, entry, LATEST), datetime(*zf.getinfo(entry).date_time).isoformat()


def _unknown(head: bytes) -> bool:
    """A zip's first bytes don't say which day it holds: whether it's kept shows only in its
    Last-Modified."""
    return False


def _day(fetch: Watcher, tags: dict[str, str], now: datetime, step: Step) -> dict:
    """Ask for the latest price zip once, with the ETag of the last one kept, and keep its price
    file if it's new; end step saying what came. The log entry."""
    folder = lists_dir()
    fresh = folder.parent / f"{LIST}.new"
    try:
        fresh.parent.mkdir(parents=True, exist_ok=True)
        got = None
        for base in BASES:
            got = fetch(
                f"{base}/{LATEST}",
                fresh,
                _unknown,
                etag=tags.get(LIST),
                accept="application/zip",
                progress=step.update,
            )
            if got is not None:
                break
        if got is None:
            raise net.FetchError(f"{LATEST}: HTTP 404")
        if got.status == "unchanged":
            step.ok("no new list since the last one kept")
            return {"result": "unchanged"}
        served = watching.served(fresh, got)
        try:
            days, body, written = _newest(fresh)
        except NoPriceFile as e:
            raise net.FetchError(f"{e}; set aside as {watching.set_aside(STORE, fresh, LATEST, now)}") from e
        day = max(days)
        entry = days[day]
        n = _prices(body, entry)
        if not n:
            empties.report(step, "goatbots/prices", "empty price file", now)
            return {"result": "empty"}
        empties.clear("goatbots/prices")
        if got.modified is None:
            where = watching.set_aside(STORE, fresh, LATEST, now)
            raise net.FetchError(f"{LATEST}: no Last-Modified, so no time it was made; set aside as {where}")
        stamp, made = runs.name(got.modified), times.shown(got.modified)
        if stamp in runs.kept(folder):
            if got.etag:
                tags[LIST] = got.etag
            step.ok(f"have {day}'s list, made {made}")
            return {"result": "known", "made": stamp}
        kept = runs.keep(folder, got.modified, body)
        if got.etag:
            tags[LIST] = got.etag
        facts = {"day": day.isoformat(), "entry": entry, "entry_time": written} | served
        notes = list(kept.notes)
        if len(days) > 1:
            facts |= {
                "days": sorted(d.isoformat() for d in days),
                "aside": watching.set_aside(STORE, fresh, LATEST, now),
            }
            others = ", ".join(sorted(d.isoformat() for d in days if d != day))
            notes.append(f"the zip also held {others}; set aside whole as {facts['aside']}")
        note = f"kept {day}'s list, made {made}, {_count(n, 'price')}: {watching.how(kept)}"
        if notes:
            step.warn(f"{note}; {'; '.join(notes)}")
        else:
            step.ok(note)
        return watching.record(kept) | facts
    finally:
        fresh.unlink(missing_ok=True)


def watch(
    fetch: Watcher = net.fetch_new,
    tracker: Tracker = SILENT,
    clock: Callable[[], datetime] = times.now,
    always: bool = False,
) -> watching.Watch:
    """Ask for the latest price zip when riffle.cadence says GoatBots' next list is due, or
    always (the sync), and keep its price file when it's new (riffle.watching.one)."""

    def ask(tags: dict[str, str], now: datetime, step: Step) -> dict:
        return _day(fetch, tags, now, step)

    return watching.one(STORE, LIST, "GoatBots prices", ask, lists_dir(), tracker, clock, always)
