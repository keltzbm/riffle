"""MTGO decklists from mtgo.com: league 5-0s, challenges, showcases, qualifiers.

Event pages render client-side. The data is one JSON object assigned to
`window.MTGO.decklists.data` in a script tag, so we lift that out instead of
parsing HTML. The monthly index (/decklists/YYYY/MM) links every event as
/decklist/<slug>, where the slug ends in the date and event id:

    modern-challenge-32-2026-04-1812839681   -> 2026-04-18, event 12839681

Each event is fetched once and stored, normalized, as
<data_dir>/mtgo/<year>/<month>/<slug>.json, with the page's whole data object gzipped
in mtgo/raw/. Published lists don't change, so nothing stored is fetched again.

Fetching is a trickle: a job asks for a few pages every 10 minutes, all day, and
keeps a list of every event an index listed that isn't stored yet. Throttled,
mtgo.com doesn't refuse: it answers with stripped pages. How a run tells those
apart from missing data is described under the trickle below.
"""

import gzip
import hashlib
import json
import re
import time
from collections.abc import Callable, Iterable, Iterator
from contextlib import AbstractContextManager
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import NoReturn

from riffle import locks, net, trickle
from riffle.config import data_dir
from riffle.progress import SILENT, Tracker

BASE = "https://www.mtgo.com"
KINDS = ("league", "challenge", "showcase", "qualifier", "preliminary", "other")
# The formats mtgo.com publishes decklists in, spelled as its event names spell them. Tab
# completion offers these; ingest takes whatever format an event name starts with.
FORMATS = ("standard", "pioneer", "modern", "legacy", "vintage", "pauper", "premodern", "duel-commander")

_DATA = re.compile(r"window\.MTGO\.decklists\.data\s*=\s*")
_LINK = re.compile(r'href="(?:https?://(?:www\.)?mtgo\.com)?/decklist/([a-z0-9-]+)"', re.I)
_SLUG = re.compile(r"^(?P<name>[a-z0-9-]+?)-(?P<date>\d{4}-\d{2}-\d{2})(?P<id>\d+)$")


@dataclass
class Card:
    name: str
    qty: int


@dataclass
class MtgoDeck:
    player: str
    main: list[Card] = field(default_factory=list)
    side: list[Card] = field(default_factory=list)
    rank: int | None = None  # final standing (challenges, showcases)
    record: str | None = None  # "5-0" for leagues

    @property
    def fingerprint(self) -> str:
        """Same 75 (main and side, any order, any printing split) -> same value.
        Groups identical lists without dropping any: a list that 5-0s twice counts twice."""

        def part(cards: list[Card]) -> str:
            return "|".join(sorted(f"{c.name.lower()}:{c.qty}" for c in cards))

        return hashlib.sha1(f"{part(self.main)}||{part(self.side)}".encode()).hexdigest()[:12]

    def to_text(self) -> str:
        """MTGO .txt: main, blank line, sideboard — readable by `riffle own`."""
        lines = [f"{c.qty} {c.name}" for c in self.main]
        if self.side:
            lines += [""] + [f"{c.qty} {c.name}" for c in self.side]
        return "\n".join(lines) + "\n"


@dataclass
class Event:
    slug: str
    event_id: str
    name: str  # "modern-challenge-32"
    format: str  # "modern"
    kind: str  # one of KINDS
    date: str  # ISO date
    decks: list[MtgoDeck] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict) -> "Event":
        decks = [
            MtgoDeck(
                player=x["player"],
                main=[Card(**c) for c in x["main"]],
                side=[Card(**c) for c in x["side"]],
                rank=x.get("rank"),
                record=x.get("record"),
            )
            for x in d["decks"]
        ]
        return cls(**{**d, "decks": decks})


# ---- slugs and pages -----------------------------------------------------------


def parse_slug(slug: str) -> tuple[str, str, str] | None:
    """(event name, ISO date, event id), or None if it isn't an event slug. A name whose
    date isn't a day isn't one: mtgo.com has listed a league dated 31 September."""
    m = _SLUG.match(slug)
    if m is None:
        return None
    try:
        date.fromisoformat(m["date"])
    except ValueError:
        return None
    return m["name"], m["date"], m["id"]


def classify(name: str) -> str:
    for kind in ("league", "showcase", "qualifier", "preliminary", "challenge"):
        if kind in name:
            return kind
    return "other"


def event_format(name: str) -> str:
    """Format from the event name: the part before the event type."""
    parts = name.split("-")
    for i, p in enumerate(parts):
        if p in {"league", "challenge", "showcase", "qualifier", "preliminary", "super", "last", "chance"}:
            return "-".join(parts[:i]) or parts[0]
    return parts[0]


def event_slugs(index_html: str) -> list[str]:
    """Event slugs linked from a monthly index page, in page order, deduplicated."""
    return list(dict.fromkeys(s.lower() for s in _LINK.findall(index_html) if parse_slug(s.lower())))


def undated_slugs(index_html: str) -> list[str]:
    """Links on an index page shaped like an event's but dated a day that doesn't exist."""
    links = dict.fromkeys(s.lower() for s in _LINK.findall(index_html))
    return [s for s in links if _SLUG.match(s) and parse_slug(s) is None]


