"""Precon boxes you own, as MTGJSON lists them, and the printings ManaBox misread from them.

ManaBox's scanner matches a card by its art and often takes an earlier printing of a reprint:
on 2026-10-05 it recorded most of the Foundations Commander boxes' cards under TDC, SOC, M3C and
other sets rather than FDC. Scryfall gives each printing's illustration, and of the 255 such rows
that day, 253 had exactly the box printing's. So a card a registered box holds, recorded under
another printing with the same illustration, is read as the box's printing: never more copies
than the boxes hold, less those recorded as it already. A basic land is left as recorded, and so
is a copy with other art. The export itself is never changed: the correction is made each time
it's read.

A box is registered by a deck note (riffle.vault.precon_boxes) with the name MTGJSON gives it,
or that name without its parenthesised part ("Counter Blitz" for "Counter Blitz (FINAL FANTASY
X)"); a Collector's Edition is named in full. Where MTGJSON has two decks of one name, their set
tells them apart: "Evasive Maneuvers (C13)". Registering a box adds no copies: what you own comes
from ManaBox alone.

Each box's list comes from MTGJSON's deck files as the store keeps them (riffle watch mtgjson):
DeckList names the file, AllDeckFiles holds it. AllDeckFiles is about 0.9 GB and takes 2 s to
read, so the lists are kept in <data_dir>/precons.json and read again only when a box is
registered or a newer build of the deck files is kept.
"""

import io
import json
import re
import tarfile
from collections import Counter, defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath

from riffle import runs
from riffle.config import data_dir
from riffle.ingest import mtgjson
from riffle.models import Holding, Printing
from riffle.runs import zstd
from riffle.store import Catalog

CACHE = "precons.json"
DECK_LIST, DECK_FILES = "deck-list", "all-deck-files"
SECTIONS = ("commander", "mainBoard", "sideBoard", "tokens")  # a box's cards; displayCommander repeats one
SET = re.compile(r"^(?P<name>.+) \((?P<code>[^()]+)\)$")
NOT_KEPT = (
    "MTGJSON's deck files aren't kept yet (riffle watch mtgjson keeps them); printings aren't corrected"
)


@dataclass(frozen=True)
class Box:
    name: str  # as registered
    deck: str  # MTGJSON's name for it
    code: str  # its set
    cards: tuple[tuple[str, int], ...]  # (Scryfall ID, copies)


@dataclass
class Boxes:
    boxes: list[Box] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)  # each box that can't be read, and why


@dataclass(frozen=True)
class Fix:
    """Copies of one row read as a box's printing."""

    name: str
    recorded: str  # "TDC 244"
    read_as: str  # "FDC 188"
    copies: int
    boxes: tuple[str, ...]  # the boxes holding the printing it's read as


@dataclass(frozen=True)
class Short:
    """Copies of a box's printing the collection doesn't record as it, after the corrections."""

    name: str
    place: str
    copies: int
    boxes: tuple[str, ...]
    elsewhere: tuple[str, ...]  # where the collection records the card instead, with other art


@dataclass
class Corrected:
    holdings: list[Holding]
    printings: int = 0  # the boxes' printings, basic lands aside
    fixes: list[Fix] = field(default_factory=list)
    short: list[Short] = field(default_factory=list)


def match(name: str, decks: list[dict]) -> list[dict]:
    """MTGJSON's decks a registered name names, from DeckList's entries."""
    hits = _named(name, decks)
    if not hits and (m := SET.match(name)):
        hits = [d for d in _named(m["name"], decks) if d.get("code", "").lower() == m["code"].lower()]
    return hits


def _named(name: str, decks: list[dict]) -> list[dict]:
    exact = [d for d in decks if d.get("name") == name]
    return exact or [
        d for d in decks if d.get("name", "").startswith(f"{name} (") and d["name"].endswith(")")
    ]


def _unmatched(name: str, notes: list[Path], hits: list[dict]) -> str:
    where = f'precon box "{name}" in {", ".join(p.name for p in notes)}'
    if not hits:
        return f"{where} isn't among MTGJSON's decks: write the name MTGJSON gives it"
    each = ", ".join(f"{d['name']} ({d.get('code', '?')})" for d in hits)
    return f"{where} names {len(hits)} of MTGJSON's decks, {each}: add the set of yours, e.g. {name} (SET)"


def _cards(deck: dict) -> list[list]:
    copies: Counter[str] = Counter()
    for section in SECTIONS:
        for card in deck.get(section) or []:
            if sid := (card.get("identifiers") or {}).get("scryfallId"):
                copies[sid] += int(card.get("count") or 1)
    return [[sid, n] for sid, n in copies.items()]


def _deck_files(path: Path, wanted: set[str]) -> dict[str, dict]:
    """The deck files named (DeckList's fileName), from a kept AllDeckFiles."""
    found = {}
    with tarfile.open(fileobj=io.BytesIO(runs.rebuild(path))) as tar:
        for member in tar:
            stem = PurePosixPath(member.name).stem
            if member.isfile() and stem in wanted and (f := tar.extractfile(member)):
                found[stem] = json.load(f)["data"]
    return found


def _read(path: Path) -> dict:
    try:
        cache = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return cache if isinstance(cache, dict) else {}


