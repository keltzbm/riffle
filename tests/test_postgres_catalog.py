"""The card catalog every command reads, over Scryfall card objects loaded by the real loader."""

from datetime import UTC, date, datetime

import pytest
from sqlalchemy import text

from riffle.db import catalog, ids, migrate
from riffle.ingest import scryfall_catalog as sc
from riffle.models import Printing
from riffle.store import postgres
from riffle.store.postgres import PostgresCatalog, Unavailable

PUBLISHED = datetime(2026, 9, 24, 21, tzinfo=UTC)
LEGAL = {"commander": "legal", "modern": "not_legal", "legacy": "legal"}
NOWHERE = dict.fromkeys(LEGAL, "not_legal")


def card(sid, oracle, name, **fields):
    """A Scryfall card object with what the loader needs, overridden by fields; a field
    given as None is one the object lacks."""
    obj = {
        "id": sid,
        "oracle_id": oracle,
        "name": name,
        "layout": "normal",
        "set_id": "set-one",
        "set": "one",
        "set_name": "One",
        "collector_number": sid,
        "lang": "en",
        "released_at": "2020-01-01",
        "rarity": "rare",
        "type_line": "Enchantment",
        "oracle_text": "Text.",
        "cmc": 1.0,
        "colors": ["G"],
        "color_identity": ["G"],
        "legalities": dict(LEGAL),
        "finishes": ["nonfoil"],
        "games": ["paper", "mtgo"],
        "prices": {"usd": "1.00", "tix": "0.10"},
    }
    code = fields.get("set", "one")
    given = obj | {"set_id": f"set-{code}", "set_name": code.upper()} | fields
    return {k: v for k, v in given.items() if v is not None}


CARDS = [
    # legality merges across printings: a newer gold-border printing legal nowhere hides nothing
    card("lib-1", "o-lib", "Sylvan Library"),
    card("lib-2", "o-lib", "Sylvan Library", released_at="2026-08-01", legalities=NOWHERE, prices={}),
    card("crypt-1", "o-crypt", "Mana Crypt", legalities={**LEGAL, "commander": "banned"}),
    card("crypt-2", "o-crypt", "Mana Crypt", released_at="2025-01-01"),
    # the real Sol Ring, a Secret Lair reversible of it, and an art card that shares its front name
    card("sol-1", "o-sol", "Sol Ring", type_line="Artifact", color_identity=[], colors=[]),
    card("sol-2", "o-sol", "Sol Ring", type_line="Artifact", color_identity=[], colors=[], set="c21"),
    card(
        "sol-sld",
        None,
        "Sol Ring // Sol Ring",
        layout="reversible_card",
        type_line=None,
        card_faces=[
            {
                "oracle_id": "o-sol",
                "name": "Sol Ring",
                "type_line": "Artifact",
                "oracle_text": "{T}: Add {C}{C}.",
            },
            {
                "oracle_id": "o-sol",
                "name": "Sol Ring",
                "type_line": "Artifact",
                "oracle_text": "{T}: Add {C}{C}.",
            },
        ],
        released_at="2026-01-01",
        prices={"usd": "12.00"},
    ),
    card(
        "sol-art",
        "o-sol-art",
        "Sol Ring // Sol Ring",
        layout="art_series",
        type_line="Card // Card",
        color_identity=[],
        released_at="2026-02-01",
        prices={"usd": "0.25"},
    ),
    # a split card and a modal double-faced card whose text lives on its faces
    card(
        "fire",
        "o-fire",
        "Fire // Ice",
        layout="split",
        color_identity=["R", "U"],
        oracle_text=None,
        card_faces=[
            {"name": "Fire", "oracle_text": "Fire text."},
            {"name": "Ice", "oracle_text": "Ice text."},
        ],
    ),
    card(
        "valakut",
        "o-valakut",
        "Valakut Awakening // Valakut Stoneforge",
        layout="modal_dfc",
        oracle_text=None,
        card_faces=[
            {"name": "Valakut Awakening", "oracle_text": "Put cards."},
            {"name": "Valakut Stoneforge"},
        ],
    ),
    card(
        "rider",
        "o-rider",
        "Murderous Rider // Swift End",
        layout="adventure",
        oracle_text=None,
        card_faces=[{"name": "Murderous Rider"}, {"name": "Swift End"}],
    ),
    # prices: the cheapest paper printing that isn't digital, the cheapest tix of any
    card("bolt-1", "o-bolt", "Lightning Bolt", prices={"usd": "2.50", "tix": "0.05"}),
    card("bolt-2", "o-bolt", "Lightning Bolt", prices={"usd": "1.25", "tix": "0.50"}, set="m10"),
    card("bolt-mo", "o-bolt", "Lightning Bolt", digital=True, prices={"usd": "0.01", "tix": "0.02"}),
    # a price is the cheapest printing that can be played (C48): not gold- or silver-bordered, not
    # from a memorabilia set, not oversized; a card with none falls back to its cheapest
    card("tomb-1", "o-tomb", "Ancient Tomb", prices={"usd": "123.01"}),
    card(
        "tomb-wc",
        "o-tomb",
        "Ancient Tomb",
        set="wc99",
        set_type="memorabilia",
        border_color="gold",
        prices={"usd": "56.21"},
    ),
    card("tomb-30a", "o-tomb", "Ancient Tomb", set="30a", set_type="memorabilia", prices={"usd": "60.00"}),
    card("tomb-big", "o-tomb", "Ancient Tomb", set="ocmd", oversized=True, prices={"usd": "1.00"}),
    card("tomb-un", "o-tomb", "Ancient Tomb", set="ptg", border_color="silver", prices={"usd": "2.00"}),
    card("hans", "o-hans", '"Ach! Hans, Run!"', set="unh", border_color="silver", prices={"usd": "0.50"}),
    card("lotus-1", "o-lotus", "Black Lotus", set="lea", prices={}),
    card("lotus-ce", "o-lotus", "Black Lotus", set="ced", set_type="memorabilia", prices={"usd": "3000.00"}),
    # a Japanese printing with the English one's set and number
    card(
        "forest-ja", "o-forest", "Forest", lang="ja", collector_number="266", type_line="Basic Land — Forest"
    ),
    card("forest-en", "o-forest", "Forest", collector_number="266", type_line="Basic Land — Forest"),
    # Arena: the lowest rarity among Arena printings, special counts as mythic
    card("oko-1", "o-oko", "Oko, Thief of Crowns", rarity="mythic", games=["paper", "arena"]),
    card("oko-2", "o-oko", "Oko, Thief of Crowns", rarity="rare", games=["paper"]),
    card("mox", "o-mox", "Mox Amber", rarity="special", games=["arena"]),
    card("choco", "o-choco", "Summon: Choco/Mog"),
]


