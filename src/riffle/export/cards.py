"""A note behind every card link, so hovering a card in any list shows it.

In the notes folder (config's notes):

    mtg/_generated/cards/<safe name>.md   one for each card a note links: its picture, links to
                                          its pages, its text, price, legality and copies owned
    mtg/_generated/cards/img/<id>.jpg     its pictures, kept with card_images = "cache"

A card's note is named by its safe name (export/links.py), so a link in that form resolves with
no change to any list, and the lists that run a card are its note's backlinks. Obsidian's page
preview shows a note when its link is hovered. Each note is written whole, only when it changed;
a card no note links any more loses its note, and a picture no note shows is removed.

Pictures come from Scryfall's image server, whose terms let software link to them and keep
copies, shown whole: nothing cropped, distorted or watermarked. A note never shows a foil.
"""

import json
import re
import shutil
import threading
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote, quote_plus

from riffle import net
from riffle.export.links import card_link, safe_name, targets
from riffle.export.obsidian import write_whole
from riffle.models import CardRules, CardView, Face, Holding, Prices
from riffle.store import Catalog

FOLDER = "cards"  # in _generated/
IMAGES = "img"  # in the cards folder
WORKERS = 4  # pictures fetched at once
GIVE_UP = 5  # failed fetches before the rest wait for the next sync: the server or network is down

Fetch = Callable[[str, Path], int | None]  # url, dest -> bytes written, or None if it isn't there
Progress = Callable[[int, int | None], None]  # (pictures fetched so far, pictures to fetch)

PRICE = re.compile(r"^Price on (\d{4}-\d{2}-\d{2}): (.*)$", re.M)
STATUSES = (
    ("legal", "Legal in"),
    ("restricted", "Restricted in"),
    ("banned", "Banned in"),
    ("not_legal", "Not legal in"),
)


@dataclass
class Links:
    """What the notes' card links come to."""

    cards: dict[str, str] = field(default_factory=dict)  # card_id -> its note's name, the safe name
    misses: dict[str, tuple[str, int]] = field(
        default_factory=dict
    )  # target -> (the link that resolves, notes)
    taken: dict[str, Path] = field(default_factory=dict)  # a card's safe name -> the note already named so
    clashes: dict[str, str] = field(default_factory=dict)  # a card -> the card its note's name names


@dataclass
class Result:
    cards: int = 0
    written: int = 0
    removed: int = 0
    misses: dict[str, tuple[str, int]] = field(default_factory=dict)
    taken: dict[str, Path] = field(default_factory=dict)
    clashes: dict[str, str] = field(default_factory=dict)
    fetched: int = 0
    unfetched: int = 0  # pictures still linked from Scryfall, to fetch at the next sync
    why: str = ""  # the last fetch's failure, when one failed


def gather(notes: Iterable[Path]) -> Counter[str]:
    """Each link target in these notes, with how many notes link it. A note that can't be read
    is passed over: the sync reports it."""
    found: Counter[str] = Counter()
    for p in notes:
        try:
            found.update(set(targets(p.read_text(encoding="utf-8"))))
        except (OSError, UnicodeDecodeError):
            continue
    return found


def sort(found: Counter[str], catalog: Catalog, names: dict[str, Path], own: Iterable[str] = ()) -> Links:
    """The cards these targets link, each under the note it gets. A link whose target is a
    note's name opens that note, and isn't a card's. Otherwise it names a card when its target
    resolves in the catalog. One whose target isn't the card's note name (a full name with a
    slash in it, say) can't open that note: it's a miss, with the link that would. A card whose
    name another note has gets no note: a link would open that one. Nor does a card whose note's
    name is another card's (Bind // Liberate's is Bind): a note belongs to the card its name
    resolves to. Riffle's own links (own: its tables and version logs) get notes the same way and
    are never reported: a version log is never edited, and the tables are rewritten each sync."""
    out = Links()
    every = dict.fromkeys(own, 0) | dict(found)  # notes linking each: none, for Riffle's alone
    for target, n in sorted(every.items()):
        if target.casefold() in names:
            continue
        card_id = catalog.resolve(target)
        if card_id is None:
            continue
        name = catalog.name(card_id)
        safe = safe_name(name)
        owner = catalog.resolve(safe)
        if owner != card_id:
            if n:
                out.clashes[name] = catalog.name(owner) if owner else safe
            continue
        if safe.casefold() in names:
            if n:
                out.taken[safe] = names[safe.casefold()]
            continue
        out.cards[card_id] = safe
        if n and target.casefold() != safe.casefold():
            out.misses[target] = (card_link(name), n)
    return out


