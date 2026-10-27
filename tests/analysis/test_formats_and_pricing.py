from collections import Counter

from riffle.analysis.ownership import diff
from riffle.analysis.pricing import price
from riffle.analysis.resolve import resolve_deck, resolve_holdings
from riffle.export import formats
from riffle.ingest.local.decklist import parse_text
from riffle.models import Holding

LIST = (
    "Commander\n1 Aesi, Tyrant of Gyre Strait\n\nDeck\n1 Sol Ring\n1 Cyclonic Rift\n1 Fire // Ice\n2 Forest\n"
)


def _deck(cat):
    d = parse_text(LIST, "aesi-lands")
    resolve_deck(d, cat)
    return d


def test_moxfield_pins_only_owned_printings(cat):
    hs = [Holding("Sol Ring", 1, set_code="M3C", collector_number="283")]
    resolve_holdings(hs, cat)
    text = formats.moxfield(_deck(cat), cat, formats.owned_printings(hs))
    assert "1 Sol Ring (M3C) 283" in text
    assert "1 Cyclonic Rift\n" in text
    assert text.startswith("Commander\n1 Aesi, Tyrant of Gyre Strait\n\nDeck\n")


def test_non_manabox_holdings_are_never_used_as_pins(cat):
    hs = [Holding("Sol Ring", 1, set_code="M3C", collector_number="283", source="arena")]
    resolve_holdings(hs, cat)
    assert formats.owned_printings(hs) == {}


def test_mtgo_puts_commander_in_sideboard_and_fixes_split_names(cat):
    text = formats.mtgo(_deck(cat), cat)
    main, side = text.split("\n\n")
    assert "1 Fire/Ice" in main and "2 Forest" in main
    assert side.strip() == "1 Aesi, Tyrant of Gyre Strait"


def test_tcgplayer_lists_only_shortfall(cat):
    d = _deck(cat)
    rows = diff(d, Counter({"o-sol": 1}), cat)
    lines = set(formats.tcgplayer(rows).strip().splitlines())
    assert lines == {"1 Aesi, Tyrant of Gyre Strait", "1 Cyclonic Rift", "1 Fire // Ice"}


def test_prices_split_paper_to_buy_from_whole_deck_mtgo(cat):
    d = _deck(cat)
    dp = price(diff(d, Counter({"o-sol": 1}), cat), cat)
    assert dp.usd_to_buy == 5.0 + 30.0 + 1.0
    assert dp.usd_total == 5.0 + 1.0 + 30.0 + 1.0 + 0.2
    assert dp.tix_total == round(0.3 + 0.05 + 2.0 + 0.1 + 0.02, 2)
