"""riffle.analysis.similarity: a zone's counts, Definition 2's measures, the propositions on data,
and the verdict."""

import random
import unicodedata
from collections import Counter

import pytest

from riffle.analysis import similarity
from riffle.ingest.decklist import parse_text
from tests.conftest import CARDS

EXTRA = {
    # the plan's fixture cards conftest lacks: an accent, a dot, a basic and a snow basic
    "x-jotun": ("Jötun Grunt", "normal", 0.5, None),
    "x-cerberus": ("Summon G.F. Cerberus", "normal", 0.5, None),
    "x-mountain": ("Mountain", "normal", 0.1, 0.01),
    "x-snowi": ("Snow-Covered Island", "normal", 0.5, 0.02),
}


@pytest.fixture
def cards(monkeypatch):
    """conftest's catalog resolves the plan's fixture cards too, for one test."""
    for oid, card in EXTRA.items():
        monkeypatch.setitem(CARDS, oid, card)


def deck(text, fmt="commander"):
    d = parse_text(text)
    d.meta = {"format": fmt}
    return d


def commander_deck(commanders, fmt="commander"):
    """A list whose commander's zone holds these names: one commander, or two partners."""
    return deck("Commander\n" + "".join(f"1 {c}\n" for c in commanders) + "\nDeck\n1 Sol Ring\n", fmt)


def d_s(a, b):
    return similarity.compare(a, b).swaps


def d_j(a, b):
    return 1 - similarity.compare(a, b).jaccard


def multisets(seed, n, cards=6, most=3):
    """n random non-empty multisets over a few cards, so pairs overlap and some are equal."""
    rng = random.Random(seed)
    out = []
    while len(out) < n:
        m = +Counter({f"c{i}": rng.randint(0, most) for i in range(cards)})
        if m:
            out.append(m)
    return out


# Lists, zones and cards: a card is its catalog card when its name resolves, else its own element as written
def test_counts_name_each_card_as_the_catalog_does_and_the_rest_as_written(cat, cards):
    d = deck("4 lightning bolt\n2 Summon G.F. Cerberus\n1 Jötun Grunt\n1 Sol Rnig\n")
    assert similarity.counts(d, cat) == Counter(
        {"Lightning Bolt": 4, "Summon G.F. Cerberus": 2, "Jötun Grunt": 1, "Sol Rnig": 1}
    )
    assert similarity.counts(d, cat, zone="sideboard") == Counter()


# Basic lands, plain and Snow-Covered, are removed from every zone before anything is measured
def test_counts_leave_basic_lands_out_plain_and_snow_covered(cat, cards):
    d = deck(
        "Deck\n4 Lightning Bolt\n20 Mountain\n2 Snow-Covered Island\n1 Snow-Covered Forest\n"
        "\nSideboard\n3 Forest\n1 Fire // Ice\n"
    )
    assert similarity.counts(d, cat) == Counter({"Lightning Bolt": 4})
    assert similarity.counts(d, cat, zone="sideboard") == Counter({"Fire // Ice": 1})


# a double-faced card written by its front face and by its whole name is one element of C
def test_counts_a_double_faced_card_by_its_front_face_and_whole_name_as_one(cat):
    d = deck("2 Delver of Secrets\n1 Delver of Secrets // Insectile Aberration\n1 Fire\n1 Fire // Ice\n")
    assert similarity.counts(d, cat) == Counter(
        {"Delver of Secrets // Insectile Aberration": 3, "Fire // Ice": 2}
    )


# the commander's zone isn't measured, nor the companion's: counts reads one board, the main deck by default
def test_counts_read_one_board_so_the_commander_and_companion_stay_out(cat):
    d = deck(
        "Commander\n1 Aesi, Tyrant of Gyre Strait\n\nCompanion\n1 Lurrus of the Dream-Den\n"
        "\nDeck\n1 Sol Ring\n\nSideboard\n1 Cyclonic Rift\n"
    )
    assert similarity.counts(d, cat) == Counter({"Sol Ring": 1})
    assert similarity.counts(d, cat, zone="sideboard") == Counter({"Cyclonic Rift": 1})
    assert similarity.counts(d, cat, zone="commander") == Counter({"Aesi, Tyrant of Gyre Strait": 1})


# Definition 2 on two singletons that differ: nothing shared, a copy only in each, one swap, every share 0
def test_two_singletons_that_differ():
    r = similarity.compare(Counter({"Sol Ring": 1}), Counter({"Mana Crypt": 1}))
    assert (r.shared, r.only_a, r.only_b, r.swaps) == (0, 1, 1, 1)
    assert (r.jaccard, r.a_in_b, r.b_in_a) == (0.0, 0.0, 0.0)
    assert (r.only_a_cards, r.only_b_cards) == (Counter({"Sol Ring": 1}), Counter({"Mana Crypt": 1}))


