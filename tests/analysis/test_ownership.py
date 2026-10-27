from collections import Counter

from riffle.analysis.ownership import BUY, OWN, diff, summary
from riffle.analysis.resolve import counts, resolve_deck, resolve_holdings
from riffle.ingest.local.decklist import parse_text
from riffle.models import Holding


def test_name_is_not_a_key_holdings_resolve_by_printing_first(cat):
    hs = [
        Holding("Sol Ring (Retro)", 1, scryfall_id="s-sol-m3c"),
        Holding("whatever", 1, set_code="2X2", collector_number="45"),
    ]
    assert resolve_holdings(hs, cat) == []
    assert counts(hs) == Counter({"o-sol": 1, "o-rift": 1})


def test_plain_basics_always_owned_snow_basics_are_not(cat):
    deck = parse_text("5 Forest\n2 Snow-Covered Forest\n")
    resolve_deck(deck, cat)
    rows = {r.name: (r.status, r.shortfall) for r in diff(deck, Counter(), cat)}
    assert rows == {"Forest": (OWN, 0), "Snow-Covered Forest": (BUY, 2)}


def test_partial_ownership(cat):
    deck = parse_text("3 Snow-Covered Forest\n")
    resolve_deck(deck, cat)
    (row,) = diff(deck, Counter({"o-snowf": 1}), cat)
    assert (row.status, row.shortfall, row.partial) == (BUY, 2, True)
    assert summary([row]) == {"own": 0, "buy": 1}


def test_owning_more_than_the_deck_needs_is_just_owned(cat):
    deck = parse_text("1 Sol Ring\n")
    resolve_deck(deck, cat)
    (row,) = diff(deck, Counter({"o-sol": 4}), cat)
    assert (row.status, row.needed, row.partial) == (OWN, 1, False)


def test_unmatched_names_are_reported_not_dropped(cat):
    deck = parse_text("1 Sol Rnig\n")
    assert resolve_deck(deck, cat) == ["Sol Rnig"]
