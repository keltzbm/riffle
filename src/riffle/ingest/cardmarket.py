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
the same server), which the loader reads. Riffle keeps every guide and every product list as
returned, by the run rule (riffle.runs): a run's first whole, twice, and each later one as a
difference against it, under its createdAt in UTC:

    <data_dir>/cardmarket/lists/<game>/<base's stamp>/...                 the guides
    <data_dir>/cardmarket/products/<game>/<singles|nonsingles>/<run>/...  the product lists

The product lists are made hours after the guides (11:27 UTC against 00:52 on 2026-09-29), so
each is a list of its own, asked when its own next is due. One that isn't whole JSON is set
aside once a publish. One Cardmarket has never served (accessories' answer 403) is asked for
again once a Cardmarket day, at the look, so it's kept from the day it first answers.

<game> is mtg, fab, or op for the games played and the name as a slug (pokemon) for the
rest. Every game's guide is kept, played or not, the games Cardmarket adds too: once a
Cardmarket day (the first run after a guide made since the last look) a run looks for new
games, asking for the guide of every game ID not known, from 1 to PAST past the highest known,
by its first bytes. Each ID whose guide answers is a game, named from the first categoryName in
its singles product list (products_singles_<id>.json: "Cyberpunk Single" is cyberpunk) and
asked for from then on like the rest; with no name there, it's game-<id>. The games learned,
and when a run last looked:

    <data_dir>/cardmarket/games.json    {"looked": <UTC>, "games": {"cyberpunk": {"id": 23,
                                         "category": "Cyberpunk Single", "found": <UTC>}}}

A look that fails isn't recorded, so the next run looks again; a games.json that can't be read
is set aside, and its games are learned again at once. `riffle watch cardmarket` asks for a
game's guide when riffle.cadence says its next is due, learned from its own createdAt times and
checks, carrying the ETag of the guide last kept: a guide not new costs a 304 and no body.
Its product lists likewise. Each list asked for is a step of its own; the lists not due share
one line. Every check goes
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
from collections.abc import Callable, Collection
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import partial
from pathlib import Path

from riffle import net, runs, times, watching
from riffle.config import data_dir
from riffle.ingest import empties
from riffle.progress import SILENT, Step, Tracker

BASE = "https://downloads.s3.cardmarket.com/productCatalog/priceGuide"
PRODUCTS = "https://downloads.s3.cardmarket.com/productCatalog/productList"
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
# How far past the highest game ID known a look for new games asks. Not learned: Cardmarket has
# added one game at a time, in ID order (Riftbound 22, Cyberpunk 23 on 2026-08-27, Gundam 24 on
# 2026-09-01), so there's nothing to learn it from. Five leaves room for IDs taken before their
# guide goes up, each a 403 at a look.
PAST = 5
LOOK = "new games"  # a look's entries in watch.jsonl
KINDS = ("singles", "nonsingles")  # each game's product lists: products_<kind>_<id>.json
CREATED = re.compile(rb'"createdAt"\s*:\s*"([^"]+)"')
CATEGORY = re.compile(rb'"categoryName"\s*:\s*("(?:[^"\\]|\\.)*")')
MISSING = (403, 404)  # what Cardmarket's download server answers for a guide it doesn't have

Fetch = Callable[..., net.Fetched | None]  # net.fetch_new


def daily_dir() -> Path:
    return data_dir() / STORE / "daily"


def lists_dir(game: str) -> Path:
    return data_dir() / STORE / "lists" / game


def products_dir(game: str, kind: str) -> Path:
    """Where a game's product lists of one kind (KINDS) are kept, by the run rule."""
    return data_dir() / STORE / "products" / game / kind


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


def games_path() -> Path:
    return data_dir() / STORE / "games.json"


def _saved() -> dict:
    """games.json as saved; {} when there's none. ValueError when it can't be read as one."""
    try:
        found = json.loads(games_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    known = found.get("games") if isinstance(found, dict) else None
    if not isinstance(known, dict) or not all(
        isinstance(game, dict) and isinstance(game.get("id"), int) for game in known.values()
    ):
        raise ValueError("not the games learned")
    return found


def _save(looked: datetime, known: dict[str, dict]) -> None:
    path = games_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_name(path.name + ".part")
    saved = {"looked": watching.at(looked), "games": known}
    part.write_text(json.dumps(saved, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    part.replace(path)


def learned() -> dict[str, int]:
    """Each game learned from Cardmarket, by name: its game ID; none while games.json can't be read."""
    try:
        saved = _saved()
    except ValueError:
        return {}
    return {name: game["id"] for name, game in saved.get("games", {}).items()}


def games() -> dict[str, int | str]:
    """Every game whose guide is asked for, by name: its Cardmarket game ID. The games played
    first, then the rest by name, the ones learned among them."""
    rest: dict[str, int | str] = {**learned(), **OTHERS}
    return {**GAMES, **{name: rest[name] for name in sorted(rest) if name not in GAMES}}


def _ids(ids: list[int]) -> str:
    """Game IDs to read at a glance: 4, 14, 25-29."""
    spans: list[list[int]] = []
    for ref in ids:
        if spans and ref == spans[-1][-1] + 1:
            spans[-1].append(ref)
        else:
            spans.append([ref])
    return ", ".join(str(s[0]) if len(s) == 1 else f"{s[0]}-{s[-1]}" for s in spans)


def _first(fetch: Fetch, url: str) -> net.Fetched | None:
    """A file's first bytes, then hang up; None if Cardmarket doesn't have it."""
    probe = data_dir() / STORE / "look.new"  # never written: the answer is known at once
    return fetch(url, probe, lambda head: True, accept="application/json", missing=MISSING)


def _name(ref: int, fetch: Fetch, taken: Collection[str]) -> tuple[str, str | None]:
    """A game found's name, and the category it's named from: the first categoryName in its
    singles product list less " Single" ("Cyberpunk Single" is cyberpunk), its ID after it if
    a game has the name already; game-<id>, and no category, if the list gives none."""
    got = _first(fetch, f"{PRODUCTS}/products_singles_{ref}.json")
    found = CATEGORY.search(got.head) if got is not None else None
    try:
        category = json.loads(found.group(1)) if found else None
    except ValueError:
        category = None
    name = watching.slug(category.removesuffix(" Single")) if isinstance(category, str) else ""
    if not name:
        return f"game-{ref}", None
    return (f"{name}-{ref}" if name in taken else name), category


@dataclass
class Look:
    asked: list[int]  # the game IDs asked for
    found: dict[str, dict] = field(default_factory=dict)  # name -> {"id", "category", "found"}
    odd: list[int] = field(default_factory=list)  # answered with something not a guide


def _look(fetch: Fetch, known: dict[str, int | str], now: datetime) -> Look:
    """Ask for the guide of every game ID not known, from 1 to PAST past the highest known, by
    its first bytes; each that answers with a guide is a game found, and named. Any FetchError
    ends the look."""
    ids = {ref for ref in known.values() if isinstance(ref, int)}
    look = Look([ref for ref in range(1, max(ids, default=0) + PAST + 1) if ref not in ids])
    for ref in look.asked:
        got = _first(fetch, f"{BASE}/price_guide_{ref}.json")
        if got is None:
            continue
        if _created(got.head) is None:
            look.odd.append(ref)
            continue
        name, category = _name(ref, fetch, [*known, *look.found])
        look.found[name] = {"id": ref, "category": category, "found": watching.at(now)}
    return look


def _looked(fetch: Fetch, tracker: Tracker, now: datetime) -> dict[str, int] | None:
    """Look for new games (_look) once a Cardmarket day: when no run has looked yet, or a guide
    kept was made after the last look. Its own step; the games found, by name: their IDs,
    saved in games.json with the look's time. None when no look was made, or it failed, which
    saves nothing."""
    damaged = ""
    try:
        saved = _saved()
    except ValueError:
        where = watching.set_aside(STORE, games_path(), "games.json", now)
        damaged = f"games.json couldn't be read: set aside as {where}; "
        saved = {}
    looked = runs.parse(str(saved.get("looked", "")))
    newest = max((at for game in games() for at in made(game)[-1:]), default=None)
    if looked is not None and (newest is None or newest <= looked):
        return None
    step = tracker.step(f"Cardmarket {LOOK}")
    entry: dict = {"at": watching.at(now), "list": LOOK}
    started = time.monotonic()
    try:
        look = _look(fetch, games(), now)
    except (net.FetchError, OSError) as e:
        step.warn(f"{damaged}not looked: {e}; looked again next run")
        seconds = round(time.monotonic() - started, 1)
        watching.log(STORE, entry | {"result": "failed", "why": str(e), "seconds": seconds})
        return None
    _save(now, saved.get("games", {}) | look.found)
    names = [f"{name} ({game['id']})" for name, game in look.found.items()]
    said = [f"{'found ' + ', '.join(names) if names else 'no new game'}; asked {_ids(look.asked)}"]
    unnamed = [name for name, game in look.found.items() if not game["category"]]
    said += [f"{name}: no name in its product list" for name in unnamed]
    said += [f"game {ref} answered with something not a price guide" for ref in look.odd]
    if damaged or len(said) > 1:
        step.warn(damaged + "; ".join(said))
    else:
        step.ok(said[0])
    found = {name: game["id"] for name, game in look.found.items()}
    seconds = round(time.monotonic() - started, 1)
    watching.log(STORE, entry | {"result": "looked", "asked": look.asked, "found": found, "seconds": seconds})
    return found


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
            made = _created(body[: net.HEAD])
            publish = runs.name(made) if made else got.etag
            raise watching.broken(
                STORE, game, publish, fresh, name, now, f"{name}: not the expected JSON"
            ) from e
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


def _missing(game: str, ref: int | str, key: str, now: datetime, step: Step, what: str = "") -> dict:
    """A game's file Cardmarket doesn't have, its guide unless what names it: a played game's
    step fails, the rest are noted."""
    if game not in GAMES:
        empties.report(step, key, f"no {what or 'guide'}", now)
        return {"result": "missing"}
    gone = empties.record(key, f"no {what or 'guide'}", now)
    since = f" since {gone.first.date()} ({gone.runs} runs in a row)" if gone.runs > 1 else ""
    why = f"Cardmarket has no {what or 'price guide'} for game {ref}{since}"
    step.fail(why)
    return {"result": "missing", "why": why}


def _products(
    game: str, ref: int | str, kind: str, fetch: Fetch, tags: dict[str, str], now: datetime, step: Step
) -> dict:
    """Ask for one of a game's product lists once and keep it if it's new, by the run rule under
    its createdAt; end step saying what came. One that isn't whole JSON is set aside once per
    publish. One Cardmarket has never served is noted, not warned about. The log entry."""
    name = f"products_{kind}_{ref}.json"
    listed = f"{game} {kind}"
    folder = products_dir(game, kind)
    have = runs.kept(folder)
    key = f"{STORE}/{game}/{kind}"

    def known(head: bytes) -> bool:
        at = _created(head)
        return at is not None and runs.name(at) in have

    fresh = folder.parent / f"{kind}.new"
    try:
        fresh.parent.mkdir(parents=True, exist_ok=True)
        got = fetch(
            f"{PRODUCTS}/{name}",
            fresh,
            known,
            etag=tags.get(listed),
            accept="application/json",
            progress=step.update,
            missing=MISSING,
        )
        if got is None and not have:
            step.ok("Cardmarket serves none yet; asked again once a Cardmarket day")
            return {"result": "missing"}
        if got is None:
            return _missing(game, ref, key, now, step, f"{kind} product list")
        if got.status == "unchanged":
            step.ok("no new product list since the last one kept")
            return {"result": "unchanged"}
        if got.status == "known":
            at = _created(got.head)
            assert at is not None  # known() found it
            if got.etag:
                tags[listed] = got.etag
            step.ok(f"have the product list made {times.shown(at)}")
            return {"result": "known", "made": runs.name(at)}
        body = fresh.read_bytes()
        try:
            doc = json.loads(body)
            rows = doc["products"]
            if not isinstance(rows, list):
                raise TypeError("products")
            at = created_at(doc["createdAt"])
        except (ValueError, KeyError, TypeError, RecursionError) as e:
            made = _created(body[: net.HEAD])
            publish = runs.name(made) if made else got.etag
            raise watching.broken(
                STORE, listed, publish, fresh, name, now, f"{name}: not the expected JSON"
            ) from e
        if not rows:
            empties.report(step, key, "empty product list", now)
            return {"result": "empty"}
        empties.clear(key)
        kept = runs.keep(folder, at, body)
        if got.etag:
            tags[listed] = got.etag
        rows_kept = _count(len(rows), "product")
        note = f"kept the product list made {times.shown(at)}, {rows_kept}: {watching.how(kept)}"
        if kept.notes:
            step.warn(f"{note}; {'; '.join(kept.notes)}")
        else:
            step.ok(note)
        return watching.record(kept)
    finally:
        fresh.unlink(missing_ok=True)


def _never_served(log: list[dict], name: str, folder: Path) -> bool:
    """A product list with nothing kept whose last answer was that Cardmarket has none."""
    if runs.kept(folder):
        return False
    results = [e["result"] for e in log if e.get("list") == name and e.get("result") not in (None, "failed")]
    return bool(results) and results[-1] == "missing"


def watch(
    fetch: Fetch = net.fetch_new,
    tracker: Tracker = SILENT,
    clock: Callable[[], datetime] = times.now,
    always: bool = False,
) -> watching.Watch:
    """Ask for each game's guide, then each game's product lists (KINDS), each when
    riffle.cadence says its next is due, or all when always (the sync), and keep each new one,
    each its own step; the lists not due share one line (riffle.watching.many). Then, once a
    Cardmarket day, look for games Cardmarket has added, and ask for each found, and for the
    product lists Cardmarket has never served, so one is kept from the day it first answers.
    A list that fails keeps nothing and is asked for again next run; once Cardmarket gives no
    answer at all, the lists after it fail without asking, and no run looks."""

    def guide(game: str, ref: int | str) -> watching.Listed:
        def ask(tags: dict[str, str], now: datetime, step: Step) -> dict:
            return _guide(game, ref, fetch, tags, now, step)

        return watching.Listed(game, f"Cardmarket {game}", lists_dir(game), ask, "guide", partial(made, game))

    def products(game: str, ref: int | str, kind: str) -> watching.Listed:
        def ask(tags: dict[str, str], now: datetime, step: Step) -> dict:
            return _products(game, ref, kind, fetch, tags, now, step)

        folder = products_dir(game, kind)
        return watching.Listed(
            f"{game} {kind}", f"Cardmarket {game} {kind}", folder, ask, noun="product list"
        )

    log = watching.entries(STORE)
    known = games()
    lists = [guide(game, ref) for game, ref in known.items()]
    deferred: list[watching.Listed] = []
    for game, ref in known.items():
        for kind in KINDS:
            item = products(game, ref, kind)
            (deferred if _never_served(log, item.name, item.folder) else lists).append(item)

    def then(run: watching.Pass) -> None:
        found = _looked(fetch, run.tracker, run.clock())
        if found is None:
            return
        for item in deferred:
            run.ask(item)
        for game, ref in found.items():
            run.ask(guide(game, ref))
            for kind in KINDS:
                run.ask(products(game, ref, kind))

    return watching.many(STORE, "Cardmarket", lists, tracker, clock, always, then)
