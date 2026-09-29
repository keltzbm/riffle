"""What every store's watch shares: its lock, its log, the ETags it last kept, and how a list
kept in a run (riffle.runs) is logged and said.

    <data_dir>/<store>/watch.lock          one run at a time a store
    <data_dir>/<store>/watch.jsonl         every check: when, which list, what came; for a list
                                           kept, its stamp, file, size, and the SHA-256 of the
                                           list and of the file, which riffle.ingest.checks checks
    <data_dir>/<store>/watch-etags.json    the ETag of each list last kept
"""

import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from riffle import locks, runs
from riffle.config import data_dir

STORES = ("cardkingdom", "manapool", "cardmarket", "tcgcsv")  # each watched by `riffle watch <store>`


@dataclass
class Watch:
    busy: bool = False  # another run held the store: nothing asked
    kept: list[str] = field(default_factory=list)  # labels of the lists kept this run
    same: list[str] = field(default_factory=list)  # the list asked for was kept already
    empty: list[str] = field(default_factory=list)  # answered with no rows
    failed: list[tuple[str, str]] = field(default_factory=list)  # (label, why)
    waiting: list[str] = field(default_factory=list)  # not due, so not asked (riffle.cadence)


def log_path(store: str) -> Path:
    return data_dir() / store / "watch.jsonl"


def _tags_path(store: str) -> Path:
    return data_dir() / store / "watch-etags.json"


@contextmanager
def held(store: str) -> Iterator[bool]:
    """Whether this run holds the store's watch lock (riffle.locks.held)."""
    with locks.held(data_dir() / store / "watch.lock") as mine:
        yield mine


def at(now: datetime) -> str:
    """A log entry's time: to the second, UTC."""
    return runs.name(now.replace(microsecond=0))


def log(store: str, entry: dict) -> None:
    path = log_path(store)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def entries(store: str) -> list[dict]:
    """The store's log, oldest first; a line that isn't a JSON object is left out
    (riffle.ingest.checks names it)."""
    try:
        lines = log_path(store).read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    found = []
    for line in lines:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict):
            found.append(entry)
    return found


def checks(found: list[dict], name: str) -> list[tuple[datetime, bool]]:
    """Each check of one list in a store's log: when, and whether it failed."""
    out = []
    for entry in found:
        when = runs.parse(str(entry.get("at", "")))
        if entry.get("list") == name and when is not None and "result" in entry:
            out.append((when, entry["result"] == "failed"))
    return out


def longest(found: list[dict], name: str, now: datetime, within: timedelta) -> timedelta:
    """The longest fetch of one list logged in the `within` before now."""
    most = 0.0
    for entry in found:
        when = runs.parse(str(entry.get("at", "")))
        seconds = entry.get("seconds")
        if (
            entry.get("list") == name
            and when is not None
            and now - when <= within
            and isinstance(seconds, int | float)
        ):
            most = max(most, float(seconds))
    return timedelta(seconds=most)


def load_tags(store: str) -> dict[str, str]:
    try:
        found = json.loads(_tags_path(store).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in found.items() if isinstance(k, str) and isinstance(v, str)}


def save_tags(store: str, tags: dict[str, str]) -> None:
    path = _tags_path(store)
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_name(path.name + ".part")
    part.write_text(json.dumps(tags, indent=1, sort_keys=True), encoding="utf-8")
    part.replace(path)


def size(n: int) -> str:
    """A size to read at a glance: a difference is often a few kilobytes, or bytes."""
    if n >= 1e6:
        return f"{n / 1e6:,.1f} MB"
    return f"{n / 1e3:,.0f} KB" if n >= 1e3 else f"{n:,} bytes"


def how(kept: runs.Kept) -> str:
    """How a list was kept: "a new run, 5.6 MB kept twice" or "a difference of 50 KB"."""
    if kept.kind == "base":
        return f"a new run, {size(kept.stored // 2)} kept twice"
    return f"a difference of {size(kept.stored)}"


def record(kept: runs.Kept) -> dict:
    """A kept list as its log entry says it, file relative to the data folder."""
    return {
        "result": "kept",
        "made": kept.stamp,
        "kind": kept.kind,
        "file": kept.path.relative_to(data_dir()).as_posix(),
        "size": kept.size,
        "stored": kept.stored,
        "sha256": kept.sha256,
        "file_sha256": kept.file_sha256,
    }
