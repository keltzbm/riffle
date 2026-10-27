"""The catalog loader and schema in a real Postgres: IDs, merging, retirement, constraints."""

import dataclasses
import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import insert, select, text
from sqlalchemy.exc import IntegrityError

from riffle.db import catalog, ids
from riffle.db.catalog import Batch, CardRow, PrintingRow, SetRow
from riffle.db.models import Card, ExternalId, Legality, MtgCard, Printing, Set

SEEN = datetime(2026, 9, 24, 21, 5, 36, tzinfo=UTC)
CARD_COLUMNS = {
    "mana_cost": "{1}",
    "mana_value": Decimal(1),
    "colors": [],
    "color_identity": [],
    "keywords": [],
    "layout": "normal",
    "reserved": False,
}
PRINTING_COLUMNS = {
    "finishes": ["nonfoil"],
    "promo": False,
    "digital": False,
    "border_color": "black",
    "frame": "2015",
    "usd": Decimal("1.50"),
    "usd_foil": None,
    "usd_etched": None,
    "eur": None,
    "eur_foil": None,
    "tix": Decimal("0.02"),
}


def card(ref, name, **columns):
    return CardRow(ref, name, "Artifact", "{T}: Add {C}{C}.", {"power": None}, {**CARD_COLUMNS, **columns})


def printing(ref, card_ref, set_ref, number, **columns):
    return PrintingRow(
        ref,
        card_ref,
        set_ref,
        number,
        extra={"artist": "Mark Tedin"},
        specific={**PRINTING_COLUMNS, **columns},
    )


def batch(**changes) -> Batch:
    """Two sets (one a promo set of the other), two cards, three printings."""
    base = Batch(
        game="mtg",
        seen_at=SEEN,
        formats={"legacy": "Legacy", "modern": "Modern"},
        sets=[
            SetRow("s-core", "cor", "Core", "core"),
            SetRow("s-promo", "pcor", "Core Promos", "promo", parent="s-core"),
        ],
        cards=[
            card("c-ring", "Sol Ring"),
            card("c-bolt", "Lightning Bolt", colors=["R"], color_identity=["R"]),
        ],
        printings=[
            printing("p-ring", "c-ring", "s-core", "1"),
            printing("p-bolt", "c-bolt", "s-core", "2"),
            printing("p-ring-promo", "c-ring", "s-promo", "1"),
        ],
        legalities={
            "c-ring": {"legacy": "banned", "modern": "not_legal"},
            "c-bolt": {"legacy": "legal", "modern": "legal"},
        },
        printing_ids=[
            ("mtgo", "1001", "p-ring"),
            ("arena", "7", "p-bolt"),
            ("arena", "8", "p-bolt"),
            ("arena", "9", "p-ring"),  # one Arena ID on two printings: left unmapped
            ("arena", "9", "p-bolt"),
        ],
    )
    return dataclasses.replace(base, **changes)


def load(pg, b=None, aliases=None):
    return catalog.load(pg, b or batch(), aliases=aliases or {})


def card_id(ref):
    return ids.derive("scryfall_oracle", ref)


def printing_id(ref):
    return ids.derive("scryfall", ref)


def set_id(ref):
    return ids.derive("scryfall_set", ref)


def snapshot(pg):
    """Every catalog row, as stored."""
    tables = (
        "formats",
        "sets",
        "cards",
        "printings",
        "legalities",
        "mtg_cards",
        "mtg_printings",
        "external_ids",
    )
    return {t: sorted(map(tuple, pg.execute(text(f"SELECT * FROM {t}")).all()), key=repr) for t in tables}


def retired(pg, table, key, ref_id):
    return pg.execute(select(table.retired_at).where(key == ref_id)).scalar_one() is not None


# ---- loading -------------------------------------------------------------------------------

pytestmark = pytest.mark.postgres


def test_a_first_load_writes_every_row_under_derived_ids(pg):
    result = load(pg)
    assert (result.sets, result.cards, result.printings) == (2, 2, 3)
    assert result.written == {
        "formats": 2,
        "sets": 2,
        "cards": 2,
        "printings": 3,
        "legalities": 4,
        "mtg_cards": 2,
        "mtg_printings": 3,
        "external_ids": 10,  # 7 rows' own IDs, 3 mapped; the shared Arena ID isn't
    }
    assert result.shared == {"arena": 1}
    promo = pg.execute(
        select(
            Printing.card_id, Printing.set_id, Printing.collector_number, Printing.extra, Printing.retired_at
        ).where(Printing.printing_id == printing_id("p-ring-promo"))
    ).one()
    assert promo == (card_id("c-ring"), set_id("s-promo"), "1", {"artist": "Mark Tedin"}, None)
    parent = pg.execute(select(Set.parent_set_id).where(Set.set_id == set_id("s-promo"))).scalar_one()
    assert parent == set_id("s-core")
    colors = pg.execute(select(MtgCard.colors).where(MtgCard.card_id == card_id("c-bolt"))).scalar_one()
    assert colors == ["R"]
    registry = pg.execute(
        select(ExternalId.source, ExternalId.external_id, ExternalId.printing_id, ExternalId.last_seen).where(
            ExternalId.source.in_(["mtgo", "arena"])
        )
    ).all()
    assert sorted(registry) == [
        ("arena", "7", printing_id("p-bolt"), SEEN),
        ("arena", "8", printing_id("p-bolt"), SEEN),
        ("mtgo", "1001", printing_id("p-ring"), SEEN),
    ]
    assert catalog.loaded_through(pg, "mtg") == SEEN


