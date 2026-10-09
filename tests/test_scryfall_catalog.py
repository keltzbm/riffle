"""Scryfall card objects and set list -> catalog rows, and the card catalog step."""

import gzip
import json
from contextlib import contextmanager
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import cast

import pytest
from sqlalchemy import Connection, Engine, make_url, text

from riffle.db import migrate
from riffle.db.catalog import LoadResult
from riffle.ingest import scryfall
from riffle.ingest import scryfall_catalog as sc

PUBLISHED = datetime(2026, 9, 24, 21, 5, 36, tzinfo=UTC)
LEGAL = {"standard": "not_legal", "modern": "legal", "commander": "legal"}
SETS = [
    {"id": "set-rav", "code": "rav", "name": "Ravnica", "set_type": "expansion", "released_at": "2005-10-07"},
    {
        "id": "set-prav",
        "code": "prav",
        "name": "Ravnica Promos",
        "set_type": "promo",
        "released_at": "2005-10-07",
        "parent_set_code": "rav",
        "digital": False,
        "block": "Ravnica",
        "icon_svg_uri": "https://svgs.scryfall.io/sets/rav.svg?1",
    },
    {"id": "set-orphan", "code": "xyz", "name": "Orphan Promos", "parent_set_code": "gone"},
]


def printing(**fields):
    """A Scryfall card object with what every one carries, overridden by fields."""
    card = {
        "id": "p-mortify",
        "oracle_id": "o-mortify",
        "name": "Mortify",
        "lang": "en",
        "layout": "normal",
        "set_id": "set-rav",
        "set": "rav",
        "set_name": "Ravnica",
        "set_type": "expansion",
        "collector_number": "221",
        "released_at": "2005-10-07",
        "rarity": "uncommon",
        "mana_cost": "{1}{W}{B}",
        "cmc": 3.0,
        "type_line": "Instant",
        "oracle_text": "Destroy target creature or enchantment.",
        "colors": ["B", "W"],
        "color_identity": ["B", "W"],
        "keywords": [],
        "legalities": dict(LEGAL),
        "reserved": False,
        "game_changer": False,
        "finishes": ["nonfoil", "foil"],
        "promo": False,
        "digital": False,
        "border_color": "black",
        "frame": "2003",
        "games": ["paper", "mtgo"],
        "artist": "Glen Angus",
        "edhrec_rank": 812,
        "penny_rank": 90,
        "uri": "https://api.scryfall.com/cards/p-mortify",
        "purchase_uris": {"tcgplayer": "https://example.com"},
        "image_uris": {"normal": "https://cards.scryfall.io/normal/front/p-mortify.jpg?1", "small": "s"},
        "prices": {
            "usd": "0.25",
            "usd_foil": "1.50",
            "usd_etched": None,
            "eur": "0.20",
            "eur_foil": None,
            "tix": "0.03",
        },
        "tcgplayer_id": 12345,
        "mtgo_id": 111,
        "mtgo_foil_id": 112,
    }
    card.update(fields)
    return card


def face(**fields):
    return {"object": "card_face", **fields}


def build(*cards, sets=SETS, progress=None):
    return sc.build(list(cards), sets, PUBLISHED, progress)


def one(rows, ref):
    return next(r for r in rows if r.ref == ref)


# ---- cards ----------------------------------------------------------------------------