def scryfall_url(name: str, view: CardView | None) -> str:
    """The card's page on Scryfall: the printing pictured, else a search for the name."""
    if view and view.set_code:
        return f"https://scryfall.com/card/{quote(view.set_code)}/{quote(view.collector_number)}"
    return f"https://scryfall.com/search?q={quote_plus(f'!"{name}"')}"


def pages(name: str, view: CardView | None) -> str:
    out = [
        f"[Scryfall]({scryfall_url(name, view)})",
        f"[EDHREC](https://edhrec.com/route/?cc={quote_plus(name)})",
    ]
    if view and view.multiverse_id:
        gatherer = f"https://gatherer.wizards.com/Pages/Card/Details.aspx?multiverseid={view.multiverse_id}"
        out.append(f"[Gatherer]({gatherer})")
    return " · ".join(out)


def face_lines(face: Face) -> list[str]:
    """A face's name and cost, its type and stats, then its text a line at a time."""
    head = face.name + (f" {face.mana_cost}" if face.mana_cost else "")
    kind = " · ".join(x for x in (face.type_line, face.stats) if x)
    return [x for x in (head, kind, *face.text.splitlines()) if x]


def price_line(prices: Prices | None, day: str | None, old: str | None = None) -> str:
    """The card's price on the Scryfall day the catalog holds. The day stays what the note said
    while the price doesn't move by a cent, so the note is rewritten only when it does."""
    if day is None:
        return ""
    usd = f"${prices.usd:,.2f} paper" if prices and prices.usd is not None else "no paper price"
    tix = f"{prices.tix:,.2f} tix" if prices and prices.tix is not None else "not on MTGO"
    said = f"{usd}, {tix} (Scryfall's, at the cheapest printing that can be played)."
    m = PRICE.search(old or "")
    if m and m.group(2) == said:
        day = m.group(1)
    return f"Price on {day}: {said}"


def legality(rules: CardRules | None, formats: Iterable[str]) -> str:
    """The card's standing in each format the vault has lists in that Scryfall knows."""
    by: dict[str, list[str]] = defaultdict(list)
    for fmt in sorted(formats):
        if rules and fmt in rules.legalities:
            by[rules.legalities[fmt]].append(fmt)
    return " ".join(f"{words} {', '.join(by[status])}." for status, words in STATUSES if by[status])


def owned(holdings: list[Holding]) -> str:
    """The copies the collection has, by printing."""
    if not holdings:
        return "Not owned."
    places: Counter[str] = Counter()
    for h in holdings:
        place = " ".join(x for x in ((h.set_code or "").upper(), h.collector_number or "") if x)
        places[(place or "printing unknown") + (" foil" if h.foil else "")] += h.quantity
    each = ", ".join(f"{n} {place}" for place, n in sorted(places.items()))
    return f"Owned: {sum(places.values())} ({each})."


def note(
    name: str,
    safe: str,
    view: CardView | None,
    pictures: list[tuple[str, str]],
    facts: list[str],
) -> str:
    """A card's note: its pictures, its pages, each face, then the facts (price, legality,
    copies owned). The full name is an alias when the note's name isn't it."""
    out = ["---", "type: card", "game: mtg"]
    if name != safe:
        out.append(f"aliases: [{json.dumps(name, ensure_ascii=False)}]")
    out += ["---", ""]
    if pictures:
        out += [f"![{face}]({src})" for face, src in pictures] + [""]
    out += [pages(name, view), ""]
    for face in view.faces if view else ():
        head = f"**{face.name}**" + (f" {face.mana_cost}" if face.mana_cost else "")
        out += ["  \n".join([head, *face_lines(face)[1:]]), ""]
    for fact in facts:
        if fact:
            out += [fact, ""]
    out.append("> [!warning] Generated by `riffle sync` — edits here are overwritten")
    return "\n".join(out) + "\n"


