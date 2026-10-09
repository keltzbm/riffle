"""What the catalog loader refuses before it touches the database."""

from datetime import UTC, datetime
from typing import cast

import pytest
from sqlalchemy import Connection

from riffle.db import catalog, ids
from riffle.db.catalog import Batch, CardRow, PrintingRow, SetRow

SEEN = datetime(2026, 9, 24, tzinfo=UTC)


def batch(game="mtg", **changes):
    base = {
        "formats": {"legacy": "Legacy"},
        "sets": [SetRow("s-1", "one", "One")],
        "cards": [CardRow("c-1", "Sol Ring")],
        "printings": [PrintingRow("p-1", "c-1", "s-1", "1")],
        "legalities": {"c-1": {"legacy": "banned"}},
    }
    return Batch(game=game, seen_at=SEEN, **{**base, **changes})


def refused(b, message):
    """load() raises before its first statement, so no connection is needed to see it."""
    with pytest.raises(ValueError, match=message):
        catalog.load(cast(Connection, None), b, aliases={})


def test_a_game_needs_a_creating_source():
    refused(batch(game="fab"), "no creating source is registered for 'fab'")


def test_game_specific_columns_need_the_game_s_own_tables(monkeypatch):
    monkeypatch.setitem(
        ids.CREATORS, "fab", ids.Creators(card="fabdb_card", printing="fabdb", set="fabdb_set")
    )
    refused(
        batch(game="fab", cards=[CardRow("c-1", "Snatch", specific={"pitch": 1})]),
        "fab has no tables of its own",
    )


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        (
            {"sets": [SetRow("s-1", "one", "One"), SetRow("s-1", "one", "One again")]},
            "1 sets listed twice, e.g. s-1",
        ),
        ({"cards": [CardRow("c-1", "Sol Ring"), CardRow("c-1", "Sol Ring")]}, "1 cards listed twice"),
        (
            {"printings": [PrintingRow("p-1", "c-1", "s-1", "1"), PrintingRow("p-1", "c-1", "s-1", "2")]},
            "1 printings listed twice",
        ),
    ],
    ids=["sets", "cards", "printings"],
)
def test_refs_are_unique(changes, message):
    refused(batch(**changes), message)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"sets": [SetRow("s-1", "one", "One", parent="s-0")]}, r"set s-0 \(parent of s-1\)"),
        ({"printings": [PrintingRow("p-1", "c-9", "s-1", "1")]}, r"card c-9 \(of printing p-1\)"),
        ({"printings": [PrintingRow("p-1", "c-1", "s-9", "1")]}, r"set s-9 \(of printing p-1\)"),
        ({"legalities": {"c-9": {"legacy": "legal"}}}, r"card c-9 \(legalities\)"),
        ({"legalities": {"c-1": {"modern": "legal"}}}, "format modern"),
    ],
    ids=["parent set", "printing's card", "printing's set", "legality's card", "legality's format"],
)
def test_references_name_rows_in_the_batch(changes, message):
    refused(batch(**changes), f"1 references to rows the batch lacks, e.g. {message}")


def test_a_creating_source_cannot_also_map_ids():
    refused(batch(printing_ids=[("scryfall", "x", "p-1")]), "scryfall creates catalog rows")
