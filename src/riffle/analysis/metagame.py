"""Questions asked of stored MTGO events: what's being played, and by whom."""

from dataclasses import dataclass

from riffle.models.metagame import Event, MtgoDeck


@dataclass
class CardStat:
    name: str
    decks: int = 0  # decks playing it anywhere
    main_decks: int = 0
    side_decks: int = 0
    copies: int = 0  # total, main + side

    def share(self, total_decks: int) -> float:
        return self.decks / total_decks if total_decks else 0.0

    @property
    def avg(self) -> float:
        """Average copies among decks that play it."""
        return self.copies / self.decks if self.decks else 0.0


def all_decks(events: list[Event]) -> list[tuple[Event, MtgoDeck]]:
    return [(e, d) for e in events for d in e.decks]


def card_stats(events: list[Event], board: str = "all") -> list[CardStat]:
    """Every card seen, most-played first. board: all | main | side."""
    stats: dict[str, CardStat] = {}
    for _, deck in all_decks(events):
        seen: dict[str, tuple[int, int]] = {}
        if board in {"all", "main"}:
            for c in deck.main:
                m, s = seen.get(c.name, (0, 0))
                seen[c.name] = (m + c.qty, s)
        if board in {"all", "side"}:
            for c in deck.side:
                m, s = seen.get(c.name, (0, 0))
                seen[c.name] = (m, s + c.qty)
        for name, (m, s) in seen.items():
            st = stats.setdefault(name, CardStat(name))
            st.decks += 1
            st.main_decks += bool(m)
            st.side_decks += bool(s)
            st.copies += m + s
    return sorted(stats.values(), key=lambda s: (-s.decks, -s.copies, s.name))


def find_decks(
    events: list[Event],
    card: str | None = None,
    player: str | None = None,
) -> list[tuple[Event, MtgoDeck]]:
    """Decks containing `card` (main or side) and/or piloted by `player`, case-insensitive."""
    card_lc = card.lower() if card else None
    player_lc = player.lower() if player else None
    out = []
    for e, d in all_decks(events):
        if player_lc and d.player.lower() != player_lc:
            continue
        if card_lc and not any(c.name.lower() == card_lc for c in d.main + d.side):
            continue
        out.append((e, d))
    return out
