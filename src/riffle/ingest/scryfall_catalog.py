"""The Magic catalog in Postgres, loaded from Scryfall's bulk card file and set list.

Scryfall is Magic's creating source: its oracle ids seed cards, its card ids
printings, its set ids sets (riffle.db.ids). build() turns the two files into a
catalog Batch; riffle.db.catalog merges it.

What goes where:

* A card's own fields (name, type line, rules text, mana cost, colors,
  legalities) are the same on every printing of it, except two things.
  Legality differs across printings for some cards, since Scryfall marks
  non-tournament printings not_legal everywhere, so the strongest status wins
  (models.merge_legalities). And a reversible printing carries the card's
  fields on its faces, both copies of one card, so another printing of the
  card supplies them when there is one.
* A double-faced card without card-level colors or mana cost (transform,
  modal) takes its front face's: the card's own characteristics everywhere but
  the battlefield (comprehensive rules 712.8a). Its rules text joins the faces'.
* Colors are stored in WUBRG order; Scryfall lists them alphabetically.
* extra keeps a listed set of Scryfall's other fields (CARD_EXTRA,
  PRINTING_EXTRA, SET_EXTRA). Left out: URLs Scryfall builds from IDs, fields
  that have columns, and EDHREC and Penny Dreadful ranks, which move daily and
  would rewrite most cards every day for nothing a card is.
* MTGO and Arena IDs are mapped to printings in external_ids when exactly one
  printing carries them; a few Arena IDs are shared by two printings, and those
  stay unmapped. TCGplayer and Cardmarket IDs stay in extra: one TCGplayer
  product can cover several printings, which the price keys (v0.4.0) handle.
* Format codes are Scryfall's legality keys (modern, duel, paupercommander).

A load is skipped when Postgres already holds the downloaded bulk file: its
external_ids.last_seen is the file's own updated_at, not the time of the load,
so loading the same file again would change nothing.

Every command reads cards from here (riffle.store.postgres). A failed load
leaves the last one in place, so commands carry on with the day before's cards.
"""

import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import Connection, Engine, func, select
from sqlalchemy.exc import OperationalError

from riffle import WUBRG, db
from riffle.db import catalog, migrate
from riffle.db.catalog import TABLES, Batch, CardRow, LoadResult, PrintingRow, SetRow
from riffle.ingest import scryfall
from riffle.models import merge_legalities
from riffle.progress import SILENT, Tracker

GAME = "mtg"
STEP = "card catalog"

FORMAT_NAMES = {
    "standard": "Standard",
    "future": "Future Standard",
    "alchemy": "Alchemy",
    "pioneer": "Pioneer",
    "historic": "Historic",
    "timeless": "Timeless",
    "modern": "Modern",
    "legacy": "Legacy",
    "vintage": "Vintage",
    "pauper": "Pauper",
    "penny": "Penny Dreadful",
    "commander": "Commander",
    "duel": "Duel Commander",
    "paupercommander": "Pauper Commander",
    "predh": "PreDH",
    "oathbreaker": "Oathbreaker",
    "brawl": "Brawl",
    "standardbrawl": "Standard Brawl",
    "gladiator": "Gladiator",
    "oldschool": "Old School",
    "premodern": "Premodern",
}  # any other key is its own name until it gets one here

CARD_EXTRA = (
    "power",
    "toughness",
    "loyalty",
    "defense",
    "hand_modifier",
    "life_modifier",
    "color_indicator",
    "produced_mana",
    "game_changer",
)
FACE_ORACLE = (
    "name",
    "mana_cost",
    "type_line",
    "oracle_text",
    "colors",
    "color_indicator",
    "power",
    "toughness",
    "loyalty",
    "defense",
)
PRINTING_EXTRA = (
    "artist",
    "artist_ids",
    "illustration_id",
    "card_back_id",
    "flavor_name",
    "flavor_text",
    "printed_name",
    "printed_type_line",
    "printed_text",
    "watermark",
    "frame_effects",
    "promo_types",
    "security_stamp",
    "full_art",
    "textless",
    "oversized",
    "booster",
    "story_spotlight",
    "reprint",
    "variation",
    "variation_of",
    "attraction_lights",
    "content_warning",
    "highres_image",
    "image_status",
    "games",
    "multiverse_ids",
    "mtgo_id",
    "mtgo_foil_id",
    "arena_id",
    "tcgplayer_id",
    "tcgplayer_etched_id",
    "cardmarket_id",
)
FACE_PRINTED = (
    "name",
    "artist",
    "artist_id",
    "illustration_id",
    "flavor_name",
    "flavor_text",
    "printed_name",
    "printed_type_line",
    "printed_text",
    "watermark",
)
SET_EXTRA = (
    "block_code",
    "block",
    "printed_size",
    "digital",
    "foil_only",
    "nonfoil_only",
    "mtgo_code",
    "arena_code",
    "tcgplayer_id",
)
PRICES = ("usd", "usd_foil", "usd_etched", "eur", "eur_foil", "tix")


class NotReady(Exception):
    """Postgres can't take the load yet: the schema is behind, or there's no bulk file."""