def cid(oracle):
    return str(ids.derive("scryfall_oracle", oracle))


def load(pg, cards=CARDS):
    catalog.load(pg, sc.build(cards, [], PUBLISHED), aliases={})
    return PostgresCatalog(pg)


pytestmark = pytest.mark.postgres


@pytest.fixture
def cat(pg):
    return load(pg)


def test_legality_is_the_strongest_status_across_printings(cat):
    rules = cat.rules([cid("o-lib"), cid("o-crypt")])
    assert rules[cid("o-lib")].legalities == LEGAL
    assert rules[cid("o-crypt")].legalities["commander"] == "banned"


def test_rules_come_from_the_card_never_a_reversible_or_art_printing(cat):
    sol = cat.rules([cid("o-sol")])[cid("o-sol")]
    assert (sol.type_line, sol.color_identity) == ("Artifact", ())
    assert cat.rules([cid("o-fire")])[cid("o-fire")].color_identity == ("U", "R")  # WUBRG order
    assert cat.rules([cid("o-valakut")])[cid("o-valakut")].oracle_text == "Put cards."
    assert cat.rules([cid("o-fire")])[cid("o-fire")].oracle_text == "Fire text.\nIce text."


def test_unknown_cards_have_no_rules(cat):
    assert cat.rules([cid("o-nope"), cid("o-lib")]).keys() == {cid("o-lib")}
    assert cat.rules([]) == {}


@pytest.mark.parametrize(
    ("name", "oracle"),
    [
        ("Fire // Ice", "o-fire"),
        ("fire // ice", "o-fire"),
        ("Fire/Ice", "o-fire"),
        ("Fire", "o-fire"),
        ("  Sylvan Library ", "o-lib"),
        ("A-Sylvan Library", "o-lib"),
        ("Valakut Awakening", "o-valakut"),
        ("Sol Ring", "o-sol"),
        ("Sol Ring // Sol Ring", "o-sol-art"),
        ("Not A Card", None),
        # a safe name, as a card's link writes it (export/links.py)
        ("Ach! Hans, Run!", "o-hans"),
        ("ach!  hans, run! ", "o-hans"),
        ("Summon ChocoMog", "o-choco"),
        # a slash inside a name is the name's, not MTGO's way of writing a split card
        ("Summon: Choco/Mog", "o-choco"),
    ],
)
def test_resolve(cat, name, oracle):
    assert cat.resolve(name) == (cid(oracle) if oracle else None)


