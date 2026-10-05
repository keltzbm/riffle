"""A Printing is a physical (or digital) object; many share one card_id, Riffle's ID for the card."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Printing:
    scryfall_id: str
    card_id: str  # Riffle's card ID, as text
    name: str
    set_code: str
    collector_number: str
    frame: str = ""
    border_color: str = ""
    usd: float | None = None  # paper, nonfoil
    illustration_id: str = ""  # Scryfall's, the front face's on a two-faced card; "" when it has none

    @property
    def is_old_border(self) -> bool:
        return self.frame in {"1993", "1997"}


@dataclass(frozen=True)
class PricedPrinting:
    """What a card's price on a day needs from one of its printings (see Catalog.prices)."""

    scryfall_id: str
    playable: bool  # paper, and none of gold or silver border, memorabilia, oversized
    digital: bool


@dataclass(frozen=True)
class CardRules:
    """Oracle-level facts the rules care about. Same for every printing."""

    legalities: dict[str, str]  # scryfall format key -> legal | not_legal | banned | restricted
    color_identity: tuple[str, ...] = ()
    type_line: str = ""
    oracle_text: str = ""  # all faces, newline-joined


@dataclass(frozen=True)
class Face:
    """One face of a card as its note shows it; a card with one face has one."""

    name: str
    mana_cost: str = ""
    type_line: str = ""
    text: str = ""
    stats: str = ""  # power/toughness, loyalty or defense


@dataclass(frozen=True)
class CardView:
    """What a card's note shows beyond its rules: its faces, and the printing pictured, which
    is never a foil (see Catalog.card_views)."""

    faces: tuple[Face, ...]
    images: tuple[tuple[str, str], ...] = ()  # (face name, image URL), front first
    scryfall_id: str = ""  # the pictured printing's; "" when the card has none to picture
    set_code: str = ""
    collector_number: str = ""
    multiverse_id: int | None = None  # Gatherer's ID for that printing, when it has one


_RANK = {"banned": 3, "restricted": 2, "legal": 1, "not_legal": 0}


def merge_legalities(per_printing: list[dict[str, str]]) -> dict[str, str]:
    """One card's legality from all its printings. Legality belongs to the card,
    but Scryfall marks non-tournament printings (gold-border World Championship
    decks, 30th Anniversary, oversized, playtest) not_legal everywhere — so the
    strongest status wins: banned > restricted > legal > not_legal."""
    out: dict[str, str] = {}
    for legal in per_printing:
        for fmt, status in (legal or {}).items():
            if _RANK.get(status, -1) > _RANK.get(out.get(fmt, ""), -1):
                out[fmt] = status
    return out


@dataclass(frozen=True)
class Prices:
    """Cheapest across all printings of one card."""

    usd: float | None = None  # paper, nonfoil
    tix: float | None = None  # MTGO, via Scryfall (Cardhoarder)
