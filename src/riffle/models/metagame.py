"""Represent event results and the decklists played in them."""

import hashlib
from dataclasses import dataclass, field


@dataclass
class Card:
    name: str
    qty: int


@dataclass
class MtgoDeck:
    player: str
    main: list[Card] = field(default_factory=list)
    side: list[Card] = field(default_factory=list)
    rank: int | None = None  # final standing (challenges, showcases)
    record: str | None = None  # "5-0" for leagues

    @property
    def fingerprint(self) -> str:
        """Same 75 (main and side, any order, any printing split) -> same value.
        Groups identical lists without dropping any: a list that 5-0s twice counts twice."""

        def part(cards: list[Card]) -> str:
            return "|".join(sorted(f"{c.name.lower()}:{c.qty}" for c in cards))

        return hashlib.sha1(f"{part(self.main)}||{part(self.side)}".encode()).hexdigest()[:12]

    def to_text(self) -> str:
        """MTGO .txt: main, blank line, sideboard — readable by `riffle own`."""
        lines = [f"{c.qty} {c.name}" for c in self.main]
        if self.side:
            lines += [""] + [f"{c.qty} {c.name}" for c in self.side]
        return "\n".join(lines) + "\n"


@dataclass
class Event:
    slug: str
    event_id: str
    name: str  # "modern-challenge-32"
    format: str  # "modern"
    kind: str  # one of KINDS
    date: str  # ISO date
    decks: list[MtgoDeck] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict) -> "Event":
        decks = [
            MtgoDeck(
                player=x["player"],
                main=[Card(**c) for c in x["main"]],
                side=[Card(**c) for c in x["side"]],
                rank=x.get("rank"),
                record=x.get("record"),
            )
            for x in d["decks"]
        ]
        return cls(**{**d, "decks": decks})