def test_a_card_and_its_printing():
    batch = build(printing())
    (card,) = batch.cards
    assert (card.ref, card.name, card.type_line, card.rules_text) == (
        "o-mortify",
        "Mortify",
        "Instant",
        "Destroy target creature or enchantment.",
    )
    assert card.specific == {
        "mana_cost": "{1}{W}{B}",
        "mana_value": Decimal("3.0"),
        "colors": ["W", "B"],  # Scryfall lists B, W
        "color_identity": ["W", "B"],
        "keywords": [],
        "layout": "normal",
        "reserved": False,
    }
    assert card.extra == {"game_changer": False}  # EDHREC and Penny ranks move daily: left out
    (p,) = batch.printings
    assert (p.ref, p.card, p.set, p.collector_number, p.lang, p.rarity, p.released_at) == (
        "p-mortify",
        "o-mortify",
        "set-rav",
        "221",
        "en",
        "uncommon",
        date(2005, 10, 7),
    )
    assert p.image_url == "https://cards.scryfall.io/normal/front/p-mortify.jpg?1"
    assert p.extra == {
        "artist": "Glen Angus",
        "games": ["paper", "mtgo"],
        "tcgplayer_id": 12345,
        "mtgo_id": 111,
        "mtgo_foil_id": 112,
    }  # no URLs, no card or set fields
    assert p.specific == {
        "finishes": ["nonfoil", "foil"],
        "promo": False,
        "digital": False,
        "border_color": "black",
        "frame": "2003",
        "usd": Decimal("0.25"),
        "usd_foil": Decimal("1.50"),
        "usd_etched": None,
        "eur": Decimal("0.20"),
        "eur_foil": None,
        "tix": Decimal("0.03"),
    }
    assert batch.legalities == {"o-mortify": LEGAL}
    assert batch.formats == {"commander": "Commander", "modern": "Modern", "standard": "Standard"}
    assert (batch.game, batch.seen_at) == ("mtg", PUBLISHED)


def test_mtgo_and_arena_ids_are_offered_for_mapping():
    batch = build(printing(arena_id=900), printing(id="p-2", mtgo_id=None, mtgo_foil_id=None))
    assert batch.printing_ids == [
        ("mtgo", "111", "p-mortify"),
        ("mtgo", "112", "p-mortify"),
        ("arena", "900", "p-mortify"),
    ]


def test_legality_merges_across_printings_strongest_first():
    """A non-tournament printing says not_legal everywhere; the card is still legal."""
    gold = printing(
        id="p-gold", legalities={"standard": "not_legal", "modern": "not_legal", "commander": "not_legal"}
    )
    banned = printing(id="p-new", legalities={**LEGAL, "commander": "banned", "future": "legal"})
    batch = build(gold, printing(), banned)
    assert batch.legalities["o-mortify"] == {
        "standard": "not_legal",
        "modern": "legal",
        "commander": "banned",
        "future": "legal",
    }
    assert batch.formats["future"] == "Future Standard"


def test_an_unnamed_format_is_named_by_its_key():
    batch = build(printing(legalities={**LEGAL, "tlr": "legal"}))
    assert batch.formats["tlr"] == "tlr"


def test_a_double_faced_card_takes_its_front_face_s_colors_and_cost():
    delver = printing(
        id="p-delver",
        oracle_id="o-delver",
        name="Delver of Secrets // Insectile Aberration",
        layout="transform",
        type_line="Creature — Human Wizard // Creature — Human Insect",
        cmc=1.0,
        color_identity=["U"],
        card_faces=[
            face(
                name="Delver of Secrets",
                mana_cost="{U}",
                type_line="Creature — Human Wizard",
                oracle_text="Transform it.",
                colors=["U"],
                power="1",
                toughness="1",
                artist="Matt Stewart",
                image_uris={"normal": "https://cards.scryfall.io/normal/front/d.jpg"},
            ),
            face(
                name="Insectile Aberration",
                mana_cost="",
                type_line="Creature — Human Insect",
                oracle_text="Flying",
                colors=["U"],
                color_indicator=["U"],
                power="3",
                toughness="2",
                image_uris={"normal": "https://cards.scryfall.io/normal/back/d.jpg"},
            ),
        ],
    )
    for key in ("mana_cost", "colors", "oracle_text", "image_uris"):
        del delver[key]
    batch = build(delver)
    (card,) = batch.cards
    assert card.specific["mana_cost"] == "{U}" and card.specific["colors"] == ["U"]
    assert card.rules_text == "Transform it.\nFlying"
    assert card.extra["card_faces"][1] == {
        "name": "Insectile Aberration",
        "mana_cost": "",
        "type_line": "Creature — Human Insect",
        "oracle_text": "Flying",
        "colors": ["U"],
        "color_indicator": ["U"],
        "power": "3",
        "toughness": "2",
    }
    (p,) = batch.printings
    assert p.image_url == "https://cards.scryfall.io/normal/front/d.jpg"
    assert p.extra["card_faces"] == [
        {
            "name": "Delver of Secrets",
            "artist": "Matt Stewart",
            "image_url": "https://cards.scryfall.io/normal/front/d.jpg",
        },
        {"name": "Insectile Aberration", "image_url": "https://cards.scryfall.io/normal/back/d.jpg"},
    ]