def test_loading_the_same_batch_again_writes_nothing(pg):
    load(pg)
    before = snapshot(pg)
    again = load(pg)
    assert again.written == {} and again.changed == 0
    assert snapshot(pg) == before


def test_only_the_rows_that_changed_are_written(pg):
    load(pg)
    b = batch()
    b.cards[1].name = "Lightning Bolt (errata)"
    b.printings[0].specific["usd"] = Decimal("2.00")
    b.sets[0].extra = {"block": "Core"}
    assert load(pg, b).written == {"cards": 1, "mtg_printings": 1, "sets": 1}


def test_newer_source_data_moves_last_seen_and_nothing_else(pg):
    load(pg)
    later = datetime(2026, 9, 25, 21, 5, tzinfo=UTC)
    assert load(pg, batch(seen_at=later)).written == {"external_ids": 10}
    assert catalog.loaded_through(pg, "mtg") == later


def test_a_row_the_source_stops_listing_is_retired_and_returns_when_listed_again(pg):
    load(pg, batch(printing_ids=[]))
    b = batch(printing_ids=[])
    b.printings = [p for p in b.printings if p.ref != "p-bolt"]
    assert load(pg, b).written == {"printings": 1}
    assert retired(pg, Printing, Printing.printing_id, printing_id("p-bolt"))
    assert load(pg, batch(printing_ids=[])).written == {"printings": 1}
    assert not retired(pg, Printing, Printing.printing_id, printing_id("p-bolt"))


def test_a_retired_card_keeps_its_rows(pg):
    load(pg, batch(printing_ids=[]))
    b = batch(cards=batch().cards[:1], legalities={"c-ring": batch().legalities["c-ring"]}, printing_ids=[])
    b.printings = [p for p in b.printings if p.card == "c-ring"]
    written = load(pg, b).written
    assert written == {"cards": 1, "printings": 1}
    assert retired(pg, Card, Card.card_id, card_id("c-bolt"))
    kept = pg.execute(select(Legality.format).where(Legality.card_id == card_id("c-bolt"))).scalars().all()
    assert sorted(kept) == ["legacy", "modern"]


def test_a_mapped_id_keeps_its_row_when_it_becomes_shared(pg):
    """Registry rows are never deleted: last_seen says when the source last named the ID on
    one printing, and a lookup can tell a current mapping from an old one by it."""
    load(pg, batch(printing_ids=[("arena", "9", "p-ring")]))
    later = datetime(2026, 9, 25, 21, 5, tzinfo=UTC)
    shared = batch(seen_at=later, printing_ids=[("arena", "9", "p-ring"), ("arena", "9", "p-bolt")])
    assert load(pg, shared).shared == {"arena": 1}
    row = select(ExternalId.printing_id, ExternalId.last_seen).where(ExternalId.source == "arena")
    assert pg.execute(row).one() == (printing_id("p-ring"), SEEN)


def test_a_legality_the_source_drops_is_deleted_and_the_format_kept(pg):
    load(pg)
    b = batch()
    del b.legalities["c-bolt"]["legacy"]
    assert load(pg, b).written == {"legalities": 1}
    formats = pg.execute(select(Legality.format).where(Legality.card_id == card_id("c-bolt"))).scalars().all()
    assert formats == ["modern"]
    assert pg.execute(text("SELECT count(*) FROM formats")).scalar_one() == 2


def test_an_alias_moves_a_renamed_card_back_to_its_first_id(pg):
    load(pg)
    renamed = batch()
    renamed.cards[0].ref = "c-ring-new"
    for p in renamed.printings:
        p.card = "c-ring-new" if p.card == "c-ring" else p.card
    renamed.legalities["c-ring-new"] = renamed.legalities.pop("c-ring")
    load(pg, renamed)  # the rename arrives before its alias: a second card, the first retired
    assert retired(pg, Card, Card.card_id, card_id("c-ring"))
    owner = select(Printing.card_id).where(Printing.printing_id == printing_id("p-ring"))
    assert pg.execute(owner).scalar_one() == card_id("c-ring-new")

    load(pg, renamed, aliases={"scryfall_oracle": {"c-ring-new": "c-ring"}})
    assert pg.execute(owner).scalar_one() == card_id("c-ring")
    assert not retired(pg, Card, Card.card_id, card_id("c-ring"))
    assert retired(pg, Card, Card.card_id, card_id("c-ring-new"))
    mapped = select(ExternalId.card_id).where(
        ExternalId.source == "scryfall_oracle", ExternalId.external_id == "c-ring-new"
    )
    assert pg.execute(mapped).scalar_one() == card_id("c-ring")