# Definition 2 on two singleton lists that are the same: one shared copy, no swaps, every share 1
def test_two_singletons_that_are_the_same():
    r = similarity.compare(Counter({"Sol Ring": 1}), Counter({"Sol Ring": 1}))
    assert (r.shared, r.only_a, r.only_b, r.swaps) == (1, 0, 0, 0)
    assert (r.jaccard, r.a_in_b, r.b_in_a) == (1.0, 1.0, 1.0)
    assert r.only_a_cards == r.only_b_cards == Counter()


# counts: a 4-of against a 2-of shares two copies and has two only in the first, so |A| = s + o(A, B)
def test_a_four_of_against_a_two_of():
    r = similarity.compare(Counter({"Lightning Bolt": 4}), Counter({"Lightning Bolt": 2}))
    assert (r.shared, r.only_a, r.only_b, r.swaps) == (2, 2, 0, 2)
    assert (r.jaccard, r.a_in_b, r.b_in_a) == (0.5, 0.5, 1.0)
    assert (r.only_a_cards, r.only_b_cards) == (Counter({"Lightning Bolt": 2}), Counter())


# no Jaccard or containment for a zone that is empty in either list: J(A, 0) = 0 and κ(0, B) is 0/0
def test_an_empty_zone_leaves_jaccard_and_containment_none():
    r = similarity.compare(Counter(), Counter({"Sol Ring": 2}))
    assert (r.shared, r.only_a, r.only_b, r.swaps) == (0, 0, 2, 2)
    assert (r.jaccard, r.a_in_b, r.b_in_a) == (None, None, None)
    assert (r.only_a_cards, r.only_b_cards) == (Counter(), Counter({"Sol Ring": 2}))
    both = similarity.compare(Counter(), Counter())
    assert (both.swaps, both.jaccard, both.a_in_b, both.b_in_a) == (0, None, None, None)


# Definition 2: |A| = s + o(A, B), the only-in cards are (A - B)^+, J = s / |A ∨ B| and κ(A, B) = s / |A|
def test_definition_2_holds_on_random_zones():
    sets = multisets(6, 200)
    for a, b in zip(sets, sets[1:], strict=False):
        r = similarity.compare(a, b)
        assert (r.shared + r.only_a, r.shared + r.only_b) == (a.total(), b.total())
        assert (r.only_a_cards, r.only_b_cards) == (a - b, b - a)
        assert r.swaps == max(r.only_a, r.only_b)
        assert r.jaccard == pytest.approx(r.shared / (r.shared + r.only_a + r.only_b))
        assert (r.a_in_b, r.b_in_a) == pytest.approx((r.shared / a.total(), r.shared / b.total()))


# Proposition 1: swaps are a metric: zero only for equal multisets, symmetric, within the triangle inequality
def test_swaps_are_a_metric_on_random_multisets():
    sets = multisets(1, 300)
    for a, b, c in zip(sets, sets[1:], sets[2:], strict=False):
        ab, ba, bc, ac = (d_s(x, y) for x, y in ((a, b), (b, a), (b, c), (a, c)))
        assert ab == ba >= 0 and (ab == 0) == (a == b)
        assert ac <= ab + bc
        assert d_s(a, a) == 0 and d_s(a, a + Counter(c0=1)) == 1


# Proposition 2: 1 - J is a metric: zero only for equal multisets, symmetric, within the triangle inequality
def test_jaccard_distance_is_a_metric_on_random_multisets():
    sets = multisets(2, 300)
    for a, b, c in zip(sets, sets[1:], sets[2:], strict=False):
        assert d_j(a, b) == d_j(b, a) >= 0 and (d_j(a, b) == 0) == (a == b)
        assert d_j(a, c) <= d_j(a, b) + d_j(b, c) + 1e-12
        assert d_j(a, a) == 0 and d_j(a, a + Counter(c0=1)) > 0


# Proposition 4: A = {1} is wholly in B = {1, 2}, so its containment is 1 though A ≠ B (one swap apart)
def test_containment_in_a_larger_zone_that_holds_it_is_one():
    r = similarity.compare(Counter({"c1": 1}), Counter({"c1": 1, "c2": 1}))
    assert (r.a_in_b, r.b_in_a, r.swaps) == (1.0, 0.5, 1)
    for a in multisets(4, 100):
        bigger = a + Counter({"c0": 2, "c9": 1})
        assert similarity.compare(a, bigger).a_in_b == 1.0
        assert similarity.compare(bigger, a).b_in_a == 1.0


