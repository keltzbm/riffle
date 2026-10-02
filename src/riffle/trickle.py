"""Slow, steady fetching from a source that throttles, without giving anything up.

Three parts, each kept in the data folder under the source's name:

- the request log (<source>-requests.jsonl): every request, one line each, so how
  hard the source was asked, and how it answered, can always be read back;
- the pace (<source>-pace.json): pages a run, at one of LEVELS. A run whose every
  answer was whole raises it one; only a pause, when the source asks for one, drops
  it. Whatever the level, no WINDOW holds more than CEILING requests;
- the owed list (<source>-owed.json): everything the source listed that isn't stored
  yet. Nothing leaves it except by being fetched or forgotten by hand. What was never
  asked for is due at once. A page that fails is asked again at each of the next runs
  until ROUND tries in a row have failed; then it waits, longer after each round.

MTGO is the first source (riffle.ingest.mtgo); a later event source reuses these with
its own name. Every time here is UTC.
"""

import json
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path

from riffle.config import data_dir

CEILING = 5  # requests in any WINDOW, whoever makes them
LEVELS = tuple(range(1, CEILING + 1))  # pages a run
WINDOW = timedelta(minutes=15)
PAUSES = (timedelta(hours=3), timedelta(hours=6), timedelta(hours=12))  # when the source names no wait
RECENT_THROTTLE = timedelta(hours=24)  # a throttle this soon after the last pauses longer
ROUND = 3  # tries in a row: a page that fails is asked again at each of the next two runs
READY = timedelta(hours=1)  # a page asked for stays ready for the next request about this long
FRESH_DAYS = 30  # an owed item this young (by its own date) is retried within a day
RETRY_FIRST = timedelta(hours=1)  # after its first round it waits this, doubling with each round
RETRY_FRESH = timedelta(days=1)  # up to this
RETRY_OLD = timedelta(days=7)  # an older one, weekly


def now() -> datetime:
    return datetime.now(UTC)


def stamp(t: datetime) -> str:
    return t.astimezone(UTC).isoformat(timespec="seconds")


def _write_json(path: Path, value: object) -> None:
    """Whole or not at all: a run cut off mid-write leaves the old file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part")
    tmp.write_text(json.dumps(value, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


# ---- the request log ------------------------------------------------------------


@dataclass
class Request:
    at: str  # UTC
    url: str
    status: int | None  # None: no answer at all
    bytes: int
    ms: int
    verdict: str  # what the answer was read as: whole, empty, missing, throttled, error, ...
    note: str = ""


class RequestLog:
    def __init__(self, source: str) -> None:
        self.path = data_dir() / f"{source}-requests.jsonl"

    def add(self, request: Request) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(request)) + "\n")

    def since(self, start: datetime) -> list[Request]:
        """Requests at or after start, oldest first. Reads back from the end of the file,
        only as far as it needs: the log only grows."""
        if not self.path.exists():
            return []
        cutoff = stamp(start)
        size = self.path.stat().st_size
        block = 1 << 16
        while True:
            with self.path.open("rb") as f:
                f.seek(max(0, size - block))
                lines = f.read().decode("utf-8", errors="replace").splitlines()
            if block < size:
                lines = lines[1:]  # the first line may be cut
            rows = [r for line in lines if (r := _request(line)) is not None]
            if block >= size or (rows and rows[0].at < cutoff):
                return [r for r in rows if r.at >= cutoff]
            block *= 4

    def count(self, start: datetime) -> int:
        return len(self.since(start))


def _request(line: str) -> Request | None:
    try:
        return Request(**json.loads(line))
    except (ValueError, TypeError):
        return None


# ---- the pace -------------------------------------------------------------------


@dataclass
class Pace:
    level: int = LEVELS[0]
    paused_until: str | None = None
    last_throttle: str | None = None
    pause_step: int = 0  # which of PAUSES the last throttle took
    why: str = ""  # what the source said, for the last pause
    oldest_first: bool = False  # the next run's first event never asked for is the oldest

    def paused(self, at: datetime) -> datetime | None:
        """When the pause ends, while there is one."""
        if self.paused_until is None:
            return None
        until = datetime.fromisoformat(self.paused_until)
        return until if until > at else None

    def budget(self, log: RequestLog, at: datetime) -> int:
        """Pages this run may fetch: the level's, cut to what the ceiling leaves."""
        return max(0, min(self.level, CEILING - log.count(at - WINDOW)))

    def all_whole(self) -> bool:
        """A run whose every answer was whole: one more page a run, up to the ceiling. True
        when it rose."""
        if self.level >= LEVELS[-1]:
            return False
        self.level += 1
        return True

    def throttled(self, at: datetime, why: str, wait: timedelta | None = None) -> datetime:
        """The source asked for a pause: as long as it says, else one of PAUSES, longer
        each time it happens again within RECENT_THROTTLE; and a page a run fewer.
        Returns when the pause ends."""
        again = (
            self.last_throttle is not None
            and at - datetime.fromisoformat(self.last_throttle) <= RECENT_THROTTLE
        )
        self.pause_step = min(self.pause_step + 1, len(PAUSES) - 1) if again else 0
        until = at + (PAUSES[self.pause_step] if wait is None else max(wait, timedelta(0)))
        self.paused_until, self.last_throttle, self.why = stamp(until), stamp(at), why
        self.level = max(LEVELS[0], self.level - 1)
        return until


