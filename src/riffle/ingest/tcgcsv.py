"""Daily TCGplayer prices from tcgcsv.com: each game's prices every day tcgcsv publishes.

tcgcsv mirrors TCGplayer's catalog and prices once a day and serves them as
plain JSON, no key needed:

    https://tcgcsv.com/last-updated.txt                     when today's data was published
    https://tcgcsv.com/tcgplayer/categories                 every game, with its categoryId
    https://tcgcsv.com/tcgplayer/{categoryId}/groups        a game's sets ("groups")
    https://tcgcsv.com/tcgplayer/{categoryId}/{groupId}/prices
    https://tcgcsv.com/tcgplayer/{categoryId}/{groupId}/products  the set's cards: names, numbers,
                                                                  and extendedData (text, rarity, ...)

It used to publish a daily archive of every price file at once. That was taken
down in September 2026 (server costs; the maintainer is waiting on TCGplayer
for terms), with the request that clients fetch the price files one at a time
and never the same file twice in a day. So Riffle keeps its own history: each
day tcgcsv publishes, for everything it carries but comics, it fetches every
group's ("set's") price file and keeps the responses as returned:

    <data_dir>/tcgcsv/daily/<day>/last-updated.txt          tcgcsv's stamp for the day
    <data_dir>/tcgcsv/daily/<day>/categories.json           the category list that day
    <data_dir>/tcgcsv/daily/<day>/<game>/groups.json        the groups response
    <data_dir>/tcgcsv/daily/<day>/<game>/prices.jsonl.part  while the game is being fetched
    <data_dir>/tcgcsv/daily/<day>/<game>/kept.json          the game, finished: its list's
                                                            entry, as a watch log's
    <data_dir>/tcgcsv/daily/<day>/<game>/missing.txt        the sets never fetched, if any
    <data_dir>/tcgcsv/lists/<game>/<base's stamp>/...       each finished day's list (riffle.runs)

A game's list is one line per set, sorted by set:

    {"groupId": ..., "fetched": <UTC>, "lastModified": <UTC>, "response": <the price file>}

kept by the run rule under the day's stamp: the run's first whole, twice, and
each later day as a difference against it. Until 2026-09-29 a finished game was
<day>/<game>/prices.jsonl.gz; those stay.

<day> is the date from last-updated.txt. `riffle watch tcgcsv` asks for it when
riffle.cadence says, learned from tcgcsv's own stamps and checks, and fetches a
new day when it comes: each set is appended to the game's part file as it
arrives, and a set that fails is asked for by the next run, which asks for
nothing already kept. A game with every set is kept; one a day left unfinished
is kept as it stood at the next day's run, with missing.txt. Every check and
every day fetched goes in <data_dir>/tcgcsv/watch/<month>.jsonl. Each set goes under the day its own
Last-Modified says: one from the next refresh moves the run on to that day.
Days kept before resuming came in have no times on their lines. The games in
GAMES are named by their tcgcsv category and resolved to IDs at run time; every
other category is kept too, named by its slug (pokemon-japan), except the
comics SKIPPED lists. Nothing here reads the files back: the price loader
(v0.4.0) does. Headers, retries, and 429 handling: riffle.net.

Each set's products are the catalog that names a price's productId, for every game tcgcsv
carries, and they're asked after the day's prices, by the same runs:

    <data_dir>/tcgcsv/daily/<day>/<game>/products.jsonl.part  the sets asked that day
    <data_dir>/tcgcsv/daily/<day>/<game>/products.json        the game's products, kept: its
                                                              list's entry, as kept.json
    <data_dir>/tcgcsv/products/<game>/<base's stamp>/...      each day's whole list (riffle.runs)

A set's products are asked when the set is new, when its modifiedOn on the day's set list
isn't the one they were asked under, or when they weren't asked since the day before's
publish: every set every two days. tcgcsv writes a set's products file only when it changes,
and the set's modifiedOn is its newest product's (checked 2026-10-03), so the third is the
net under the second: each run counts the sets it caught. New sets go first, then changed
ones, then the longest unasked, across every game. A game's list is one line per set on the
day's set list, sorted by set, each the latest asked:

    {"groupId": ..., "fetched": <UTC>, "lastModified": <UTC>, "modifiedOn": ..., "response": ...}

kept under the day's stamp once none of its sets is due; one a day left unfinished is kept
as it stood at the next day's run. A set gone from the set list is in the lists before.

Every request is counted under its UTC day in <data_dir>/tcgcsv/requests.json, by every run,
and no day asks for more than DAILY_REQUESTS (C13). Prices come first: products ask only
what the day leaves, and while the day's prices are still to come, they leave room for them.
"""

import gzip
import hashlib
import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from riffle import cadence, lateness, net, runs, times, watching
from riffle.config import data_dir
from riffle.progress import SILENT, Step, Tracker
from riffle.runs import zstd

