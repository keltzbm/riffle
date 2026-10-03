"""Checks that every price file Riffle keeps is dated as it should be: under the day its own
stamp says, and made no later than it was fetched. Read-only: each file's first bytes (every
byte of a store's watched lists, to hash them), and when it was fetched (the time in the gzip
header of a file Riffle gzipped, the file's own time otherwise). `riffle check` runs them all.

    Card Kingdom, Mana Pool   each list kept a day under the day of its created_at or as_of, and
                              each list kept since (riffle.runs) with the file its log names, by
                              the file's SHA-256, a base's second copy too; every list made no
                              later than its fetch. Card Kingdom's created_at names no zone: read
                              as Pacific time, a list made after its fetch means Card Kingdom's
                              clock isn't Pacific. The lists set aside are counted, and each list's
                              usual gap is noted, and whether it's late by its own learned margin
                              (riffle.lateness).
    Cardmarket                each guide kept a day under the day of its createdAt, and each kept
                              since in its runs as its log says; every guide made before its fetch,
                              and each game's lateness (riffle.lateness)
    MTGJSON                   each file kept a day named by the date in its meta, no later than
                              its fetch; each build kept since in its runs as its log says, made
                              before its fetch; the files set aside counted; its lateness
    GoatBots                  each day's zip holds that day's price file, no later than its fetch;
                              each price file kept since in its runs as its log says, made before
                              its fetch; the zips set aside counted; its lateness; each whole
                              year's archive runs to Dec 31
    tcgcsv                    each day's last-updated.txt is that day's, from before its fetch, and
                              no set under it from a later refresh (by its Last-Modified); each game
                              kept in its runs as its kept.json says. Games not finished are noted:
                              tcgcsv's day passed, or it's being fetched. And its lateness.
    Scryfall                  no day's prices kept before that day began (UTC); each bulk type's
                              lateness, from its publishes kept or found the same as one kept
"""

import gzip
import json
import lzma
import re
import zipfile
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from riffle import lateness, runs, times, watching
from riffle.config import data_dir
from riffle.ingest import cardmarket, goatbots, mtgjson, pricelists, scryfall, tcgcsv
from riffle.progress import elapsed
from riffle.runs import zstd

SLACK = pricelists.SLACK
MTGJSON_DATE = re.compile(rb'"date"\s*:\s*"(\d{4}-\d{2}-\d{2})"')
TCGCSV_SET = re.compile(rb'^\{"groupId": (\d+), "fetched": "[^"]*", "lastModified": "([^"]+)"', re.MULTILINE)
SHOWN = 5  # unfinished games named in a note; the rest are counted


@dataclass
class Report:
    source: str
    what: str  # what a file is: "list", "guide", "day"
    files: int = 0
    days: set[str] = field(default_factory=set)
    problems: list[str] = field(default_factory=list)  # "<file>: what's wrong"
    notes: list[str] = field(default_factory=list)
    kept: dict[str, Counter[str]] = field(default_factory=dict)  # row -> ISO day -> kept: riffle status
    late: bool = False  # a list late by its own margin (riffle.lateness)

    def tally(self, day: str, row: str = "") -> None:
        """One more kept on day, in a row of its own when a source has more than one."""
        self.kept.setdefault(row, Counter())[day] += 1

    def summary(self) -> str:
        count = f"{self.files:,} {self.what}{'s' * (self.files != 1)}"
        if self.days and len(self.days) != self.files:
            count += f" over {len(self.days):,} day{'s' * (len(self.days) != 1)}"
        found = f"{len(self.problems)} wrong" if self.problems else "all right"
        return f"{count}: {found}" if self.files else "nothing kept yet"


def _rel(path: Path) -> str:
    return path.relative_to(data_dir()).as_posix()


def _day(name: str) -> date | None:
    try:
        return date.fromisoformat(name)
    except ValueError:
        return None


def _file_time(path: Path) -> datetime:
    return datetime.fromtimestamp(path.stat().st_mtime, UTC)


def _made_late(rep: Report, path: Path, made: datetime, got: datetime | None) -> None:
    """Note a file made after it was fetched, or with no fetch time to tell."""
    if got is None:
        rep.problems.append(f"{_rel(path)}: no time it was fetched")
    elif made - got > SLACK:
        rep.problems.append(
            f"{_rel(path)}: made {times.shown(made)}, after it was fetched at {times.shown(got)}"
        )


