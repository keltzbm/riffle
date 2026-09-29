"""What every store's watch shares: its lock, its log, the ETags it last kept, how a list
kept in a run (riffle.runs) is logged and said, and the watch of a store with one list.

    <data_dir>/<store>/watch.lock          one run at a time a store
    <data_dir>/<store>/watch.jsonl         every check: when, which list, what came; for a list
                                           kept, its stamp, file, size, and the SHA-256 of the
                                           list and of the file, which riffle.ingest.checks checks
    <data_dir>/<store>/watch-etags.json    the ETag of each list last kept
    <data_dir>/<store>/aside/              what a watch fetched and couldn't keep as a list,
                                           named by when it was fetched
"""

import json
import re
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from riffle import cadence, lateness, locks, net, runs, times
from riffle.config import data_dir
from riffle.progress import Step, Tracker

# each watched by `riffle watch <store>`
STORES = ("cardkingdom", "manapool", "cardmarket", "tcgcsv", "mtgjson", "goatbots")


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


FOUND = ("kept", "new")  # a check that got a new list: tcgcsv's says "new"


def online(found: list[dict], name: str) -> dict[datetime, datetime]:
    """When each list a check of it got could first have been online, by when it was made: the
    last check before that got nothing new, or its made if that came later. A failed check tells
    nothing, so it's passed over."""
    out: dict[datetime, datetime] = {}
    last: datetime | None = None  # the last check that got nothing new
    for entry in found:
        when = runs.parse(str(entry.get("at", "")))
        result = entry.get("result")
        if entry.get("list") != name or when is None or result in (None, "failed"):
            continue
        made = runs.parse(str(entry.get("made", "")))
        if result in FOUND and made is not None and made not in out:
            out[made] = made if last is None else max(made, last)
        last = when
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


def slug(name: str) -> str:
    """A list's name as a folder name: "Pokemon Japan" is pokemon-japan."""
    return re.sub(r"[^a-z0-9]+", "-", name.casefold()).strip("-")


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


def served(path: Path, got: net.Fetched) -> dict:
    """The file a source served, as its log entry says it: its SHA-256, size and ETag."""
    return {"served_sha256": runs.file_sha256(path), "served_size": path.stat().st_size, "etag": got.etag}


def days(store: str, name: str) -> list[date]:
    """The days of one list's kept lists, as the store's log says them, oldest first."""
    found = set()
    for entry in entries(store):
        if entry.get("list") == name and entry.get("result") == "kept":
            try:
                found.add(date.fromisoformat(str(entry.get("day"))))
            except ValueError:
                continue
    return sorted(found)


def aside(store: str, stem: str, suffix: str, at: datetime) -> Path:
    """A free name in the store's aside folder for a file fetched at `at`: stem, the UTC time,
    then suffix."""
    when = f"{stem}-{at.astimezone(UTC):%Y-%m-%dT%H%M%SZ}"
    folder = data_dir() / store / "aside"
    dest, n = folder / f"{when}{suffix}", 1
    while dest.exists():
        n += 1
        dest = folder / f"{when}-{n}{suffix}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    return dest


def set_aside(store: str, fresh: Path, name: str, at: datetime) -> str:
    """Keep a file as served in the store's aside folder, named by name and when it was
    fetched: AllPricesToday.json.xz becomes AllPricesToday-<UTC time>.json.xz. Where it went,
    relative to the data folder."""
    stem, dot, rest = name.partition(".")
    dest = aside(store, stem, dot + rest, at)
    fresh.replace(dest)
    return dest.relative_to(data_dir()).as_posix()


def waiting(plan: cadence.Plan, what: str) -> str:
    """When a list not due is asked next, and when the next is expected, once that's learned;
    and when it's online, if it's learned to go online after that."""
    said = f"next asked {times.shown(plan.next)}"
    if plan.expected is None:
        learned = f"learned from {lateness.LEAST_GAPS} gaps, {plan.gaps} so far"
        return f"{said}; its next {what}'s time is {learned}"
    said = f"{said}; its next {what} expected {times.shown(plan.expected)}"
    if plan.opens is not None and plan.opens > plan.expected:
        said = f"{said}, online from about {times.shown(plan.opens)}"
    return said


def made(folder: Path) -> list[datetime]:
    """When each list kept in folder was made, oldest first."""
    return sorted(filter(None, map(runs.parse, runs.kept(folder))))


Ask = Callable[[dict[str, str], datetime, Step], dict]  # (ETags, now, step) -> the check's log entry


def one(
    store: str,
    name: str,
    label: str,
    ask: Ask,
    folder: Path,
    tracker: Tracker,
    clock: Callable[[], datetime] = times.now,
    always: bool = False,
) -> Watch:
    """The watch of a store with one list, kept in folder: ask for it when riffle.cadence says
    it's due (from when its lists kept were made and its checks), or always (the sync), and log
    the check. ask gets the ETags, the time and the step, ends the step, and returns the log
    entry's result; one that fails raises, and the step fails. A run that finds the store's
    lock held asks nothing."""
    res = Watch()
    with held(store) as mine:
        if not mine:
            res.busy = True
            tracker.step(label).ok("another run is asking for it")
            return res
        tags, found, now = load_tags(store), entries(store), clock()
        if not always:
            busy = longest(found, name, now, lateness.WINDOW)
            plan = cadence.plan(made(folder), checks(found, name), now, busy, online(found, name))
            if not plan.ask:
                res.waiting.append(label)
                tracker.step(label).ok(waiting(plan, "list"))
                return res
        step = tracker.step(label, unit="bytes")
        entry: dict = {"at": at(now), "list": name}
        started = time.monotonic()
        try:
            entry |= ask(tags, now, step)
        except (net.FetchError, OSError) as e:
            res.failed.append((label, str(e)))
            step.fail(str(e))
            entry |= {"result": "failed", "why": str(e)}
        else:
            {"kept": res.kept, "empty": res.empty}.get(entry["result"], res.same).append(label)
        log(store, entry | {"seconds": round(time.monotonic() - started, 1)})
        save_tags(store, tags)
    return res