def test_a_shared_name_means_a_real_card_then_the_one_with_more_printings(pg):
    shared = [
        # two real cards with one name: two printings beat one
        card("twin-1", "o-twin-a", "Twin"),
        card("twin-2", "o-twin-b", "Twin"),
        card("twin-3", "o-twin-b", "Twin", set="two"),
        # a token printed three times never beats the card it's named after
        *(card(f"elf-t{n}", "o-elf-token", "Elf", layout="token", set=f"t{n}") for n in range(3)),
        card("elf", "o-elf", "Elf"),
    ]
    cat = load(pg, CARDS + shared)
    assert cat.resolve("Twin") == cid("o-twin-b")
    assert cat.resolve("Elf") == cid("o-elf")


def test_names_mtgo_names_and_basics(cat):
    assert cat.name(cid("o-rider")) == "Murderous Rider // Swift End"
    assert cat.mtgo_name(cid("o-fire")) == "Fire/Ice"
    assert cat.mtgo_name(cid("o-rider")) == "Murderous Rider"
    assert cat.mtgo_name(cid("o-lib")) == "Sylvan Library"
    assert cat.is_basic(cid("o-forest")) and not cat.is_basic(cid("o-lib"))
    assert cat.name("nope") == "nope" and cat.mtgo_name("nope") == "nope"


def test_the_prices_day_is_the_bulk_file_s_utc_date(pg):
    assert PostgresCatalog(pg).prices_day() is None  # nothing loaded yet
    assert load(pg).prices_day() == date(2026, 9, 24)


def test_prices_are_the_cheapest_printing(cat):
    found = cat.prices([cid("o-bolt"), cid("o-sol"), "nope"])
    assert found.keys() == {cid("o-bolt"), cid("o-sol")}
    bolt, sol = found[cid("o-bolt")], found[cid("o-sol")]
    assert (bolt.usd, bolt.tix) == (1.25, 0.02)  # the digital printing's paper price doesn't count
    assert (sol.usd, sol.tix) == (1.0, 0.1)  # the art card's $0.25 is its own
    assert cat.prices([cid("o-sol")]) == {cid("o-sol"): sol} and cat.prices([]) == {}


def test_a_price_is_the_cheapest_printing_that_can_be_played(cat):
    found = cat.prices([cid("o-tomb"), cid("o-hans"), cid("o-lotus")])
    tomb, hans, lotus = found[cid("o-tomb")], found[cid("o-hans")], found[cid("o-lotus")]
    assert tomb.usd == 123.01  # not the gold border's $56.21, 30A's, the oversized or the silver
    assert hans.usd == 0.5  # every printing silver-bordered: its cheapest
    assert lotus.usd is None  # its playable printing has no price; Collectors' Edition's isn't it


def test_a_kept_day_prices_each_card_as_the_catalog_does(cat):
    """`riffle prices log` applies pricing.on_day to a kept day; for the day the catalog holds,
    it must give what Catalog.prices gives: the rule written twice, once in SQL."""
    from riffle.analysis import pricing

    card_ids = {cid(sc.oracle_id(c)) for c in CARDS}
    day = {c["id"]: c.get("prices") or {} for c in CARDS}
    printings = cat.price_printings(card_ids | {"nope"})
    assert printings.keys() == card_ids
    assert pricing.on_day(printings, day) == cat.prices(card_ids)
    tomb = {p.scryfall_id: p.playable for p in printings[cid("o-tomb")]}
    assert tomb == {"tomb-1": True, "tomb-wc": False, "tomb-30a": False, "tomb-big": False, "tomb-un": False}
    assert cat.price_printings([]) == {}


def test_arena_rarity(cat):
    assert cat.arena_rarity(cid("o-oko")) == "mythic"  # the rare printing isn't on Arena
    assert cat.arena_rarity(cid("o-mox")) == "mythic"
    assert cat.arena_rarity(cid("o-lib")) is None


