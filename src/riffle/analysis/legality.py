"""Format legality and playset eligibility.

Per-card status comes from Scryfall's `legalities`; deck rules (size, copy
limits, sideboard, commander color identity) live here.

Playset rule: buy 4 only if the card is legal in a format being built
(Modern, Legacy, Pioneer, Pauper). Commander-only cards stay at one copy and
rotate between decks.
"""

import re
from dataclasses import dataclass, field

from riffle.analysis.colors import wubrg_sort
from riffle.analysis.resolve import resolve_deck
from riffle.models import CardRules, Deck, DeckEntry
from riffle.store import Catalog

BUILT_FORMATS = ("modern", "legacy", "pioneer", "pauper")


@dataclass(frozen=True)
class FormatRules:
    size: int  # minimum, or exact when `exact`
    exact: bool = False
    copies: int = 4
    sideboard: int = 15  # max; 0 = no sideboard
    commander: bool = False  # command zone + color identity


_CONSTRUCTED = FormatRules(60)
_COMMANDER = FormatRules(100, exact=True, copies=1, sideboard=0, commander=True)

# Keys match Scryfall's `legalities` keys.
FORMAT_RULES: dict[str, FormatRules] = {
    **{
        f: _CONSTRUCTED
        for f in (
            "standard",
            "future",
            "pioneer",
            "explorer",
            "modern",
            "legacy",
            "vintage",
            "pauper",
            "premodern",
            "oldschool",
            "historic",
            "timeless",
            "alchemy",
            "penny",
        )
    },
    "commander": _COMMANDER,
    "duel": _COMMANDER,
    "paupercommander": _COMMANDER,
    "predh": _COMMANDER,
    "brawl": _COMMANDER,
    "standardbrawl": FormatRules(60, exact=True, copies=1, sideboard=0, commander=True),
    "oathbreaker": FormatRules(60, exact=True, copies=1, sideboard=0, commander=True),
    "gladiator": FormatRules(100, exact=True, copies=1, sideboard=0),
}

ALIASES = {
    "edh": "commander",
    "cedh": "commander",
    "duelcommander": "duel",
    "pdh": "paupercommander",
    "pauperedh": "paupercommander",
    "historicbrawl": "brawl",
    "pennydreadful": "penny",
}

_WORDS = {"two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}
_ANY_NUMBER = re.compile(r"a deck can have any number of cards named", re.I)
_UP_TO = re.compile(r"a deck can have up to (\w+) cards named", re.I)
_PAIRING = ("partner", "friends forever", "choose a background", "doctor's companion")


def normalize_format(fmt: str) -> str:
    key = re.sub(r"[\s_-]+", "", fmt.strip().lower())
    return ALIASES.get(key, key)


# ---- single cards --------------------------------------------------------------


def status(card_id: str, fmt: str, catalog: Catalog) -> str:
    """legal | not_legal | banned | restricted | unknown"""
    r = catalog.rules([card_id]).get(card_id)
    return r.legalities.get(normalize_format(fmt), "unknown") if r else "unknown"


def is_legal(card_id: str, fmt: str, catalog: Catalog) -> bool:
    return status(card_id, fmt, catalog) in {"legal", "restricted"}


def copy_limit(card_id: str, fmt: str, catalog: Catalog, r: CardRules | None = None) -> int | None:
    """Copies allowed in one deck; None means any number. r: the card's rules, if already at hand."""
    fmt = normalize_format(fmt)
    r = r or catalog.rules([card_id]).get(card_id)
    if r:
        if "Basic" in r.type_line and "Land" in r.type_line:
            return None
        if _ANY_NUMBER.search(r.oracle_text):
            return None
        m = _UP_TO.search(r.oracle_text)
        if m and m[1].lower() in _WORDS:
            return _WORDS[m[1].lower()]
        if r.legalities.get(fmt) == "restricted":
            return 1
    elif catalog.is_basic(card_id):
        return None
    rules = FORMAT_RULES.get(fmt)
    return rules.copies if rules else 4


def playset_eligible(card_id: str, catalog: Catalog) -> bool:
    """Legal in at least one actively built constructed format."""
    return any(is_legal(card_id, f, catalog) for f in BUILT_FORMATS)


# ---- whole decks ---------------------------------------------------------------


@dataclass
class Issue:
    severity: str  # error | warning
    message: str
    card: str | None = None


@dataclass
class LegalityReport:
    slug: str
    format: str
    issues: list[Issue] = field(default_factory=list)

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == "error"]

    @property
    def warnings(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == "warning"]

    @property
    def legal(self) -> bool:
        return not self.errors

    def error(self, message: str, card: str | None = None) -> None:
        self.issues.append(Issue("error", message, card))

    def warn(self, message: str, card: str | None = None) -> None:
        self.issues.append(Issue("warning", message, card))