def store(lists: tuple[pricelists.PriceList, ...]) -> Report:
    """A store's kept lists: each under its own day, made before its fetch."""
    first = lists[0]
    rep = Report(first.label.rsplit(" ", 1)[0], "list")
    root = data_dir() / first.store
    named = {plist.name: plist for plist in lists}
    ages = []
    for path in sorted((root / "daily").glob("*/*.json.gz")):
        plist = named.get(path.name.removesuffix(".json.gz"))
        if plist is None:
            continue
        rep.files += 1
        rep.days.add(path.parent.name)
        rep.tally(path.parent.name)
        made = pricelists.kept_made(path, plist)
        if made is None:
            rep.problems.append(f"{_rel(path)}: no readable {plist.stamp}")
            continue
        day = pricelists.day_of(made, plist)
        if day.isoformat() != path.parent.name:
            rep.problems.append(f"{_rel(path)}: made on {day}, kept under {path.parent.name}")
        got = pricelists.fetched(path)
        if got is None:
            rep.problems.append(f"{_rel(path)}: no time it was fetched")
        elif made - got > SLACK:
            stamp = made.astimezone(plist.zone).strftime("%Y-%m-%d %H:%M:%S")
            rep.problems.append(
                f"{_rel(path)}: its {plist.stamp} {stamp}, read as {plist.zone_name}, is "
                f"{times.shown(made)}, after it was fetched at {times.shown(got)}: "
                f"{rep.source}'s clock isn't {plist.zone_name}"
            )
        else:
            ages.append(got - made)
    if ages:
        span = elapsed(min(ages).total_seconds())
        if len(ages) > 1:
            span += f" to {elapsed(max(ages).total_seconds())}"
        rep.notes.append(f"read as {first.zone_name}, each list was made {span} before it was fetched")
    _watched(rep, lists)
    aside = sorted((root / "aside").glob("*.json.gz"))
    if aside:
        rep.notes.append(f"{len(aside)} set aside in {first.store}/aside, not as any day's")
    return rep


def _kept_entries(rep: Report, store: str) -> list[dict]:
    """The lists a store's watch log says it kept."""
    path = watching.log_path(store)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    found = []
    for n, line in enumerate(lines, 1):
        try:
            entry = json.loads(line)
        except ValueError:
            rep.problems.append(f"{_rel(path)}: line {n} isn't JSON")
            continue
        if isinstance(entry, dict) and entry.get("result") == "kept":
            found.append(entry)
    return found


def _hashed(rep: Report, entry: dict) -> None:
    """Each of a kept list's files hashes as it did when kept."""
    first = data_dir() / entry["file"]
    files = [first, runs.copies(first.parent)[1]] if entry["kind"] == "base" else [first]
    bad = []
    for path in files:
        if not path.exists():
            bad.append(f"{_rel(path)}: missing")
        elif runs.file_sha256(path) != entry["file_sha256"]:
            bad.append(f"{_rel(path)}: changed since it was kept")
    if len(files) == 2 and len(bad) == 1:
        bad = [f"{bad[0]}; its other copy is whole, and the next list kept writes it again"]
    rep.problems += bad


def _watched(rep: Report, lists: tuple[pricelists.PriceList, ...]) -> None:
    """A store's lists kept since 2026-09-29: each file as kept, each list made before its
    fetch; and each list's usual gap, and whether it's late."""
    named = {plist.name: plist for plist in lists}
    for entry in _kept_entries(rep, lists[0].store):
        plist = named.get(entry.get("list", ""))
        made, got = runs.parse(entry.get("made", "")), runs.parse(entry.get("at", ""))
        if plist is None or made is None or got is None or "file" not in entry:
            rep.problems.append(f"{_rel(watching.log_path(lists[0].store))}: an entry it can't read: {entry}")
            continue
        rep.files += 1
        rep.days.add(pricelists.day_of(made, plist).isoformat())
        rep.tally(pricelists.day_of(made, plist).isoformat())
        _hashed(rep, entry)
        if made - got > SLACK:
            rep.problems.append(
                f"{entry['file']}: made {times.shown(made)}, after it was fetched at {times.shown(got)}: "
                f"{rep.source}'s clock isn't {plist.zone_name}"
            )
    for plist in lists:
        _usual_gap(rep, plist)


