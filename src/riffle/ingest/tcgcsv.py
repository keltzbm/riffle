"""Daily TCGplayer prices from tcgcsv.com, one snapshot per day per game.

tcgcsv mirrors TCGplayer's catalog and prices once a day and serves them as
plain JSON, no key needed:

    https://tcgcsv.com/last-updated.txt                     when today's data was published
    https://tcgcsv.com/tcgplayer/categories                 every game, with its categoryId
    https://tcgcsv.com/tcgplayer/{categoryId}/groups        a game's sets ("groups")
    https://tcgcsv.com/tcgplayer/{categoryId}/{groupId}/prices

It used to publish a daily archive of every price file at once. That was taken
down in September 2026 (server costs; the maintainer is waiting on TCGplayer
for terms), with the request that clients fetch the price files one at a time
and never the same file twice in a day. So Riffle keeps its own history: once
a day, for everything tcgcsv carries but comics, it fetches every group's price
file and stores the responses as returned, in

    <data_dir>/tcgcsv/daily/<day>/categories.json           the category list that day
    <data_dir>/tcgcsv/daily/<day>/<game>/groups.json        the groups response
    <data_dir>/tcgcsv/daily/<day>/<game>/prices.jsonl.gz    one line per group:
                                                            {"groupId": ..., "response": <the price file>}

<day> is the date from last-updated.txt, so a day is fetched once however
often sync runs. The games in GAMES are named by their tcgcsv category and
resolved to IDs at run time; every other category is kept too, named by its
slug (pokemon-japan), except the comics SKIPPED lists. Nothing here reads the
files back: the price loader (v0.4.0) does. Headers, retries, and 429
handling: riffle.net.
"""

import gzip
import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from riffle import net
from riffle.config import data_dir
from riffle.progress import SILENT, Tracker

BASE = "https://tcgcsv.com"
# Game code -> the game's category name on tcgcsv; IDs are looked up at run time. These are the
# games played; every other category is kept too, named by its slug. Each game is its own step.
GAMES = {"mtg": "Magic", "fab": "Flesh & Blood TCG", "op": "One Piece Card Game"}
# The only categories not kept, by ID: Marvel and DC comics alone are 19,800 groups, twice
# tcgcsv's daily request limit. Card games, miniatures, board games, and supplies all are.
SKIPPED = {69: "Marvel Comics", 70: "DC Comics"}
DAILY_REQUESTS = 9_000  # tcgcsv asks for under 10,000 a day; games past this wait for the next day
OVER_BUDGET = "past the day's request budget; the next day's run gets it"

Fetch = Callable[[str], bytes | None]  # url -> body, or None for 404


@dataclass
class Snapshot:
    day: date
    fetched: list[str] = field(default_factory=list)  # games stored this run
    skipped: list[str] = field(default_factory=list)  # games already stored for the day
    failed: list[tuple[str, str]] = field(default_factory=list)  # (game, why)
    groups: dict[str, int] = field(default_factory=dict)  # game -> price files fetched
    requests: int = 0


def _get(url: str) -> bytes | None:
    return net.get(url, accept="application/json")


def daily_dir() -> Path:
    return data_dir() / "tcgcsv" / "daily"


def day_dir(day: date, game: str) -> Path:
    return daily_dir() / day.isoformat() / game


def stored_days(games: dict[str, str] = GAMES) -> list[date]:
    """Days with a price file for every game, oldest first."""
    if not daily_dir().exists():
        return []
    days = []
    for entry in daily_dir().iterdir():
        try:
            day = date.fromisoformat(entry.name)
        except ValueError:
            continue
        if all((entry / game / "prices.jsonl.gz").exists() for game in games):
            days.append(day)
    return sorted(days)


def last_updated(fetch: Fetch = _get) -> datetime:
    """When tcgcsv last refreshed its data, e.g. 2026-09-24T20:05:50+0000."""
    body = fetch(f"{BASE}/last-updated.txt")
    if body is None:
        raise net.FetchError("HTTP 404")
    text = body.decode().strip()
    try:
        return datetime.strptime(text, "%Y-%m-%dT%H:%M:%S%z")
    except ValueError as e:
        raise net.FetchError(f"unexpected last-updated.txt: {text[:40]!r}") from e


def _parse(body: bytes, what: str) -> dict:
    """The response as a dict with a "results" list, or a FetchError naming what came back instead."""
    try:
        doc = json.loads(body)
        if not isinstance(doc, dict) or not isinstance(doc["results"], list):
            raise KeyError("results")
    except (ValueError, KeyError, TypeError) as e:
        raise net.FetchError(f"{what}: not the expected JSON") from e
    return doc


def _results(body: bytes, what: str) -> list[dict]:
    return _parse(body, what)["results"]


def _one_line(body: bytes, what: str) -> str:
    """The response verbatim if it's one line, else compacted: the file is one JSON object per line."""
    doc = _parse(body, what)
    text = body.decode("utf-8").strip()
    return text if "\n" not in text and "\r" not in text else json.dumps(doc, separators=(",", ":"))


def resolve(cats: list[dict], games: dict[str, str] = GAMES) -> dict[str, int]:
    """Game code -> tcgcsv categoryId, matched by name (case-insensitive). Unknown games are left out."""
    by_name = {str(c.get("name", "")).casefold(): int(c["categoryId"]) for c in cats if "categoryId" in c}
    return {code: by_name[name.casefold()] for code, name in games.items() if name.casefold() in by_name}


def _slug(name: str) -> str:
    """A category's name as a folder name: "Pokemon Japan" is pokemon-japan."""
    return re.sub(r"[^a-z0-9]+", "-", name.casefold()).strip("-")