def extract_data(page_html: str) -> dict:
    m = _DATA.search(page_html)
    if not m:
        raise ValueError("no decklist data on page — mtgo.com's layout may have changed")
    obj, _ = json.JSONDecoder().raw_decode(page_html, m.end())
    return obj


def _cards(
    main_rows: Iterable[dict] | None,
    side_rows: Iterable[dict] | None,
) -> tuple[list[Card], list[Card]]:
    """MTGO lists a card once per printing; merge by name, keep first-seen order.
    A row flagged "sideboard": "true" goes to the sideboard whichever list it's in."""
    boards: dict[str, dict[str, int]] = {"main": {}, "side": {}}
    for default, rows in (("main", main_rows), ("side", side_rows)):
        for r in rows or []:
            attrs = r.get("card_attributes") or {}
            name = attrs.get("card_name") or r.get("card_name")
            if not name:
                continue
            board = boards["side" if str(r.get("sideboard", "")).lower() == "true" else default]
            board[name] = board.get(name, 0) + int(r.get("qty") or r.get("quantity") or 0)
    return ([Card(n, q) for n, q in boards["main"].items()], [Card(n, q) for n, q in boards["side"].items()])


def _record(d: dict, kind: str) -> str | None:
    w = d.get("wins")
    if isinstance(w, dict) and w.get("wins") is not None:
        return f"{w['wins']}-{w.get('losses', 0)}"
    return "5-0" if kind == "league" else None  # MTGO only publishes 5-0 league lists


def parse_event(slug: str, data: dict) -> Event:
    parsed = parse_slug(slug)
    if not parsed:
        raise ValueError(f"not an event slug: {slug}")
    name, day, event_id = parsed
    kind = classify(name)

    ranks: dict[str, int] = {}
    for s in data.get("standings") or []:
        rank = s.get("rank")
        if rank is None:
            continue
        for key in (s.get("loginid"), s.get("login_name")):
            if key is not None:
                ranks[str(key).lower()] = int(rank)

    decks = []
    for d in data.get("decklists") or []:
        player = d.get("player") or d.get("login_name") or "?"
        main, side = _cards(d.get("main_deck"), d.get("sideboard_deck"))
        rank = ranks.get(str(d.get("loginid")).lower()) or ranks.get(player.lower())
        decks.append(MtgoDeck(player, main, side, rank, _record(d, kind)))
    decks.sort(key=lambda x: (x.rank is None, x.rank or 0))
    return Event(slug, event_id, name, event_format(name), kind, day, decks)


# ---- storage -------------------------------------------------------------------


def store_dir() -> Path:
    return data_dir() / "mtgo"


def event_path(slug: str) -> Path:
    """Where an event is kept: under its year and month, so a command that reads recent
    events lists only their months' folders."""
    parsed = parse_slug(slug)
    if parsed is None:
        return store_dir() / f"{slug}.json"
    return store_dir() / parsed[1][:4] / parsed[1][5:7] / f"{slug}.json"


def stored_path(slug: str) -> Path:
    """The event's file: under its month, or in the store's top folder, where earlier
    builds kept every event until the next trickle run moves it."""
    path, top = event_path(slug), store_dir() / f"{slug}.json"
    return top if not path.exists() and top.exists() else path


