"""Slow, steady fetching from a source that throttles, without giving anything up.

Three parts, each kept in the data folder under the source's name:

- the request log (<source>-requests.jsonl): every request, one line each, so how
  hard the source was asked, and how it answered, can always be read back;
- the pace (<source>-pace.json): pages a run, at one of LEVELS. A throttle pauses
  the source for hours and drops a level; SPEED_UP_AFTER whole answers in a row raise
  one. Whatever the level, no WINDOW holds more than CEILING requests;
- the owed list (<source>-owed.json): everything the source listed that isn't stored
  yet. Nothing leaves it except by being fetched or forgotten by hand.

MTGO is the first source (riffle.ingest.mtgo); a later event source reuses these with
its own name. Every time here is UTC.
"""

import json
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from riffle.config import data_dir

LEVELS = (1, 2, 3)  # pages a run
CEILING = 5  # requests in any WINDOW, whoever makes them
WINDOW = timedelta(minutes=15)
PAUSES = (timedelta(hours=3), timedelta(hours=6), timedelta(hours=12))
RECENT_THROTTLE = timedelta(hours=24)  # a throttle this soon after the last pauses longer
SPEED_UP_AFTER = 144  # whole answers in a row before the pace rises a level
FRESH_DAYS = 30  # an owed item this young (by its own date) is due on every run
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
    level: int = LEVELS[-1]
    paused_until: str | None = None
    whole_streak: int = 0
    last_throttle: str | None = None
    pause_step: int = 0  # which of PAUSES the last throttle took

    def paused(self, at: datetime) -> datetime | None:
        """When the pause ends, while there is one."""
        if self.paused_until is None:
            return None
        until = datetime.fromisoformat(self.paused_until)
        return until if until > at else None

    def budget(self, log: RequestLog, at: datetime) -> int:
        """Pages this run may fetch: the level's, cut to what the ceiling leaves."""
        return max(0, min(self.level, CEILING - log.count(at - WINDOW)))

    def whole(self) -> bool:
        """A whole answer. True when it raised the pace a level."""
        self.whole_streak += 1
        if self.whole_streak >= SPEED_UP_AFTER and self.level < LEVELS[-1]:
            self.level += 1
            self.whole_streak = 0
            return True
        return False

    def throttled(self, at: datetime) -> datetime:
        """The source is throttling: pause, longer each time it happens again within
        RECENT_THROTTLE, and come back a level slower. Returns when the pause ends."""
        again = (
            self.last_throttle is not None
            and at - datetime.fromisoformat(self.last_throttle) <= RECENT_THROTTLE
        )
        self.pause_step = min(self.pause_step + 1, len(PAUSES) - 1) if again else 0
        until = at + PAUSES[self.pause_step]
        self.paused_until, self.last_throttle = stamp(until), stamp(at)
        self.level = max(LEVELS[0], self.level - 1)
        self.whole_streak = 0
        return until


def pace_path(source: str) -> Path:
    return data_dir() / f"{source}-pace.json"


def load_pace(source: str) -> Pace:
    saved = _read_json(pace_path(source))
    try:
        pace = Pace(**saved)
    except TypeError:
        return Pace()
    pace.level = min(max(pace.level, LEVELS[0]), LEVELS[-1])
    return pace


def save_pace(source: str, pace: Pace) -> None:
    _write_json(pace_path(source), asdict(pace))


# ---- the owed list ----------------------------------------------------------------


@dataclass
class Owed:
    day: str  # the item's own date, by the source's clock
    found: str  # UTC time it was first listed
    tries: int = 0  # misses: answers that were really empty, 404s, redirects
    last_try: str | None = None
    last: str = ""  # what the last try came to

    def due(self, at: datetime) -> bool:
        """Due on every run while FRESH_DAYS young, then weekly."""
        if self.last_try is None:
            return True
        if (at.date() - date.fromisoformat(self.day)).days <= FRESH_DAYS:
            return True
        return at - datetime.fromisoformat(self.last_try) >= RETRY_OLD

    def tried(self, at: datetime, outcome: str, miss: bool) -> None:
        self.last_try, self.last = stamp(at), outcome
        self.tries += miss


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