BASE = "https://tcgcsv.com"
STORE = "tcgcsv"
# Game code -> the game's category name on tcgcsv; IDs are looked up at run time. These are the
# games played; every other category is kept too, named by its slug. Each game is its own step.
GAMES = {"mtg": "Magic", "fab": "Flesh & Blood TCG", "op": "One Piece Card Game"}
# The only categories not kept, by ID: Marvel and DC comics alone are 19,800 groups, twice
# tcgcsv's daily request limit. Card games, miniatures, board games, and supplies all are.
SKIPPED = {69: "Marvel Comics", 70: "DC Comics"}
DAILY_REQUESTS = 9_000  # tcgcsv asks for under 10,000 a day; games past this wait for the next day
OVER_BUDGET = "past the day's request budget; the next day's run gets it"
NO_ANSWER = "not asked: tcgcsv gave no answer"
NEXT_REFRESH = timedelta(hours=1)  # a file this far past its day's stamp came from the next refresh
PRICES, PART, MISSING = "prices.jsonl.gz", "prices.jsonl.part", "missing.txt"  # PRICES: before 0033
KEPT = "kept.json"
CHECKED, DAY, PRODUCTS = "last-updated", "day", "products"  # the watch log's kinds of entry
PRODUCTS_PART, PRODUCTS_KEPT = "products.jsonl.part", "products.json"
REQUESTS, OTHER = "requests.json", "other"  # each UTC day's requests: products, and the rest
# A set's products not asked since the publish before its day's are asked again: every set every
# two days, decided 2026-09-29 to keep a day near 7,300 requests (every day would take about 9,600,
# past what tcgcsv's maintainer has OK'd). Not learned yet: each run counts the sets it re-asked
# by age that had changed, the number that would lengthen it.
AGE = timedelta(days=1)
STAMP = "%Y-%m-%dT%H:%M:%S%z"  # last-updated.txt: 2026-09-24T20:05:50+0000
SET_LINE = re.compile(rb'^\{"groupId": (\d+),', re.MULTILINE)
PRODUCT_LINE = re.compile(
    rb'^\{"groupId": (\d+), "fetched": "([^"]*)", "lastModified": (?:null|"[^"]*"), '
    rb'"modifiedOn": (null|"(?:[^"\\]|\\.)*"), "response": '
)

Fetch = Callable[[str], net.Reply | None]  # url -> body and headers, or None for 404


@dataclass
class Snapshot:
    day: date  # tcgcsv's day when the run began
    fetched: list[str] = field(default_factory=list)  # games finished this run
    skipped: list[str] = field(default_factory=list)  # games already finished for the day
    failed: list[tuple[str, str]] = field(default_factory=list)  # (game, why)
    empty: list[str] = field(default_factory=list)  # games tcgcsv lists but has no set list for
    groups: dict[str, int] = field(default_factory=dict)  # game -> sets in its finished file
    requests: int = 0  # every request this run but the products'
    refreshed: date | None = None  # a day tcgcsv published mid-run, which the rest of the run went under
    products: "Products | None" = None  # the products step, when it ran


@dataclass
class Products:
    """A run's products step: the sets asked, by why each was due."""

    made: str  # the stamp of the day they went under
    due: int = 0  # sets due, not asked yet that day
    asked: int = 0  # requests
    new: int = 0  # kept: never asked before
    modified: int = 0  # kept: with a modifiedOn they weren't asked under
    aged: int = 0  # kept: not asked since the day before's publish
    changed: int = 0  # of the aged, those whose products had changed all the same
    owed: int = 0  # due, past what the day's requests leave
    failed: list[tuple[str, int, str]] = field(default_factory=list)  # (game, set, why): asked the next day
    silent: str = ""  # why tcgcsv gave no answer, which stopped the step
    kept: list[str] = field(default_factory=list)  # games whose day's list was kept
    unkept: list[tuple[str, str]] = field(default_factory=list)  # (game, why) a list couldn't be kept


@dataclass
class Watched:
    busy: bool = False  # another run held tcgcsv: nothing asked
    asked: bool = False  # last-updated.txt was asked for
    snap: Snapshot | None = None  # the day fetched, when one was
    plan: cadence.Plan | None = None  # when it's asked next, when it wasn't due


@dataclass
class _Run:
    fetch: Fetch
    delay: float
    tracker: Tracker
    snap: Snapshot
    day: date  # the day sets go under
    stamp: datetime  # tcgcsv's stamp for that day
    clock: Callable[[], datetime]
    silent: bool = False  # tcgcsv gave no answer: the rest of its games aren't asked

    def spent(self) -> int:
        """Requests asked of tcgcsv today (UTC), by every run."""
        return sum(requests_on(self.clock().astimezone(UTC).date()).values())


@dataclass
class _Tally:
    """One game's sets for one day."""

    listed: int  # on the day's set list
    had: int  # of those, kept before this run
    kept: int = 0  # kept this run
    failed: list[tuple[int, str]] = field(default_factory=list)  # (set, why): asked again next run
    silent: str = ""  # why tcgcsv gave no answer, which stopped the game
    unstamped: int = 0  # kept with no Last-Modified, so under the run's day


def _get(url: str) -> net.Reply | None:
    return net.get_reply(url, accept="application/json")


def daily_dir() -> Path:
    return data_dir() / "tcgcsv" / "daily"


def day_dir(day: date, game: str) -> Path:
    return daily_dir() / day.isoformat() / game


def lists_dir(game: str) -> Path:
    return data_dir() / STORE / "lists" / game


def products_dir(game: str) -> Path:
    return data_dir() / STORE / PRODUCTS / game


def requests_on(day: date) -> dict[str, int]:
    """The requests asked of tcgcsv on a UTC day by every run, by kind: products, and other."""
    return _requests().get(day.isoformat(), {})