def can_be_commander(r: CardRules) -> bool:
    front = r.type_line.split(" // ")[0]
    return ("Legendary" in front and "Creature" in front) or "can be your commander" in r.oracle_text.lower()


def _pairs(r: CardRules) -> bool:
    text = r.oracle_text.lower()
    return any(k in text for k in _PAIRING) or "Background" in r.type_line or "Doctor" in r.type_line


def _matched(entries: list[DeckEntry]) -> list[tuple[str, DeckEntry]]:
    """Entries resolved to a card, each paired with its card id."""
    out: list[tuple[str, DeckEntry]] = []
    for e in entries:
        if e.card_id:
            out.append((e.card_id, e))
    return out


def check_deck(deck: Deck, catalog: Catalog, fmt: str | None = None) -> LegalityReport:
    """Every rule the deck breaks in `fmt` (default: the note's format)."""
    fmt = normalize_format(fmt or deck.format)
    rep = LegalityReport(deck.slug, fmt)
    rules = FORMAT_RULES.get(fmt)
    if rules is None:
        rep.error(f"unknown format '{fmt}'")
        return rep

    for name in resolve_deck(deck, catalog):
        rep.error("not matched to a card", name)
    matched = _matched(deck.entries)
    found = catalog.rules({card_id for card_id, _ in matched})
    card_rules = {card_id: found.get(card_id) for card_id, _ in matched}

    commanders = deck.board("commander")
    main = sum(e.quantity for e in deck.board("main"))
    side = sum(e.quantity for e in deck.entries if e.board in {"sideboard", "companion"})

    # size
    if rules.exact:
        total = main + sum(e.quantity for e in commanders)
        if total != rules.size:
            rep.error(f"{total} cards — {fmt} decks are exactly {rules.size}")
        extra = sum(e.quantity for e in deck.board("sideboard"))
        if extra:
            rep.warn(f"{extra} sideboard cards ignored — {fmt} has no sideboard")
    else:
        if main < rules.size:
            rep.error(f"{main} cards in the main deck — {fmt} needs at least {rules.size}")
        if side > rules.sideboard:
            rep.error(f"{side} sideboard cards — max {rules.sideboard}")
        if commanders:
            rep.warn(f"commander section ignored — {fmt} has no command zone")

    # per card: status and copies (main + sideboard combined)
    counted = [(card_id, e) for card_id, e in matched if rules.commander or e.board != "commander"]
    if rules.commander:
        counted = [(card_id, e) for card_id, e in counted if e.board != "sideboard"]
    totals: dict[str, int] = {}
    for card_id, e in counted:
        totals[card_id] = totals.get(card_id, 0) + e.quantity
    for card_id, n in totals.items():
        name = catalog.name(card_id)
        r = card_rules[card_id]
        if r is None:
            rep.warn("no legality data", name)
            continue
        st = r.legalities.get(fmt)
        if st == "banned":
            rep.error(f"banned in {fmt}", name)
        elif st == "not_legal":
            rep.error(f"not legal in {fmt}", name)
        elif st is None:
            rep.warn(f"Scryfall has no {fmt} legality for this card", name)
        limit = copy_limit(card_id, fmt, catalog, r)
        if limit is not None and n > limit:
            why = "restricted" if st == "restricted" else f"max {limit}"
            rep.error(f"{n} copies — {why}", name)

    if rules.commander:
        _check_commanders(deck, catalog, card_rules, fmt, rep)
    return rep


def _check_commanders(
    deck: Deck, catalog: Catalog, card_rules: dict[str, CardRules | None], fmt: str, rep: LegalityReport
) -> None:
    cmdrs = [card_id for card_id, _ in _matched(deck.board("commander"))]
    if not deck.board("commander"):
        rep.error("no commander — add a Commander section")
        return
    if len(cmdrs) > 2 and fmt != "oathbreaker":
        rep.error(f"{len(cmdrs)} commanders — at most 2")
    cmdr_rules = [card_rules[card_id] for card_id in cmdrs]
    if fmt != "oathbreaker":
        for card_id, r in zip(cmdrs, cmdr_rules, strict=True):
            if r and not can_be_commander(r):
                rep.warn("not a legendary creature — check it can lead", catalog.name(card_id))
        pairable = [r is not None and _pairs(r) for r in cmdr_rules]
        if len(cmdrs) == 2 and not all(pairable):
            rep.warn("two commanders, but not both partner / background / companion pairs")

    identity = {c for r in cmdr_rules if r for c in r.color_identity}
    seen: set[str] = set()
    for e in deck.entries:
        if e.board in {"commander", "sideboard"} or not e.card_id or e.card_id in seen:
            continue
        seen.add(e.card_id)
        r = card_rules[e.card_id]
        outside = wubrg_sort([c for c in (r.color_identity if r else ()) if c not in identity])
        if outside:
            rep.error(f"outside color identity ({''.join(outside)})", catalog.name(e.card_id))
