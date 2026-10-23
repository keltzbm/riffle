from dataclasses import dataclass


@dataclass
class Holding:
    """One row of owned cards: a printing and how many."""

    name: str
    quantity: int
    scryfall_id: str | None = None
    set_code: str | None = None
    collector_number: str | None = None
    foil: bool = False
    source: str = "manabox"  # manabox | arena
    card_id: str | None = None
    # where the copies are and what they are, as a ManaBox export records them; None without the column
    binder_name: str | None = None
    binder_type: str | None = None
    condition: str | None = None
    language: str | None = None
    purchase_price: str | None = None  # as the export writes it: "0.23"
    purchase_currency: str | None = None
