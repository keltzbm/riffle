"""Card data lookups. Catalog is the interface; PostgresCatalog (store.postgres) the real one.

Cards are named by card_id, Riffle's own ID for a card (a UUID, as text).
Prices, printings, and rules are looked up many at a time: a collection or a deck asks
for all of its cards in one call, not one call per card.
"""

from collections.abc import Collection
from datetime import date
from typing import Protocol

from riffle.models import CardRules, Prices, Printing


class Catalog(Protocol):
    def resolve(self, name: str) -> str | None:
        """Card name (full or front face, any case) -> card_id."""

    def name(self, card_id: str) -> str: ...

    def prices(self, card_ids: Collection[str]) -> dict[str, Prices]:
        """Each card's cheapest paper and MTGO price across its printings; unknown IDs are left out."""

    def prices_day(self) -> date | None:
        """The day of Scryfall's prices the catalog holds: the UTC date Scryfall published the
        bulk file it was loaded from. None before the first load."""

    def printings(self, scryfall_ids: Collection[str]) -> dict[str, Printing]:
        """The printings these Scryfall IDs name, by Scryfall ID; unknown IDs are left out."""

    def printings_at(self, places: Collection[tuple[str, str]]) -> dict[tuple[str, str], Printing]:
        """The printing at each (set code, collector number), keyed as asked. Set codes in any case."""

    def mtgo_name(self, card_id: str) -> str: ...

    def arena_rarity(self, card_id: str) -> str | None:
        """Lowest rarity the card has on Arena — the wildcard it costs — or None if not on Arena."""

    def is_basic(self, card_id: str) -> bool:
        """Plains, Island, Swamp, Mountain, Forest, Wastes. Snow basics are not."""

    def rules(self, card_ids: Collection[str]) -> dict[str, CardRules]:
        """Legalities, color identity, type line, and oracle text of these cards; unknown IDs are left out."""