def test_printings_by_scryfall_id(cat):
    found = cat.printings(["bolt-2", "sol-sld", "nope"])
    assert found == {
        "bolt-2": Printing("bolt-2", cid("o-bolt"), "Lightning Bolt", "m10", "bolt-2", "", "", 1.25),
        "sol-sld": Printing("sol-sld", cid("o-sol"), "Sol Ring", "one", "sol-sld", "", "", 12.0),
    }
    assert cat.printings(["nope"]) == {} and cat.printings([]) == {}


def test_printings_at_a_set_and_number(cat):
    found = cat.printings_at([("ONE", "266"), ("M10", "bolt-2"), ("one", "999")])
    assert {place: p.scryfall_id for place, p in found.items()} == {
        ("ONE", "266"): "forest-en",
        ("M10", "bolt-2"): "bolt-2",
    }
    assert cat.printings_at([]) == {}


def test_retired_rows_are_left_out(pg):
    load(pg)
    cat = load(pg, [c for c in CARDS if c["id"] not in {"bolt-2", "fire"}])
    assert cat.printings(["bolt-2", "bolt-1"]).keys() == {"bolt-1"}
    assert cat.printings_at([("M10", "bolt-2")]) == {}
    assert cat.prices([cid("o-bolt")])[cid("o-bolt")].usd == 2.5
    assert cat.resolve("Fire // Ice") is None and cat.rules([cid("o-fire")]) == {}


# ---- opening the catalog -----------------------------------------------------------------


def test_an_unreachable_server_says_how_to_start_it():
    with pytest.raises(Unavailable) as e, postgres.open_catalog():
        pass
    message = str(e.value)
    assert message.startswith("can't reach Postgres at postgresql+psycopg://tcg@127.0.0.1:1/unreachable: ")
    assert message.endswith("start it with: riffle db up")


def test_an_empty_catalog_says_to_ingest(pg_engine):
    no_cards = "no Magic cards in Postgres yet — run: riffle ingest scryfall"
    with pytest.raises(Unavailable, match=no_cards), postgres.open_catalog(pg_engine):
        pass


def test_a_schema_behind_says_to_upgrade(pg_engine, monkeypatch):
    monkeypatch.setattr(migrate, "revision", lambda conn: "0001")
    behind = f"schema 0001, head is {migrate.head()} — run: riffle db upgrade"
    with pytest.raises(Unavailable, match=behind), postgres.open_catalog(pg_engine):
        pass


def test_a_command_reads_one_read_only_snapshot(pg_engine, monkeypatch):
    monkeypatch.setattr(postgres, "check", lambda conn: None)
    with postgres.open_catalog(pg_engine) as cat:
        settings = cat.conn.execute(
            text("SELECT current_setting('transaction_read_only'), current_setting('transaction_isolation')")
        ).one()
    assert tuple(settings) == ("on", "repeatable read")


# ---- what a card's note shows (card_views) ---------------------------------------------


def pictured(sid, **fields):
    """A printing of one card, with a picture and a high-resolution scan unless fields say not."""
    image = {"normal": f"https://cards.scryfall.io/normal/front/{sid}.jpg?1"}
    return card(sid, "o-pic", "Pictured", **{"image_uris": image, "image_status": "highres_scan", **fields})


# Best first: the note pictures the first of these that's loaded. Never a foil: a printing
# sold only in foil is loaded with every one of them and never pictured.
PICTURE_ORDER = [
    pictured("pic-new", released_at="2020-01-01", multiverse_ids=[456]),
    pictured("pic-old", released_at="2010-01-01"),
    pictured("pic-soon", released_at="2099-01-01"),
    pictured("pic-low", image_status="lowres"),
    pictured("pic-ja", lang="ja"),
    pictured("pic-list", set="plst"),
    pictured("pic-showcase", frame_effects=["showcase"]),
    pictured("pic-sld", set="sld", set_type="box"),
    pictured("pic-promo", promo=True),
    pictured("pic-gold", set="wc97", border_color="gold"),
]
FOIL = pictured("pic-foil", released_at="2026-09-01", finishes=["foil"])


