"""Read-only access to the Obsidian vault: deck notes and buy lines.

A deck note is any .md under the notes folder's mtg/ whose frontmatter says game: mtg and
whose body holds a decklist in a fenced code block — preferably under a
"Moxfield import" heading. Nothing here writes; see export/obsidian.py.
"""

import re
from pathlib import Path

from riffle.ingest.decklist import LINE, parse_text
from riffle.models import Deck

SKIP_DIRS = {"_generated", "_log", "archetypes", "matchups"}

Unreadable = list[tuple[Path, str]]  # a note that couldn't be read, and why
FENCE = re.compile(r"^```[^\n]*\n(.*?)^```", re.M | re.S)
BUY_LINE = re.compile(r"^\s*- \[ \] .*?\[\[([^\]|#]+)(?:[|#][^\]]*)?\]\].*#mtg/buy\b", re.M)


def frontmatter(text: str) -> dict:
    """The small YAML subset the vault uses: scalars, [a, b] lists, - item lists."""
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end < 0:
        return {}
    out: dict = {}
    key = None
    for line in text[3:end].splitlines():
        if not line.strip():
            continue
        if line.lstrip().startswith("- ") and key:
            out.setdefault(key, [])
            if isinstance(out[key], list):
                out[key].append(line.strip()[2:].strip().strip("\"'"))
            continue
        k, sep, v = line.partition(":")
        if not sep or line.startswith((" ", "\t")):
            continue
        key, v = k.strip(), v.strip()
        if " #" in v and not v.startswith(('"', "'")):
            v = v.split(" #", 1)[0].strip()
        if v.startswith("[") and v.endswith("]"):
            out[key] = [x.strip().strip("\"'") for x in v[1:-1].split(",") if x.strip()]
        elif v == "":
            out[key] = []
        else:
            out[key] = v.strip("\"'")
    return out


def decklist_block(text: str) -> str | None:
    """The fenced block under a "... import" heading, else the first block
    whose lines are mostly "N Card Name"."""
    m = re.search(r"^#+ .*\bimport\b.*$", text, re.M | re.I)
    if m:
        f = FENCE.search(text, m.end())
        if f:
            return f.group(1)
    for f in FENCE.finditer(text):
        lines = [line for line in f.group(1).splitlines() if line.strip()]
        if lines and sum(bool(LINE.match(line)) for line in lines) >= len(lines) * 0.6:
            return f.group(1)
    return None


def read_deck(path: Path) -> Deck | None:
    text = path.read_text(encoding="utf-8")
    meta = frontmatter(text)
    if meta.get("game") != "mtg":
        return None
    block = decklist_block(text)
    if block is None:
        return None
    deck = parse_text(block, slug=path.stem)
    if not deck.entries:
        return None  # a stub whose list hasn't been written yet
    deck.meta = meta | {"path": str(path)}
    return deck


def deck_notes(mtg_dir: Path) -> list[Path]:
    return sorted(
        p for p in mtg_dir.rglob("*.md") if not SKIP_DIRS.intersection(p.relative_to(mtg_dir).parts[:-1])
    )


def _why(e: OSError | UnicodeDecodeError) -> str:
    return "not UTF-8" if isinstance(e, UnicodeDecodeError) else e.strerror or str(e)


def decks(mtg_dir: Path, unreadable: Unreadable | None = None) -> list[Deck]:
    """Every deck note's deck. A note that can't be read (not UTF-8, say) raises, unless
    unreadable is given: then it's skipped and listed there, so one note can't stop a sync."""
    found = []
    for p in deck_notes(mtg_dir):
        try:
            deck = read_deck(p)
        except (OSError, UnicodeDecodeError) as e:
            if unreadable is None:
                raise
            unreadable.append((p, _why(e)))
            continue
        if deck:
            found.append(deck)
    return found


def find(mtg_dir: Path, ref: str) -> Deck:
    """A deck by slug ("aesi-lands") or by path to a .md or .txt file."""
    p = Path(ref).expanduser()
    if p.suffix == ".txt" and p.exists():
        return parse_text(p.read_text(encoding="utf-8-sig"), slug=p.stem)
    if p.suffix == ".md" and p.exists():
        deck = read_deck(p)
        if deck:
            return deck
        raise ValueError(f"{p} has no game: mtg frontmatter or no decklist block")
    for note in deck_notes(mtg_dir):
        if note.stem == ref:
            deck = read_deck(note)
            if deck:
                return deck
    raise LookupError(f"no deck note named {ref!r} under {mtg_dir}")


def buy_cards(tcg_dir: Path, unreadable: Unreadable | None = None) -> list[str]:
    """Card names on every unticked #mtg/buy line in the vault, deduplicated. A note that
    can't be read raises, or is skipped and listed in unreadable, as in decks()."""
    seen: dict[str, None] = {}
    for p in tcg_dir.rglob("*.md"):
        if "_generated" in p.parts or "_log" in p.parts:
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            if unreadable is None:
                raise
            unreadable.append((p, _why(e)))
            continue
        for m in BUY_LINE.finditer(text):
            seen.setdefault(m.group(1).strip(), None)
    return list(seen)
