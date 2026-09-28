"""Daily prices from Cardmarket, Europe's card marketplace, in euros, for every game it sells
and its accessories.

Cardmarket publishes a price guide per game once a day (around 02:45 Central European
Time) on its public download server, as plain JSON, no key needed:

    https://downloads.s3.cardmarket.com/productCatalog/priceGuide/price_guide_<id>.json

<id> is Cardmarket's game ID (1 is Magic, 16 Flesh and Blood, 18 One Piece), or accessories
for sleeves, binders, and the rest, which belong to no game. A price guide is

    {"version": 1, "createdAt": "2026-09-27T02:45:12+0200",
     "priceGuides": [{"idProduct", "idCategory", "avg", "low", "trend", "avg1", "avg7",
                      "avg30", "avg-foil", "low-foil", ...}]}

one row per product, sealed ones included, prices in EUR or null ("-holo" instead of "-foil"
for Pokémon). A Magic single's idProduct is Scryfall's cardmarket_id; every other product,
Magic's sealed ones included, is named in Cardmarket's product lists
(productCatalog/productList/products_singles_<id>.json and products_nonsingles_<id>.json on
the same server), which the loader reads. Riffle keeps each guide as returned, gzipped:

    <data_dir>/cardmarket/daily/<day>/<game>.json.gz    <day> is the guide's createdAt date

<game> is mtg, fab, or op for the games played and the name as a slug (pokemon) for the
rest. Each game fetched is a step of its own; the games still fresh from an earlier run
share one line. A guide under FRESH old isn't asked for again, since the next one comes a
day after it, so a rerun costs no request. Every game's guide is kept, played or not.

A game Cardmarket has no guide for (404, or the 403 its download server answers for a file
it doesn't have) fails for the games played. For the rest it's noted and asked for again
every run, and after seven runs in a row it's a warning (riffle.ingest.empties). A guide
with no rows is kept nowhere and handled the same way, played or not. If Cardmarket doesn't
answer at all, the games after it fail at once instead of each waiting out its own retries.
Headers, retries, and 429 handling: riffle.net.
"""

import gzip
import json
import re
import shutil
import zlib
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from riffle import net
from riffle.config import data_dir
from riffle.ingest import empties
from riffle.progress import SILENT, Step, Tracker

BASE = "https://downloads.s3.cardmarket.com/productCatalog/priceGuide"
# The games played, by Cardmarket game ID: fetched first, and a missing guide fails them.
GAMES = {"mtg": 1, "fab": 16, "op": 18}
# Every other game Cardmarket sells, and its accessories, kept too; one no longer published is skipped.
OTHERS: dict[str, int | str] = {
    "accessories": "accessories",
    "battle-spirits-saga": 20,
    "digimon": 17,
    "dragoborne": 11,
    "dragon-ball-super": 13,
    "final-fantasy": 9,
    "force-of-will": 7,
    "lorcana": 19,
    "my-little-pony": 12,
    "pokemon": 6,
    "riftbound": 22,
    "spoils": 5,
    "star-wars-destiny": 15,
    "star-wars-unlimited": 21,
    "vanguard": 8,
    "weiss-schwarz": 10,
    "world-of-warcraft": 2,
    "yugioh": 3,
}
FRESH = timedelta(hours=20)  # a guide this young is the latest there is; the next comes ~24 hours after it
CREATED = re.compile(rb'"createdAt"\s*:\s*"([^"]+)"')
MISSING = (403, 404)  # what Cardmarket's download server answers for a guide it doesn't have

Download = Callable[[str, Path, net.Progress | None], int | None]  # url, dest -> bytes, or None if missing


@dataclass
class Snapshot:
    fetched: list[str] = field(default_factory=list)  # games kept this run
    skipped: list[str] = field(default_factory=list)  # games whose latest guide was kept already
    missing: list[str] = field(default_factory=list)  # games Cardmarket has no guide for
    empty: list[str] = field(default_factory=list)  # games whose guide has no rows
    failed: list[tuple[str, str]] = field(default_factory=list)  # (game, why)


def _download(url: str, dest: Path, progress: net.Progress | None = None) -> int | None:
    return net.download(url, dest, accept="application/json", progress=progress, missing=MISSING)


def daily_dir() -> Path:
    return data_dir() / "cardmarket" / "daily"


def _count(n: int, noun: str) -> str:
    return f"{n:,} {noun}" if n == 1 else f"{n:,} {noun}s"


def created_at(stamp: object) -> datetime:
    """A guide's createdAt, e.g. 2026-09-27T02:45:12+0200."""
    if not isinstance(stamp, str):
        raise ValueError(f"createdAt {stamp!r}")
    return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%S%z")


def stamp(path: Path) -> datetime | None:
    """When Cardmarket made a kept guide, from its first bytes; None if it can't be read."""
    try:
        with gzip.open(path) as f:
            found = CREATED.search(f.read(256))
        return created_at(found.group(1).decode()) if found else None
    except (OSError, EOFError, zlib.error, ValueError):
        return None