def wait_asked(retry_after: str | None, at: datetime) -> timedelta | None:
    """How long a Retry-After header asks for: seconds, or an HTTP date. None when there's
    none, or it's neither."""
    if retry_after is None:
        return None
    try:
        return timedelta(seconds=float(retry_after))
    except (ValueError, OverflowError):
        pass
    try:
        when = parsedate_to_datetime(retry_after)
    except (TypeError, ValueError, IndexError):
        return None
    return when - at if when.tzinfo is not None else None


def pace_path(source: str) -> Path:
    return data_dir() / f"{source}-pace.json"


def load_pace(source: str) -> Pace:
    """The saved pace; what an older build saved that this one doesn't keep is left out."""
    saved = _read_json(pace_path(source))
    try:
        pace = Pace(**{k: v for k, v in saved.items() if k in Pace.__dataclass_fields__})
        pace.level = min(max(pace.level, LEVELS[0]), LEVELS[-1])
    except TypeError:
        return Pace()
    return pace


def save_pace(source: str, pace: Pace) -> None:
    _write_json(pace_path(source), asdict(pace))


# ---- the owed list ----------------------------------------------------------------


@dataclass
class Owed:
    day: str  # the item's own date, by the source's clock
    found: str  # UTC time it was first listed
    tries: int = 0  # misses: answers believed empty, 404s, redirects
    last_try: str | None = None
    last: str = ""  # what the last try came to
    asks: int = 0  # every try, whatever it came to
    again: int = 0  # retries left in its round, one at each of the next runs
    rounds: int | None = None  # waits so far: how long the next one is

    def __post_init__(self) -> None:
        if self.last_try is not None:  # a list saved before asks were counted
            self.asks = max(self.asks, self.tries, 1)
        if self.rounds is None:  # saved before rounds: every try waited, as a round does
            self.rounds = self.asks

    def warm(self) -> bool:
        """In a round not over: its last try failed, and a page that was slow to build is
        ready for the next request."""
        return self.again > 0

    def ready(self, at: datetime) -> bool:
        """In a round not over, and asked within READY: the page its last try started
        building is ready now. A round whose next run came late starts cold."""
        return self.warm() and at - datetime.fromisoformat(str(self.last_try)) < READY

    def retry_at(self) -> datetime | None:
        """When it's due again: at the next run while its round lasts; after the round,
        RETRY_FIRST after the last try, doubling with each round up to RETRY_FRESH while
        FRESH_DAYS young at that try, else RETRY_OLD. None when it was never asked for,
        and so is due now."""
        if self.last_try is None:
            return None
        last = datetime.fromisoformat(self.last_try)
        old = (last.date() - date.fromisoformat(self.day)).days > FRESH_DAYS
        if self.warm():
            return last
        if old:
            return last + RETRY_OLD
        return last + min(RETRY_FIRST * 2 ** min(max(self.rounds or 0, 1) - 1, 10), RETRY_FRESH)

    def due(self, at: datetime) -> bool:
        retry = self.retry_at()
        return retry is None or at >= retry

    def tried(self, at: datetime, outcome: str, miss: bool) -> None:
        """A failed try: it starts a round, or goes on with the one it's in. A round that's
        over adds a wait."""
        self.last_try, self.last = stamp(at), outcome
        self.tries += miss
        self.asks += 1
        self.again = self.again - 1 if self.again else ROUND - 1
        if not self.again:
            self.rounds = (self.rounds or 0) + 1


def owed_path(source: str) -> Path:
    return data_dir() / f"{source}-owed.json"


def load_owed(source: str) -> dict[str, Owed]:
    out = {}
    for key, value in _read_json(owed_path(source)).items():
        try:
            out[key] = Owed(**value)
        except TypeError:
            continue
    return out


def save_owed(source: str, owed: dict[str, Owed]) -> None:
    _write_json(owed_path(source), {k: asdict(v) for k, v in owed.items()})


def set_aside_path(source: str) -> Path:
    return data_dir() / f"{source}-owed-set-aside.jsonl"


def set_aside(source: str, key: str, entry: Owed, why: str, at: datetime) -> None:
    """Keep an owed item that will never be asked for: one line, with why and when."""
    path = set_aside_path(source)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"key": key, "why": why, "at": stamp(at), **asdict(entry)}) + "\n")
