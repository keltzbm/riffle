"""Daily TCGplayer prices from tcgcsv.com: each game's prices every day tcgcsv publishes.

tcgcsv mirrors TCGplayer's catalog and prices once a day and serves them as
plain JSON, no key needed:

    https://tcgcsv.com/last-updated.txt                     when today's data was published
    https://tcgcsv.com/tcgplayer/categories                 every game, with its categoryId
    https://tcgcsv.com/tcgplayer/{categoryId}/groups        a game's sets ("groups")
    https://tcgcsv.com/tcgplayer/{categoryId}/{groupId}/prices

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
every day fetched goes in <data_dir>/tcgcsv/watch.jsonl. Each set goes under the day its own
Last-Modified says: one from the next refresh moves the run on to that day.
Days kept before resuming came in have no times on their lines. The games in
GAMES are named by their tcgcsv category and resolved to IDs at run time; every
other category is kept too, named by its slug (pokemon-japan), except the
comics SKIPPED lists. Nothing here reads the files back: the price loader
(v0.4.0) does. Headers, retries, and 429 handling: riffle.net.
"""

import gzip
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
CHECKED, DAY = "last-updated", "day"  # the watch log's two kinds of entry
STAMP = "%Y-%m-%dT%H:%M:%S%z"  # last-updated.txt: 2026-09-24T20:05:50+0000
SET_LINE = re.compile(rb'^\{"groupId": (\d+),', re.MULTILINE)

Fetch = Callable[[str], net.Reply | None]  # url -> body and headers, or None for 404


@dataclass
class Snapshot:
    day: date  # tcgcsv's day when the run began
    fetched: list[str] = field(default_factory=list)  # games finished this run
    skipped: list[str] = field(default_factory=list)  # games already finished for the day
    failed: list[tuple[str, str]] = field(default_factory=list)  # (game, why)
    empty: list[str] = field(default_factory=list)  # games tcgcsv lists but has no set list for
    groups: dict[str, int] = field(default_factory=dict)  # game -> sets in its finished file
    requests: int = 0
    refreshed: date | None = None  # a day tcgcsv published mid-run, which the rest of the run went under


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
    silent: bool = False  # tcgcsv gave no answer: the rest of its games aren't asked


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
        raise net.FetchError(f"unexpected last-updated.txt: {text[:40]!r}") from e


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
    cats = _results(reply.body, "categories")
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
    group_ids = _set_ids(reply.body)
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


def _append(part: Path, gid: int, fetched: datetime, modified: datetime | None, response: str) -> None:
    """One set's line: its ID, when it was fetched and last modified (UTC), then the file."""
    at, mod = json.dumps(_utc(fetched)), json.dumps(modified and _utc(modified))
    part.parent.mkdir(parents=True, exist_ok=True)
    with part.open("a", encoding="utf-8") as f:
        f.write(f'{{"groupId": {gid}, "fetched": {at}, "lastModified": {mod}, "response": {response}}}\n')


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
    that fails isn't written, and no answer at all stops the game. A set from the next
    refresh goes under its own day, and ends this one: the run moves on to that day."""
    day = run.day
    have = _kept_sets(day_dir(day, game) / PART)
    todo = [g for g in group_ids if g not in have]
    if run.snap.requests + len(todo) > DAILY_REQUESTS:
        raise net.FetchError(OVER_BUDGET)
    tally = _Tally(len(group_ids), len(group_ids) - len(todo))
    step.update(tally.had, tally.listed)
    for gid in todo:
        time.sleep(run.delay)
        run.snap.requests += 1
        fetched = times.now()
        try:
            reply = run.fetch(f"{BASE}/tcgplayer/{category}/{gid}/prices")
            response = "null" if reply is None else _one_line(reply.body)  # null: no price file (404)
        except net.NoAnswer as e:
            tally.silent = str(e)
            break
        except (net.FetchError, OSError) as e:
            tally.failed.append((gid, str(e)))
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
            if run.snap.requests >= DAILY_REQUESTS:
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


def snapshot(
    games: dict[str, str] | None = None,
    delay: float = 0.1,
    fetch: Fetch = _get,
    tracker: Tracker = SILENT,
    stamp: datetime | None = None,
) -> Snapshot:
    """Keep today's price files for every category tcgcsv carries but SKIPPED, or with
    `games` (code -> category name) for just those, asking for nothing already kept.

    "Today" is tcgcsv's last-updated date. A finished game costs no request, and neither does
    the day's category list once kept. Each game asked for is a step on the tracker; the
    ones already finished beyond GAMES share one line. A game keeps each set as it arrives,
    so the next run of the day asks only for the sets it lacks, and one that would take the
    run past DAILY_REQUESTS waits. Once tcgcsv gives no answer, the rest of its games fail
    without being asked. Games an earlier day left unfinished are finished as they stood.
    delay is the pause before each request after the day's first two (tcgcsv asks for ~100 ms).
    stamp is last-updated.txt when it was just read, and counts as the run's first request.
    """
    stamp = stamp or last_updated(fetch)
    snap = Snapshot(day=stamp.date(), requests=1)
    run = _Run(fetch, delay, tracker, snap, snap.day, stamp)
    try:
        _games(games, run, stamp)
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
    at every run until it is. Every check and every day fetched is logged. A run that finds
    tcgcsv's lock held asks nothing."""
    res = Watched()
    with watching.held(STORE) as mine:
        if not mine:
            res.busy = True
            tracker.step("tcgcsv").ok("another run is asking for it")
            return res
        now = clock()
        log = watching.entries(STORE)
        if not always and _whole(log) is not False:
            busy = watching.longest(log, DAY, now, lateness.WINDOW)
            res.plan = cadence.plan(
                made(), watching.checks(log, CHECKED), now, busy, watching.online(log, CHECKED)
            )
            if not res.plan.ask:
                tracker.step("tcgcsv").ok(watching.waiting(res.plan, "day"))
                return res
        res.asked = True
        entry = {"at": watching.at(now), "list": CHECKED}
        try:
            stamp = last_updated(fetch)
        except (net.FetchError, OSError) as e:
            watching.log(STORE, entry | {"result": "failed", "why": str(e)})
            tracker.step("tcgcsv").fail(str(e))
            return res
        if _whole(log) == runs.name(stamp):
            watching.log(STORE, entry | {"result": "same", "made": runs.name(stamp)})
            tracker.step("tcgcsv").ok(f"no new day since the one made {times.shown(stamp)}")
            return res
        watching.log(STORE, entry | {"result": "new", "made": runs.name(stamp)})
        started = time.monotonic()
        day: dict = {"list": DAY, "made": runs.name(stamp), "result": "short"}
        try:
            res.snap = snapshot(games, delay, fetch, tracker, stamp)
        except (net.FetchError, OSError) as e:
            tracker.step("tcgcsv").fail(str(e))
        else:
            whole = not res.snap.failed and res.snap.refreshed is None
            day |= {"result": "whole" if whole else "short", "requests": res.snap.requests}
        seconds = round(time.monotonic() - started, 1)
        watching.log(STORE, {"at": watching.at(clock())} | day | {"seconds": seconds})
    return res


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
