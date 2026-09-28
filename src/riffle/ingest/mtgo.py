"""MTGO decklists from mtgo.com: league 5-0s, challenges, showcases, qualifiers.

Event pages render client-side. The data is one JSON object assigned to
`window.MTGO.decklists.data` in a script tag, so we lift that out instead of
parsing HTML. The monthly index (/decklists/YYYY/MM) links every event as
/decklist/<slug>, where the slug ends in the date and event id:

    modern-challenge-32-2026-04-1812839681   -> 2026-04-18, event 12839681

Each event is fetched once and stored, normalized, as
<data_dir>/mtgo/<slug>.json, with the page's whole data object gzipped beside it.
Published lists don't change, so nothing stored is fetched again.

Fetching is a trickle: a job asks for a few pages every 10 minutes, all day, and
keeps a list of every event an index listed that isn't stored yet. Throttled,
mtgo.com doesn't refuse: it answers with stripped pages. How a run tells those
apart from missing data is described under the trickle below.
"""

import fcntl
import gzip
import hashlib
import json
import re
import time
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import NoReturn

from riffle import net, trickle
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
    """(event name, ISO date, event id), or None if it isn't an event slug."""
    m = _SLUG.match(slug)
    return (m["name"], m["date"], m["id"]) if m else None


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


def save(event: Event) -> Path:
    path = store_dir() / f"{event.slug}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(event), indent=1), encoding="utf-8")
    return path


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
    for p in folder.glob("*.json") if folder.exists() else []:
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
    """The page's whole data object, kept beside the parsed event. In a folder of its
    own: load() reads every .json in the store."""
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
    path = store_dir() / f"{slug}.json"
    if not path.exists():
        return False
    try:
        return bool(json.loads(path.read_text(encoding="utf-8")).get("decks"))
    except (OSError, ValueError):
        return False


# ---- the trickle ---------------------------------------------------------------
# mtgo.com throttles by request rate, and throttled it doesn't refuse: it answers 200
# with stripped pages, an index with no event links or an event page with no lists.
# So a job asks for a few pages every 10 minutes, all day (riffle.trickle sets the
# pace), and an empty answer is only believed after a stored event (the canary) comes
# back whole. Stripped too means throttled, and nothing counts against the event.

SOURCE = "mtgo"
PENDING_DAYS = 3  # an empty page for an event younger than this is waiting for its lists
NEW_MONTH_DAYS = 2  # only a month this new may list no events
REREAD = timedelta(hours=1)  # how often the current month's index is read again
RECENT_MONTH = timedelta(days=7)  # a month that ended this recently is read as often
SWEEP_END = 3  # months in a row listing no events end the sweep back through the indexes
GAP = 5.0  # seconds between requests in a run
INDEX_TIMEOUT = 20.0  # seconds; mtgo.com occasionally stalls instead of answering
EVENT_TIMEOUT = 60.0  # league pages run past 250 KB and come back slowly


def _get(url: str) -> net.Answer:
    """One request. Event pages get longer to answer than the monthly index."""
    timeout = EVENT_TIMEOUT if "/decklist/" in url else INDEX_TIMEOUT
    return net.get_once(url, accept="text/html", timeout=timeout)


def misses_path() -> Path:
    """Where builds before the trickle kept old events they had given up on. The first
    trickle run carries its counts into the owed list and deletes it."""
    return data_dir() / "mtgo-misses.json"


def months_path() -> Path:
    """Each month's index: when it was last read and how many events it listed."""
    return data_dir() / "mtgo-months.json"


def _key(y: int, m: int) -> str:
    return f"{y}-{m:02d}"


def _prev(y: int, m: int) -> tuple[int, int]:
    return (y - 1, 12) if m == 1 else (y, m - 1)


def next_index(months: dict[str, dict], at: datetime) -> tuple[int, int] | None:
    """The one index page a run reads, if any: the current month, or one that ended
    within RECENT_MONTH, when last read REREAD ago or more; else the sweep's next month,
    the newest never read, going back until SWEEP_END months in a row list nothing."""
    current = (at.year, at.month)
    previous = _prev(*current)
    ended = datetime(at.year, at.month, 1, tzinfo=UTC)
    for ym in (current, previous):
        if ym == previous and at - ended > RECENT_MONTH:
            continue
        seen = months.get(_key(*ym))
        if seen is None or at - datetime.fromisoformat(seen["read"]) >= REREAD:
            return ym
    return sweep_next(months, at)


def sweep_next(months: dict[str, dict], at: datetime) -> tuple[int, int] | None:
    """The newest month never read, going back from last month; None once SWEEP_END
    months in a row have listed no events."""
    empty, ym = 0, _prev(at.year, at.month)
    while (seen := months.get(_key(*ym))) is not None:
        empty = 0 if seen.get("events") else empty + 1
        if empty >= SWEEP_END:
            return None
        ym = _prev(*ym)
    return ym


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