@dataclass
class Loaded:
    published: datetime  # the bulk file's updated_at
    result: LoadResult | None  # None: the catalog already held this bulk file


def oracle_id(card: dict[str, Any]) -> str:
    """The card a printing is of. Reversible printings name it on their faces instead."""
    found = card.get("oracle_id") or next(
        (f["oracle_id"] for f in card.get("card_faces") or [] if f.get("oracle_id")), None
    )
    if not found:
        raise ValueError(f"no oracle id on {card.get('name')} ({card.get('id')})")
    return str(found)


def build(
    cards: Iterable[dict[str, Any]],
    sets: list[dict[str, Any]],
    published: datetime,
    progress: Callable[[int], None] | None = None,
) -> Batch:
    """Card objects from a bulk file and Scryfall's set list, as a catalog batch."""
    batch = Batch(game=GAME, seen_at=published)
    found: dict[str, CardRow] = {}
    stand_ins: set[str] = set()  # cards so far seen only on reversible printings
    card_sets: dict[str, tuple[str, str, str | None]] = {}  # set id -> (code, name, type), as cards give them
    # IDs, format keys, and statuses repeat across 100,000+ printings; interned, each is held once.
    for n, card in enumerate(cards, 1):
        ref = sys.intern(oracle_id(card))
        reversible = card.get("layout") == "reversible_card"
        if ref not in found or (ref in stand_ins and not reversible):
            found[ref] = _card(card, ref)
            if reversible:
                stand_ins.add(ref)
            else:
                stand_ins.discard(ref)
        statuses = card.get("legalities") or {}
        held = batch.legalities.get(ref)
        if held != statuses:  # most printings say what's held already
            merged = merge_legalities([held or {}, statuses])
            batch.legalities[ref] = {sys.intern(fmt): sys.intern(status) for fmt, status in merged.items()}
        batch.printings.append(_printing(card, ref))
        batch.printing_ids.extend(_other_ids(card))
        card_sets.setdefault(card["set_id"], (card["set"], card["set_name"], card.get("set_type")))
        if progress and n % 1000 == 0:
            progress(n)
    batch.cards = list(found.values())
    formats = {fmt for by_format in batch.legalities.values() for fmt in by_format}
    batch.formats = {fmt: FORMAT_NAMES.get(fmt, fmt) for fmt in sorted(formats)}
    batch.sets = _sets(sets, card_sets)
    return batch


def load_catalog(
    conn: Connection, force: bool = False, progress: Callable[[int, int | None], None] | None = None
) -> Loaded:
    """Load the downloaded bulk file and set list on conn, inside the caller's transaction,
    unless the catalog already holds this bulk file (force loads it anyway). progress gets
    the printings read so far and the count the last load held, a close guess at the total."""
    why = migrate.behind(conn)
    if why:
        raise NotReady(why)
    bulk = scryfall.bulk_file()
    if bulk is None:
        raise NotReady("no bulk file yet — run: riffle ingest scryfall")
    published = scryfall.bulk_updated_at(bulk)
    loaded = catalog.loaded_through(conn, GAME)
    if not force and loaded is not None and loaded >= published:
        return Loaded(published, None)
    total = _printings_held(conn) or None
    counted = (lambda n: progress(n, total)) if progress else None
    batch = build(scryfall.cards(bulk), scryfall.read_sets(), published, counted)
    return Loaded(published, catalog.load(conn, batch))


def _printings_held(conn: Connection) -> int:
    printings = TABLES["printings"]
    held = select(func.count()).where(printings.c.game_id == GAME, printings.c.retired_at.is_(None))
    return int(conn.execute(held).scalar_one())


def update(tracker: Tracker = SILENT, force: bool = False, engine: Engine | None = None) -> LoadResult | None:
    """The card catalog step of a Scryfall refresh. A failure is reported on the step, never
    raised: the catalog keeps its last load, which a sync can still use."""
    step = tracker.step(STEP, unit="printings")
    try:
        eng = engine or db.engine()
    except db.BadURL as e:
        step.fail(str(e))
        return None
    try:
        conn = eng.connect()
    except OperationalError as e:
        url = db.display(eng.url.render_as_string(hide_password=False))
        step.fail(f"can't reach Postgres at {url}: {db.reason(e)} (riffle db up starts it)")
        return None
    try:
        with conn, conn.begin():
            loaded = load_catalog(conn, force, progress=step.update)
    except NotReady as e:
        step.fail(str(e))
        return None
    except Exception as e:
        step.fail(f"{type(e).__name__}: {db.reason(e)}")
        return None
    if loaded.result is None:
        step.ok(f"current, Scryfall {loaded.published:%Y-%m-%d}")
    else:
        step.ok(summary(loaded.result))
    return loaded.result


def summary(result: LoadResult) -> str:
    """38,690 cards · 118,389 printings · 1,052 sets · 1,204 rows written · 22 shared arena IDs unmapped"""
    parts = [f"{result.cards:,} cards", f"{result.printings:,} printings", f"{result.sets:,} sets"]
    parts.append(f"{result.changed:,} rows written" if result.changed else "unchanged")
    parts += [f"{n:,} shared {source} IDs unmapped" for source, n in sorted(result.shared.items())]
    return " · ".join(parts)