def test_an_extension_row_must_name_every_column(pg):
    b = batch()
    del b.printings[0].specific["tix"]
    with pytest.raises(ValueError, match="mtg_printings: a row's columns differ from the table's: tix"):
        load(pg, b)


# ---- collector numbers: one active Magic printing per set, number, and language --------------


def check_now(pg):
    """Deferred constraints are checked at commit, and tests roll back: check them here."""
    pg.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))


def test_two_printings_may_swap_numbers_in_one_load(pg):
    load(pg)
    b = batch()
    b.printings[0].collector_number, b.printings[1].collector_number = "2", "1"
    load(pg, b)
    check_now(pg)


def test_a_replaced_printing_frees_its_number(pg):
    load(pg)
    b = batch()
    b.printings[1].ref = "p-bolt-reissued"  # the source replaced the object: new ID, same number
    b.printing_ids = [(s, e, "p-bolt-reissued" if r == "p-bolt" else r) for s, e, r in b.printing_ids]
    load(pg, b)
    check_now(pg)
    assert retired(pg, Printing, Printing.printing_id, printing_id("p-bolt"))


def test_two_listed_printings_cannot_share_a_number(pg):
    b = batch()
    b.printings[1].collector_number = "1"
    load(pg, b)
    with pytest.raises(IntegrityError, match="ex_printings_set_id_collector_number_lang"):
        check_now(pg)


def fab_set_and_card(pg):
    fab_set, fab_card = uuid.uuid4(), uuid.uuid4()
    pg.execute(insert(Set).values(set_id=fab_set, game_id="fab", code="WTR", name="Welcome to Rathe"))
    pg.execute(insert(Card).values(card_id=fab_card, game_id="fab", name="Snatch"))
    return fab_set, fab_card


def test_other_games_may_share_numbers(pg):
    """FaB editions and foilings share a collector number; only Magic's rows are held to one."""
    fab_set, fab_card = fab_set_and_card(pg)
    for _ in range(2):
        pg.execute(
            insert(Printing).values(
                printing_id=uuid.uuid4(),
                game_id="fab",
                card_id=fab_card,
                set_id=fab_set,
                collector_number="WTR167",
            )
        )
    check_now(pg)


# ---- the database refuses what the design rules out ---------------------------------------


def a_printing_of_a_magic_card_in_fab(pg, ring, fab_set, fab_card):
    pg.execute(
        insert(Printing).values(
            printing_id=uuid.uuid4(), game_id="fab", card_id=ring, set_id=fab_set, collector_number="1"
        )
    )


def magic_columns_for_a_fab_card(pg, ring, fab_set, fab_card):
    pg.execute(insert(MtgCard).values(card_id=fab_card, game_id="fab", **CARD_COLUMNS))


def an_external_id_for_two_rows(pg, ring, fab_set, fab_card):
    pg.execute(
        insert(ExternalId).values(
            source="mtgo",
            external_id="5",
            game_id="mtg",
            card_id=ring,
            printing_id=printing_id("p-ring"),
            last_seen=SEEN,
        )
    )


def an_unknown_status(pg, ring, fab_set, fab_card):
    pg.execute(text("UPDATE legalities SET status = 'maybe' WHERE card_id = :c"), {"c": ring})


def a_color_that_isnt_one(pg, ring, fab_set, fab_card):
    pg.execute(text("UPDATE mtg_cards SET colors = '{W,C}' WHERE card_id = :c"), {"c": ring})


@pytest.mark.parametrize(
    ("violation", "constraint"),
    [
        (a_printing_of_a_magic_card_in_fab, "fk_printings_game_id_card_id_cards"),
        (magic_columns_for_a_fab_card, "ck_mtg_cards_game_id"),
        (an_external_id_for_two_rows, "ck_external_ids_one_target"),
        (an_unknown_status, "ck_legalities_status"),
        (a_color_that_isnt_one, "ck_mtg_cards_colors"),
    ],
    ids=lambda v: v.__name__.replace("_", " ") if callable(v) else "",
)
def test_constraints(pg, violation, constraint):
    load(pg)
    fab_set, fab_card = fab_set_and_card(pg)
    with pytest.raises(IntegrityError, match=constraint):
        violation(pg, card_id("c-ring"), fab_set, fab_card)