def due(owed: dict[str, trickle.Owed], at: datetime) -> list[str]:
    """Owed events due a try: those never asked for, newest first, then retries, newest
    first. A retry gets only the pages new events leave, so one that keeps missing can't
    hold up the rest."""
    return sorted(
        (s for s, o in owed.items() if o.due(at)),
        key=lambda s: (owed[s].last_try is None, owed[s].day, _event_id(s)),
        reverse=True,
    )


def _canary(exclude: str | None) -> str | None:
    """The newest stored event: the page asked for again to tell a throttled site from
    an empty page."""
    stored = []
    for path in store_dir().glob("*.json") if store_dir().exists() else ():
        parsed = parse_slug(path.stem)
        if parsed and path.stem != exclude:
            stored.append((parsed[1], int(parsed[2]), path.stem))
    return max(stored)[2] if stored else None


@contextmanager
def _lock() -> Iterator[bool]:
    """Whether this run holds the trickle: the job and a run by hand never overlap."""
    path = data_dir() / "mtgo-trickle.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        yield True


@dataclass
class TrickleResult:
    busy: bool = False  # another run held the trickle
    paused_until: datetime | None = None  # paused by an earlier throttle: nothing asked
    level: int = 0  # pages a run, after this one
    budget: int = 0  # pages this run could fetch
    index: str | None = None  # the month whose index it read
    listed: int = 0  # events that index listed
    newly_owed: int = 0
    fetched: list[Event] = field(default_factory=list)
    pending: list[str] = field(default_factory=list)  # young; lists not published yet
    missed: list[tuple[str, str]] = field(default_factory=list)  # (slug, outcome): still owed
    throttled: str | None = None  # what showed the site throttling
    resume: datetime | None = None  # when the pause that throttle started ends
    stopped: str | None = None  # why the run ended early otherwise
    broken: list[tuple[str, str]] = field(default_factory=list)  # lists that wouldn't parse
    carried: int = 0  # events the old misses file handed to the owed list
    raised: bool = False  # the pace rose a level
    owed: int = 0
    due: int = 0


class _Stop(Exception):
    """Ends a run early: throttled, no answer, or no room left to check an empty answer."""


@dataclass
class _Trickle:
    get: Callable[[str], net.Answer]
    clock: Callable[[], datetime]
    sleep: Callable[[float], None]
    log: trickle.RequestLog
    pace: trickle.Pace
    owed: dict[str, trickle.Owed]
    months: dict[str, dict]
    res: TrickleResult
    asked: int = 0

    def record(self, url: str, answer: net.Answer | None, ms: int, verdict: str, note: str = "") -> None:
        status, size = (answer.status, len(answer.body)) if answer else (None, 0)
        self.log.add(trickle.Request(trickle.stamp(self.clock()), url, status, size, ms, verdict, note))

    def fetch(self, url: str) -> tuple[net.Answer, int]:
        """One request, GAP after the last. A 429 is a throttle; no answer or a 5xx ends
        the run, and the next one tries again."""
        if self.asked:
            self.sleep(GAP)
        self.asked += 1
        start = time.monotonic()
        try:
            answer = self.get(url)
        except net.FetchError as e:
            self.record(url, None, int((time.monotonic() - start) * 1000), "error", str(e))
            raise _Stop(f"no answer from {url}: {e}") from e
        ms = int((time.monotonic() - start) * 1000)
        if answer.status == 429:
            self.record(url, answer, ms, "throttled")
            self.throttle(f"{url} answered 429")
        if answer.status not in (200, 404):
            self.record(url, answer, ms, "error", f"HTTP {answer.status}")
            raise _Stop(f"{url} answered HTTP {answer.status}")
        return answer, ms

    def throttle(self, why: str) -> NoReturn:
        self.res.throttled, self.res.resume = why, self.pace.throttled(self.clock())
        raise _Stop(why)

    def canary_whole(self, suspect: str | None) -> bool:
        """Whether the site is answering whole: the newest stored event, asked for again."""
        slug = _canary(suspect)
        if slug is None:
            raise _Stop("nothing stored yet to check an empty answer against")
        if self.log.count(self.clock() - trickle.WINDOW) >= trickle.CEILING:
            raise _Stop("no room left under the ceiling to check an empty answer")
        url = f"{BASE}/decklist/{slug}"
        answer, ms = self.fetch(url)
        whole = answer.status == 200 and _decklists(answer.body) is not None
        self.record(url, answer, ms, "canary whole" if whole else "canary stripped")
        if whole:
            self.res.raised |= self.pace.whole()
        return whole

    def index(self, y: int, m: int) -> None:
        """A month's index. Every event it lists that isn't stored joins the owed list."""
        key, url = _key(y, m), f"{BASE}/decklists/{y}/{m:02d}"
        self.res.index = key
        answer, ms = self.fetch(url)
        slugs = event_slugs(answer.body.decode("utf-8", errors="replace")) if answer.status == 200 else []
        if not slugs:
            self.record(url, answer, ms, "missing" if answer.status == 404 else "empty")
            young = (self.clock().date() - date(y, m, 1)).days < NEW_MONTH_DAYS
            if answer.status == 200 and not young and not self.canary_whole(None):
                self.throttle(f"the {key} index listed no events, and a stored event came back stripped")
            self.months[key] = {"read": trickle.stamp(self.clock()), "events": 0}
            return
        self.record(url, answer, ms, "whole", f"{len(slugs)} events")
        self.res.raised |= self.pace.whole()
        at = self.clock()
        self.months[key] = {"read": trickle.stamp(at), "events": len(slugs)}
        self.res.listed = len(slugs)
        for slug in slugs:
            parsed = parse_slug(slug)
            if parsed and slug not in self.owed and not is_stored(slug):
                self.owed[slug] = trickle.Owed(day=parsed[1], found=trickle.stamp(at))
                self.res.newly_owed += 1

    def event(self, slug: str) -> None:
        """One owed event: stored and paid off, or tried and still owed."""
        owed, url = self.owed[slug], f"{BASE}/decklist/{slug}"
        answer, ms = self.fetch(url)
        if answer.status == 404 or _moved(url, answer.url):
            outcome = "missing" if answer.status == 404 else "redirect"
            self.record(url, answer, ms, outcome, "" if outcome == "missing" else answer.url)
            owed.tried(self.clock(), outcome, miss=True)
            self.res.missed.append((slug, outcome))
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
            self.res.raised |= self.pace.whole()
            return
        self.record(url, answer, ms, "empty")
        if (self.clock().date() - date.fromisoformat(owed.day)).days < PENDING_DAYS:
            owed.tried(self.clock(), "not published yet", miss=False)
            self.res.pending.append(slug)
            return
        if not self.canary_whole(slug):
            self.throttle(f"{slug} came back empty, and so did a stored event")
        owed.tried(self.clock(), "empty", miss=True)
        self.res.missed.append((slug, "empty"))


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