# Proposition 5: the whole lists' swaps are at most the sum of the zones' swaps
def test_the_whole_lists_swaps_are_at_most_the_zones_sum():
    sets = multisets(5, 400)
    for a, a2, b, b2 in zip(sets, sets[1:], sets[2:], sets[3:], strict=False):
        assert d_s(a + a2, b + b2) <= d_s(a, b) + d_s(a2, b2)


# Proposition 5's example: 4 of x in the main deck and 4 of y in the sideboard, and the reverse, are 4 swaps
# in each zone measured apart, and 0 on the whole lists
def test_main_and_sideboard_swapped_are_four_swaps_in_each_zone_and_none_as_a_whole(cat):
    a = deck("Deck\n4 Lightning Bolt\n1 Sol Ring\n\nSideboard\n4 Cyclonic Rift\n")
    b = deck("Deck\n4 Cyclonic Rift\n1 Sol Ring\n\nSideboard\n4 Lightning Bolt\n")
    main = [similarity.counts(d, cat) for d in (a, b)]
    side = [similarity.counts(d, cat, zone="sideboard") for d in (a, b)]
    assert (d_s(*main), d_s(*side)) == (4, 4)
    assert d_s(main[0] + side[0], main[1] + side[1]) == 0


# Where it goes: the lines as constants, for deck-variants and the vault's merge
def test_the_lines_are_constants():
    assert (similarity.VARIANT_SWAPS, similarity.OTHER_COMMANDER_SWAPS) == (20, 10)


# The verdict: a variant at 20 swaps under the same commander and at 10 under another, another deck past each
@pytest.mark.parametrize(
    ("same", "swaps", "said", "variant"),
    [
        (True, 20, "a variant: 20 swaps or fewer under the same commander", True),
        (True, 21, "another deck: more than 20 swaps under the same commander", False),
        (False, 10, "a variant: 10 swaps or fewer under another commander", True),
        (False, 11, "another deck: more than 10 swaps under another commander", False),
    ],
)
def test_the_line_is_20_swaps_under_one_commander_and_10_under_another(same, swaps, said, variant):
    a = commander_deck(["Aesi, Tyrant of Gyre Strait"])
    b = commander_deck(["Aesi, Tyrant of Gyre Strait" if same else "Teferi, Hero of Dominaria"])
    assert similarity.verdict(a, b, swaps) == said
    assert similarity.is_variant(a, b, swaps) is variant


# The verdict: partners are one commander, the set of names in the commander's zone, whatever their order
def test_partners_are_one_commander_whatever_their_order():
    pair = commander_deck(["Thrasios, Triton Hero", "Tymna the Weaver"])
    reversed_pair = commander_deck(["Tymna the Weaver", "Thrasios, Triton Hero"])
    alone = commander_deck(["Thrasios, Triton Hero"])
    assert (
        similarity.verdict(pair, reversed_pair, 20) == "a variant: 20 swaps or fewer under the same commander"
    )
    assert similarity.verdict(pair, alone, 20) == "another deck: more than 10 swaps under another commander"
    assert similarity.is_variant(pair, alone, 10) is True


# The verdict: Duel Commander (format: duel) reads the same line as Commander
def test_duel_commander_reads_the_same_line():
    a = commander_deck(["Aesi, Tyrant of Gyre Strait"], fmt="duel")
    b = commander_deck(["Aesi, Tyrant of Gyre Strait"], fmt="duel")
    assert similarity.verdict(a, b, 20) == "a variant: 20 swaps or fewer under the same commander"
    assert similarity.verdict(a, b, 21) == "another deck: more than 20 swaps under the same commander"
    assert (similarity.is_variant(a, b, 20), similarity.is_variant(a, b, 21)) == (True, False)


# The verdict: another format gets no verdict until its line is measured
def test_another_format_has_no_line_yet():
    a, b = deck("4 Lightning Bolt\n", "modern"), deck("4 Lightning Bolt\n", "modern")
    assert similarity.verdict(a, b, 0) == "no line measured for modern yet"
    assert similarity.is_variant(a, b, 0) is None


# The verdict: two lists in different formats get none
@pytest.mark.parametrize(("fmt_a", "fmt_b"), [("commander", "duel"), ("modern", "legacy")])
def test_different_formats_get_no_verdict(fmt_a, fmt_b):
    a = commander_deck(["Aesi, Tyrant of Gyre Strait"], fmt=fmt_a)
    b = commander_deck(["Aesi, Tyrant of Gyre Strait"], fmt=fmt_b)
    assert similarity.verdict(a, b, 0) == f"different formats: {fmt_a} and {fmt_b}"
    assert similarity.is_variant(a, b, 0) is None