def _usual_gap(rep: Report, plist: pricelists.PriceList) -> None:
    """How often a list has come lately, and whether it's late (riffle.lateness), from its own stamps."""
    folder = pricelists.lists_dir(plist)
    made = sorted(filter(None, map(runs.parse, runs.kept(folder))))
    if len(made) < 2:
        return
    n = len(runs.runs(folder))
    said = f"{plist.name}: {len(made):,} kept in {n:,} run{'s' * (n != 1)}"
    verdict = lateness.judge(made, times.now())
    if verdict is None:
        gaps = len(made) - 1
        rep.notes.append(f"{said}; lateness judged from {lateness.LEAST_GAPS} gaps, {gaps} so far")
        return
    said += f", one every {elapsed(verdict.usual.total_seconds())} lately"
    if verdict.margin > lateness.MARGIN:
        said += f", its margin {verdict.margin:.2f} from its own gaps"
    if verdict.late:
        bar = elapsed((verdict.longest * verdict.margin).total_seconds())
        ago = elapsed(verdict.since.total_seconds())
        said += (
            f"; late: the last was made {times.shown(made[-1])}, {ago} ago, "
            f"past {verdict.margin:.2f} × its longest gap in 30 days ({bar})"
        )
    rep.notes.append(said)


def cardmarket_guides() -> Report:
    """Each guide and product list under its createdAt day, made before its fetch."""
    rep = Report("Cardmarket", "list")
    for path in sorted(cardmarket.daily_dir().glob("*/*.json.gz")):
        rep.files += 1
        rep.days.add(path.parent.name)
        rep.tally(path.parent.name)
        made = cardmarket.stamp(path)
        if made is None:
            rep.problems.append(f"{_rel(path)}: no readable createdAt")
            continue
        if made.date().isoformat() != path.parent.name:
            rep.problems.append(f"{_rel(path)}: made on {made.date()}, kept under {path.parent.name}")
        _made_late(rep, path, made, pricelists.fetched(path))
    _logged(rep, cardmarket.STORE, lambda entry, made: made.date())
    _lateness(rep, {game: cardmarket.made(game) for game in cardmarket.games()}, "guide")
    products = {
        f"{game} {kind}": watching.made(cardmarket.products_dir(game, kind))
        for game in cardmarket.games()
        for kind in cardmarket.KINDS
    }
    _lateness(rep, {name: kept for name, kept in products.items() if kept}, "product list")
    return rep


def _logged(rep: Report, store: str, day: Callable[[dict, datetime], date | None]) -> None:
    """Each list a store's log says it kept: its files as kept, made before its fetch, and its
    day, which day gives from the entry and when the list was made."""
    for entry in _kept_entries(rep, store):
        made, got = runs.parse(entry.get("made", "")), runs.parse(entry.get("at", ""))
        when = None if made is None else day(entry, made)
        if made is None or got is None or when is None or "file" not in entry:
            rep.problems.append(f"{_rel(watching.log_path(store))}: an entry it can't read: {entry}")
            continue
        rep.files += 1
        rep.days.add(when.isoformat())
        rep.tally(when.isoformat())
        _hashed(rep, entry)
        _made_late(rep, data_dir() / entry["file"], made, got)


def _lists(rep: Report, store: str, folders: dict[str, Path]) -> None:
    """A store watched for its lists (riffle.watching.many), each kept in its folder: each list
    kept under the day its log names, the files set aside, and whether each list is late."""
    _logged(rep, store, lambda entry, made: _day(str(entry.get("day", ""))))
    aside = [path for path in (data_dir() / store / "aside").glob("*") if path.is_file()]
    if aside:
        rep.notes.append(f"{len(aside)} set aside in {store}/aside, not kept as any list")
    made = {name: watching.made(folder) for name, folder in folders.items()}
    _lateness(rep, {name: kept for name, kept in made.items() if kept}, "list")


def _lateness(rep: Report, lists: dict[str, list[datetime]], what: str) -> None:
    """Whether each list is late by its own margin (riffle.lateness), from when it was made: a
    note for each one late, and one for the rest."""
    waiting, judged = [], []
    for name, made in lists.items():
        if len(made) < 2:
            continue
        verdict = lateness.judge(made, times.now())
        if verdict is None:
            waiting.append(len(made) - 1)
            continue
        judged.append(verdict)
        rep.late |= verdict.late
        if verdict.late:
            bar = elapsed((verdict.longest * verdict.margin).total_seconds())
            ago = elapsed(verdict.since.total_seconds())
            who = f"{name}: " if len(lists) > 1 else ""
            rep.notes.append(
                f"{who}late: the last was made {times.shown(made[-1])}, {ago} ago, "
                f"past {verdict.margin:.2f} × its longest gap in 30 days ({bar})"
            )
    if waiting:
        fewest = f"{min(waiting)} so far" + (" at the fewest" if len(waiting) > 1 else "")
        many = f"{len(waiting)} {what}{'s' * (len(waiting) != 1)}: " if len(lists) > 1 else ""
        rep.notes.append(f"{many}lateness judged from {lateness.LEAST_GAPS} gaps, {fewest}")
    if len(lists) > 1 and judged:
        rep.notes.append(f"{len(judged)} {what}{'s' * (len(judged) != 1)} judged for lateness")
    elif judged and not judged[0].late:
        rep.notes.append(f"one every {elapsed(judged[0].usual.total_seconds())} lately")