def test_a_split_card_keeps_its_own_cost_and_joins_its_faces_text():
    fire_ice = printing(
        id="p-fi",
        oracle_id="o-fi",
        name="Fire // Ice",
        layout="split",
        mana_cost="{1}{R} // {1}{U}",
        colors=["R", "U"],
        card_faces=[
            face(name="Fire", mana_cost="{1}{R}", oracle_text="Fire deals 2 damage."),
            face(name="Ice", mana_cost="{1}{U}", oracle_text=""),
        ],
    )
    del fire_ice["oracle_text"]
    (card,) = build(fire_ice).cards
    assert card.specific["mana_cost"] == "{1}{R} // {1}{U}" and card.specific["colors"] == ["U", "R"]
    assert card.rules_text == "Fire deals 2 damage."  # empty faces add nothing


def reversible(ref="p-rev", oracle="o-mortify"):
    """A Secret Lair reversible printing: the card's fields are on both faces."""
    side = {
        "oracle_id": oracle,
        "layout": "normal",
        "name": "Mortify",
        "mana_cost": "{1}{W}{B}",
        "cmc": 3.0,
        "type_line": "Instant",
        "oracle_text": "Destroy target creature or enchantment.",
        "colors": ["B", "W"],
    }
    card = printing(
        id=ref,
        name="Mortify // Mortify",
        layout="reversible_card",
        set_id="set-prav",
        collector_number="9",
        card_faces=[face(**side, artist="A", flavor_text="front"), face(**side, artist="B")],
    )
    for key in ("oracle_id", "mana_cost", "cmc", "type_line", "oracle_text", "colors", "image_uris"):
        del card[key]
    return card


def test_a_reversible_printing_belongs_to_its_faces_card():
    batch = build(reversible(), printing())
    card = one(batch.cards, "o-mortify")
    assert card.name == "Mortify" and "card_faces" not in card.extra  # from the normal printing
    rev = one(batch.printings, "p-rev")
    assert rev.card == "o-mortify"
    assert rev.extra["card_faces"] == [
        {"name": "Mortify", "artist": "A", "flavor_text": "front"},
        {"name": "Mortify", "artist": "B"},
    ]


def test_a_card_known_only_from_a_reversible_printing_takes_the_face_s_fields():
    (card,) = build(reversible()).cards
    assert (card.name, card.type_line, card.rules_text) == (
        "Mortify",
        "Instant",
        "Destroy target creature or enchantment.",
    )
    assert card.specific["layout"] == "normal" and card.specific["mana_value"] == Decimal("3.0")
    assert card.specific["colors"] == ["W", "B"] and "card_faces" not in card.extra


def test_a_printing_without_an_oracle_id_anywhere_is_an_error():
    card = printing()
    del card["oracle_id"]
    with pytest.raises(ValueError, match=r"no oracle id on Mortify \(p-mortify\)"):
        build(card)


@pytest.mark.parametrize(
    ("given", "expected"),
    [("0.25", Decimal("0.25")), (None, None), ("", None), ("n/a", None), ("NaN", None), (2, Decimal("2"))],
)
def test_prices(given, expected):
    (p,) = build(printing(prices={"usd": given})).printings
    assert p.specific["usd"] == expected and p.specific["tix"] is None


