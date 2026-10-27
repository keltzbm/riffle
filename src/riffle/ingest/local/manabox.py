"""ManaBox collection export (CSV).

Encoded here so they cost time once:
  * UTF-8 with a BOM — read with utf-8-sig.
  * One row per printing; the same card appears on several rows.
  * Scryfall ID is the best key when present; set code + collector number
    next; the name is the last resort.
  * An export names its cards in a Name column. A CSV without one isn't an export, whatever
    it's called: on 2026-10-05 a list of corrections named ManaBox_Collection-5-fixes.csv was
    the newest ManaBox*.csv in Downloads, and the sync read it as a collection of no cards.
  * Its name carries no date: ManaBox_Collection.csv, then ManaBox_Collection 2.csv and on as
    Downloads fills. An export is dated by its file's modification time.
  * ManaBox keeps no history of its own, so every export Riffle sees is kept, as the file it
    was (keep): the collection's history is what changed from one to the next (change).
  * A row's binder (its name and type), condition, language and purchase price, with the
    price's currency, are columns of their own. The export of 2026-09-21 had them; Signed and
    Proxy came by 2026-09-29.
"""

import csv
import hashlib
import os
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from riffle import runs
from riffle.models import Holding

NAMES = ("Name", "name")
# The columns each field is read from, ManaBox's own name first.
COLUMNS = {
    "name": NAMES,
    "quantity": ("Quantity", "quantity", "Count"),
    "scryfall_id": ("Scryfall ID", "scryfall_id"),
    "set_code": ("Set code", "Set Code", "set_code"),
    "collector_number": ("Collector number", "Collector Number"),
    "foil": ("Foil", "foil"),
    "binder_name": ("Binder Name",),
    "binder_type": ("Binder Type",),
    "condition": ("Condition",),
    "language": ("Language",),
    "purchase_price": ("Purchase price",),
    "purchase_currency": ("Purchase price currency",),
}
COMPARED = tuple(COLUMNS)[:10]  # the fields a change between two exports reads


class NotExport(ValueError):
    """A file that isn't a ManaBox collection export."""


def _get(row: dict, *keys: str) -> str:
    for k in keys:
        if row.get(k):
            return row[k].strip()
    return ""


def _field(row: dict, key: str) -> str | None:
    return _get(row, *COLUMNS[key]) or None


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
            qty = int(_get(row, *COLUMNS["quantity"]) or 1)
            out.append(
                Holding(
                    name=name,
                    quantity=qty,
                    scryfall_id=_field(row, "scryfall_id"),
                    set_code=(_get(row, *COLUMNS["set_code"]).upper() or None),
                    collector_number=_field(row, "collector_number"),
                    foil=_get(row, *COLUMNS["foil"]).lower() in {"foil", "etched", "true", "yes"},
                    binder_name=_field(row, "binder_name"),
                    binder_type=_field(row, "binder_type"),
                    condition=_field(row, "condition"),
                    language=_field(row, "language"),
                    purchase_price=_field(row, "purchase_price"),
                    purchase_currency=_field(row, "purchase_currency"),
                )
            )
    return out


def exports(folder: Path) -> list[Path]:
    """Every ManaBox*.csv in a folder (ManaBox names its exports so), newest first."""
    return sorted(folder.glob("ManaBox*.csv"), key=lambda p: p.stat().st_mtime, reverse=True)


def newest_export(folder: Path) -> Path | None:
    """The most recent ManaBox export in a folder; a ManaBox*.csv that isn't one is passed over."""
    return next((p for p in exports(folder) if why_not(p) is None), None)


# ---- the exports kept -----------------------------------------------------------------