def kept(cats: list[dict]) -> dict[str, int]:
    """Game code -> categoryId for every category in tcgcsv's list but SKIPPED, each code
    the category's name as a slug."""
    return {
        _slug(str(c.get("name", ""))) or str(c["categoryId"]): int(c["categoryId"])
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


def _target(day: date, game: str) -> Path:
    return day_dir(day, game) / "prices.jsonl.gz"


def _categories(snap: Snapshot, fetch: Fetch) -> list[dict]:
    """The day's category list, fetched once a day and kept beside that day's price files."""
    path = daily_dir() / snap.day.isoformat() / "categories.json"
    if path.exists():
        return _results(path.read_bytes(), "categories")
    body = fetch(f"{BASE}/tcgplayer/categories")
    snap.requests += 1
    if body is None:
        raise net.FetchError("categories: HTTP 404")
    cats = _results(body, "categories")
    _write(path, body)
    return cats


def _groups(category: int, game_dir: Path, fetch: Fetch, snap: Snapshot) -> list[int]:
    """A game's group IDs, sorted, keeping its groups response as groups.json."""
    body = fetch(f"{BASE}/tcgplayer/{category}/groups")
    snap.requests += 1
    if body is None:
        raise net.FetchError("groups: HTTP 404")
    group_ids = sorted(int(g["groupId"]) for g in _results(body, "groups"))
    _write(game_dir / "groups.json", body)
    return group_ids


def _prices(
    category: int,
    group_ids: list[int],
    target: Path,
    fetch: Fetch,
    delay: float,
    snap: Snapshot,
    fetched: Callable[[int], None],
) -> None:
    """Every group's price file into target, all or nothing, calling fetched with the count so far."""
    if snap.requests + len(group_ids) > DAILY_REQUESTS:
        raise net.FetchError(OVER_BUDGET)
    tmp = target.with_name(target.name + ".part")
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        with gzip.open(tmp, "wt", encoding="utf-8") as out:
            for n, gid in enumerate(group_ids, 1):
                time.sleep(delay)
                body = fetch(f"{BASE}/tcgplayer/{category}/{gid}/prices")
                snap.requests += 1
                if body is None:
                    out.write(f'{{"groupId": {gid}, "response": null}}\n')
                else:
                    out.write(f'{{"groupId": {gid}, "response": {_one_line(body, f"group {gid}")}}}\n')
                fetched(n)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    tmp.replace(target)


def _game(
    game: str,
    category: int | None,
    name: str,
    snap: Snapshot,
    fetch: Fetch,
    delay: float,
    tracker: Tracker,
) -> None:
    """One game as its own step: its groups, then every group's price file. category is
    None when no category goes by the game's name."""
    step = tracker.step(f"tcgcsv {game}", unit="groups")
    try:
        if category is None:
            raise net.FetchError(f"tcgcsv has no category named {name!r}")
        if snap.requests >= DAILY_REQUESTS:
            raise net.FetchError(OVER_BUDGET)
        time.sleep(delay)
        group_ids = _groups(category, day_dir(snap.day, game), fetch, snap)
        step.update(0, len(group_ids))
        _prices(category, group_ids, _target(snap.day, game), fetch, delay, snap, step.update)
    except net.FetchError as e:
        snap.failed.append((game, str(e)))
        step.fail(str(e))
    else:
        snap.groups[game] = len(group_ids)
        snap.fetched.append(game)
        step.ok(_count(len(group_ids), "group"))


def snapshot(
    games: dict[str, str] | None = None,
    delay: float = 0.1,
    fetch: Fetch = _get,
    tracker: Tracker = SILENT,
) -> Snapshot:
    """Store today's price files for every category tcgcsv carries but SKIPPED, or with
    `games` (code -> category name) for just those, skipping what's already stored.

    "Today" is tcgcsv's last-updated date. A stored game costs no request, and neither does
    the day's category list once stored. Each game fetched is a step on the tracker; the ones
    already stored beyond GAMES share one line. A game that fails part-way keeps nothing, so
    the next run fetches it whole, and a game that would take the day past DAILY_REQUESTS
    waits for the next day. delay is the pause before each request after the day's first
    two (tcgcsv asks for ~100 ms).
    """
    stamp = last_updated(fetch)
    snap = Snapshot(day=stamp.date())
    snap.requests += 1
    named = GAMES if games is None else games
    todo = []
    for game in named:
        if _target(snap.day, game).exists():
            snap.skipped.append(game)
            tracker.step(f"tcgcsv {game}").ok(f"already have {snap.day}")
        else:
            todo.append(game)
    if not todo and games is not None:
        return snap
    cats = _categories(snap, fetch)
    stamp_file = daily_dir() / snap.day.isoformat() / "last-updated.txt"
    if not stamp_file.exists():
        _write(stamp_file, stamp.strftime("%Y-%m-%dT%H:%M:%S%z").encode())
    ids = resolve(cats, named)
    for game in todo:
        _game(game, ids.get(game), named[game], snap, fetch, delay, tracker)
    if games is None:
        others = {game: cid for game, cid in sorted(kept(cats).items()) if cid not in ids.values()}
        stored = [game for game in others if _target(snap.day, game).exists()]
        if stored:
            snap.skipped.extend(stored)
            tracker.step(f"tcgcsv {_count(len(stored), 'more game')}").ok(f"already have {snap.day}")
        for game, cid in others.items():
            if game not in stored:
                _game(game, cid, game, snap, fetch, delay, tracker)
    return snap