def _mtgjson_day(path: Path) -> date | None:
    try:
        with lzma.open(path) as f:
            found = MTGJSON_DATE.search(f.read(512))
    except (OSError, EOFError, lzma.LZMAError):
        return None
    return _day(found.group(1).decode()) if found else None


def mtgjson_files() -> Report:
    """Each file named by its meta's date, from no later than its fetch."""
    rep = Report("MTGJSON", "file")
    for folder in (mtgjson.daily_dir(), mtgjson.history_dir()):
        for path in sorted(folder.glob("*.json.xz")):
            rep.files += 1
            named, inside = path.name.removesuffix(".json.xz"), _mtgjson_day(path)
            rep.tally(named)
            if inside is None:
                rep.problems.append(f"{_rel(path)}: no readable meta date")
            elif inside.isoformat() != named:
                rep.problems.append(f"{_rel(path)}: its meta says {inside}")
            elif inside > _file_time(path).date():
                rep.problems.append(
                    f"{_rel(path)}: dated after it was fetched, {times.shown(_file_time(path))}"
                )
    _lists(rep, mtgjson.STORE, {file.list: mtgjson.lists_dir(file.list) for file in mtgjson.FILES})
    return rep


def goatbots_days() -> Report:
    """Each day's zip holds that day's price file, from no later than its fetch."""
    rep = Report("GoatBots", "day")
    for path in sorted(goatbots.daily_dir().glob("*.zip")):
        rep.files += 1
        named = path.name.removesuffix(".zip")
        rep.tally(named)
        try:
            with zipfile.ZipFile(path) as zf:
                days = goatbots.price_days(zf)
        except goatbots.DAMAGED:
            rep.problems.append(f"{_rel(path)}: not a zip")
            continue
        newest = max(days) if days else None
        if newest is None or newest.isoformat() != named:
            rep.problems.append(f"{_rel(path)}: holds {newest or 'no price file'}")
        elif newest > _file_time(path).date() + timedelta(days=1):  # GoatBots' day is Central European
            rep.problems.append(f"{_rel(path)}: dated after it was fetched, {times.shown(_file_time(path))}")
    for path in sorted(goatbots.yearly_dir().glob("[0-9][0-9][0-9][0-9].zip")):
        year = int(path.stem)
        try:
            with zipfile.ZipFile(path) as zf:
                last = max((day for day in goatbots.price_days(zf) if day.year == year), default=None)
        except goatbots.DAMAGED:
            rep.problems.append(f"{_rel(path)}: not a zip")
            continue
        if last != date(year, 12, 31):
            rep.problems.append(f"{_rel(path)}: runs to {last or 'no day of the year'}, short of Dec 31")
    _lists(rep, goatbots.STORE, {name: goatbots.lists_dir(name) for name in goatbots.LISTS})
    return rep


def _set_times(path: Path) -> list[tuple[int, datetime]]:
    """Each set in a tcgcsv game's day (kept gzipped, in its runs as kept.json says, or being
    fetched) with its Last-Modified, if it has one."""
    if path.name == tcgcsv.KEPT:
        body = tcgcsv.kept_game(path.parent) or b""
    else:
        with gzip.open(path, "rb") if path.suffix == ".gz" else path.open("rb") as f:
            body = f.read()
    return [(int(m[1]), datetime.fromisoformat(m[2].decode())) for m in TCGCSV_SET.finditer(body)]


