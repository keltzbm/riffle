"""Answers that held nothing: a price list with no rows, a Cardmarket game with no guide.

Nothing is kept for one, so the next run asks again. This file remembers how long each has
been empty, so one a source stops publishing turns into a warning instead of passing quietly:

    <data_dir>/empty-answers.json    {"<source>/<list>": {"what": "empty", "first": <UTC>,
                                                          "last": <UTC>, "runs": 3}}

Every run asks again however long it's been: a price day can't be fetched later, so nothing
backs off. An answer with data clears the entry. An unreadable file starts the counts again.
"""

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from riffle import times
from riffle.config import data_dir
from riffle.progress import Step

WARN_AFTER = 7  # empty runs in a row before one is a warning


@dataclass(frozen=True)
class Empty:
    what: str  # what came back: "empty", "no guide"
    first: datetime  # UTC, the first of this run of empty answers
    runs: int  # empty answers in a row, this one included


def path() -> Path:
    return data_dir() / "empty-answers.json"


def _load() -> dict[str, dict]:
    try:
        found = json.loads(path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return found if isinstance(found, dict) else {}


def _save(entries: dict[str, dict]) -> None:
    dest = path()
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    part.write_text(json.dumps(entries, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    part.replace(dest)


def record(key: str, what: str, at: datetime | None = None) -> Empty:
    """One more empty answer for key. The count starts again when what came back changes."""
    at = (at or times.now()).astimezone(UTC)
    entries = _load()
    seen = entries.get(key)
    try:
        if not isinstance(seen, dict) or seen.get("what") != what:
            raise ValueError
        first, runs = datetime.fromisoformat(seen["first"]), int(seen["runs"]) + 1
    except (KeyError, TypeError, ValueError):
        first, runs = at, 1
    stamp = at.astimezone(UTC).isoformat(timespec="seconds")
    entries[key] = {"what": what, "first": first.isoformat(timespec="seconds"), "last": stamp, "runs": runs}
    _save(entries)
    return Empty(what, first, runs)


def clear(key: str) -> None:
    """key answered with data: forget its empty answers."""
    entries = _load()
    if entries.pop(key, None) is not None:
        _save(entries)


def report(step: Step, key: str, what: str, at: datetime | None = None) -> Empty:
    """Record an empty answer and end step with it: a note until it's been WARN_AFTER runs in a
    row, a warning from then on. Never a failure: the next run asks again."""
    empty = record(key, what, at)
    if empty.runs < WARN_AFTER:
        step.ok(f"{what}, nothing kept; asked again next run")
    else:
        since = empty.first.astimezone(UTC).date()
        step.warn(f"{what} since {since} ({empty.runs} runs in a row); asked again every run")
    return empty