def newest(game: str) -> tuple[date, datetime | None] | None:
    """The newest guide kept for game: its day and, if it can be read, when Cardmarket made it."""
    days = []
    for entry in daily_dir().glob(f"*/{game}.json.gz"):
        try:
            days.append(date.fromisoformat(entry.parent.name))
        except ValueError:
            continue
    if not days:
        return None
    day = max(days)
    return day, stamp(daily_dir() / day.isoformat() / f"{game}.json.gz")


def _guide(game: str, gid: int | str, download: Download, step: Step) -> tuple[date, int, bool] | None:
    """Download a game's guide, check it, and keep it gzipped under its createdAt day. Returns
    (day, products, whether it's new), or None when Cardmarket has no guide for the game. A
    guide with no rows isn't kept."""
    name = f"price_guide_{gid}.json"
    fresh = daily_dir() / f"{name}.new"
    fresh.parent.mkdir(parents=True, exist_ok=True)
    try:
        if download(f"{BASE}/{name}", fresh, step.update) is None:
            return None
        try:
            doc = json.loads(fresh.read_bytes())
            rows = doc["priceGuides"]
            if not isinstance(rows, list):
                raise TypeError("priceGuides")
            day = created_at(doc["createdAt"]).date()
        except (ValueError, KeyError, TypeError, RecursionError) as e:
            raise net.FetchError(f"{name}: not the expected JSON") from e
        dest = daily_dir() / day.isoformat() / f"{game}.json.gz"
        if not rows:
            return day, 0, False
        if dest.exists() and stamp(dest) is not None:
            return day, len(rows), False  # an unreadable one is replaced below
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = dest.with_name(dest.name + ".part")
        try:
            with fresh.open("rb") as src, gzip.open(part, "wb") as out:
                shutil.copyfileobj(src, out)
        except BaseException:
            part.unlink(missing_ok=True)
            raise
        part.replace(dest)
        return day, len(rows), True
    finally:
        fresh.unlink(missing_ok=True)


def _fetch(
    game: str, gid: int | str, snap: Snapshot, download: Download, tracker: Tracker, now: datetime
) -> bool:
    """One game's guide as its own step. False when Cardmarket didn't answer at all."""
    step = tracker.step(f"Cardmarket {game}", unit="bytes")
    key = f"cardmarket/{game}"
    try:
        got = _guide(game, gid, download, step)
    except (net.FetchError, OSError) as e:
        snap.failed.append((game, str(e)))
        step.fail(str(e))
        return not isinstance(e, net.NoAnswer)
    if got is None:
        snap.missing.append(game)
        if game in GAMES:
            gone = empties.record(key, "no guide", now)
            since = f" since {gone.first.date()} ({gone.runs} runs in a row)" if gone.runs > 1 else ""
            step.fail(f"Cardmarket has no price guide for game {gid}{since}")
        else:
            empties.report(step, key, "no guide", now)
        return True
    day, products, new = got
    if not products:
        snap.empty.append(game)
        empties.report(step, key, "empty guide", now)
        return True
    empties.clear(key)
    if new:
        snap.fetched.append(game)
        step.ok(f"kept {day}, {_count(products, 'product')}")
    else:
        snap.skipped.append(game)
        step.ok(f"already have {day}")
    return True


def snapshot(
    download: Download = _download, tracker: Tracker = SILENT, now: datetime | None = None
) -> Snapshot:
    """Keep every game's latest price guide not kept yet: the games played first, then the rest
    by name. A guide under FRESH old isn't asked for; the rest already kept share one line. A
    game that fails keeps nothing, and the next run tries it again; once Cardmarket gives no
    answer at all, the games after it fail without asking."""
    now = now or datetime.now(UTC)
    snap = Snapshot()
    answering = True

    def fetch(game: str, ref: int | str) -> None:
        nonlocal answering
        if answering:
            answering = _fetch(game, ref, snap, download, tracker, now)
        else:
            why = "not asked: Cardmarket gave no answer"
            snap.failed.append((game, why))
            tracker.step(f"Cardmarket {game}").fail(why)

    recent: dict[str, date] = {}
    for game in [*GAMES, *OTHERS]:
        last = newest(game)
        if last is not None and last[1] is not None and now < last[1] + FRESH:
            recent[game] = last[0]
    for game, gid in GAMES.items():
        if game in recent:
            snap.skipped.append(game)
            tracker.step(f"Cardmarket {game}").ok(f"already have {recent[game]}")
        else:
            fetch(game, gid)
    kept = [game for game in OTHERS if game in recent]
    if kept:
        snap.skipped.extend(kept)
        tracker.step(f"Cardmarket {_count(len(kept), 'more game')}").ok(
            f"already have {max(recent[g] for g in kept)}"
        )
    for game, ref in OTHERS.items():
        if game not in recent:
            fetch(game, ref)
    return snap