# ---- one Scryfall object at a time -----------------------------------------------------


def _card(card: dict[str, Any], ref: str) -> CardRow:
    faces = card.get("card_faces") or []
    reversible = card.get("layout") == "reversible_card" and bool(faces)
    fields = {**card, **faces[0]} if reversible else card  # a reversible's faces carry the card's fields
    card_faces = [] if reversible else faces
    front = card_faces[0] if card_faces else {}
    if "oracle_text" in fields:
        text = fields["oracle_text"]
    else:
        text = "\n".join(t for f in card_faces if (t := f.get("oracle_text")))
    extra = {k: fields[k] for k in CARD_EXTRA if fields.get(k) is not None}
    for key in ("color_indicator", "produced_mana"):
        if key in extra:
            extra[key] = _wubrg(extra[key])
    if card_faces:
        extra["card_faces"] = [_face(f) for f in card_faces]
    specific = {
        "mana_cost": fields.get("mana_cost", front.get("mana_cost")),
        "mana_value": Decimal(str(fields.get("cmc", front.get("cmc")) or 0)),
        "colors": _wubrg(fields.get("colors", front.get("colors")) or []),
        "color_identity": _wubrg(card.get("color_identity") or []),
        "keywords": list(card.get("keywords") or []),
        "layout": fields["layout"],
        "reserved": bool(card.get("reserved")),
    }
    return CardRow(ref, fields["name"], fields.get("type_line"), text, extra, specific)


def _face(face: dict[str, Any]) -> dict[str, Any]:
    out = {k: face[k] for k in FACE_ORACLE if face.get(k) is not None}
    for key in ("colors", "color_indicator"):
        if key in out:
            out[key] = _wubrg(out[key])
    return out


def _printing(card: dict[str, Any], card_ref: str) -> PrintingRow:
    faces = card.get("card_faces") or []
    extra = {k: card[k] for k in PRINTING_EXTRA if card.get(k) is not None}
    if faces:
        extra["card_faces"] = [_printed_face(f) for f in faces]
    images = card.get("image_uris") or (faces[0].get("image_uris") if faces else None) or {}
    prices = card.get("prices") or {}
    specific = {
        "finishes": list(card.get("finishes") or []),
        "promo": bool(card.get("promo")),
        "digital": bool(card.get("digital")),
        "border_color": card.get("border_color"),
        "frame": card.get("frame"),
        **{key: _price(prices.get(key)) for key in PRICES},
    }
    return PrintingRow(
        ref=card["id"],
        card=card_ref,
        set=sys.intern(card["set_id"]),
        collector_number=card["collector_number"],
        lang=sys.intern(card.get("lang") or "en"),
        rarity=card.get("rarity"),
        released_at=_date(card.get("released_at")),
        image_url=images.get("normal"),
        extra=extra,
        specific=specific,
    )


def _printed_face(face: dict[str, Any]) -> dict[str, Any]:
    out = {k: face[k] for k in FACE_PRINTED if face.get(k) is not None}
    image = (face.get("image_uris") or {}).get("normal")
    if image:
        out["image_url"] = image
    return out


def _other_ids(card: dict[str, Any]) -> list[tuple[str, str, str]]:
    """MTGO's catalog IDs (a foil has its own) and Arena's, for the external_ids registry."""
    given = (
        ("mtgo", card.get("mtgo_id")),
        ("mtgo", card.get("mtgo_foil_id")),
        ("arena", card.get("arena_id")),
    )
    return [(source, str(value), card["id"]) for source, value in given if value is not None]


def _sets(listed: list[dict[str, Any]], from_cards: dict[str, tuple[str, str, str | None]]) -> list[SetRow]:
    """Scryfall's set list, plus any set a card names that the list lacks (a set list older
    than the bulk file, or none yet), built from what cards say about it."""
    by_code = {s["code"]: s["id"] for s in listed}
    rows = [
        SetRow(
            ref=s["id"],
            code=s["code"],
            name=s["name"],
            set_type=s.get("set_type"),
            released_at=_date(s.get("released_at")),
            parent=by_code.get(s["parent_set_code"]) if s.get("parent_set_code") else None,
            extra={k: s[k] for k in SET_EXTRA if s.get(k) is not None},
        )
        for s in listed
    ]
    known = set(by_code.values())
    rows += [
        SetRow(ref=ref, code=code, name=name, set_type=kind)
        for ref, (code, name, kind) in from_cards.items()
        if ref not in known
    ]
    return rows


def _wubrg(symbols: Iterable[str]) -> list[str]:
    """Colors in WUBRG order; any other symbol (C, in produced mana) after them, as given."""
    given = list(symbols)
    return [c for c in WUBRG if c in given] + [s for s in given if s not in WUBRG]


def _price(value: object) -> Decimal | None:
    if value is None:
        return None
    try:
        price = Decimal(str(value))
    except InvalidOperation:
        return None
    return price if price.is_finite() else None


def _date(value: object) -> date | None:
    return date.fromisoformat(value) if isinstance(value, str) and value else None