def save(event: Event) -> Path:
    """Whole or not at all: a run cut off mid-write leaves no event file."""
    path = event_path(event.slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part")
    tmp.write_text(json.dumps(asdict(event), indent=1), encoding="utf-8")
    tmp.replace(path)
    return path


def _event_files(folder: Path, since: date | None) -> Iterator[Path]:
    """Every event file in the store, skipping the months before since: the top folder's,
    kept there by earlier builds, then each month's."""
    if not folder.exists():
        return
    yield from folder.glob("*.json")
    first = since.isoformat()[:7] if since else ""
    for month in sorted(folder.glob("[0-9][0-9][0-9][0-9]/[0-9][0-9]")):
        if f"{month.parent.name}-{month.name}" >= first:
            yield from month.glob("*.json")


def _file_events() -> int:
    """Events earlier builds kept in the store's top folder, moved under their months. A
    rename: nothing is rewritten, and a file whose name isn't an event's stays."""
    moved = 0
    for path in sorted(store_dir().glob("*.json")) if store_dir().exists() else ():
        dest = event_path(path.stem)
        if parse_slug(path.stem) is None or dest.exists():
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        path.replace(dest)
        moved += 1
    return moved


def load(
    fmt: str | Iterable[str] | None = None,
    since: date | None = None,
    kinds: Iterable[str] | None = None,
    folder: Path | None = None,
) -> list[Event]:
    """Stored events, newest first."""
    folder = folder or store_dir()
    kinds = set(kinds or KINDS)
    fmts = {fmt} if isinstance(fmt, str) else set(fmt or ())
    events = []
    for p in _event_files(folder, since):
        e = Event.from_dict(json.loads(p.read_text(encoding="utf-8")))
        if not e.decks:
            continue
        if fmts and e.format not in fmts:
            continue
        if since and e.date < since.isoformat():
            continue
        if e.kind in kinds:
            events.append(e)
    return sorted(events, key=lambda e: (e.date, e.event_id), reverse=True)


def raw_path(slug: str) -> Path:
    """The page's whole data object, kept apart from the parsed events: load() reads
    every .json under the months' folders."""
    return store_dir() / "raw" / f"{slug}.json.gz"


def save_raw(slug: str, data: dict, url: str, fetched: datetime, size: int) -> Path:
    """Standings, records, player IDs and per-printing rows, as mtgo.com gave them."""
    path = raw_path(slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part")
    payload = {"url": url, "fetched": trickle.stamp(fetched), "bytes": size, "data": data}
    with gzip.open(tmp, "wt", encoding="utf-8") as f:
        json.dump(payload, f)
    tmp.replace(path)
    return path


def is_stored(slug: str) -> bool:
    """Stored with decks. An empty event was saved before its lists were
    published (older versions did this) — it counts as not stored."""
    path = stored_path(slug)
    if not path.exists():
        return False
    try:
        return bool(json.loads(path.read_text(encoding="utf-8")).get("decks"))
    except (OSError, ValueError):
        return False


# ---- the trickle ---------------------------------------------------------------
# mtgo.com builds a page when it's first asked for. A build that takes more than about
# 30 seconds answers with a page without lists, or a redirect to /decklists, and the page
# is ready for the next request, for under an hour. So a page that fails is asked again
# at each of the next two runs (riffle.trickle's rounds), an index that lists nothing is
# a failed try like any other, and only a 429, a 403 or a Retry-After pauses the job.
# An empty answer on a retry in its round is believed only when a page fetched whole in
# the last half hour (the canary) comes back whole again.

SOURCE = "mtgo"
PENDING_DAYS = 3  # an empty page for an event younger than this is waiting for its lists
NEW_MONTH_DAYS = 2  # only a month this new may list no events
REREAD = timedelta(hours=1)  # how often the current month's index is read again
RECENT_MONTH = timedelta(days=7)  # a month that ended this recently is read as often
SWEEP_END = 3  # months in a row listing no events end the sweep back through the indexes,
EMPTY_DAYS = 3  # once each has listed nothing on this many different days
PROBE = timedelta(days=7)  # after that, the next older month is still asked this often
CANARY_AGE = timedelta(minutes=30)  # a canary is a page fetched whole this recently
GAP = 5.0  # seconds between requests in a run
TIMEOUT = 60.0  # seconds, any page: an old month's index, like a league, runs to 300 KB and comes slowly
WHOLE = ("whole", "canary whole")  # the request log's verdicts for an answer with its lists
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def _get(url: str) -> net.Answer:
    """One request."""
    return net.get_once(url, accept="text/html", timeout=TIMEOUT)


def misses_path() -> Path:
    """Where builds before the trickle kept old events they had given up on. The first
    trickle run carries its counts into the owed list and deletes it."""
    return data_dir() / "mtgo-misses.json"


def months_path() -> Path:
    """Each month's index: when it was last read whole and how many events it listed;
    for one not read whole yet, its failed tries (an owed entry) and the days it was
    believed to list nothing."""
    return data_dir() / "mtgo-months.json"


def ages_path() -> Path:
    """For each kind of event, the youngest age, in hours from the start of its date
    (UTC), at which one came back whole. No event is asked for younger. Since none is,
    the age is the earliest seen, not the earliest possible: it can't fall."""
    return data_dir() / "mtgo-ages.json"


def _key(y: int, m: int) -> str:
    return f"{y}-{m:02d}"


def _prev(y: int, m: int) -> tuple[int, int]:
    return (y - 1, 12) if m == 1 else (y, m - 1)


def _month_end(y: int, m: int) -> date:
    return (date(y + 1, 1, 1) if m == 12 else date(y, m + 1, 1)) - timedelta(days=1)


def _whole(seen: dict) -> bool:
    return bool(seen.get("events"))


def _miss(seen: dict) -> trickle.Owed | None:
    """A month's failed tries, while it isn't read whole."""
    miss = seen.get("miss")
    try:
        return trickle.Owed(**miss) if isinstance(miss, dict) else None
    except TypeError:
        return None


def _settled(seen: dict) -> bool:
    """Believed to list no events: nothing listed on EMPTY_DAYS different days."""
    return not _whole(seen) and len(seen.get("empty_days") or ()) >= EMPTY_DAYS


def _last_asked(seen: dict) -> datetime | None:
    miss = _miss(seen)
    asked = [t for t in (seen.get("read"), miss.last_try if miss else None) if t]
    return max((datetime.fromisoformat(t) for t in asked), default=None)


def next_index(months: dict[str, dict], at: datetime) -> tuple[int, int] | None:
    """The one index page a run reads, if any: the current month, or one that ended
    within RECENT_MONTH, when last asked REREAD ago or more, or failed in a round not
    over yet; else an older month whose index failed and is due again; else the sweep's
    next month."""
    current = (at.year, at.month)
    previous = _prev(*current)
    ended = datetime(at.year, at.month, 1, tzinfo=UTC)
    near = [current] if at - ended > RECENT_MONTH else [current, previous]
    for ym in near:
        seen = months.get(_key(*ym))
        if seen is None or (miss := _miss(seen)) is not None and miss.warm():
            return ym
        last = _last_asked(seen)
        if last is None or at - last >= REREAD:
            return ym
    return _month_retry(months, at, {_key(*ym) for ym in near}) or sweep_next(months, at)


def _month_retry(months: dict[str, dict], at: datetime, near: set[str]) -> tuple[int, int] | None:
    """An older month whose index failed and is due again, as an owed event would be: one
    in its round first, then the newest. A month believed empty isn't retried here."""
    ready = []
    for key, seen in months.items():
        miss = _miss(seen)
        if key in near or miss is None or _whole(seen) or _settled(seen):
            continue
        try:
            if miss.due(at):
                ready.append((miss.ready(at), key))
        except ValueError:
            continue
    if not ready:
        return None
    key = max(ready)[1]
    return int(key[:4]), int(key[5:7])


def sweep_next(months: dict[str, dict], at: datetime) -> tuple[int, int] | None:
    """The month the sweep back through the indexes asks next, if any."""
    return _sweep(months, at)[0]


def _sweep(months: dict[str, dict], at: datetime) -> tuple[tuple[int, int] | None, bool]:
    """The sweep back from last month: (the month it asks next, if any; whether it's done).
    It asks the newest month never asked. SWEEP_END months in a row that list no events
    stop it: until each is believed empty it waits for their retries; then it's done,
    and the next older month is still asked once every PROBE, the sweep going on past
    it if it ever lists events."""
    run, unsettled, ym = 0, False, _prev(at.year, at.month)
    while True:
        seen = months.get(_key(*ym))
        if seen is not None and _whole(seen):
            run, unsettled = 0, False
        elif run < SWEEP_END:
            if seen is None:
                return ym, False
            run, unsettled = run + 1, unsettled or not _settled(seen)
        elif unsettled:
            return None, False
        else:
            last = None if seen is None else _last_asked(seen)
            return (ym if last is None or at - last >= PROBE else None), True
        ym = _prev(*ym)


def _forget_empty(months: dict[str, dict]) -> list[str]:
    """Months an earlier build saved as listing no events once they were past their first
    NEW_MONTH_DAYS: most were mtgo.com's time limit, not an empty month. Forgotten, so
    each is read again; newest first."""
    gone = []
    for key, seen in months.items():
        try:
            read = date.fromisoformat(seen["read"][:10])
            young = (read - date(int(key[:4]), int(key[5:7]), 1)).days < NEW_MONTH_DAYS
        except (KeyError, TypeError, ValueError):
            continue
        if seen.get("events") == 0 and not young:
            gone.append(key)
    for key in gone:
        del months[key]
    return sorted(gone, reverse=True)


def _decklists(body: bytes) -> dict | None:
    """The page's data object when it holds lists; None for a page without them."""
    try:
        data = extract_data(body.decode("utf-8", errors="replace"))
    except ValueError:
        return None
    return data if isinstance(data, dict) and data.get("decklists") else None


def _moved(asked: str, answered: str) -> bool:
    return answered.rstrip("/").lower() != asked.rstrip("/").lower()


def _event_id(slug: str) -> int:
    parsed = parse_slug(slug)
    return int(parsed[2]) if parsed else 0


def _slug_of(url: str) -> str | None:
    """The event an event page's URL names; None for any other page."""
    prefix = f"{BASE}/decklist/"
    return url[len(prefix) :] if url.startswith(prefix) else None


def _learn(ages: dict[str, float], slug: str, at: datetime) -> None:
    """An event fetched whole at at: its age, in hours from the start of its date (UTC),
    is its kind's when it's the youngest yet. Only an event under PENDING_DAYS old says
    when its kind is ready: one fetched from the backlog would teach every newer event
    to wait as long."""
    parsed = parse_slug(slug)
    if parsed is None:
        return
    kind = classify(parsed[0])
    hours = (at - datetime.fromisoformat(parsed[1]).replace(tzinfo=UTC)) / timedelta(hours=1)
    if hours < PENDING_DAYS * 24 and (kind not in ages or hours < ages[kind]):
        ages[kind] = round(hours, 2)


def _read_ages(log: trickle.RequestLog) -> dict[str, float]:
    """The learned ages. Without a file, learned from every event page the request log
    has whole: the same fetch times the stored events' raw files hold."""
    try:
        saved = json.loads(ages_path().read_text(encoding="utf-8"))
        return {str(kind): float(hours) for kind, hours in saved.items()}
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    ages: dict[str, float] = {}
    for r in log.since(EPOCH):
        slug = _slug_of(r.url) if r.verdict == "whole" else None
        if slug:
            _learn(ages, slug, datetime.fromisoformat(r.at))
    return ages


def _too_young(slug: str, day: str, ages: dict[str, float], at: datetime) -> bool:
    """Younger than the age at which events of its kind have ever come back whole."""
    parsed = parse_slug(slug)
    if parsed is None or (kind := classify(parsed[0])) not in ages:
        return False
    start = datetime.fromisoformat(day).replace(tzinfo=UTC)
    return (at - start) / timedelta(hours=1) < ages[kind]


def waiting(owed: dict[str, trickle.Owed], ages: dict[str, float], at: datetime) -> int:
    """Owed events too young to ask for yet."""
    count = 0
    for slug, o in owed.items():
        try:
            count += _too_young(slug, o.day, ages, at)
        except ValueError:
            continue
    return count


def _in_turn(slugs: list[str], oldest_first: bool) -> list[str]:
    """Slugs given newest first, taken from each end in turn."""
    out, lo, hi, old = [], 0, len(slugs) - 1, oldest_first
    while lo <= hi:
        if old:
            out.append(slugs[hi])
            hi -= 1
        else:
            out.append(slugs[lo])
            lo += 1
        old = not old
    return out


def due(
    owed: dict[str, trickle.Owed],
    at: datetime,
    undue: list[tuple[str, Exception]] | None = None,
    ages: dict[str, float] | None = None,
    oldest_first: bool | None = None,
) -> list[str]:
    """Owed events due a try, in the order a run asks for them: those in a round not over
    yet whose page was asked for within trickle.READY, and so is ready, newest first; then those
    never asked for, newest first, or, when oldest_first isn't None, newest and oldest in
    turn, from the oldest when it's True; then retries after their round, newest first,
    so one that keeps missing can't hold up the rest. One younger than the age at which
    its kind has ever come back whole (ages) waits, at no request. An event that can't
    say when it's due is left out and added to undue, so one bad entry doesn't stop the
    others."""
    warm: list[str] = []
    never: list[str] = []
    cold: list[str] = []
    for slug, o in owed.items():
        try:
            if not o.due(at) or _too_young(slug, o.day, ages or {}, at):
                continue
        except Exception as e:
            if undue is not None:
                undue.append((slug, e))
            continue
        (never if o.last_try is None else warm if o.ready(at) else cold).append(slug)

    def newest(slugs: list[str]) -> list[str]:
        return sorted(slugs, key=lambda s: (owed[s].day, _event_id(s)), reverse=True)

    never = newest(never)
    if oldest_first is not None:
        never = _in_turn(never, oldest_first)
    return newest(warm) + never + newest(cold)


def next_try(at: datetime, when: datetime | None) -> str:
    """When an owed page is asked for again, said from at."""
    if when is None or when <= at:
        return "asked again at the next run"
    hours = round((when - at) / timedelta(hours=1))
    if hours <= 1:
        return "next try in an hour"
    return f"next try in {hours} hours" if hours < 48 else f"next try in {round(hours / 24)} days"


def _lock() -> AbstractContextManager[bool]:
    """Whether this run holds the trickle: the job and a run by hand never overlap."""
    return locks.held(data_dir() / "mtgo-trickle.lock")


@dataclass
class TrickleResult:
    busy: bool = False  # another run held the trickle
    paused_until: datetime | None = None  # paused by an earlier throttle: nothing asked
    why: str = ""  # what mtgo.com said, for that pause
    level: int = 0  # pages a run, after this one
    budget: int = 0  # pages this run could fetch
    index: str | None = None  # the month whose index it read
    index_outcome: str = ""  # what that index came to
    listed: int = 0  # events that index listed
    newly_owed: int = 0
    fetched: list[Event] = field(default_factory=list)
    retried: int = 0  # of those, whole on a retry in their round
    pending: list[str] = field(default_factory=list)  # young; lists not published yet
    # (slug, outcome, next try; None while its round lasts): still owed
    missed: list[tuple[str, str, datetime | None]] = field(default_factory=list)
    throttled: str | None = None  # what mtgo.com said, asking for a pause
    resume: datetime | None = None  # when the pause that throttle started ends
    stopped: str | None = None  # why the run ended early otherwise
    broken: list[tuple[str, str]] = field(default_factory=list)  # lists that wouldn't parse
    carried: int = 0  # events the old misses file handed to the owed list
    raised: bool = False  # the pace rose a level
    set_aside: list[str] = field(default_factory=list)  # owed pages whose name holds no real date
    undated: list[str] = field(default_factory=list)  # names that index listed with no real date
    undue: list[tuple[str, Exception]] = field(default_factory=list)  # can't say when they're due
    moved: int = 0  # stored events moved under their months
    forgotten: list[str] = field(default_factory=list)  # months saved as empty, to be read again
    owed: int = 0
    due: int = 0
    waiting: int = 0  # owed, too young to ask


class _Stop(Exception):
    """Ends a run early: throttled, no answer, a canary that failed, or the ceiling."""


class _NoAnswer(_Stop):
    """No answer at all: the page asked for counts it as a failed try."""


@dataclass
class _Trickle:
    get: Callable[[str], net.Answer]
    clock: Callable[[], datetime]
    sleep: Callable[[float], None]
    log: trickle.RequestLog
    pace: trickle.Pace
    owed: dict[str, trickle.Owed]
    months: dict[str, dict]
    ages: dict[str, float]
    res: TrickleResult
    oldest_first: bool | None = None  # never-asked events in turn, from this end; None: newest first
    room: int = 0  # requests the ceiling leaves this run
    asked: int = 0
    failed: bool = False  # an answer this run wasn't whole

    def record(self, url: str, answer: net.Answer | None, ms: int, verdict: str, note: str = "") -> None:
        status, size = (answer.status, len(answer.body)) if answer else (None, 0)
        self.failed |= verdict not in WHOLE
        self.log.add(trickle.Request(trickle.stamp(self.clock()), url, status, size, ms, verdict, note))

    def fetch(self, url: str) -> tuple[net.Answer, int]:
        """One request, GAP after the last, while the ceiling has room. A 429, a 403, or
        any answer with a Retry-After pauses the trickle; no answer or a 5xx ends the
        run, and the next one tries again."""
        if self.asked >= self.room:
            raise _Stop("the last 15 minutes hold as many requests as allowed")
        if self.asked:
            self.sleep(GAP)
        self.asked += 1
        start = time.monotonic()
        try:
            answer = self.get(url)
        except net.FetchError as e:
            self.record(url, None, int((time.monotonic() - start) * 1000), "error", str(e))
            raise _NoAnswer(f"no answer from {url}: {e}") from e
        ms = int((time.monotonic() - start) * 1000)
        if answer.status in (429, 403) or answer.retry_after is not None:
            asked = f", Retry-After {answer.retry_after}" if answer.retry_after is not None else ""
            self.record(url, answer, ms, "throttled", asked.removeprefix(", "))
            wait = trickle.wait_asked(answer.retry_after, self.clock())
            self.throttle(f"mtgo.com answered {answer.status}{asked}", wait)
        if answer.status not in (200, 404):
            self.record(url, answer, ms, "error", f"HTTP {answer.status}")
            raise _Stop(f"{url} answered HTTP {answer.status}")
        return answer, ms

    def throttle(self, why: str, wait: timedelta | None) -> NoReturn:
        self.res.throttled, self.res.resume = why, self.pace.throttled(self.clock(), why, wait)
        raise _Stop(why)

    def canary(self, suspect: str | None) -> str | None:
        """The page fetched whole most recently in the last CANARY_AGE, not today's event,
        which mtgo.com may still be adding lists to."""
        at = self.clock()
        today = at.date().isoformat()
        for r in reversed(self.log.since(at - CANARY_AGE)):
            slug = _slug_of(r.url) if r.verdict in WHOLE else None
            parsed = parse_slug(slug) if slug else None
            if parsed and parsed[1] < today and slug != suspect:
                return slug
        return None

    def canary_whole(self, suspect: str | None) -> bool:
        """Whether an empty answer is believed: the canary, asked for again, came back
        whole. False when there's none to ask, or no room under the ceiling. A canary
        that fails ends the run, and nothing counts against the page."""
        slug = self.canary(suspect)
        if slug is None or self.asked >= self.room:
            return False
        url = f"{BASE}/decklist/{slug}"
        answer, ms = self.fetch(url)
        if answer.status == 200 and _moved(url, answer.url):
            self.record(url, answer, ms, "canary redirect", answer.url)
            raise _Stop(f"the canary {slug} was redirected to {answer.url}; nothing counted")
        if answer.status != 200 or _decklists(answer.body) is None:
            self.record(url, answer, ms, "canary stripped")
            raise _Stop(f"the canary {slug} came back without its lists; nothing counted")
        self.record(url, answer, ms, "canary whole")
        return True

    def index(self, y: int, m: int) -> None:
        """A month's index. Every event it lists that isn't stored joins the owed list. One
        that lists nothing, or answers from another page, is a failed try, retried like an
        event, unless the month is under NEW_MONTH_DAYS old. Listing nothing counts as a
        day believed empty on a 404, or on a retry in its round with the canary whole."""
        key, url = _key(y, m), f"{BASE}/decklists/{y}/{m:02d}"
        self.res.index = key
        found = trickle.stamp(self.clock())
        miss = _miss(self.months.get(key, {})) or trickle.Owed(day=_month_end(y, m).isoformat(), found=found)
        warm = miss.ready(self.clock())
        try:
            answer, ms = self.fetch(url)
        except _NoAnswer:
            self.month_failed(key, miss, "no answer")
            raise
        if answer.status == 200 and _moved(url, answer.url):
            self.record(url, answer, ms, "redirect", answer.url)
            self.month_failed(key, miss, "redirect")
            return
        page = answer.body.decode("utf-8", errors="replace") if answer.status == 200 else ""
        slugs, self.res.undated = event_slugs(page), undated_slugs(page)
        if not slugs:
            outcome = "missing" if answer.status == 404 else "empty"
            self.record(url, answer, ms, outcome)
            if answer.status == 200 and (self.clock().date() - date(y, m, 1)).days < NEW_MONTH_DAYS:
                self.months[key] = {"read": trickle.stamp(self.clock()), "events": 0}
                self.res.index_outcome = "no events yet"
                return
            self.month_failed(key, miss, outcome)
            if answer.status == 404 or (warm and self.canary_whole(None)):
                seen = self.months[key]
                seen["miss"]["tries"] += 1
                seen["empty_days"] = sorted({*seen.get("empty_days", ()), self.clock().date().isoformat()})
            return
        self.record(url, answer, ms, "whole", f"{len(slugs)} events")
        at = self.clock()
        self.months[key] = {"read": trickle.stamp(at), "events": len(slugs)}
        if self.res.undated:
            self.months[key]["undated"] = self.res.undated
        self.res.listed = len(slugs)
        for slug in slugs:
            parsed = parse_slug(slug)
            if parsed and slug not in self.owed and not is_stored(slug):
                self.owed[slug] = trickle.Owed(day=parsed[1], found=trickle.stamp(at))
                self.res.newly_owed += 1
        self.res.index_outcome = f"{len(slugs)} events, {self.res.newly_owed} newly owed"

    def month_failed(self, key: str, miss: trickle.Owed, outcome: str) -> None:
        """A failed try at a month's index, kept with whatever it was read as before."""
        at = self.clock()
        miss.tried(at, outcome, miss=False)
        self.months[key] = {**self.months.get(key, {}), "miss": asdict(miss)}
        self.res.index_outcome = f"{outcome}; {next_try(at, miss.retry_at())}"

    def event(self, slug: str) -> None:
        """One owed event: stored and paid off, or tried and still owed."""
        owed, url = self.owed[slug], f"{BASE}/decklist/{slug}"
        warm = owed.ready(self.clock())
        try:
            answer, ms = self.fetch(url)
        except _NoAnswer:
            owed.tried(self.clock(), "no answer", miss=False)
            raise
        if answer.status == 404 or _moved(url, answer.url):
            outcome = "missing" if answer.status == 404 else "redirect"
            self.record(url, answer, ms, outcome, "" if outcome == "missing" else answer.url)
            owed.tried(self.clock(), outcome, miss=True)
            self.missed(slug, outcome)
            return
        data = _decklists(answer.body)
        if data is not None:
            try:
                event = parse_event(slug, data)
            except (ValueError, KeyError, TypeError, AttributeError) as e:
                self.record(url, answer, ms, "unparseable", str(e))
                owed.tried(self.clock(), "unparseable", miss=False)
                self.res.broken.append((slug, str(e) or type(e).__name__))
                return
            self.record(url, answer, ms, "whole", f"{len(event.decks)} decks")
            save(event)
            save_raw(slug, data, url, self.clock(), len(answer.body))
            del self.owed[slug]
            self.res.fetched.append(event)
            self.res.retried += warm
            _learn(self.ages, slug, self.clock())
            return
        self.record(url, answer, ms, "empty")
        if (self.clock().date() - date.fromisoformat(owed.day)).days < PENDING_DAYS:
            owed.tried(self.clock(), "not published yet", miss=False)
            self.res.pending.append(slug)
            return
        owed.tried(self.clock(), "empty", miss=False)
        self.missed(slug, "empty")
        if warm and self.canary_whole(slug):
            owed.tries += 1

    def missed(self, slug: str, outcome: str) -> None:
        owed = self.owed[slug]
        self.res.missed.append((slug, outcome, None if owed.warm() else owed.retry_at()))


def _carry_misses(owed: dict[str, trickle.Owed], at: datetime) -> int:
    """The old misses file's events, into the owed list with their counts as tries."""
    path = misses_path()
    if not path.exists():
        return 0
    try:
        misses = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        misses = {}
    carried = 0
    for slug, count in misses.items() if isinstance(misses, dict) else ():
        parsed = parse_slug(slug)
        if parsed is None or is_stored(slug):
            continue
        entry = owed.setdefault(slug, trickle.Owed(day=parsed[1], found=trickle.stamp(at)))
        entry.tries = max(entry.tries, count if isinstance(count, int) else 0)
        entry.last = entry.last or "empty"
        carried += 1
    trickle.save_owed(SOURCE, owed)
    path.unlink()
    return carried


def _set_aside_undated(owed: dict[str, trickle.Owed], at: datetime) -> list[str]:
    """Owed pages whose name isn't an event's leave the owed list for the set-aside file:
    kept, and never asked for. Written there before it leaves the list."""
    gone = [slug for slug in owed if parse_slug(slug) is None]
    for slug in gone:
        trickle.set_aside(SOURCE, slug, owed[slug], "its name holds no real date", at)
        del owed[slug]
    return gone


def _read_months() -> dict[str, dict]:
    try:
        months = json.loads(months_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return months if isinstance(months, dict) else {}


def _save_json(path: Path, value: dict) -> None:
    """Whole or not at all: a run cut off mid-write leaves the old file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part")
    tmp.write_text(json.dumps(dict(sorted(value.items())), indent=1) + "\n", encoding="utf-8")
    tmp.replace(path)


def _save_months(months: dict[str, dict]) -> None:
    _save_json(months_path(), months)


def run_trickle(
    get: Callable[[str], net.Answer] = _get,
    clock: Callable[[], datetime] = trickle.now,
    sleep: Callable[[float], None] = time.sleep,
    tracker: Tracker = SILENT,
) -> TrickleResult:
    """One run: what the job does every 10 minutes. Paused, it asks for nothing; otherwise
    at most one index page, then owed events that are due (see due), as many as the pace
    allows. While the sweep back through the indexes goes on, the events never asked for
    are taken newest and oldest in turn, each run starting from the other end."""
    with _lock() as held:
        if not held:
            return TrickleResult(busy=True)
        at = clock()
        owed = trickle.load_owed(SOURCE)
        res = TrickleResult(set_aside=_set_aside_undated(owed, at), carried=_carry_misses(owed, at))
        res.moved = _file_events()
        log = trickle.RequestLog(SOURCE)
        pace, months, ages = trickle.load_pace(SOURCE), _read_months(), _read_ages(log)
        res.forgotten = _forget_empty(months)
        sweeping = not _sweep(months, at)[1]
        start = pace.oldest_first if sweeping else None
        run = _Trickle(get, clock, sleep, log, pace, owed, months, ages, res, start)
        try:
            res.paused_until = pace.paused(at)
            if res.paused_until is not None:
                res.why = pace.why
            else:
                res.budget = pace.budget(log, at)
                run.room = trickle.CEILING - log.count(at - trickle.WINDOW)
                _pages(run, tracker)
                pace.oldest_first = not pace.oldest_first
                if run.asked and not run.failed:
                    res.raised = pace.all_whole()
        finally:
            trickle.save_owed(SOURCE, owed)
            trickle.save_pace(SOURCE, pace)
            _save_months(months)
            _save_json(ages_path(), ages)
            undue: list[tuple[str, Exception]] = []
            now = clock()
            res.level, res.owed, res.due = pace.level, len(owed), len(due(owed, now, undue, ages))
            res.waiting = waiting(owed, ages, now)
            res.undue = res.undue or undue
            if res.undue:
                step = tracker.step("mtgo owed list")
                step.fail(f"{len(res.undue)} owed events can't say when they're due")
        return res


def _pages(run: _Trickle, tracker: Tracker) -> None:
    """The run's requests, as progress steps. A throttle, a missing answer or a canary
    that fails defers the rest to the next run; only lists that won't parse fail a step."""
    res, left = run.res, run.res.budget
    step = None
    try:
        if left and (ym := next_index(run.months, run.clock())) is not None:
            step = tracker.step(f"mtgo.com index {_key(*ym)}")
            run.index(*ym)
            step.ok(res.index_outcome)
            left -= 1
        todo = due(run.owed, run.clock(), res.undue, run.ages, run.oldest_first)[:left]
        if todo:
            step = tracker.step("mtgo events", total=len(todo), unit="events")
            for n, slug in enumerate(todo, 1):
                run.event(slug)
                step.update(n)
            retried = f" ({res.retried} on a retry)" if res.retried else ""
            pending, missed = len(res.pending), len(res.missed)
            outcome = f"{len(res.fetched)} new{retried}, {pending} not published yet, {missed} missed"
            if res.broken:
                step.fail(f"{outcome}, {len(res.broken)} unreadable")
            else:
                step.ok(outcome)
    except _Stop as e:
        if res.throttled is None:
            res.stopped = str(e)
        if step is not None:
            step.ok(f"deferred: {e}")


def forget(slug: str) -> bool:
    """Drop one event from the owed list by hand. False when it wasn't owed."""
    with _lock() as held:
        if not held:
            raise RuntimeError("a trickle run is in progress; try again in a minute")
        owed = trickle.load_owed(SOURCE)
        if owed.pop(slug, None) is None:
            return False
        trickle.save_owed(SOURCE, owed)
        return True


@dataclass
class TrickleStatus:
    at: datetime
    pace: trickle.Pace
    paused_until: datetime | None
    window: list[trickle.Request]  # the last trickle.WINDOW
    day: list[trickle.Request]  # the last 24 hours
    owed: dict[str, trickle.Owed]
    months: dict[str, dict]
    sweep_done: bool
    ages: dict[str, float] = field(default_factory=dict)  # by kind, hours: see ages_path
    waiting: int = 0  # owed, too young to ask


def status(clock: Callable[[], datetime] = trickle.now) -> TrickleStatus:
    at = clock()
    pace, log, owed, months = (
        trickle.load_pace(SOURCE),
        trickle.RequestLog(SOURCE),
        trickle.load_owed(SOURCE),
        _read_months(),
    )
    ages = _read_ages(log)
    day = log.since(at - timedelta(days=1))
    window = [r for r in day if r.at >= trickle.stamp(at - trickle.WINDOW)]
    done = bool(months) and _sweep(months, at)[1]
    return TrickleStatus(
        at, pace, pace.paused(at), window, day, owed, months, done, ages, waiting(owed, ages, at)
    )


def unread_months(months: dict[str, dict], at: datetime) -> list[tuple[str, trickle.Owed, int, str]]:
    """Months whose index failed and isn't read whole yet, newest first: (month, its
    tries, days believed empty, when it's asked again)."""
    out = []
    for key in sorted(months, reverse=True):
        seen = months[key]
        miss = _miss(seen)
        if miss is None or _whole(seen):
            continue
        days = len(seen.get("empty_days") or ())
        if _settled(seen):
            when = "not asked again"
        else:
            try:
                when = next_try(at, miss.retry_at())
            except ValueError:
                when = "can't say when it's due"
        out.append((key, miss, days, when))
    return out
