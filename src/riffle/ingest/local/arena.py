"""MTG Arena collection, from whatever exporter you use.

Arena has no API and stopped logging the collection years ago; exporters
either read the running game's memory or are a paid Untapped.gg feature.
Accepted here:
  * text lists, one card per line: "4 Sheoldred, the Apocalypse (DMU) 107"
  * CSV with a name column and a count/quantity column
"""

import csv
from pathlib import Path

from riffle.ingest.local.decklist import parse_text
from riffle.models import Holding

NAME_COLS = ("name", "card", "card name", "cardname")
COUNT_COLS = ("count", "quantity", "qty", "owned", "amount")


def load(path: Path) -> list[Holding]:
    text = path.read_text(encoding="utf-8-sig")
    if path.suffix.lower() == ".csv":
        rows = list(csv.DictReader(text.splitlines()))
        if rows:
            cols = {k.strip().lower(): k for k in rows[0]}
            name = next((cols[c] for c in NAME_COLS if c in cols), None)
            count = next((cols[c] for c in COUNT_COLS if c in cols), None)
            if name and count:
                return [
                    Holding(r[name].strip(), int(r[count]), source="arena")
                    for r in rows
                    if r[name].strip() and (r[count] or "").strip().isdigit() and int(r[count]) > 0
                ]
        raise ValueError(f"{path.name}: expected columns like Name and Count")
    deck = parse_text(text)
    return [Holding(e.name, e.quantity, source="arena") for e in deck.entries]
