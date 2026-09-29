"""Every price guide Cardmarket, Europe's card marketplace, publishes, in euros, for every game
it sells and its accessories.

Cardmarket publishes a price guide per game once a day (every guide within a minute of the
others, 02:44 to 02:48 Central European Time on the days kept) on its public download server,
as plain JSON, no key needed:

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
the same server), which the loader reads. Riffle keeps every guide as returned, by the run
rule (riffle.runs): a run's first whole, twice, and each later one as a difference against it,
under its createdAt in UTC:

    <data_dir>/cardmarket/lists/<game>/<base's stamp>/...

<game> is mtg, fab, or op for the games played and the name as a slug (pokemon) for the
rest. Every game's guide is kept, played or not. `riffle watch cardmarket` asks for a game's
guide when riffle.cadence says its next is due, learned from its own createdAt times and
checks, carrying the ETag of the guide last kept: a guide not new costs a 304 and no body.
Each game asked for is a step of its own; the games not due share one line. Every check goes
in <data_dir>/cardmarket/watch.jsonl. Until 2026-09-29 Riffle kept one guide a day, gzipped,
under the day of its createdAt; those stay:

    <data_dir>/cardmarket/daily/<day>/<game>.json.gz

A game Cardmarket has no guide for (404, or the 403 its download server answers for a file
it doesn't have) fails for the games played. For the rest it's noted and asked for again
each time it's due, and after seven times in a row it's a warning (riffle.ingest.empties). A guide
with no rows is kept nowhere and handled the same way, played or not. If Cardmarket doesn't
answer at all, the games after it fail at once instead of each waiting out its own retries.
Headers, retries, and 429 handling: riffle.net.
"""

import gzip
import json
import re
import time
import zlib
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from riffle import cadence, lateness, net, runs, times, watching
from riffle.config import data_dir
from riffle.ingest import empties
from riffle.progress import SILENT, Step, Tracker

BASE = "https://downloads.s3.cardmarket.com/productCatalog/priceGuide"
STORE = "cardmarket"
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
CREATED = re.compile(rb'"createdAt"\s*:\s*"([^"]+)"')
MISSING = (403, 404)  # what Cardmarket's download server answers for a guide it doesn't have

Fetch = Callable[..., net.Fetched | None]  # net.fetch_new


def daily_dir() -> Path:
    return data_dir() / STORE / "daily"


def lists_dir(game: str) -> Path:
    return data_dir() / STORE / "lists" / game


def _count(n: int, noun: str) -> str:
    return f"{n:,} {noun}" if n == 1 else f"{n:,} {noun}s"


def created_at(stamp: object) -> datetime:
    """A guide's createdAt, e.g. 2026-09-27T02:45:12+0200."""
    if not isinstance(stamp, str):
        raise ValueError(f"createdAt {stamp!r}")
    return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%S%z")


def _created(head: bytes) -> datetime | None:
    """When a guide says it was made, from its first bytes; None if they don't say."""
    found = CREATED.search(head)
    try:
        return created_at(found.group(1).decode()) if found else None
    except ValueError:
        return None


def stamp(path: Path) -> datetime | None:
    """When Cardmarket made a guide kept a day (before 2026-09-29); None if it can't be read."""
    try:
        with gzip.open(path) as f:
            return _created(f.read(256))
    except (OSError, EOFError, zlib.error):
        return None


def made(game: str) -> list[datetime]:
    """When Cardmarket made each guide kept for a game, in UTC, oldest first."""
    found = [stamp(path) for path in daily_dir().glob(f"*/{game}.json.gz")]
    found += [runs.parse(name) for name in runs.kept(lists_dir(game))]
    return sorted(at.astimezone(UTC) for at in found if at is not None)


