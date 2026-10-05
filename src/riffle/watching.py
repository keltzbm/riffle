"""What every store's watch shares: its lock, its log, the ETags it last kept, how a list
kept in a run (riffle.runs) is logged and said, what's set aside, and the watch of a store's
lists, each asked when it's due.

    <data_dir>/<store>/watch.lock          one run at a time a store
    <data_dir>/<store>/watch/<month>.jsonl every check made in that month (UTC): when, which list,
                                           what came; for a list kept, its stamp, file, size, and
                                           the SHA-256 of the list and of the file, which
                                           riffle.ingest.checks checks
    <data_dir>/<store>/watch/<month>.summary.json
                                           what a run needs of a month it no longer reads (summarize)
    <data_dir>/<store>/watch-etags.json    the ETag of each list last kept
    <data_dir>/<store>/aside/              what a watch fetched and couldn't keep as a list,
                                           named by when it was fetched

Until 0054 the log was one file, <store>/watch.jsonl, read whole at every firing and walked once
for each list: Cardmarket's took 41 seconds a firing on 2026-10-05, a week in. The first run that
holds a store's lock moves it to the month file of its last entry (moved). A run reads the
months from lateness.WINDOW before it on (read): two files, three on 1 and 2 March. What its
rules need from before that, each list's last check and when each list it got in the last
lateness.HISTORY could first have been online, comes from the summaries.
"""

import json
import re
import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from riffle import cadence, lateness, locks, net, runs, times
from riffle.config import data_dir
from riffle.progress import Step, Tracker, failure

# each watched by `riffle watch <store>`
STORES = ("cardkingdom", "manapool", "cardmarket", "tcgcsv", "mtgjson", "goatbots", "scryfall")


@dataclass
class Watch:
    busy: bool = False  # another run held the store: nothing asked
    kept: list[str] = field(default_factory=list)  # labels of the lists kept this run
    same: list[str] = field(default_factory=list)  # the list asked for was kept already
    empty: list[str] = field(default_factory=list)  # answered with no rows
    failed: list[tuple[str, str]] = field(default_factory=list)  # (label, why)
    waiting: list[str] = field(default_factory=list)  # not due, so not asked (riffle.cadence)


def log_path(store: str, when: datetime) -> Path:
    """The month file a check made at `when` is logged in."""
    return data_dir() / store / "watch" / f"{when.astimezone(UTC):%Y-%m}.jsonl"


def _old_path(store: str) -> Path:
    return data_dir() / store / "watch.jsonl"


def _summary_path(path: Path) -> Path:
    return path.with_name(f"{path.stem}.summary.json")


def _months(store: str) -> list[Path]:
    return sorted((data_dir() / store / "watch").glob("????-??.jsonl"))


def files(store: str) -> list[Path]:
    """The store's log files, oldest first: the one file kept before 0054 while it's still
    there (moved), then each month's."""
    old = _old_path(store)
    return [old, *_months(store)] if old.exists() else _months(store)


def _tags_path(store: str) -> Path:
    return data_dir() / store / "watch-etags.json"


@contextmanager
def held(store: str) -> Iterator[bool]:
    """Whether this run holds the store's watch lock (riffle.locks.held). A run that holds it
    first moves the log kept before 0054 (moved)."""
    with locks.held(data_dir() / store / "watch.lock") as mine:
        if mine:
            moved(store)
        yield mine


def at(now: datetime) -> str:
    """A log entry's time: to the second, UTC."""
    return runs.name(now.replace(microsecond=0))


def log(store: str, entry: dict) -> None:
    """Log an entry in the month file of its time, or of now when it has none."""
    path = log_path(store, runs.parse(str(entry.get("at", ""))) or times.now())
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def _parsed(text: str) -> list[dict]:
    """A log file's entries, oldest first; a line that isn't a JSON object is left out
    (riffle.ingest.checks names it)."""
    found = []
    for line in text.splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict):
            found.append(entry)
    return found


