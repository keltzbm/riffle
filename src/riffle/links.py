"""How a note links a card: the one place the link form lives, for every writer and reader.

A card links by its safe name, the front face without the characters a file name can't hold,
and shows its own name: [[Fire|Fire // Ice]], [[Summon Bahamut|Summon: Bahamut]]. A name
that's already safe links plainly, [[Sol Ring]]. The safe name is also the name of the card's
note (export/cards.py), so every link resolves. In a table the bar is escaped.
"""

import re
import unicodedata

# What a note's file name can't hold: the union of what macOS, Linux, Windows, Obsidian and
# the sync services refuse, so one vault works on every machine.
UNSAFE = re.compile(r'[\\/:*?"<>|]')
# A link's target, and what it shows when it carries a name; a table escapes the bar.
LINK = re.compile(r"\[\[([^\]|#\\]+)\\?(?:[|#][^\]]*)?\]\]")


def safe_name(name: str) -> str:
    """A card's name as a note can be named: the front face, without the characters a file
    name can't hold, spaces collapsed."""
    return " ".join(UNSAFE.sub("", name.split(" // ")[0]).split())


def card_link(name: str, table: bool = False) -> str:
    """The link to a card, shown under its own name. In a table the bar is escaped."""
    target = safe_name(name)
    if target == name:
        return f"[[{name}]]"
    bar = "\\|" if table else "|"
    return f"[[{target}{bar}{name}]]"


def written(target: str) -> str:
    """A link to target, as a note wrote it."""
    return f"[[{target}]]"


def note_key(name: str) -> str:
    """A note's name as macOS and Obsidian compare it: an accent composed or decomposed, any case."""
    return unicodedata.normalize("NFC", name).casefold()


def targets(text: str) -> list[str]:
    """Every link target in text, its accents composed."""
    return [unicodedata.normalize("NFC", t.strip()) for t in LINK.findall(text)]