def _requests() -> dict[str, dict[str, int]]:
    """requests.json: each UTC day's requests by kind. One that can't be read counts none."""
    try:
        found = json.loads((data_dir() / STORE / REQUESTS).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(found, dict):
        return {}
    return {
        day: {kind: n for kind, n in kinds.items() if isinstance(n, int)}
        for day, kinds in found.items()
        if isinstance(kinds, dict)
    }


def _counted(fetch: Fetch, clock: Callable[[], datetime]) -> Fetch:
    """fetch, each request first counted under its UTC day in requests.json, so every run of
    the day sees it (C13)."""

    def ask(url: str) -> net.Reply | None:
        found = _requests()
        day = found.setdefault(clock().astimezone(UTC).date().isoformat(), {})
        kind = PRODUCTS if url.endswith("/products") else OTHER
        day[kind] = day.get(kind, 0) + 1
        days = ",\n".join(f"{json.dumps(d)}: {json.dumps(found[d], sort_keys=True)}" for d in sorted(found))
        _write(data_dir() / STORE / REQUESTS, f"{{\n{days}\n}}\n".encode())
        return fetch(url)

    return ask


def finished(game_dir: Path) -> bool:
    """Whether a game's day is finished: kept in its runs, or gzipped before 0033."""
    return (game_dir / KEPT).exists() or (game_dir / PRICES).exists()


def kept_game(game_dir: Path) -> bytes | None:
    """A finished game's day as kept, one line per set; None if it isn't finished."""
    record = game_dir / KEPT
    if record.exists():
        return runs.rebuild(data_dir() / json.loads(record.read_text(encoding="utf-8"))["file"])
    old = game_dir / PRICES
    return gzip.decompress(old.read_bytes()) if old.exists() else None


def stored_days(games: dict[str, str] = GAMES) -> list[date]:
    """Days with every game finished, oldest first."""
    if not daily_dir().exists():
        return []
    days = []
    for entry in daily_dir().iterdir():
        day = _date(entry.name)
        if day is not None and all(finished(entry / game) for game in games):
            days.append(day)
    return sorted(days)


def made() -> list[datetime]:
    """When tcgcsv published each day kept, from its last-updated.txt, oldest first."""
    found = []
    for path in daily_dir().glob("*/last-updated.txt") if daily_dir().is_dir() else []:
        try:
            found.append(datetime.strptime(path.read_text().strip(), STAMP).astimezone(UTC))
        except (OSError, ValueError):
            continue
    return sorted(found)


def _date(name: str) -> date | None:
    try:
        return date.fromisoformat(name)
    except ValueError:
        return None


def last_updated(fetch: Fetch = _get) -> datetime:
    """When tcgcsv last refreshed its data, e.g. 2026-09-24T20:05:50+0000."""
    reply = fetch(f"{BASE}/last-updated.txt")
    if reply is None:
        raise net.FetchError("HTTP 404")
    text = reply.body.decode(errors="replace").strip()
    try:
        return datetime.strptime(text, STAMP)
    except ValueError as e:
        raise _broken("last-updated.txt", reply, f"unexpected last-updated.txt: {text[:40]!r}") from e


def _broken(served: str, reply: net.Reply, why: str) -> watching.Broken:
    """A file tcgcsv served whole that isn't what it should be, set aside once a publish
    (riffle.watching.broken_logged, logged under served): named by its Last-Modified, else its
    ETag."""
    modified = net.http_time(reply.headers.get("last-modified"))
    publish = runs.name(modified) if modified else reply.headers.get("etag")
    named = served.replace("/", "-")  # mtg/23.json is set aside as mtg-23-<UTC time>.json
    fresh = data_dir() / STORE / f"{named}.new"
    fresh.parent.mkdir(parents=True, exist_ok=True)
    fresh.write_bytes(reply.body)
    try:
        return watching.broken_logged(STORE, served, publish, fresh, named, times.now(), why)
    finally:
        fresh.unlink(missing_ok=True)


def _parse(body: bytes, what: str) -> dict:
    """The response as a dict with a "results" list, or a FetchError naming what came back instead."""
    try:
        doc = json.loads(body)
        if not isinstance(doc, dict) or not isinstance(doc["results"], list):
            raise KeyError("results")
    except (ValueError, KeyError, TypeError) as e:
        raise net.FetchError(f"{what}: not the expected JSON" if what else "not the expected JSON") from e
    return doc


def _results(body: bytes, what: str) -> list[dict]:
    return _parse(body, what)["results"]


def _set_ids(body: bytes) -> list[int]:
    """A groups response's set IDs, sorted."""
    try:
        return sorted(int(g["groupId"]) for g in _results(body, "groups"))
    except (KeyError, TypeError, ValueError) as e:
        raise net.FetchError("groups: a group without a whole-number groupId") from e


def _one_line(body: bytes) -> str:
    """A price file verbatim if it's one line, else compacted: the file is one JSON object per line."""
    doc = _parse(body, "")  # json.loads also reads UTF-16 and UTF-32; the file is UTF-8
    try:
        text = body.decode("utf-8").strip()
    except UnicodeDecodeError as e:
        raise net.FetchError("not UTF-8") from e
    return text if "\n" not in text and "\r" not in text else json.dumps(doc, separators=(",", ":"))


def _utc(t: datetime) -> str:
    return t.astimezone(UTC).isoformat(timespec="seconds")


def resolve(cats: list[dict], games: dict[str, str] = GAMES) -> dict[str, int]:
    """Game code -> tcgcsv categoryId, matched by name (case-insensitive). Unknown games are left out."""
    by_name = {str(c.get("name", "")).casefold(): int(c["categoryId"]) for c in cats if "categoryId" in c}
    return {code: by_name[name.casefold()] for code, name in games.items() if name.casefold() in by_name}


def kept(cats: list[dict]) -> dict[str, int]:
    """Game code -> categoryId for every category in tcgcsv's list but SKIPPED, each code
    the category's name as a slug."""
    return {
        watching.slug(str(c.get("name", ""))) or str(c["categoryId"]): int(c["categoryId"])
        for c in cats
        if "categoryId" in c and int(c["categoryId"]) not in SKIPPED
    }


def _count(n: int, noun: str) -> str:
    return f"{n:,} {noun}" if n == 1 else f"{n:,} {noun}s"


def _write(dest: Path, body: bytes) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    tmp.write_bytes(body)
    tmp.replace(dest)


def _keep_stamp(stamp: datetime) -> None:
    """A day's last-updated.txt, kept the first time."""
    path = daily_dir() / stamp.date().isoformat() / "last-updated.txt"
    if not path.exists():
        _write(path, stamp.strftime(STAMP).encode())


def _categories(snap: Snapshot, fetch: Fetch) -> list[dict]:
    """The day's category list, fetched once a day and kept beside that day's price files."""
    path = daily_dir() / snap.day.isoformat() / "categories.json"
    if path.exists():
        return _results(path.read_bytes(), "categories")
    reply = fetch(f"{BASE}/tcgplayer/categories")
    snap.requests += 1
    if reply is None:
        raise net.FetchError("categories: HTTP 404")
    try:
        cats = _results(reply.body, "categories")
    except net.FetchError as e:
        raise _broken("categories.json", reply, str(e)) from e
    _write(path, reply.body)
    return cats


def _groups(category: int, game_dir: Path, run: _Run) -> list[int] | None:
    """A game's set IDs for the day, sorted, from its groups.json: fetched and kept the first
    time, read back after. None when tcgcsv has no set list for the category (404)."""
    path = game_dir / "groups.json"
    if path.exists():
        return _set_ids(path.read_bytes())
    time.sleep(run.delay)
    run.snap.requests += 1
    reply = run.fetch(f"{BASE}/tcgplayer/{category}/groups")
    if reply is None:
        return None
    try:
        group_ids = _set_ids(reply.body)
    except net.FetchError as e:
        raise _broken(f"{game_dir.name}/groups.json", reply, str(e)) from e
    _write(path, reply.body)
    return group_ids


def _kept_sets(part: Path) -> set[int]:
    """The sets a part file holds. A last line cut off mid-write is dropped first, so the
    next set appended starts a line of its own."""
    try:
        text = part.read_bytes()
    except FileNotFoundError:
        return set()
    whole = text[: text.rfind(b"\n") + 1]
    if len(whole) < len(text):
        with part.open("r+b") as f:
            f.truncate(len(whole))
    return {int(found.group(1)) for found in SET_LINE.finditer(whole)}


def _append(
    part: Path, gid: int, fetched: datetime, modified: datetime | None, response: str, on: str | None = None
) -> None:
    """One set's line: its ID, when it was fetched and last modified (UTC), then the file. A
    products line also has the set's modifiedOn it was asked under, as JSON (on)."""
    at, mod = json.dumps(_utc(fetched)), json.dumps(modified and _utc(modified))
    more = "" if on is None else f'"modifiedOn": {on}, '
    part.parent.mkdir(parents=True, exist_ok=True)
    with part.open("a", encoding="utf-8") as f:
        f.write(
            f'{{"groupId": {gid}, "fetched": {at}, "lastModified": {mod}, {more}"response": {response}}}\n'
        )


def _finish(game_dir: Path, stamp: datetime) -> tuple[list[int] | None, runs.Kept]:
    """A game's part file kept in its runs under the day's stamp, one line per set sorted by
    set, with missing.txt naming the day's sets not in it and kept.json saying where it's kept.
    Those sets, or None when the day's set list wasn't kept to tell; and how it was kept."""
    part = game_dir / PART
    text = part.read_bytes() if part.exists() else b""  # no part: a set list with no sets
    lines: dict[int, bytes] = {}
    for line in text[: text.rfind(b"\n") + 1].splitlines():
        found = SET_LINE.match(line)
        if found:
            lines.setdefault(int(found.group(1)), line)
    try:
        missing: list[int] | None = [
            g for g in _set_ids((game_dir / "groups.json").read_bytes()) if g not in lines
        ]
    except (OSError, net.FetchError):
        missing = None
    if missing is None:
        _write(game_dir / MISSING, b"unknown: the day's set list wasn't kept\n")
    elif missing:
        _write(game_dir / MISSING, "".join(f"{g}\n" for g in missing).encode())
    data = b"".join(lines[gid] + b"\n" for gid in sorted(lines))
    kept = runs.keep(lists_dir(game_dir.name), stamp, data)
    record = watching.record(kept) | {"day": game_dir.parent.name, "sets": len(lines)}
    _write(game_dir / KEPT, json.dumps(record).encode())
    part.unlink(missing_ok=True)
    return missing, kept


def _day_stamp(folder: Path, day: date) -> datetime:
    """A day's stamp from its last-updated.txt; midnight UTC when it has none to read."""
    try:
        return datetime.strptime((folder / "last-updated.txt").read_text().strip(), STAMP)
    except (OSError, ValueError):
        return datetime.combine(day, datetime.min.time(), UTC)


def _finish_days(run: _Run) -> None:
    """Every game a day before the run's left part-way, finished as it stood: tcgcsv has
    moved on, so the sets it lacks can't be fetched now. A day with a game short warns."""
    root = daily_dir()
    for folder in sorted(root.iterdir()) if root.is_dir() else []:
        day = _date(folder.name)
        if day is None or day >= run.day:
            continue
        games = sorted(p.parent for p in folder.glob(f"*/{PART}") if not finished(p.parent))
        if not games:
            continue
        step = run.tracker.step(f"tcgcsv {day}")
        short = []
        try:
            for game_dir in games:
                missing, _ = _finish(game_dir, _day_stamp(folder, day))
                if missing is None:
                    short.append(f"{game_dir.name}, which unknown")
                elif missing:
                    short.append(f"{game_dir.name} {len(missing):,}")
        except OSError as e:
            step.fail(str(e))
            continue
        if short:
            step.warn(f"finished as it stood; sets never fetched, named in missing.txt: {', '.join(short)}")
        else:
            step.ok(f"finished {_count(len(games), 'game')}")


def _refresh(run: _Run, modified: datetime) -> None:
    """A set from the next refresh: tcgcsv has published modified's day, and the run moves on
    to it. last-updated.txt is read again for that day's stamp; tcgcsv writes it last, so
    until it says the new day, the set's own time stands in."""
    run.day, run.stamp = modified.date(), modified
    run.snap.refreshed = run.day
    time.sleep(run.delay)
    run.snap.requests += 1
    try:
        stamp: datetime | None = last_updated(run.fetch)
    except net.FetchError:
        stamp = None
    if stamp is not None and stamp.date() == run.day:
        run.stamp = stamp
        _keep_stamp(stamp)


def _sets(game: str, category: int, group_ids: list[int], run: _Run, step: Step) -> _Tally:
    """The day's sets not yet kept, each appended to the game's part file as it arrives. A set
    that fails isn't written (one served broken is set aside once a publish, _broken), and no
    answer at all stops the game. A set from the next
    refresh goes under its own day, and ends this one: the run moves on to that day."""
    day = run.day
    have = _kept_sets(day_dir(day, game) / PART)
    todo = [g for g in group_ids if g not in have]
    if run.spent() + len(todo) > DAILY_REQUESTS:
        raise net.FetchError(OVER_BUDGET)
    tally = _Tally(len(group_ids), len(group_ids) - len(todo))
    step.update(tally.had, tally.listed)
    for gid in todo:
        time.sleep(run.delay)
        run.snap.requests += 1
        fetched = times.now()
        try:
            reply = run.fetch(f"{BASE}/tcgplayer/{category}/{gid}/prices")
        except net.NoAnswer as e:
            tally.silent = str(e)
            break
        except (net.FetchError, OSError) as e:
            tally.failed.append((gid, str(e)))
            continue
        try:
            response = "null" if reply is None else _one_line(reply.body)  # null: no price file (404)
        except net.FetchError as e:
            assert reply is not None  # _one_line read it
            aside = _broken(f"{game}/{gid}.json", reply, str(e))
            tally.failed.append((gid, str(aside).removesuffix(", asked again next run")))  # the line says it
            continue
        modified = None if reply is None else net.http_time(reply.headers.get("last-modified"))
        tally.unstamped += reply is not None and modified is None
        moved = modified is not None and modified - run.stamp > NEXT_REFRESH and modified.date() > day
        if moved and modified is not None:
            _refresh(run, modified)
        try:
            _append(day_dir(run.day, game) / PART, gid, fetched, modified, response)
        except OSError as e:
            tally.failed.append((gid, str(e)))
            continue
        if moved:
            break
        tally.kept += 1
        step.update(tally.had + tally.kept)
    return tally


def _game(game: str, category: int | None, name: str, run: _Run) -> None:
    """One game as its own step: its set list, then every set's price file not yet kept for
    the day. category is None when no category goes by the game's name. A category tcgcsv
    lists but has no set list for is skipped, noted on its step, unless it's one of GAMES:
    then it fails. Whatever goes wrong fails only this game, keeping what it has."""
    step = run.tracker.step(f"tcgcsv {game}", unit="sets")
    notes: list[str] = []
    try:
        if run.silent:
            raise net.FetchError(NO_ANSWER)
        if category is None:
            raise net.FetchError(f"tcgcsv has no category named {name!r}")
        while True:
            if run.spent() >= DAILY_REQUESTS:
                raise net.FetchError(OVER_BUDGET)
            day = run.day
            group_ids = _groups(category, day_dir(day, game), run)
            if group_ids is None:
                if game in GAMES:
                    raise net.FetchError("groups: HTTP 404")
                run.snap.empty.append(game)
                step.ok("no sets on tcgcsv (HTTP 404); skipped")
                return
            tally = _sets(game, category, group_ids, run, step)
            if run.day == day:
                break
            notes.append(
                f"{tally.had + tally.kept:,} of {tally.listed:,} sets kept for {day}; "
                f"tcgcsv published {run.day} mid-run, and the rest go under it"
            )
        _outcome(game, tally, notes, run, step)
    except (net.FetchError, OSError) as e:
        run.silent |= isinstance(e, net.NoAnswer)
        why = "; ".join([*notes, str(e)])
        run.snap.failed.append((game, why))
        step.fail(why)


def _outcome(game: str, tally: _Tally, notes: list[str], run: _Run, step: Step) -> None:
    """A game's step, ended: finished once it has every set, else failed saying what it lacks."""
    have = tally.had + tally.kept
    flags = [f"{tally.unstamped:,} without a Last-Modified, kept under {run.day}"] if tally.unstamped else []
    if have == tally.listed:
        _, kept = _finish(day_dir(run.day, game), run.stamp)
        run.snap.groups[game] = tally.listed
        run.snap.fetched.append(game)
        sets = _count(have, "set")
        if tally.had:
            sets = f"the last {_count(tally.kept, 'set')} of {tally.listed:,}"
        sets += f", kept as {watching.how(kept)}"
        (step.warn if notes or flags else step.ok)("; ".join([*notes, sets, *flags]))
        return
    lacks = f"{have:,} of {tally.listed:,} sets kept"
    if tally.silent:
        run.silent = True
        lacks += f"; tcgcsv gave no answer ({tally.silent}), the rest asked again next run"
    else:  # short without a failure is only ever no answer
        gid, why = tally.failed[0]
        more = f", and {_count(len(tally.failed) - 1, 'more set')}" if len(tally.failed) > 1 else ""
        lacks += f"; set {gid} failed ({why}){more}, asked again next run"
    why = "; ".join([*notes, lacks, *flags])
    run.snap.failed.append((game, why))
    step.fail(why)


@dataclass(frozen=True)
class _Line:
    """A set's line in a game's kept products: when it was asked, the modifiedOn it was asked
    under (as JSON), and its response's SHA-256."""

    fetched: datetime
    on: str
    sha256: str


@dataclass(frozen=True)
class _Due:
    """A set whose products are due."""

    game: str
    category: int
    gid: int
    why: str  # "new", "modified" or "aged"
    on: str  # its modifiedOn on the day's set list, as JSON
    last: _Line | None  # its line in the game's latest list, if it has one


def _lines(data: bytes) -> dict[int, bytes]:
    """A products list's or part file's whole lines, by set; the last for a set stands."""
    found = {}
    for line in data[: data.rfind(b"\n") + 1].splitlines():
        match = SET_LINE.match(line)
        if match:
            found[int(match.group(1))] = line
    return found


def _asked(line: bytes) -> _Line | None:
    """A products line's _Line; None for one this module didn't write."""
    found = PRODUCT_LINE.match(line)
    if found is None:
        return None
    try:
        fetched = datetime.fromisoformat(found.group(2).decode())
    except ValueError:
        return None
    return _Line(fetched, found.group(3).decode(), hashlib.sha256(line[found.end() : -1]).hexdigest())


def _latest_products(game: str) -> bytes:
    """A game's latest products list as kept; nothing before its first."""
    found = runs.kept(products_dir(game))
    return runs.rebuild(found[next(reversed(found))]) if found else b""


def _set_list(path: Path) -> dict[int, str]:
    """A game's set list for a day (its groups.json): each set's modifiedOn, as JSON."""
    try:
        return {
            int(g["groupId"]): json.dumps(
                g.get("modifiedOn") if isinstance(g.get("modifiedOn"), str) else None
            )
            for g in _results(path.read_bytes(), "groups")
        }
    except (KeyError, TypeError, ValueError, AttributeError) as e:
        raise net.FetchError("groups: a group without a whole-number groupId") from e


def _finish_products(game_dir: Path, stamp: datetime) -> tuple[runs.Kept, int]:
    """A game's products for a day, kept in its runs under the day's stamp: the latest list's
    lines with the day's over them, one for each set on the day's set list, sorted by set; and
    products.json saying where. How it was kept, and the sets on the list in no line yet."""
    part = game_dir / PRODUCTS_PART
    fresh = _lines(part.read_bytes())
    lines = _lines(_latest_products(game_dir.name)) | fresh
    try:
        listed = list(_set_list(game_dir / "groups.json"))
    except (OSError, net.FetchError):
        listed = list(lines)  # the day's set list wasn't kept: every set there's a line for
    whole = {gid: lines[gid] for gid in listed if gid in lines}
    kept = runs.keep(products_dir(game_dir.name), stamp, b"".join(whole[g] + b"\n" for g in sorted(whole)))
    missing = len(listed) - len(whole)
    record = watching.record(kept) | {
        "day": game_dir.parent.name,
        "sets": len(whole),
        "asked": len(fresh),
        "missing": missing,
    }
    _write(game_dir / PRODUCTS_KEPT, json.dumps(record).encode())
    part.unlink()
    return kept, missing


def _finish_product_days(run: _Run) -> None:
    """Every game's products an earlier day left part-way, kept as they stood: tcgcsv has moved
    on, and the sets still due are asked under the run's day."""
    root = daily_dir()
    for folder in sorted(root.iterdir()) if root.is_dir() else []:
        day = _date(folder.name)
        if day is None or day >= run.day:
            continue
        parts = sorted(folder.glob(f"*/{PRODUCTS_PART}"))
        if not parts:
            continue
        step = run.tracker.step(f"tcgcsv {day} products")
        done, short, failed = 0, [], []
        for part in parts:
            if (part.parent / PRODUCTS_KEPT).exists():
                part.unlink()  # kept already, by a run cut off before it could remove this
                continue
            try:
                _, missing = _finish_products(part.parent, _day_stamp(folder, day))
            except (OSError, zstd.ZstdError) as e:
                failed.append(f"{part.parent.name} ({e})")
                continue
            done += 1
            if missing:
                short.append(f"{part.parent.name} {missing:,}")
        said = f"kept {_count(done, 'game')} as they stood"
        if short:
            said += f"; sets never asked, asked under {run.day}: {', '.join(short)}"
        if failed:
            step.fail(f"{said}; not kept, the part file left: {', '.join(failed)}")
        else:
            (step.warn if short else step.ok)(said)


def _product_games(games: dict[str, str] | None, run: _Run) -> list[tuple[str, int]]:
    """The games whose products the run asks, in the prices' order: each with a set list for
    the run's day, and its products for the day not yet kept."""
    try:
        cats = _results((daily_dir() / run.day.isoformat() / "categories.json").read_bytes(), "categories")
        named = GAMES if games is None else games
        ids = resolve(cats, named)
        order = [(game, ids[game]) for game in named if game in ids]
        if games is None:
            order += [(game, cid) for game, cid in sorted(kept(cats).items()) if cid not in ids.values()]
    except (OSError, net.FetchError, KeyError, TypeError, ValueError):
        return []
    return [
        (game, cid)
        for game, cid in order
        if (day_dir(run.day, game) / "groups.json").exists()
        and not (day_dir(run.day, game) / PRODUCTS_KEPT).exists()
    ]


def _queue(order: list[tuple[str, int]], run: _Run, res: Products) -> list[_Due]:
    """Every set due, in the order asked: new sets, then modified, then the longest unasked,
    each first by game order. A game whose set list or products can't be read is left out."""
    new: list[_Due] = []
    modified: list[_Due] = []
    aged: list[tuple[datetime, _Due]] = []
    for game, category in order:
        game_dir = day_dir(run.day, game)
        try:
            listed = _set_list(game_dir / "groups.json")
            have = _kept_sets(game_dir / PRODUCTS_PART)
            last = {gid: _asked(line) for gid, line in _lines(_latest_products(game)).items()}
        except (OSError, net.FetchError, zstd.ZstdError) as e:
            res.unkept.append((game, str(e)))
            continue
        for gid, on in sorted(listed.items()):
            if gid in have:
                continue
            line = last.get(gid)
            if line is None:
                new.append(_Due(game, category, gid, "new", on, None))
            elif line.on != on:
                modified.append(_Due(game, category, gid, "modified", on, line))
            elif line.fetched < run.stamp - AGE:
                aged.append((line.fetched, _Due(game, category, gid, "aged", on, line)))
    aged.sort(key=lambda found: found[0])
    return [*new, *modified, *(due for _, due in aged)]


def _price_day(log: list[dict]) -> int:
    """The most requests a price day took, of the last lateness.LEAST_GAPS logged: each day's
    runs together."""
    by_day: dict[str, int] = {}
    for entry in log:
        n = entry.get("requests")
        if entry.get("list") == DAY and isinstance(n, int):
            made = str(entry.get("made"))
            by_day[made] = by_day.get(made, 0) + n
    return max((by_day[made] for made in sorted(by_day)[-lateness.LEAST_GAPS :]), default=0)


def _share(run: _Run) -> int:
    """The requests products may ask: what DAILY_REQUESTS leaves after every request asked today
    (UTC), less, while today's prices are still to come, room for them: the most a price day
    took lately (_price_day, or this run's, if more), and the last day's checks."""
    now = run.clock()
    room = DAILY_REQUESTS - run.spent()
    if run.day < now.astimezone(UTC).date():
        log = watching.read(STORE, now).entries
        since = now - timedelta(days=1)
        checks = sum(1 for when, _ in watching.checks(log, CHECKED) if when >= since)
        room -= max(_price_day(log), run.snap.requests) + checks
    return max(room, 0)


def _product(due: _Due, run: _Run, res: Products) -> bool:
    """One set's products, asked and appended to its game's part file for the day: whether
    they were. A set that fails is asked again the next day; no answer at all says so in res."""
    time.sleep(run.delay)
    res.asked += 1
    fetched = run.clock()
    try:
        reply = run.fetch(f"{BASE}/tcgplayer/{due.category}/{due.gid}/products")
    except net.NoAnswer as e:
        res.silent = str(e)
        return False
    except (net.FetchError, OSError) as e:
        res.failed.append((due.game, due.gid, str(e)))
        return False
    try:
        response = "null" if reply is None else _one_line(reply.body)  # null: no products file (404)
        modified = None if reply is None else net.http_time(reply.headers.get("last-modified"))
        _append(day_dir(run.day, due.game) / PRODUCTS_PART, due.gid, fetched, modified, response, due.on)
    except net.FetchError as e:
        assert reply is not None  # _one_line read it
        aside = _broken(f"{due.game}/{due.gid}-products.json", reply, str(e))
        res.failed.append((due.game, due.gid, str(aside).removesuffix(", asked again next run")))
        return False
    except OSError as e:
        res.failed.append((due.game, due.gid, str(e)))
        return False
    if due.why == "new":
        res.new += 1
    elif due.why == "modified":
        res.modified += 1
    else:
        res.aged += 1
        res.changed += (
            due.last is not None and hashlib.sha256(response.encode()).hexdigest() != due.last.sha256
        )
    return True


def _products(games: dict[str, str] | None, run: _Run) -> None:
    """Each set's products, as the module says, as one step: the sets due asked in order as far
    as the day's requests go, and each game's list kept once none of its sets is due."""
    _finish_product_days(run)
    order = _product_games(games, run)
    if not order:
        return
    res = run.snap.products = Products(runs.name(run.stamp))
    step = run.tracker.step("tcgcsv products", unit="sets")
    if run.silent:
        res.silent = NO_ANSWER
        step.fail(NO_ANSWER)
        return
    queue = _queue(order, run, res)
    res.due, share = len(queue), _share(run)
    step.update(0, len(queue))
    left = {game: 0 for game, _ in order}
    for due in queue:
        left[due.game] += 1
    for n, due in enumerate(queue):
        if res.asked >= share:
            res.owed = len(queue) - n
            break
        if _product(due, run, res):
            left[due.game] -= 1
        if res.silent:
            break
        step.update(n + 1)
    unkept = {game for game, _ in res.unkept}
    for game, _ in order:
        game_dir = day_dir(run.day, game)
        if left[game] or game in unkept or not (game_dir / PRODUCTS_PART).exists():
            continue
        try:
            _finish_products(game_dir, run.stamp)
        except (OSError, zstd.ZstdError) as e:
            res.unkept.append((game, str(e)))
            continue
        res.kept.append(game)
    _said(res, step)


def _said(res: Products, step: Step) -> None:
    """The products step, ended: failed if a set or a game's list failed, warned if sets are owed."""
    asked = f"{_count(res.asked, 'set')} asked"
    if res.asked < res.due:
        asked = f"{res.asked:,} of {res.due:,} sets asked"
    kinds = [f"{res.new:,} new"] if res.new else []
    kinds += [f"{res.modified:,} with a new modifiedOn"] if res.modified else []
    if res.aged:
        kinds.append(f"{res.aged:,} not asked for two days, {res.changed:,} of those changed")
    said = [f"{asked} ({', '.join(kinds)})" if kinds else asked]
    if not res.due:
        said = ["no sets due"]
    if res.silent:
        said.append(f"tcgcsv gave no answer ({res.silent}), the rest asked at the next day's run")
    elif res.failed:
        game, gid, why = res.failed[0]
        more = f", and {_count(len(res.failed) - 1, 'more set')}" if len(res.failed) > 1 else ""
        said.append(f"{game} set {gid} failed ({why}){more}, asked again at the next day's run")
    if res.owed:
        said.append(f"{res.owed:,} owed: today's requests are spent, so the next day's run asks them")
    said += [f"{game}'s products not kept ({why})" for game, why in res.unkept]
    if res.due or res.kept:
        said.append(f"{_count(len(res.kept), 'game')} kept")
    if res.silent or res.failed or res.unkept:
        step.fail("; ".join(said))
    else:
        (step.warn if res.owed else step.ok)("; ".join(said))


def snapshot(
    games: dict[str, str] | None = None,
    delay: float = 0.1,
    fetch: Fetch = _get,
    tracker: Tracker = SILENT,
    stamp: datetime | None = None,
    clock: Callable[[], datetime] | None = None,
) -> Snapshot:
    """Keep today's price files for every category tcgcsv carries but SKIPPED, or with
    `games` (code -> category name) for just those, asking for nothing already kept.

    "Today" is tcgcsv's last-updated date. A finished game costs no request, and neither does
    the day's category list once kept. Each game asked for is a step on the tracker; the
    ones already finished beyond GAMES share one line. A game keeps each set as it arrives,
    so the next run of the day asks only for the sets it lacks, and one that would take the
    run past DAILY_REQUESTS waits. Once tcgcsv gives no answer, the rest of its games fail
    without being asked. Games an earlier day left unfinished are finished as they stood.
    Then each set's products, as far as the day's requests go (_products). Every request is
    counted under its UTC day, by every run (requests_on), and the budget is the day's.
    delay is the pause before each request after the day's first two (tcgcsv asks for ~100 ms).
    stamp is last-updated.txt when it was just read, and counts as the run's first request.
    """
    clock = clock or times.now
    fetch = _counted(fetch, clock)
    stamp = stamp or last_updated(fetch)
    snap = Snapshot(day=stamp.date(), requests=1)
    run = _Run(fetch, delay, tracker, snap, snap.day, stamp, clock)
    try:
        _games(games, run, stamp)
        _products(games, run)
    finally:
        _finish_days(run)
    return snap


def _games(games: dict[str, str] | None, run: _Run, stamp: datetime) -> None:
    snap, tracker = run.snap, run.tracker
    named = GAMES if games is None else games
    todo = []
    for game in named:
        if finished(day_dir(snap.day, game)):
            snap.skipped.append(game)
            tracker.step(f"tcgcsv {game}").ok(f"already have {snap.day}")
        else:
            todo.append(game)
    if not todo and games is not None:
        return
    cats = _categories(snap, run.fetch)
    _keep_stamp(stamp)
    ids = resolve(cats, named)
    for game in todo:
        _game(game, ids.get(game), named[game], run)
    if games is None:
        others = {game: cid for game, cid in sorted(kept(cats).items()) if cid not in ids.values()}
        stored = [game for game in others if finished(day_dir(run.day, game))]
        if stored:
            snap.skipped.extend(stored)
            tracker.step(f"tcgcsv {_count(len(stored), 'more game')}").ok(f"already have {run.day}")
        for game, cid in others.items():
            if game not in stored:
                _game(game, cid, game, run)


def watch(
    games: dict[str, str] | None = None,
    delay: float = 0.1,
    fetch: Fetch = _get,
    tracker: Tracker = SILENT,
    clock: Callable[[], datetime] = times.now,
    always: bool = False,
) -> Watched:
    """Ask last-updated.txt when riffle.cadence says tcgcsv's next day is due, or always (the
    sync), and fetch the day when a new one has come (snapshot). A day not yet whole is asked for
    at every run until it is. Every check, every day fetched and each run's products are logged.
    A run that finds tcgcsv's lock held asks nothing."""
    res = Watched()
    with watching.held(STORE) as mine:
        if not mine:
            res.busy = True
            tracker.step("tcgcsv").ok("another run is asking for it")
            return res
        now = clock()
        got = watching.read(STORE, now, keep=True)
        log = got.entries
        if not always and _whole(log) is not False:
            busy = watching.longest(log, DAY, now, lateness.WINDOW)
            online = watching.online(log, CHECKED, got.online.get(CHECKED))
            res.plan = cadence.plan(made(), watching.checks(log, CHECKED), now, busy, online)
            if not res.plan.ask:
                tracker.step("tcgcsv").ok(watching.waiting(res.plan, "day"))
                return res
        res.asked = True
        entry = {"at": watching.at(now), "list": CHECKED}
        try:
            stamp = last_updated(_counted(fetch, clock))
        except (net.FetchError, OSError) as e:
            watching.log(STORE, entry | {"result": "failed", "why": str(e)})
            tracker.step("tcgcsv").fail(str(e))
            return res
        if _whole(log) == runs.name(stamp):
            watching.log(STORE, entry | {"result": "same", "made": runs.name(stamp)})
            said = f"no new day since the one made {times.shown(stamp)}"
            if res.plan is not None and res.plan.learning:
                said = f"{said}; {watching.learning(res.plan)}"
            tracker.step("tcgcsv").ok(said)
            return res
        watching.log(STORE, entry | {"result": "new", "made": runs.name(stamp)})
        started = time.monotonic()
        day: dict = {"list": DAY, "made": runs.name(stamp), "result": "short"}
        try:
            res.snap = snapshot(games, delay, fetch, tracker, stamp, clock)
        except (net.FetchError, OSError) as e:
            tracker.step("tcgcsv").fail(str(e))
        else:
            whole = not res.snap.failed and res.snap.refreshed is None
            day |= {"result": "whole" if whole else "short", "requests": res.snap.requests}
        seconds = round(time.monotonic() - started, 1)
        watching.log(STORE, {"at": watching.at(clock())} | day | {"seconds": seconds})
        if res.snap is not None and res.snap.products is not None:
            watching.log(STORE, {"at": watching.at(clock())} | _logged(res.snap.products))
    return res


def _logged(res: Products) -> dict:
    """A run's products step as its log entry says it: the sets due and asked, by why, and those
    the two-day sweep caught changed (what would let AGE be learned)."""
    counts = ("due", "asked", "new", "modified", "aged", "changed", "owed")
    entry: dict = {"list": PRODUCTS, "made": res.made} | {name: getattr(res, name) for name in counts}
    return (
        entry
        | {"failed": len(res.failed), "kept": len(res.kept)}
        | ({"silent": res.silent} if res.silent else {})
    )


def _whole(log: list[dict]) -> str | bool | None:
    """The stamp of the last day the log fetched whole; False when a new day has been asked
    for since and isn't whole yet; None when it has fetched none."""
    last: str | bool | None = None
    for entry in log:
        if entry.get("list") == CHECKED and entry.get("result") == "new":
            last = False
        elif entry.get("list") == DAY:
            last = entry.get("made") if entry.get("result") == "whole" else False
    return last