def picture_files(view: CardView | None) -> list[tuple[str, str, str]]:
    """(face, Scryfall's URL, the file kept) for each picture of the printing pictured. The URL's
    query, a cache-buster that changes when Scryfall rescans, is left off."""
    if view is None:
        return []
    return [
        (face, url.split("?")[0], f"{view.scryfall_id}{'' if i == 0 else f'-{i + 1}'}.jpg")
        for i, (face, url) in enumerate(view.images)
    ]


def fetch_pictures(
    wanted: dict[Path, str], fetch: Fetch, progress: Progress | None = None
) -> tuple[int, str]:
    """Fetch each picture to its file, a few at a time. After GIVE_UP failures the rest wait for
    the next sync. The pictures fetched, and the last failure's why."""
    lock = threading.Lock()
    fetched, failed, why = 0, 0, ""

    def one(dest: Path, url: str) -> None:
        nonlocal fetched, failed, why
        if failed >= GIVE_UP:
            return
        try:
            got = fetch(url, dest) is not None
            reason = "" if got else f"{url} isn't there"
        except (net.FetchError, OSError) as e:
            got, reason = False, str(e)
        with lock:
            fetched += got
            if not got:
                failed, why = failed + 1, reason
            if progress:
                progress(fetched, len(wanted))

    with ThreadPoolExecutor(WORKERS) as pool:
        list(pool.map(lambda item: one(*item), wanted.items()))
    return fetched, why


def write(
    gen_dir: Path,
    links: Links,
    catalog: Catalog,
    holdings: list[Holding],
    formats: Iterable[str],
    pictures: str = "cache",
    fetch: Fetch | None = None,
    progress: Progress | None = None,
) -> Result:
    """Write a note for each linked card into gen_dir/cards/, those that changed, and remove the
    rest. With pictures = "cache", fetch each picture not kept yet (none when fetch is None, as
    offline) and link the copy; one not fetched yet is linked from Scryfall."""
    folder = gen_dir / FOLDER
    img = folder / IMAGES
    ids = list(links.cards)
    views, rules, prices = catalog.card_views(ids), catalog.rules(ids), catalog.prices(ids)
    day = catalog.prices_day()
    formats = sorted(set(formats))
    copies: dict[str, list[Holding]] = defaultdict(list)
    for h in holdings:
        if h.card_id:
            copies[h.card_id].append(h)
    res = Result(cards=len(ids), misses=links.misses, taken=links.taken, clashes=links.clashes)

    files = {cid: picture_files(views.get(cid)) for cid in ids} if pictures != "off" else {}
    if pictures == "cache":
        kept = {name for shown in files.values() for _, _, name in shown}
        wanted = {
            img / name: url for shown in files.values() for _, url, name in shown if not (img / name).exists()
        }
        if wanted and fetch:
            res.fetched, res.why = fetch_pictures(wanted, fetch, progress)
        res.unfetched = sum(not dest.exists() for dest in wanted)
        if img.is_dir():
            for p in img.iterdir():
                if p.name not in kept:
                    p.unlink()
    elif img.is_dir():  # pictures no longer kept
        shutil.rmtree(img)

    existing = {p.name.casefold(): p for p in folder.glob("*.md")} if folder.is_dir() else {}
    keep = set()
    for cid, safe in links.cards.items():
        path = folder / f"{safe}.md"
        was = existing.get(path.name.casefold())
        old = was.read_text(encoding="utf-8") if was else None
        shown = [
            (face, f"{IMAGES}/{name}" if pictures == "cache" and (img / name).exists() else url)
            for face, url, name in files.get(cid, [])
        ]
        facts = [
            price_line(prices.get(cid), day.isoformat() if day else None, old),
            legality(rules.get(cid), formats),
            owned(copies.get(cid, [])),
        ]
        text = note(catalog.name(cid), safe, views.get(cid), shown, facts)
        if was and was.name != path.name:  # the same name in another case: take its name
            was.unlink()
        res.written += write_whole(path, text)
        keep.add(path.name.casefold())
    for key, p in existing.items():
        if key not in keep:
            p.unlink()
            res.removed += 1
    return res


def remove(gen_dir: Path) -> int:
    """With card notes off: remove the folder, and say how many notes were in it."""
    folder = gen_dir / FOLDER
    if not folder.is_dir():
        return 0
    n = sum(1 for _ in folder.glob("*.md"))
    shutil.rmtree(folder)
    return n