def _guide(game: str, ref: int | str, fetch: Fetch, tags: dict[str, str], now: datetime, step: Step) -> dict:
    """Ask for a game's guide once and keep it if it's new; end step saying what came. The log
    entry: "why" is in it when the step failed without an error, for a played game with no guide."""
    name = f"price_guide_{ref}.json"
    folder = lists_dir(game)
    have = runs.kept(folder)
    key = f"{STORE}/{game}"

    def known(head: bytes) -> bool:
        at = _created(head)
        return at is not None and runs.name(at) in have

    fresh = folder.parent / f"{game}.new"
    try:
        fresh.parent.mkdir(parents=True, exist_ok=True)
        got = fetch(
            f"{BASE}/{name}",
            fresh,
            known,
            etag=tags.get(game),
            accept="application/json",
            progress=step.update,
            missing=MISSING,
        )
        if got is None:
            return _missing(game, ref, key, now, step)
        if got.status == "unchanged":
            step.ok("no new guide since the last one kept")
            return {"result": "unchanged"}
        if got.status == "known":
            at = _created(got.head)
            assert at is not None  # known() found it
            if got.etag:
                tags[game] = got.etag
            step.ok(f"have the guide made {times.shown(at)}")
            return {"result": "known", "made": runs.name(at)}
        body = fresh.read_bytes()
        try:
            doc = json.loads(body)
            rows = doc["priceGuides"]
            if not isinstance(rows, list):
                raise TypeError("priceGuides")
            at = created_at(doc["createdAt"])
        except (ValueError, KeyError, TypeError, RecursionError) as e:
            raise net.FetchError(f"{name}: not the expected JSON") from e
        if not rows:
            empties.report(step, key, "empty guide", now)
            return {"result": "empty"}
        empties.clear(key)
        kept = runs.keep(folder, at, body)
        if got.etag:
            tags[game] = got.etag
        note = f"kept the guide made {times.shown(at)}, {_count(len(rows), 'product')}: {watching.how(kept)}"
        if kept.notes:
            step.warn(f"{note}; {'; '.join(kept.notes)}")
        else:
            step.ok(note)
        return watching.record(kept)
    finally:
        fresh.unlink(missing_ok=True)


def _missing(game: str, ref: int | str, key: str, now: datetime, step: Step) -> dict:
    """A game Cardmarket has no guide for: a played game's step fails, the rest are noted."""
    if game not in GAMES:
        empties.report(step, key, "no guide", now)
        return {"result": "missing"}
    gone = empties.record(key, "no guide", now)
    since = f" since {gone.first.date()} ({gone.runs} runs in a row)" if gone.runs > 1 else ""
    why = f"Cardmarket has no price guide for game {ref}{since}"
    step.fail(why)
    return {"result": "missing", "why": why}


def watch(
    fetch: Fetch = net.fetch_new,
    tracker: Tracker = SILENT,
    clock: Callable[[], datetime] = times.now,
    always: bool = False,
) -> watching.Watch:
    """Ask for each game's guide riffle.cadence says is due, or every game's when always (the
    sync), the games played first, and keep each new one, each its own step; the games not due
    share one line. A guide that fails keeps nothing and is asked for again next run; once
    Cardmarket gives no answer at all, the games after it fail without asking. A run that
    finds Cardmarket's lock held asks nothing."""
    res = watching.Watch()
    with watching.held(STORE) as mine:
        if not mine:
            res.busy = True
            tracker.step("Cardmarket guides").ok("another run is asking for them")
            return res
        tags = watching.load_tags(STORE)
        log = watching.entries(STORE)
        plans: list[cadence.Plan] = []
        answering = True
        for game, ref in [*GAMES.items(), *OTHERS.items()]:
            now = clock()
            if not always:
                busy = watching.longest(log, game, now, lateness.WINDOW)
                plan = cadence.plan(made(game), watching.checks(log, game), now, busy)
                if not plan.ask:
                    res.waiting.append(game)
                    plans.append(plan)
                    continue
            label = f"Cardmarket {game}"
            entry: dict = {"at": watching.at(now), "list": game}
            if not answering:
                why = "not asked: Cardmarket gave no answer"
                res.failed.append((label, why))
                tracker.step(label).fail(why)
                watching.log(STORE, entry | {"result": "failed", "why": why})
                continue
            step = tracker.step(label, unit="bytes")
            started = time.monotonic()
            try:
                entry |= _guide(game, ref, fetch, tags, now, step)
            except (net.FetchError, OSError) as e:
                answering = not isinstance(e, net.NoAnswer)
                res.failed.append((label, str(e)))
                step.fail(str(e))
                entry |= {"result": "failed", "why": str(e)}
            else:
                if "why" in entry:
                    res.failed.append((label, entry["why"]))
                sorts = {"kept": res.kept, "empty": res.empty, "missing": res.empty}
                sorts.get(entry["result"], res.same).append(label)
            watching.log(STORE, entry | {"seconds": round(time.monotonic() - started, 1)})
        if plans:
            first = min(plan.next for plan in plans)
            step = tracker.step(f"Cardmarket {_count(len(plans), 'guide')}")
            step.ok(f"none due; the next asked {times.shown(first)}")
        watching.save_tags(STORE, tags)
    return res