def _read_months() -> dict[str, dict]:
    try:
        months = json.loads(months_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return months if isinstance(months, dict) else {}


def _save_months(months: dict[str, dict]) -> None:
    path = months_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part")
    tmp.write_text(json.dumps(dict(sorted(months.items())), indent=1) + "\n", encoding="utf-8")
    tmp.replace(path)


def run_trickle(
    get: Callable[[str], net.Answer] = _get,
    clock: Callable[[], datetime] = trickle.now,
    sleep: Callable[[float], None] = time.sleep,
    tracker: Tracker = SILENT,
) -> TrickleResult:
    """One run: what the job does every 10 minutes. Paused after a throttle, it asks for
    nothing; otherwise at most one index page, then owed events that are due (never
    asked for first, then retries, each newest first), as many as the pace allows."""
    with _lock() as held:
        if not held:
            return TrickleResult(busy=True)
        at = clock()
        owed = trickle.load_owed(SOURCE)
        res = TrickleResult(carried=_carry_misses(owed, at))
        pace, months = trickle.load_pace(SOURCE), _read_months()
        run = _Trickle(get, clock, sleep, trickle.RequestLog(SOURCE), pace, owed, months, res)
        try:
            res.paused_until = pace.paused(at)
            if res.paused_until is None:
                res.budget = pace.budget(run.log, at)
                _pages(run, tracker)
        finally:
            trickle.save_owed(SOURCE, owed)
            trickle.save_pace(SOURCE, pace)
            _save_months(months)
            res.level, res.owed, res.due = pace.level, len(owed), len(due(owed, clock()))
        return res


def _pages(run: _Trickle, tracker: Tracker) -> None:
    """The run's requests, as progress steps. A throttle or a missing answer defers the
    rest to the next run; only lists that won't parse fail a step."""
    res, left = run.res, run.res.budget
    step = None
    try:
        if left and (ym := next_index(run.months, run.clock())) is not None:
            step = tracker.step(f"mtgo.com index {_key(*ym)}")
            run.index(*ym)
            step.ok(f"{res.listed} events, {res.newly_owed} newly owed" if res.listed else "no events")
            left -= 1
        todo = due(run.owed, run.clock())[:left]
        if todo:
            step = tracker.step("mtgo events", total=len(todo), unit="events")
            for n, slug in enumerate(todo, 1):
                run.event(slug)
                step.update(n)
            outcome = (
                f"{len(res.fetched)} new, {len(res.pending)} not published yet, {len(res.missed)} missed"
            )
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


def status(clock: Callable[[], datetime] = trickle.now) -> TrickleStatus:
    at = clock()
    pace, log, owed, months = (
        trickle.load_pace(SOURCE),
        trickle.RequestLog(SOURCE),
        trickle.load_owed(SOURCE),
        _read_months(),
    )
    day = log.since(at - timedelta(days=1))
    window = [r for r in day if r.at >= trickle.stamp(at - trickle.WINDOW)]
    done = bool(months) and sweep_next(months, at) is None
    return TrickleStatus(at, pace, pace.paused(at), window, day, owed, months, done)
