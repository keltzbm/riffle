"""Plain-text decklists: Moxfield, Archidekt, MTGA, MTGO .txt.

Accepted line shapes:
    1 Sol Ring
    1x Sol Ring
    1 Sol Ring (M3C) 283
    1 Sol Ring (M3C) 283 *F*
Section headers switch the board: Commander, Deck, Mainboard, Sideboard,
Companion. Maybeboard / Considering / Tokens sections are skipped — those
cards aren't in the deck. Zero-quantity lines are skipped. Lines starting
with # or // are comments. A blank line after the main deck starts the
sideboard only in MTGO-style lists (no headers).
"""

import re
from pathlib import Path

from riffle.models import Deck, DeckEntry

LINE = re.compile(
    r"^\s*(?P<qty>\d+)x?\s+(?P<name>.+?)"
    r"(?:\s+\((?P<set>[A-Za-z0-9]{2,6})\)(?:\s+(?P<num>[^\s*]+))?)?"
    r"(?:\s+\*[A-Z]+\*)*\s*$"
)

HEADERS = {
    "commander": "commander",
    "commanders": "commander",
    "deck": "main",
    "main": "main",
    "mainboard": "main",
    "maindeck": "main",
    "sideboard": "sideboard",
    "companion": "companion",
}
SKIPPED = {"maybeboard", "maybe", "considering", "tokens", "attractions", "stickers"}


def parse_text(text: str, slug: str = "deck") -> Deck:
    deck = Deck(slug=slug)
    text = text.lstrip("\ufeff")  # byte-order mark from Windows-made files
    board: str | None = "main"
    saw_header = False
    saw_main = False
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            if saw_main and not saw_header:
                board = "sideboard"  # MTGO .txt convention
            continue
        if line.startswith(("#", "//")):
            continue
        key = line.rstrip(":").lower()
        if key in HEADERS or key in SKIPPED:
            board, saw_header = HEADERS.get(key), True  # None = skip this section
            continue
        m = LINE.match(line)
        if not m or board is None or int(m["qty"]) == 0:
            continue
        deck.entries.append(
            DeckEntry(
                name=m["name"].strip(),
                quantity=int(m["qty"]),
                board=board,
                set_code=m["set"].upper() if m["set"] else None,
                collector_number=m["num"],
            )
        )
        saw_main = saw_main or board == "main"
    return deck


def load(path: Path) -> Deck:
    return parse_text(path.read_text(encoding="utf-8-sig"), slug=path.stem)
