"""How alike two lists are, zone by zone, and whether one is a variant of the other.

Each zone of a list (the main deck, the sideboard) is compared on its own as copies of each
card, basic lands aside, each card by its catalog name when it resolves, so a double-faced card
written by its front face and by its whole name is one card. The whole list is never measured
as one: cards moved between zones would cancel. Swaps and Jaccard are metrics; containment says
how much of one zone is in the other, and isn't one (a zone inside a larger one scores 1). The
proofs are in the vault's plan note, deck-similarity-commands; the 20-swap line comes from its
deck-similarity analysis, MTGO Duel Commander players' own lists against different commanders.
"""

import unicodedata
from collections import Counter
from dataclasses import dataclass, field

from riffle.models import Deck
from riffle.store import Catalog

# A list this close to a deck's main list under the same commander is a variant of it; further,
# another deck. Under another commander the line is the vault's of 2026-10-05.
VARIANT_SWAPS = 20
OTHER_COMMANDER_SWAPS = 10
# Where the lines were measured: Commander and Duel Commander lists.
MEASURED = ("commander", "duel")
BASIC_LANDS = frozenset({"plains", "island", "swamp", "mountain", "forest", "wastes"})


def is_basic_land(name: str) -> bool:
    """A basic land by name, Snow-Covered ones included."""
    key = name.strip().lower()
    return key.removeprefix("snow-covered ") in BASIC_LANDS


def counts(deck: Deck, catalog: Catalog, zone: str = "main") -> Counter[str]:
    """One zone of a deck as copies of each card, basic lands aside, by catalog name."""
    out: Counter[str] = Counter()
    for e in deck.board(zone):
        card_id = e.card_id or catalog.resolve(e.name)
        name = catalog.name(card_id) if card_id else e.name
        if not is_basic_land(name):
            out[name] += e.quantity
    return out


@dataclass(frozen=True)
class Comparison:
    """Two lists' copies in common and apart."""

    only_a_cards: Counter[str] = field(default_factory=Counter)
    only_b_cards: Counter[str] = field(default_factory=Counter)
    shared: int = 0

    @property
    def only_a(self) -> int:
        return sum(self.only_a_cards.values())

    @property
    def only_b(self) -> int:
        return sum(self.only_b_cards.values())

    @property
    def swaps(self) -> int:
        """The cards to change to turn one list into the other."""
        return max(self.only_a, self.only_b)

    @property
    def jaccard(self) -> float | None:
        """Shared copies over the copies in either list; None when either list is empty."""
        if not self.shared + self.only_a or not self.shared + self.only_b:
            return None
        return self.shared / (self.shared + self.only_a + self.only_b)

    @property
    def a_in_b(self) -> float | None:
        return self._within(self.only_a)

    @property
    def b_in_a(self) -> float | None:
        return self._within(self.only_b)

    def _within(self, only: int) -> float | None:
        if not self.shared + self.only_a or not self.shared + self.only_b:
            return None
        return self.shared / (self.shared + only)


def compare(a: Counter[str], b: Counter[str]) -> Comparison:
    """Two lists' counts, from counts()."""
    shared = sum((a & b).values())
    return Comparison(only_a_cards=a - b, only_b_cards=b - a, shared=shared)


def _key(name: str) -> str:
    """A commander as one key: its front face, in one Unicode form, case-folded."""
    return unicodedata.normalize("NFC", name.split(" // ")[0]).strip().casefold()


def commanders(deck: Deck, catalog: Catalog | None = None) -> frozenset[str]:
    """The cards in a deck's command zone, so partners are one commander: each by its catalog name
    when a catalog is given and the name resolves, and by its front face, so a double-faced
    commander written either way is one."""
    keys = set()
    for e in deck.board("commander"):
        card_id = (e.card_id or catalog.resolve(e.name)) if catalog else None
        keys.add(_key(catalog.name(card_id) if catalog and card_id else e.name))
    return frozenset(keys)


def _named(deck: Deck) -> str | None:
    """The format a deck's note names; None for a list given by its path."""
    value = deck.meta.get("format")
    return str(value).lower() if value else None


def deck_format(deck: Deck) -> str | None:
    """The format a deck is read in: its note's, or, for a list that names none, Commander when it
    has a commander, else none."""
    return _named(deck) or ("commander" if deck.board("commander") else None)


def _formats(a: Deck, b: Deck) -> tuple[str | None, str | None]:
    """The two decks' formats. A list that names none is read in the other's named format, so a Duel
    Commander note's own list given by its path is Duel Commander too; when neither names one, each
    is read by deck_format, and one with none takes the other's."""
    na, nb = _named(a), _named(b)
    if na or nb:
        return na or nb, nb or na
    fa, fb = deck_format(a), deck_format(b)
    return fa or fb, fb or fa


def is_variant(a: Deck, b: Deck, swaps: int, catalog: Catalog | None = None) -> bool | None:
    """Whether b is a variant of a by the lines; None where no line was measured or a commander
    is missing."""
    fa, fb = _formats(a, b)
    ca, cb = commanders(a, catalog), commanders(b, catalog)
    if fa is None or fa != fb or fa not in MEASURED or not ca or not cb:
        return None
    return swaps <= (VARIANT_SWAPS if ca == cb else OTHER_COMMANDER_SWAPS)


def verdict(a: Deck, b: Deck, swaps: int, catalog: Catalog | None = None) -> str:
    """The verdict line `riffle compare` prints."""
    fa, fb = _formats(a, b)
    if fa is None:
        return "no format named for either list"
    if fa != fb:
        return f"different formats: {fa} and {fb}"
    if fa not in MEASURED:
        return f"no line measured for {fa} yet"
    ca, cb = commanders(a, catalog), commanders(b, catalog)
    missing = [d.slug for d, c in ((a, ca), (b, cb)) if not c]
    if missing:
        return f"no commander named in {' or '.join(missing)}"
    same = ca == cb
    line = VARIANT_SWAPS if same else OTHER_COMMANDER_SWAPS
    under = "under the same commander" if same else "under another commander"
    if swaps <= line:
        return f"a variant: {line} swaps or fewer {under}"
    return f"another deck: more than {line} swaps {under}"