def test_colorless_symbols_follow_the_colors():
    (card,) = build(printing(produced_mana=["C", "G", "W"], color_indicator=["G", "W"])).cards
    assert card.extra["produced_mana"] == ["W", "G", "C"]
    assert card.extra["color_indicator"] == ["W", "G"]


# ---- sets -------------------------------------------------------------------------------


def test_sets_come_from_the_set_list_with_their_parents():
    batch = build(printing(), printing(id="p-promo", set_id="set-prav", set="prav"))
    rav, prav, orphan = (one(batch.sets, ref) for ref in ("set-rav", "set-prav", "set-orphan"))
    assert (rav.code, rav.name, rav.set_type, rav.released_at, rav.parent) == (
        "rav",
        "Ravnica",
        "expansion",
        date(2005, 10, 7),
        None,
    )
    assert prav.parent == "set-rav"
    assert prav.extra == {"digital": False, "block": "Ravnica"}  # the icon URL is left out
    assert orphan.parent is None  # its parent isn't in the list
    assert len(batch.sets) == 3  # listed sets without printings are kept


def test_a_set_the_list_lacks_is_built_from_what_its_cards_say():
    batch = build(printing(set_id="set-new", set="new", set_name="Brand New", set_type="expansion"), sets=[])
    (new,) = batch.sets
    assert (new.ref, new.code, new.name, new.set_type, new.released_at, new.parent) == (
        "set-new",
        "new",
        "Brand New",
        "expansion",
        None,
        None,
    )


def test_progress_is_reported_every_thousand_printings():
    seen: list[int] = []
    build(*(printing(id=f"p-{n}", collector_number=str(n)) for n in range(2500)), progress=seen.append)
    assert seen == [1000, 2000]


# ---- the step's note ------------------------------------------------------------------------


def test_summary():
    result = LoadResult(sets=1052, cards=38690, printings=118389, written={"cards": 3, "external_ids": 1201})
    assert sc.summary(result) == "38,690 cards · 118,389 printings · 1,052 sets · 1,204 rows written"
    result = LoadResult(sets=1, cards=2, printings=3, shared={"arena": 22})
    assert sc.summary(result) == "2 cards · 3 printings · 1 sets · unchanged · 22 shared arena IDs unmapped"


# ---- loading the download ------------------------------------------------------------------


def write_download(cards, sets=SETS, published=PUBLISHED):
    """A bulk file, set list, and bulk-meta.json, as riffle ingest scryfall leaves them."""
    folder = scryfall.meta_path().parent
    (folder / "scryfall").mkdir(parents=True, exist_ok=True)
    with gzip.open(folder / "default-cards.jsonl.gz", "wt", encoding="utf-8") as f:
        f.writelines(json.dumps(card) + "\n" for card in cards)
    scryfall.sets_path().write_text(json.dumps({"object": "list", "data": sets}))
    scryfall.meta_path().write_text(json.dumps({"updated_at": published.isoformat()}))


@pytest.mark.postgres
def test_the_download_is_loaded_once(pg):
    write_download([printing(), printing(id="p-promo", set_id="set-prav", set="prav", collector_number="9")])
    first = sc.load_catalog(pg)
    assert first.published == PUBLISHED and first.result is not None
    assert (first.result.cards, first.result.printings, first.result.sets) == (1, 2, 3)
    assert sc.load_catalog(pg) == sc.Loaded(PUBLISHED, None)  # Postgres holds this file already
    forced = sc.load_catalog(pg, force=True)
    assert forced.result is not None and forced.result.changed == 0
    counts = pg.execute(
        text("SELECT (SELECT count(*) FROM printings), (SELECT count(*) FROM legalities)")
    ).one()
    assert counts == (2, 3)


