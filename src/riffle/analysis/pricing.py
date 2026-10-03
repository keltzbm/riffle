"""Paper and MTGO prices for a deck: cheapest printing of each card.

USD is paper nonfoil. Tix is MTGO; Scryfall takes it from Cardhoarder, so
it's roughly what a Cardhoarder rental or purchase costs. MTGO totals use
the whole list — a digital collection doesn't share your paper cards.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from riffle.analysis.ownership import Row
from riffle.models import PricedPrinting, Prices
from riffle.store import Catalog


@dataclass
class Line:
    name: str
    needed: int
    to_buy: int
    usd: float | None
    tix: float | None


@dataclass
class DeckPrice:
    lines: list[Line]

    @property
    def usd_total(self) -> float:
        return round(sum((line.usd or 0) * line.needed for line in self.lines), 2)

    @property
    def usd_to_buy(self) -> float:
        return round(sum((line.usd or 0) * line.to_buy for line in self.lines), 2)

    @property
    def tix_total(self) -> float:
        return round(sum((line.tix or 0) * line.needed for line in self.lines), 2)

    @property
    def missing_on_mtgo(self) -> list[str]:
        return [line.name for line in self.lines if line.tix is None]

    @property
    def unpriced(self) -> list[str]:
        return [line.name for line in self.lines if line.usd is None and line.to_buy]


def price(rows: list[Row], catalog: Catalog) -> DeckPrice:
    prices = catalog.prices([row.card_id for row in rows])
    lines = []
    for row in rows:
        p = prices.get(row.card_id, Prices())
        lines.append(Line(row.name, row.needed, row.shortfall, p.usd, p.tix))
    return DeckPrice(lines)


def scryfall_ids(printings: Mapping[str, list[PricedPrinting]]) -> set[str]:
    """Every printing a kept day must be read for, to price these cards."""
    return {p.scryfall_id for found in printings.values() for p in found}


def _amount(value: object) -> float | None:
    """A price as Scryfall writes it, "1.23"; None for none, or one that isn't a number."""
    if value is None:
        return None
    try:
        amount = Decimal(str(value))
    except InvalidOperation:
        return None
    return float(amount) if amount.is_finite() else None


def on_day(printings: Mapping[str, list[PricedPrinting]], day: Mapping[str, Mapping]) -> dict[str, Prices]:
    """Each card's price on a kept day (scryfall.day_prices), by Catalog.prices' rule: paper, the
    cheapest printing that can be played, or for a card with none its cheapest paper printing;
    a card whose playable printings have no price has none; MTGO, the cheapest tix of any."""
    out = {}
    for card_id, found in printings.items():
        playable = [p for p in found if p.playable]
        paper = playable or [p for p in found if not p.digital]
        usd = [a for p in paper if (a := _amount(day.get(p.scryfall_id, {}).get("usd"))) is not None]
        tix = [a for p in found if (a := _amount(day.get(p.scryfall_id, {}).get("tix"))) is not None]
        out[card_id] = Prices(usd=min(usd, default=None), tix=min(tix, default=None))
    return out