def _lines(path: Path) -> list[dict]:
    try:
        return _parsed(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []


def entries(store: str) -> list[dict]:
    """The store's whole log, oldest first. A run reads only what it needs (read)."""
    return [entry for path in files(store) for entry in _lines(path)]


def back(store: str) -> Iterator[dict]:
    """The store's log newest first, a month at a time, read only as far as it's taken."""
    for path in reversed(files(store)):
        yield from reversed(_lines(path))


def moved(store: str) -> None:
    """Move the log kept in one file before 0054 to the month file of its last entry. A month
    file there already keeps its lines, after the old file's; the old file goes last, so a move
    cut off is finished by the next run, with no line lost or doubled."""
    old = _old_path(store)
    try:
        text = old.read_text(encoding="utf-8")
    except FileNotFoundError:
        return
    stamps = (runs.parse(str(entry.get("at", ""))) for entry in reversed(_parsed(text)))
    dest = log_path(store, next(filter(None, stamps), None) or times.now())
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        there = dest.read_text(encoding="utf-8")
    except FileNotFoundError:
        old.replace(dest)
        return
    if not there.startswith(text):
        part = dest.with_name(dest.name + ".part")
        part.write_text(text + ("\n" if text and not text.endswith("\n") else "") + there, encoding="utf-8")
        part.replace(dest)
    old.unlink()


def _carried(carry: Mapping[str, dict]) -> list[dict]:
    """The entries a summary carries, oldest first: each list's last check answered, when that
    isn't its last check, and its last check."""
    found = []
    for kept in carry.values():
        last, ok = kept.get("last"), kept.get("ok")
        if isinstance(ok, dict) and ok != last:
            found.append(ok)
        if isinstance(last, dict):
            found.append(last)
    return sorted(found, key=lambda entry: str(entry.get("at", "")))


def by_list(found: Iterable[dict]) -> dict[str, list[dict]]:
    """A log's entries by list name, each list's oldest first."""
    out: dict[str, list[dict]] = {}
    for entry in found:
        name = entry.get("list")
        if isinstance(name, str):
            out.setdefault(name, []).append(entry)
    return out


def summarize(found: list[dict], carry: Mapping[str, dict]) -> dict:
    """What a run needs of a month's entries once it no longer reads them, with what the
    summary of the month before carries. Per list, in lists: its checks, failures, longest
    fetch in seconds, and when each list got that month could first have been online, by its
    made (online; a list got again in a later month has a bound in each, and read keeps the
    first); in carry: its last check and its last check answered (not failed), this month or
    before. A run's last check of each list, tcgcsv's last whole day and Cardmarket's lists
    never served read the carry for what came before the months they read."""
    before = by_list(_carried(carry))
    lists: dict[str, dict] = {}
    out = {name: dict(kept) for name, kept in carry.items()}
    for name, mine in by_list(found).items():
        made = checks(mine, name)
        seen = {runs.parse(str(entry.get("made", ""))) for entry in before.get(name, [])}
        bounds = online(before.get(name, []) + mine, name)
        fetches = [float(entry["seconds"]) for entry in mine if _seconds(entry)]
        lists[name] = {
            "checks": len(made),
            "failures": sum(failed for _, failed in made),
            "longest": max(fetches, default=0.0),
            "online": {runs.name(m): runs.name(b) for m, b in bounds.items() if m not in seen},
        }
        for entry in mine:
            if "result" in entry and runs.parse(str(entry.get("at", ""))) is not None:
                kept = out.setdefault(name, {})
                kept["last"] = entry
                if entry["result"] != "failed":
                    kept["ok"] = entry
    return {"lists": lists, "carry": out}


def _seconds(entry: dict) -> bool:
    seconds = entry.get("seconds")
    return isinstance(seconds, int | float) and not isinstance(seconds, bool)


def _readable(found: object) -> bool:
    """Whether a summary read back has the shape summarize gives it."""
    if not isinstance(found, dict):
        return False
    lists, carry = found.get("lists"), found.get("carry")
    return (
        isinstance(lists, dict)
        and isinstance(carry, dict)
        and all(isinstance(each, dict) and isinstance(each.get("online"), dict) for each in lists.values())
        and all(isinstance(kept, dict) for kept in carry.values())
    )


def _summary(months: list[Path], i: int, keep: bool, made: dict[int, dict]) -> dict:
    """The summary of months[i] (summarize): its .summary.json, or, when that's missing or
    can't be read, made from the month and the summary of the month before, and written when
    keep (the run holds the store's lock). made holds those a read has already."""
    if i in made:
        return made[i]
    path = _summary_path(months[i])
    try:
        found = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        found = None
    if not _readable(found):
        carry = _summary(months, i - 1, keep, made)["carry"] if i else {}
        found = summarize(_lines(months[i]), carry)
        if keep:
            part = path.with_name(path.name + ".part")
            part.write_text(json.dumps(found), encoding="utf-8")
            part.replace(path)
    made[i] = found
    return found


@dataclass(frozen=True)
class Read:
    """What a run reads of a store's log (read)."""

    entries: list[dict]  # what the summary before the months read carries, then their entries
    online: dict[str, dict[datetime, datetime]]  # by list: online bounds from the summaries


def read(store: str, now: datetime, keep: bool = False) -> Read:
    """The store's log as a run at `now` needs it: the month files from lateness.WINDOW before
    now on, whole, after what the summary of the month before them carries; and, from the
    summaries of the months in the lateness.HISTORY before them, when each list got could first
    have been online, the oldest month's for a list got twice. A summary missing is made
    (_summary), and written when keep."""
    months = _months(store)
    first = f"{(now - lateness.WINDOW).astimezone(UTC):%Y-%m}"
    year = f"{(now - lateness.HISTORY).astimezone(UTC):%Y-%m}"
    older = [path for path in months if path.stem < first]
    made: dict[int, dict] = {}
    bounds: dict[str, dict[datetime, datetime]] = {}
    for i, path in enumerate(older):
        if path.stem < year:
            continue
        for name, summed in _summary(months, i, keep, made)["lists"].items():
            found = bounds.setdefault(name, {})
            for m, b in summed["online"].items():
                when, bound = runs.parse(str(m)), runs.parse(str(b))
                if when is not None and bound is not None:
                    found.setdefault(when, bound)
    entries = _carried(_summary(months, len(older) - 1, keep, made)["carry"]) if older else []
    entries += _lines(_old_path(store))
    entries += [entry for path in months[len(older) :] for entry in _lines(path)]
    return Read(entries, bounds)


def checks(found: list[dict], name: str) -> list[tuple[datetime, bool]]:
    """Each check of one list in a store's log: when, and whether it failed."""
    out = []
    for entry in found:
        when = runs.parse(str(entry.get("at", "")))
        if entry.get("list") == name and when is not None and "result" in entry:
            out.append((when, entry["result"] == "failed"))
    return out


FOUND = ("kept", "new")  # a check that got a new list: tcgcsv's says "new"


def online(
    found: list[dict], name: str, known: Mapping[datetime, datetime] | None = None
) -> dict[datetime, datetime]:
    """When each list a check of it got could first have been online, by when it was made: the
    last check before that got nothing new, or its made if that came later. A failed check tells
    nothing, so it's passed over. known holds those got before found (read's online), and a list
    got again keeps when it was first got."""
    out: dict[datetime, datetime] = dict(known or {})
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
    """When a list not due is asked next, and when the next is expected; and when it's online,
    if it's learned to go online after that."""
    assert plan.expected is not None  # a list still learning is always due (riffle.cadence)
    said = f"next asked {times.shown(plan.next)}; its next {what} expected {times.shown(plan.expected)}"
    if plan.opens is not None and plan.opens > plan.expected:
        said = f"{said}, online from about {times.shown(plan.opens)}"
    return said


def learning(plan: cadence.Plan) -> str:
    """What a list asked because it's still learning says after its result, so a list that
    never learns is seen: how many gaps it has of the ones it needs."""
    return f"asked at every firing until it has {lateness.LEAST_GAPS} gaps, {plan.gaps} so far"


class Noted:
    """A step whose ok and warn notes end with one of its own (learning)."""

    def __init__(self, inner: Step, note: str):
        self.inner, self.note = inner, note

    def update(self, done: int, total: int | None = None) -> None:
        self.inner.update(done, total)

    def ok(self, note: str = "") -> None:
        self.inner.ok(f"{note}; {self.note}" if note else self.note)

    def warn(self, note: str) -> None:
        self.inner.warn(f"{note}; {self.note}")

    def fail(self, why: str) -> None:
        self.inner.fail(why)

    def drop(self) -> None:
        self.inner.drop()


def made(folder: Path) -> list[datetime]:
    """When each list kept in folder was made, oldest first."""
    return sorted(filter(None, map(runs.parse, runs.kept(folder))))


def again(
    store: str, folder: Path, stamp: str, sha256: str, fresh: Path, name: str, now: datetime
) -> tuple[dict, str]:
    """A list fetched whole under a stamp already kept (its ETag lost, or changed with the list
    the same age): compared with the one kept. The same: nothing more is kept. Different, or the
    kept one can't be read: the file as served is set aside (set_aside), since it may be the
    only copy of what the source served. The log entry's facts, and what the step adds."""
    if runs.matches(runs.kept(folder)[stamp], sha256):
        return {"same": True}, "the same as the one kept"
    where = set_aside(store, fresh, name, now)
    return {"same": False, "aside": where}, f"this copy isn't the one kept: set aside as {where}"


class Broken(net.FetchError):
    """A file served whole that isn't what it should be. facts go in the check's log entry."""

    def __init__(self, why: str, facts: dict):
        super().__init__(why)
        self.facts = facts


def broken(
    store: str,
    name: str,
    publish: str | None,
    fresh: Path,
    served: str,
    now: datetime,
    why: str,
    put: Callable[[], str] | None = None,
) -> Broken:
    """A download that can't be kept as a list, set aside in the store's aside folder (by put,
    or set_aside named by served) unless a copy of the same publish is set aside already, or one
    the same byte for byte, so a file served broken for days is kept once. publish names it: its
    stamp, or its ETag; with neither, only a copy the same byte for byte isn't set aside again.
    The error to raise, its facts for the log (publish, and aside or not_kept)."""
    sha256 = runs.file_sha256(fresh)
    facts: dict = {"publish": publish, "served_sha256": sha256, "served_size": fresh.stat().st_size}
    for entry in back(store):
        if entry.get("list") != name or not entry.get("aside"):
            continue
        if entry.get("served_sha256") == sha256 or (publish is not None and entry.get("publish") == publish):
            was = entry["aside"]
            said = f"{why}; a copy of this publish is set aside already as {was}, asked again next run"
            return Broken(said, facts | {"not_kept": True})
    where = put() if put is not None else set_aside(store, fresh, served, now)
    return Broken(f"{why}; set aside as {where}, asked again next run", facts | {"aside": where})


def broken_logged(
    store: str, name: str, publish: str | None, fresh: Path, served: str, now: datetime, why: str
) -> Broken:
    """broken(), for a file fetched outside a watch of lists (Pass), which logs each check: its
    own entry logged under name, so the next copy of the publish finds it."""
    e = broken(store, name, publish, fresh, served, now, why)
    log(store, {"at": at(now), "list": name, "result": "failed", "why": str(e)} | e.facts)
    return e


Ask = Callable[[dict[str, str], datetime, Step], dict]  # (ETags, now, step) -> the check's log entry


@dataclass(frozen=True)
class Listed:
    """One list a store's watch asks for. ask gets the ETags, the time and the step, ends the
    step, and returns the check's log entry; one that fails raises, and the step fails."""

    name: str  # in the log, and the ETag it's saved under
    label: str  # its step
    folder: Path  # where its lists are kept, by the run rule
    ask: Ask
    noun: str = "list"  # what the line of lists not due counts
    history: Callable[[], list[datetime]] | None = None  # when each was made, if more than folder holds


def _count(n: int, noun: str) -> str:
    return f"{n:,} {noun}" if n == 1 else f"{n:,} {noun}s"


class Pass:
    """One run of a store's watch over its lists, holding the store's lock: each list asked
    when riffle.cadence says it's due (from when its lists kept were made, its checks, and
    when each went online, read once and sorted by list), or always (the sync), and each check
    logged. A list still learning
    says how many gaps it has (learning). Once the source gives no answer at all, the lists
    after it fail without asking; any other error fails only its own list's step."""

    def __init__(
        self, store: str, source: str, tracker: Tracker, clock: Callable[[], datetime], always: bool
    ):
        self.store, self.source, self.tracker, self.clock, self.always = store, source, tracker, clock, always
        self.res = Watch()
        self.tags = load_tags(store)
        self.log = read(store, clock(), keep=True)
        self.lists = by_list(self.log.entries)
        self.answering = True
        self._waiting: list[tuple[cadence.Plan, Listed]] = []
        self._learning: dict[str, str] = {}  # what each list due while learning adds, by name

    def due(self, item: Listed) -> bool:
        """Whether a list is due; one that isn't waits for the line of lists not due."""
        if self.always:
            return True
        now = self.clock()
        found = self.lists.get(item.name, [])
        busy = longest(found, item.name, now, lateness.WINDOW)
        kept = item.history() if item.history else made(item.folder)
        bounds = online(found, item.name, self.log.online.get(item.name))
        plan = cadence.plan(kept, checks(found, item.name), now, busy, bounds)
        if not plan.ask:
            self.res.waiting.append(item.label)
            self._waiting.append((plan, item))
        elif plan.learning:
            self._learning[item.name] = learning(plan)
        return plan.ask

    def ask(self, item: Listed) -> dict:
        """Ask for a list once, on its own step, and log the check. Its log entry."""
        now = self.clock()
        entry: dict = {"at": at(now), "list": item.name}
        if not self.answering:
            why = f"not asked: {self.source} gave no answer"
            self.res.failed.append((item.label, why))
            self.tracker.step(item.label).fail(why)
            log(self.store, entry | {"result": "failed", "why": why})
            return entry | {"result": "failed"}
        step = self.tracker.step(item.label, unit="bytes")
        note = self._learning.pop(item.name, None)
        started = time.monotonic()
        try:
            entry |= item.ask(self.tags, now, step if note is None else Noted(step, note))
        except (net.FetchError, OSError) as e:
            self.answering = not isinstance(e, net.NoAnswer)
            self.res.failed.append((item.label, str(e)))
            step.fail(str(e))
            entry |= {"result": "failed", "why": str(e)} | getattr(e, "facts", {})
        except Exception as e:  # a bug: its traceback to errors.log, and the next list is asked
            why = f"{failure(e)}; asked again next run"
            self.res.failed.append((item.label, why))
            step.fail(why)
            entry |= {"result": "failed", "why": why}
        else:
            if "why" in entry:
                self.res.failed.append((item.label, entry["why"]))
            sorts = {"kept": self.res.kept, "empty": self.res.empty, "missing": self.res.empty}
            sorts.get(entry["result"], self.res.same).append(item.label)
        entry["seconds"] = round(time.monotonic() - started, 1)
        log(self.store, entry)
        self.lists.setdefault(item.name, []).append(entry)
        return entry

    def each(self, lists: Iterable[Listed]) -> None:
        for item in lists:
            if self.due(item):
                self.ask(item)

    def said_waiting(self) -> None:
        """The lists not due, on one line: how many, and the next asked, with its schedule."""
        if not self._waiting:
            return
        nouns: dict[str, int] = {}
        for _, item in self._waiting:
            nouns[item.noun] = nouns.get(item.noun, 0) + 1
        plan, item = min(self._waiting, key=lambda waited: waited[0].next)
        counted = " and ".join(_count(n, noun) for noun, n in nouns.items())
        which = item.label.removeprefix(f"{self.source} ")
        self.tracker.step(f"{self.source} {counted}").ok(f"none due; {which} {waiting(plan, item.noun)}")


def many(
    store: str,
    source: str,
    lists: Iterable[Listed],
    tracker: Tracker,
    clock: Callable[[], datetime] = times.now,
    always: bool = False,
    then: Callable[[Pass], None] | None = None,
) -> Watch:
    """The watch of a store's lists (Pass), the lists not due on one line; then, still holding
    the lock, whatever else the store asks once a run. The ETags kept are saved however the run
    ends. A run that finds the store's lock held asks nothing."""
    with held(store) as mine:
        if not mine:
            tracker.step(source).ok("another run is asking for its lists")
            return Watch(busy=True)
        run = Pass(store, source, tracker, clock, always)
        try:
            run.each(lists)
            run.said_waiting()
            if then is not None and run.answering:
                then(run)
        finally:
            save_tags(store, run.tags)
    return run.res
