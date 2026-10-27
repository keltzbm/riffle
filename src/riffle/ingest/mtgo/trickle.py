"""Collect MTGO pages in bounded runs and keep the backfill moving."""

import json
import time
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import NoReturn

from riffle import files, locks, net, trickle
from riffle.config import data_dir
from riffle.ingest.mtgo import pages as pages_mod
from riffle.ingest.mtgo import storage as storage_mod
from riffle.models.metagame import Event
from riffle.progress import SILENT, Tracker

SOURCE = "mtgo"


PENDING_DAYS = 3


NEW_MONTH_DAYS = 2


REREAD = timedelta(hours=1)


RECENT_MONTH = timedelta(days=7)


SWEEP_END = 3


EMPTY_DAYS = 3


PROBE = timedelta(days=7)


CANARY_AGE = timedelta(minutes=30)


GAP = 5.0


TIMEOUT = 60.0


WHOLE = ("whole", "canary whole")


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
        data = pages_mod.extract_data(body.decode("utf-8", errors="replace"))
    except ValueError:
        return None
    return data if isinstance(data, dict) and data.get("decklists") else None


def _moved(asked: str, answered: str) -> bool:
    return answered.rstrip("/").lower() != asked.rstrip("/").lower()


def _event_id(slug: str) -> int:
    parsed = pages_mod.parse_slug(slug)
    return int(parsed[2]) if parsed else 0


def _slug_of(url: str) -> str | None:
    """The event an event page's URL names; None for any other page."""
    prefix = f"{pages_mod.BASE}/decklist/"
    return url[len(prefix) :] if url.startswith(prefix) else None


def _learn(ages: dict[str, float], slug: str, at: datetime) -> None:
    """An event fetched whole at at: its age, in hours from the start of its date (UTC),
    is its kind's when it's the youngest yet. Only an event under PENDING_DAYS old says
    when its kind is ready: one fetched from the backlog would teach every newer event
    to wait as long."""
    parsed = pages_mod.parse_slug(slug)
    if parsed is None:
        return
    kind = pages_mod.classify(parsed[0])
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
    parsed = pages_mod.parse_slug(slug)
    if parsed is None or (kind := pages_mod.classify(parsed[0])) not in ages:
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
    waiting: int = 0


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
            parsed = pages_mod.parse_slug(slug) if slug else None
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
        url = f"{pages_mod.BASE}/decklist/{slug}"
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
        key, url = _key(y, m), f"{pages_mod.BASE}/decklists/{y}/{m:02d}"
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
        slugs, self.res.undated = pages_mod.event_slugs(page), pages_mod.undated_slugs(page)
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
            parsed = pages_mod.parse_slug(slug)
            if parsed and slug not in self.owed and not storage_mod.is_stored(slug):
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
        owed, url = self.owed[slug], f"{pages_mod.BASE}/decklist/{slug}"
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
                event = pages_mod.parse_event(slug, data)
            except (ValueError, KeyError, TypeError, AttributeError) as e:
                self.record(url, answer, ms, "unparseable", str(e))
                owed.tried(self.clock(), "unparseable", miss=False)
                self.res.broken.append((slug, str(e) or type(e).__name__))
                return
            self.record(url, answer, ms, "whole", f"{len(event.decks)} decks")
            storage_mod.save(event)
            storage_mod.save_raw(slug, data, url, self.clock(), len(answer.body))
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
        parsed = pages_mod.parse_slug(slug)
        if parsed is None or storage_mod.is_stored(slug):
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
    gone = [slug for slug in owed if pages_mod.parse_slug(slug) is None]
    for slug in gone:
        trickle.set_aside(SOURCE, slug, owed[slug], "its name holds no real date", at)
        del owed[slug]
    return gone


def _read_months() -> dict[str, dict]:
    return files.read_json_object(months_path())


def _save_json(path: Path, value: dict) -> None:
    """Whole or not at all: a run cut off mid-write leaves the old file."""
    with files.replacing(path) as part:
        part.write_text(json.dumps(dict(sorted(value.items())), indent=1) + "\n", encoding="utf-8")


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
        res.moved = storage_mod._file_events()
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
    waiting: int = 0


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