@pytest.mark.postgres
def test_a_newer_download_is_loaded(pg):
    write_download([printing()])
    assert sc._printings_held(pg) == 0
    sc.load_catalog(pg)
    assert sc._printings_held(pg) == 1  # the next load's progress total
    later = datetime(2026, 9, 25, 21, tzinfo=UTC)
    write_download([printing(prices={"usd": "0.30"})], published=later)
    loaded = sc.load_catalog(pg)
    assert loaded.result is not None
    assert loaded.result.written == {
        "mtg_printings": 1,
        "external_ids": 7,
    }  # 3 sets, a card, a printing, 2 MTGO IDs


@pytest.mark.postgres
def test_there_is_nothing_to_load_before_the_first_download(pg):
    with pytest.raises(sc.NotReady, match="no bulk file yet — run: riffle ingest scryfall"):
        sc.load_catalog(pg)


@pytest.mark.parametrize(("revision", "shown"), [("0001", "0001"), (None, "empty")])
def test_the_schema_must_be_at_head(monkeypatch, revision, shown):
    monkeypatch.setattr(migrate, "revision", lambda conn: revision)
    with pytest.raises(
        sc.NotReady, match=f"schema {shown}, head is {migrate.head()} — run: riffle db upgrade"
    ):
        # nothing usable: NotReady is raised before the connection is touched
        sc.load_catalog(cast(Connection, None))


class FakeConnection:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    @contextmanager
    def begin(self):
        yield


class FakeEngine:
    url = make_url("postgresql+psycopg://tcg@localhost:5432/tcg")

    def connect(self):
        return FakeConnection()


def update_with(monkeypatch, tracker, load_catalog):
    monkeypatch.setattr(sc, "load_catalog", load_catalog)
    return sc.update(tracker, engine=cast(Engine, FakeEngine()))


def test_the_step_reports_what_the_load_wrote(monkeypatch, tracker):
    write_download([printing()])
    result = LoadResult(sets=1, cards=2, printings=3, written={"cards": 2})

    def load_catalog(conn, force, progress):
        progress(1000, 3)
        return sc.Loaded(PUBLISHED, result)

    assert update_with(monkeypatch, tracker, load_catalog) is result
    assert tracker.outcomes() == {"card catalog": ("ok", "2 cards · 3 printings · 1 sets · 2 rows written")}
    (step,) = tracker.steps
    assert (step.unit, step.updates) == ("printings", [(1000, 3)])


def test_the_step_says_when_postgres_holds_the_download_already(monkeypatch, tracker):
    forced = []

    def load_catalog(conn, force, progress):
        forced.append(force)
        return sc.Loaded(PUBLISHED, None)

    assert update_with(monkeypatch, tracker, load_catalog) is None
    assert forced == [False]
    assert tracker.outcomes() == {"card catalog": ("ok", "current, Scryfall 2026-09-24")}


@pytest.mark.parametrize(
    ("error", "note"),
    [
        (
            sc.NotReady("no bulk file yet — run: riffle ingest scryfall"),
            "no bulk file yet — run: riffle ingest scryfall",
        ),
        (KeyError("layout"), "KeyError: 'layout'"),
    ],
    ids=["not ready", "unexpected"],
)
def test_a_failed_load_is_reported_never_raised(monkeypatch, tracker, error, note):
    """The catalog keeps its last load, so a failed one can't stop a sync."""

    def load_catalog(conn, force, progress):
        raise error

    assert update_with(monkeypatch, tracker, load_catalog) is None
    assert tracker.outcomes() == {"card catalog": ("fail", note)}


def test_an_unreachable_server_is_reported(tracker):
    assert sc.update(tracker) is None  # the test config's database, on a port nothing listens on
    (outcome,) = tracker.outcomes().values()
    assert outcome is not None and outcome[0] == "fail"
    assert outcome[1].startswith("can't reach Postgres at postgresql+psycopg://tcg@127.0.0.1:1/unreachable: ")
    assert outcome[1].endswith("(riffle db up starts it)")