def _write(path: Path, cache: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_name(path.name + ".part")
    part.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    part.replace(path)


def _box(name: str, kept: dict) -> Box:
    return Box(name, kept["deck"], kept["code"], tuple((sid, n) for sid, n in kept["cards"]))


def load(registry: Mapping[str, list[Path]]) -> Boxes:
    """The registered boxes' lists (registry: each box's name and the notes naming it), from the
    cache when it was read from the newest deck files kept, else from the store; the cache then
    holds the registered boxes alone."""
    path = data_dir() / CACHE
    cache = _read(path)
    if not registry:
        if cache:
            path.unlink(missing_ok=True)
        return Boxes()
    files = runs.kept(mtgjson.lists_dir(DECK_FILES))
    lists = runs.kept(mtgjson.lists_dir(DECK_LIST))
    if not (files and lists):
        return Boxes(problems=[NOT_KEPT])
    stamp = next(reversed(files))
    have = dict(cache.get("boxes") or {}) if cache.get("deck_files") == stamp else {}
    out = Boxes()
    missing = [name for name in registry if name not in have]
    try:
        if missing:
            decks = json.loads(runs.rebuild(next(reversed(lists.values()))))["data"]
            wanted = {}
            for name in missing:
                hits = match(name, decks)
                if len(hits) == 1:
                    wanted[name] = hits[0]
                else:
                    out.problems.append(_unmatched(name, registry[name], hits))
            read = _deck_files(files[stamp], {d["fileName"] for d in wanted.values()}) if wanted else {}
            for name, d in wanted.items():
                if d["fileName"] in read:
                    have[name] = {
                        "deck": d["name"],
                        "code": d.get("code", ""),
                        "cards": _cards(read[d["fileName"]]),
                    }
                else:
                    out.problems.append(
                        f"precon box \"{name}\": {d['fileName']} isn't in MTGJSON's deck files"
                    )
    except (OSError, ValueError, KeyError, zstd.ZstdError, tarfile.TarError) as e:
        out.problems.append(f"MTGJSON's deck files can't be read ({e}); printings aren't corrected")
        return out
    registered = {name: have[name] for name in registry if name in have}
    if {"deck_files": stamp, "boxes": registered} != cache:
        _write(path, {"deck_files": stamp, "boxes": registered})
    out.boxes = [_box(name, kept) for name, kept in registered.items()]
    return out


def cached() -> Boxes:
    """The boxes the last load kept, for a command that doesn't read the vault."""
    try:
        return Boxes(
            [_box(name, kept) for name, kept in (_read(data_dir() / CACHE).get("boxes") or {}).items()]
        )
    except (KeyError, TypeError, ValueError):  # a cache in another shape: none, until the next load
        return Boxes()


def _place(p: Printing) -> str:
    return f"{p.set_code.upper()} {p.collector_number}"


def correct(holdings: list[Holding], boxes: Boxes, catalog: Catalog) -> Corrected:
    """The holdings with each misread copy read as its box's printing (see above). The holdings
    are resolved first (riffle.analysis.resolve), and stay in their order; a row whose copies
    go to a box's printing in part is split."""
    owners: dict[str, list[str]] = defaultdict(list)  # a box's printing -> the boxes holding it
    for box in boxes.boxes:
        for sid, _n in box.cards:
            owners[sid].append(box.name)
    printings = catalog.printings(set(owners) | {h.scryfall_id for h in holdings if h.scryfall_id})
    left: dict[str, Counter[str]] = defaultdict(Counter)  # card -> box printing -> copies to find
    for box in boxes.boxes:
        for sid, n in box.cards:
            if (p := printings.get(sid)) and not catalog.is_basic(p.card_id):
                left[p.card_id][sid] += n
    res = Corrected([], sum(len(c) for c in left.values()))
    for h in holdings:
        if h.card_id in left and h.scryfall_id and h.scryfall_id in left[h.card_id]:
            left[h.card_id][h.scryfall_id] -= h.quantity
    for h in holdings:
        p = printings.get(h.scryfall_id or "")
        want = left.get(h.card_id or "")
        qty = h.quantity
        if want and p and p.illustration_id and h.scryfall_id not in want and h.source == "manabox":
            for sid, n in want.items():
                target = printings[sid]
                if n > 0 and qty and target.illustration_id == p.illustration_id:
                    k = min(qty, n)
                    want[sid] -= k
                    qty -= k
                    moved = replace(
                        h,
                        quantity=k,
                        scryfall_id=sid,
                        set_code=target.set_code.upper(),
                        collector_number=target.collector_number,
                    )
                    res.holdings.append(moved)
                    res.fixes.append(Fix(h.name, _place(p), _place(target), k, tuple(owners[sid])))
        if qty:
            res.holdings.append(h if qty == h.quantity else replace(h, quantity=qty))
    elsewhere: dict[str, set[str]] = defaultdict(set)
    for h in res.holdings:
        if h.card_id in left and h.scryfall_id not in left[h.card_id]:
            p = printings.get(h.scryfall_id or "")
            elsewhere[h.card_id].add(_place(p) if p else f"{h.set_code or '?'} {h.collector_number or '?'}")
    res.short = sorted(
        (
            Short(
                printings[sid].name,
                _place(printings[sid]),
                n,
                tuple(owners[sid]),
                tuple(sorted(elsewhere[card])),
            )
            for card, want in left.items()
            for sid, n in want.items()
            if n > 0
        ),
        key=lambda s: (s.name, s.place),
    )
    res.fixes.sort(key=lambda f: (f.name, f.recorded, f.read_as))
    return res