@pytest.mark.parametrize("first", range(len(PICTURE_ORDER)), ids=[c["id"] for c in PICTURE_ORDER])
def test_a_note_pictures_the_plainest_newest_printing_never_a_foil(pg, first):
    cat = load(pg, [FOIL, *PICTURE_ORDER[first:]])
    view = cat.card_views([cid("o-pic")])[cid("o-pic")]
    best = PICTURE_ORDER[first]
    assert view.scryfall_id == best["id"]
    assert view.images == (("Pictured", f"https://cards.scryfall.io/normal/front/{best['id']}.jpg?1"),)
    assert (view.set_code, view.collector_number) == (best.get("set", "one"), best["id"])
    assert view.multiverse_id == (456 if best["id"] == "pic-new" else None)


def test_a_card_printed_only_in_foil_has_no_picture(pg):
    view = load(pg, [FOIL]).card_views([cid("o-pic")])[cid("o-pic")]
    assert (view.images, view.scryfall_id, view.set_code) == ((), "", "")
    assert view.faces[0].name == "Pictured"


def test_a_view_shows_each_face_with_its_cost_and_stats(pg):
    jace = card(
        "jace",
        "o-jace",
        "Jace, Vryn's Prodigy // Jace, Telepath Unbound",
        layout="transform",
        type_line="Legendary Creature — Human Wizard // Legendary Planeswalker — Jace",
        oracle_text=None,
        card_faces=[
            {
                "name": "Jace, Vryn's Prodigy",
                "mana_cost": "{1}{U}",
                "type_line": "Legendary Creature — Human Wizard",
                "oracle_text": "Loot.",
                "power": "0",
                "toughness": "2",
                "image_uris": {"normal": "https://cards.scryfall.io/normal/front/jace.jpg?1"},
            },
            {
                "name": "Jace, Telepath Unbound",
                "mana_cost": "",
                "type_line": "Legendary Planeswalker — Jace",
                "oracle_text": "+1: Up.",
                "loyalty": "5",
                "image_uris": {"normal": "https://cards.scryfall.io/normal/back/jace.jpg?1"},
            },
        ],
    )
    bear = card(
        "bear",
        "o-bear",
        "Grizzly Bears",
        mana_cost="{1}{G}",
        type_line="Creature — Bear",
        power="2",
        toughness="2",
    )
    siege = card("siege", "o-siege", "Invasion of Ergamon", type_line="Battle — Siege", defense="5")
    cat = load(pg, [*CARDS, jace, bear, siege])
    views = cat.card_views(
        [cid("o-jace"), cid("o-bear"), cid("o-siege"), cid("o-fire"), cid("o-lib"), "nope"]
    )
    assert views.keys() == {cid(o) for o in ("o-jace", "o-bear", "o-siege", "o-fire", "o-lib")}
    front, back = views[cid("o-jace")].faces
    assert (front.name, front.mana_cost, front.text, front.stats) == (
        "Jace, Vryn's Prodigy",
        "{1}{U}",
        "Loot.",
        "0/2",
    )
    assert (back.name, back.type_line, back.stats) == (
        "Jace, Telepath Unbound",
        "Legendary Planeswalker — Jace",
        "Loyalty 5",
    )
    assert [face for face, _ in views[cid("o-jace")].images] == [
        "Jace, Vryn's Prodigy",
        "Jace, Telepath Unbound",
    ]
    (bears,) = views[cid("o-bear")].faces
    assert (bears.mana_cost, bears.type_line, bears.stats) == ("{1}{G}", "Creature — Bear", "2/2")
    assert views[cid("o-siege")].faces[0].stats == "Defense 5"
    assert [f.name for f in views[cid("o-fire")].faces] == ["Fire", "Ice"]
    assert views[cid("o-fire")].images == ()  # the fixture's printings have no picture
    assert views[cid("o-lib")].faces[0].stats == ""
    assert cat.card_views([]) == {}


def test_every_real_card_s_safe_name_resolves_to_it(cat):
    """A card's note is named by its safe name: it must lead back to the card. Art cards and
    tokens that share a real card's name lead to the real card, as their full names do."""
    from riffle.export.links import safe_name

    real = {c: card for c, card in cat._cards.items() if card.layout not in postgres.STAND_INS}
    assert all(cat.resolve(safe_name(card.name)) == c for c, card in real.items())


def test_a_name_resolves_whatever_form_its_accents_take(pg):
    import unicodedata

    cat = load(pg, [*CARDS, card("vault", "o-vault", "Lim-Dûl's Vault")])
    assert cat.resolve(unicodedata.normalize("NFD", "Lim-Dûl's Vault")) == cid("o-vault")