def _made(path: Path) -> datetime:
    return datetime.fromtimestamp(path.stat().st_mtime_ns // 1_000_000_000, UTC)


def stamp(path: Path) -> str:
    """The stamp an export is kept under: its file's modification time in UTC, to the second."""
    return runs.name(_made(path))


@dataclass(frozen=True)
class Kept:
    """An export kept: the file, its SHA-256, and when its stamp says it was made."""

    path: Path
    sha256: str
    at: datetime


def _recorded(path: Path) -> str | None:
    """The SHA-256 a kept export's .sha256 records; None without one that reads as a SHA-256."""
    try:
        sha256 = path.with_suffix(".sha256").read_text().split(maxsplit=1)[0]
    except (OSError, ValueError, IndexError):
        return None
    return sha256 if len(sha256) == 64 and set(sha256) <= set("0123456789abcdef") else None


def kept(folder: Path) -> list[Kept]:
    """Every export kept in the folder, oldest first. One without a .sha256 that can be read (a
    keep cut off between the two files) is hashed instead, and one that can't be hashed either
    is left out: nothing can be compared with it."""
    out = []
    for path in folder.glob("*.csv") if folder.is_dir() else ():
        if not path.is_file() or (at := runs.parse(path.stem)) is None:
            continue
        try:
            out.append(Kept(path, _recorded(path) or runs.file_sha256(path), at))
        except OSError:
            continue
    return sorted(out, key=lambda k: k.at)


def keep(path: Path, folder: Path) -> tuple[Kept, bool]:
    """Keep an export in the folder as <stamp>.csv, with its SHA-256 in <stamp>.sha256 as
    sha256sum writes it, the copy keeping the export's modification time: (what's kept, True).
    An export with the same bytes kept already, under any name, is (that one, False), and
    nothing is written. Nothing is overwritten: a stamp another export has moves on a
    microsecond. OSError when the folder can't be written."""
    data = path.read_bytes()
    sha256 = hashlib.sha256(data).hexdigest()
    for k in kept(folder):
        if k.sha256 == sha256:
            return k, False
    at = _made(path)
    while (folder / f"{runs.name(at)}.csv").exists():
        at += timedelta(microseconds=1)
    dest = folder / f"{runs.name(at)}.csv"
    made = path.stat()
    runs.write(dest, data)
    os.utime(dest, ns=(made.st_atime_ns, made.st_mtime_ns))
    runs.write(dest.with_suffix(".sha256"), f"{sha256}  {dest.name}\n".encode())
    return Kept(dest, sha256, at), True


# ---- what changed from one export to the next ---------------------------------------------

Binder = tuple[str | None, str | None]  # its type and name


@dataclass
class Change:
    """Copies added, copies removed and copies moved to another binder, and a line for each."""

    added: int
    removed: int
    moved: int
    lines: list[str]

    def __str__(self) -> str:
        if not (self.added or self.removed or self.moved):
            return "no change"
        said = f"+{self.added:,} cop{'y' if self.added == 1 else 'ies'}, −{self.removed:,}"
        return f"{said}, {self.moved:,} moved binders" if self.moved else said


def _card(h: Holding) -> tuple:
    """A copy, its binder aside: the printing (its Scryfall ID, else its name, set and number),
    foil, condition and language."""
    printing = (h.scryfall_id,) if h.scryfall_id else (h.name, h.set_code, h.collector_number)
    return printing, h.foil, h.condition, h.language


def _shown(h: Holding) -> str:
    where = " ".join(p for p in (h.set_code, h.collector_number) if p)
    parts = [p for p in (where, "foil" if h.foil else None, h.condition, h.language) if p]
    return f"{h.name} ({', '.join(parts)})" if parts else h.name


def _binder(b: Binder) -> str:
    return " ".join(p for p in b if p) or "no binder"


def _order(h: Holding, b: Binder) -> tuple[str, str, str, str]:
    return h.name.casefold(), h.set_code or "", h.collector_number or "", _binder(b).casefold()


def change(old: list[Holding], new: list[Holding]) -> Change:
    """What changed from the `old` export to the `new`, copies summed by card and binder. A copy
    gone from one binder that's in another, the same card, is a move."""
    counts: dict[tuple, Counter[Binder]] = defaultdict(Counter)
    shown: dict[tuple, Holding] = {}
    for sign, holdings in ((-1, old), (1, new)):
        for h in holdings:
            counts[_card(h)][h.binder_type, h.binder_name] += sign * h.quantity
            shown.setdefault(_card(h), h)
    res = Change(0, 0, 0, [])
    lines: list[tuple[tuple[str, str, str, str], str]] = []
    for card, binders in counts.items():
        h = shown[card]
        order = sorted(binders, key=lambda b: _binder(b).casefold())
        gone = {b: -binders[b] for b in order if binders[b] < 0}
        came = {b: binders[b] for b in order if binders[b] > 0}
        for a in gone:
            for b in came:
                if k := min(gone[a], came[b]):
                    gone[a] -= k
                    came[b] -= k
                    res.moved += k
                    lines.append((_order(h, a), f"{k:,} {_shown(h)} moved from {_binder(a)} to {_binder(b)}"))
        for b, n in gone.items():
            if n:
                res.removed += n
                lines.append((_order(h, b), f"−{n:,} {_shown(h)} in {_binder(b)}"))
        for b, n in came.items():
            if n:
                res.added += n
                lines.append((_order(h, b), f"+{n:,} {_shown(h)} in {_binder(b)}"))
    res.lines = [line for _v, line in sorted(lines, key=lambda kv: kv[0])]
    return res


def _fields(path: Path) -> set[str]:
    columns = set(_columns(path))
    return {key for key in COMPARED if columns & set(COLUMNS[key])}


def columns_changed(old: Path, new: Path) -> list[str]:
    """The columns a change reads that one export has and the other hasn't, as ManaBox names
    them: Binder Name missing, or Binder Name added. Columns a change doesn't read aren't compared."""
    was, now = _fields(old), _fields(new)
    return [
        f"{COLUMNS[key][0]} {'missing' if key in was else 'added'}"
        for key in COMPARED
        if (key in was) != (key in now)
    ]
