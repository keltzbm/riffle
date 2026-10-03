"""Own / buy. Milestone 1 — the thing needed twice before it existed.

Two states only. Owned means scanned into ManaBox.
"""

from collections import Counter
from dataclasses import dataclass

from riffle.models import Deck
from riffle.store import Catalog

OWN, BUY = "own", "buy"
MARK = {OWN: "✓", BUY: "○"}  # apart by shape, not color: read the same in any terminal or vision
RARITIES = ("mythic", "rare", "uncommon", "common")


@dataclass
class Row:
    card_id: str
    name: str
    needed: int  # copies the deck plays
    owned: int  # copies you have, anywhere

    @property
    def status(self) -> str:
        return OWN if self.owned >= self.needed else BUY

    @property
    def shortfall(self) -> int:
        return max(0, self.needed - self.owned)

    @property
    def partial(self) -> bool:
        return 0 < self.owned < self.needed


def diff(deck: Deck, owned: Counter, catalog: Catalog) -> list[Row]:
    """Assumes resolve_deck() has run. Plain basics always count as owned."""
    needed: Counter = Counter()
    for e in deck.entries:
        if e.card_id:
            needed[e.card_id] += e.quantity
    rows = [
        Row(card_id, catalog.name(card_id), n, n if catalog.is_basic(card_id) else owned.get(card_id, 0))
        for card_id, n in needed.items()
    ]
    return sorted(rows, key=lambda r: (r.status != BUY, r.name))


def summary(rows: list[Row]) -> dict[str, int]:
    out = {OWN: 0, BUY: 0}
    for r in rows:
        out[r.status] += 1
    return out


def wildcards(rows: list[Row], catalog: Catalog) -> dict[str, int]:
    """Arena wildcards needed for the shortfall, by rarity; "not on Arena" for the rest."""
    out = {r: 0 for r in RARITIES} | {"not on Arena": 0}
    for r in rows:
        if r.status == BUY:
            out[catalog.arena_rarity(r.card_id) or "not on Arena"] += r.shortfall
    return out
