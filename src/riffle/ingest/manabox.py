"""ManaBox collection export (CSV).

Encoded here so they cost time once:
  * UTF-8 with a BOM — read with utf-8-sig.
  * One row per printing; the same card appears on several rows.
  * Scryfall ID is the best key when present; set code + collector number
    next; the name is the last resort.
  * An export names its cards in a Name column. A CSV without one isn't an export, whatever
    it's called: on 2026-10-05 a list of corrections named ManaBox_Collection-5-fixes.csv was
    the newest ManaBox*.csv in Downloads, and the sync read it as a collection of no cards.
"""

import csv
from pathlib import Path

from riffle.models import Holding

NAMES = ("Name", "name")


class NotExport(ValueError):
    """A file that isn't a ManaBox collection export."""


def _get(row: dict, *keys: str) -> str:
    for k in keys:
        if row.get(k):
            return row[k].strip()
    return ""


def _columns(path: Path) -> list[str]:
    with path.open(encoding="utf-8-sig", newline="") as f:
        return next(csv.reader(f), [])


def why_not(path: Path) -> str | None:
    """Why a file isn't a ManaBox export, or None when it is one."""
    try:
        columns = _columns(path)
    except (OSError, UnicodeDecodeError, csv.Error) as e:
        return f"can't be read ({e})"
    return None if any(n in columns for n in NAMES) else "no Name column"


def load(path: Path) -> list[Holding]:
    """Every row that names a card; NotExport for a file without a Name column."""
    why = why_not(path)
    if why:
        raise NotExport(f"{path.name} isn't a ManaBox export: {why}")
    out = []
    with path.open(encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            name = _get(row, *NAMES)
            if not name:
                continue
            qty = int(_get(row, "Quantity", "quantity", "Count") or 1)
            out.append(
                Holding(
                    name=name,
                    quantity=qty,
                    scryfall_id=_get(row, "Scryfall ID", "scryfall_id") or None,
                    set_code=(_get(row, "Set code", "Set Code", "set_code").upper() or None),
                    collector_number=_get(row, "Collector number", "Collector Number") or None,
                    foil=_get(row, "Foil", "foil").lower() in {"foil", "etched", "true", "yes"},
                )
            )
    return out


def exports(folder: Path) -> list[Path]:
    """Every ManaBox*.csv in a folder (ManaBox names its exports so), newest first."""
    return sorted(folder.glob("ManaBox*.csv"), key=lambda p: p.stat().st_mtime, reverse=True)


def newest_export(folder: Path) -> Path | None:
    """The most recent ManaBox export in a folder; a ManaBox*.csv that isn't one is passed over."""
    return next((p for p in exports(folder) if why_not(p) is None), None)