# Fixed after review: a double-faced commander written by its front face and by its whole name is one
# commander, through the catalog and, without one, by its front face; one Unicode form either way
def test_a_double_faced_commander_written_either_way_is_one(cat):
    front = commander_deck(["Delver of Secrets"])
    whole = commander_deck(["Delver of Secrets // Insectile Aberration"])
    assert similarity.commanders(front, cat) == similarity.commanders(whole, cat)
    assert similarity.commanders(front) == similarity.commanders(whole)
    assert (
        similarity.verdict(front, whole, 20, cat) == "a variant: 20 swaps or fewer under the same commander"
    )
    composed = commander_deck(["Andúril, Flame of the West"])
    decomposed = commander_deck([unicodedata.normalize("NFD", "Andúril, Flame of the West")])
    assert similarity.commanders(composed) == similarity.commanders(decomposed)


# Fixed after review: a list with no format, as a path gives one, is read as Commander when it has a
# commander, else in the other deck's format; with neither format named, no verdict
def test_a_list_without_a_format_takes_the_other_decks():
    note = commander_deck(["Aesi, Tyrant of Gyre Strait"])
    listed = parse_text("Commander\n1 Aesi, Tyrant of Gyre Strait\n\nDeck\n1 Sol Ring\n")
    assert similarity.deck_format(listed) == "commander"
    assert similarity.verdict(note, listed, 0) == "a variant: 20 swaps or fewer under the same commander"
    modern = deck("4 Lightning Bolt\n", "modern")
    plain = parse_text("4 Lightning Bolt\n")
    assert similarity.deck_format(plain) is None
    assert similarity.verdict(modern, plain, 0) == "no line measured for modern yet"
    assert similarity.verdict(plain, parse_text("4 Lightning Bolt\n"), 0) == "no format named for either list"
    assert similarity.is_variant(plain, plain, 0) is None


# Fixed after review: an MTGO-form list puts its commander after the blank line, where it reads as the
# sideboard, so the verdict says it names none rather than calling it another commander
def test_a_list_that_names_no_commander_gets_no_line():
    note = commander_deck(["Aesi, Tyrant of Gyre Strait"])
    mtgo = parse_text("1 Sol Ring\n\n1 Aesi, Tyrant of Gyre Strait\n", slug="aesi")
    assert similarity.verdict(note, mtgo, 0) == "no commander named in aesi"
    assert similarity.is_variant(note, mtgo, 0) is None


class Aliases:
    """A catalog that knows one card by two names, as MTGO names some Universes Beyond cards its own way."""

    def resolve(self, name):
        return "o-spider" if name in ("Uharis, the Stormspinner", "Spider-Man 2099") else None

    def name(self, card_id):
        return "Spider-Man 2099"


# Fixed after review: with a catalog, commanders are compared as the cards their names resolve to, so two
# names for one card are one commander; without it, the names stay apart
def test_commanders_go_through_the_catalog_when_it_is_given():
    mtgo = commander_deck(["Uharis, the Stormspinner"])
    paper = commander_deck(["Spider-Man 2099"])
    assert similarity.commanders(mtgo, Aliases()) == similarity.commanders(paper, Aliases())
    assert similarity.commanders(mtgo) != similarity.commanders(paper)
    assert (
        similarity.verdict(mtgo, paper, 20, Aliases())
        == "a variant: 20 swaps or fewer under the same commander"
    )
    assert similarity.verdict(mtgo, paper, 20) == "another deck: more than 10 swaps under another commander"
    assert similarity.is_variant(mtgo, paper, 20, Aliases()) is True


# Basic lands: Wastes and its Snow-Covered version are basic too
def test_wastes_are_a_basic_land(cat):
    assert similarity.is_basic_land("Wastes") and similarity.is_basic_land("Snow-Covered Wastes")
    assert similarity.counts(deck("4 Wastes\n2 Snow-Covered Wastes\n1 Sol Ring\n"), cat) == Counter(
        {"Sol Ring": 1}
    )


# Fixed after review: a list that names no format is read in the other deck's named one, so a Duel
# Commander note's own list given by its path reads the Duel Commander line
def test_a_list_without_a_format_reads_in_a_duel_notes_format():
    note = commander_deck(["Aesi, Tyrant of Gyre Strait"], fmt="duel")
    listed = parse_text("Commander\n1 Aesi, Tyrant of Gyre Strait\n\nDeck\n1 Sol Ring\n")
    assert similarity.verdict(note, listed, 20) == "a variant: 20 swaps or fewer under the same commander"
    assert similarity.verdict(listed, note, 21) == "another deck: more than 20 swaps under the same commander"