def _tcgcsv_game(rep: Report, game: Path, stamp: datetime | None, unfinished: list[str]) -> None:
    """A game's sets, each from no later refresh than its day's; whether it's finished; and its
    products kept that day, as kept."""
    day = date.fromisoformat(game.parent.name)
    kept, part, missing = (game / name for name in (tcgcsv.PRICES, tcgcsv.PART, tcgcsv.MISSING))
    record = game / tcgcsv.KEPT
    days = [kept, part]
    if record.exists():
        try:
            _hashed(rep, json.loads(record.read_text(encoding="utf-8")))
            days.append(record)
        except (ValueError, KeyError, TypeError):
            rep.problems.append(f"{_rel(record)}: unreadable")
    products = game / tcgcsv.PRODUCTS_KEPT
    if products.exists():
        try:
            _hashed(rep, json.loads(products.read_text(encoding="utf-8")))
        except (ValueError, KeyError, TypeError):
            rep.problems.append(f"{_rel(products)}: unreadable")
    for path in days:
        if stamp is None or not path.exists():
            continue
        try:
            late = [
                (gid, when)
                for gid, when in _set_times(path)
                if when - stamp > tcgcsv.NEXT_REFRESH and when.astimezone(UTC).date() > day
            ]
        except (OSError, EOFError, ValueError, KeyError, TypeError, zstd.ZstdError) as e:
            rep.problems.append(f"{_rel(path)}: unreadable ({e})")
            continue
        if late:
            gid, when = late[0]
            more = f", and {len(late) - 1} more" if len(late) > 1 else ""
            rep.problems.append(f"{_rel(path)}: set {gid} is from a later refresh, {times.shown(when)}{more}")
    if tcgcsv.finished(game):
        rep.tally(day.isoformat(), "prices")
    if products.exists():
        rep.tally(day.isoformat(), "products")
    if part.exists():
        unfinished.append(f"{day} {game.name} (being fetched)")
    elif missing.exists():
        never = missing.read_text(encoding="utf-8").split()
        count = f"{len(never):,} sets never fetched" if all(n.isdigit() for n in never) else "which unknown"
        unfinished.append(f"{day} {game.name} ({count})")
    elif not tcgcsv.finished(game):
        unfinished.append(f"{day} {game.name} (no prices kept)")


def tcgcsv_days() -> Report:
    """Each day's last-updated.txt says that day, from before it was fetched; no set under the
    day from a later refresh; and the games not finished, noted."""
    rep = Report("tcgcsv", "day")
    root = tcgcsv.daily_dir()
    unfinished: list[str] = []
    for folder in sorted(root.iterdir() if root.is_dir() else []):
        if not folder.is_dir() or _day(folder.name) is None:
            continue
        rep.files += 1
        stamp = folder / "last-updated.txt"
        made: datetime | None = None
        try:
            made = datetime.strptime(stamp.read_text().strip(), "%Y-%m-%dT%H:%M:%S%z")
        except (OSError, ValueError):
            rep.problems.append(f"{_rel(stamp)}: missing or unreadable")
        if made is not None:
            if made.astimezone(UTC).date().isoformat() != folder.name:
                rep.problems.append(f"{_rel(stamp)}: says {made.astimezone(UTC).date()}")
            _made_late(rep, stamp, made, _file_time(stamp))
        for game in sorted(p for p in folder.iterdir() if p.is_dir()):
            _tcgcsv_game(rep, game, made, unfinished)
    if unfinished:
        more = f", and {len(unfinished) - SHOWN:,} more" if len(unfinished) > SHOWN else ""
        games = f"{len(unfinished):,} game{'s' * (len(unfinished) != 1)}"
        rep.notes.append(f"{games} unfinished: {', '.join(unfinished[:SHOWN])}{more}")
    _lateness(rep, {"days": tcgcsv.made()}, "list")
    return rep


def scryfall_days() -> Report:
    """No day's prices kept before that day began (UTC); whether each bulk type is late."""
    rep = Report("Scryfall", "day")
    for path in sorted(scryfall.prices_dir().glob("*.jsonl.gz")):
        rep.files += 1
        day = _day(path.name.removesuffix(".jsonl.gz"))
        got = pricelists.fetched(path)
        if day is not None:
            rep.tally(day.isoformat())
        if day is None:
            rep.problems.append(f"{_rel(path)}: not named by a day")
        elif got is None:
            rep.problems.append(f"{_rel(path)}: no time it was kept")
        elif got.date() < day:
            rep.problems.append(f"{_rel(path)}: kept {times.shown(got)}, before its day began")
    published = {kind.replace("_", " "): scryfall.publishes(kind) for kind in scryfall.kinds()}
    for made in (m for kept in published.values() for m in kept):
        rep.tally(made.astimezone(UTC).date().isoformat())
    _lateness(rep, {kind: made for kind, made in published.items() if made}, "file")
    return rep


# Each source's check, by the name its Report gives it, so one that can't run can be named.
CHECKS: dict[str, Callable[[], Report]] = {
    "Card Kingdom": lambda: store(pricelists.CARD_KINGDOM),
    "Mana Pool": lambda: store(pricelists.MANA_POOL),
    "Cardmarket": cardmarket_guides,
    "MTGJSON": mtgjson_files,
    "GoatBots": goatbots_days,
    "tcgcsv": tcgcsv_days,
    "Scryfall": scryfall_days,
}


def run() -> list[Report]:
    return [check() for check in CHECKS.values()]
