"""Every ManaBox export kept as the file it was, the change between two of them, and the
collection written fresh so an older export put back by hand stays the collection (C33)."""

import csv
import dataclasses
import hashlib
import io
import os
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from riffle import cli, config
from riffle.cli import app
from riffle.config import data_dir
from riffle.ingest import manabox, scryfall
from riffle.models import Holding

# ---- exports as ManaBox writes them ---------------------------------------------------

COLUMNS = (
    "Binder Name",
    "Binder Type",
    "Name",
    "Set code",
    "Set name",
    "Collector number",
    "Foil",
    "Rarity",
    "Quantity",
    "ManaBox ID",
    "Scryfall ID",
    "Purchase price",
    "Misprint",
    "Altered",
    "Signed",
    "Condition",
    "Language",
    "Proxy",
    "Purchase price currency",
    "Added",
)
# the export of 2026-09-21, before ManaBox added Signed and Proxy
EARLY_COLUMNS = tuple(c for c in COLUMNS if c not in {"Signed", "Proxy"})
WITHOUT_BINDER_NAME = tuple(c for c in COLUMNS if c != "Binder Name")
SIX_COLUMNS = ("Name", "Set code", "Collector number", "Foil", "Quantity", "Scryfall ID")

FIRE = ("Fire // Ice", "mh2", "290", "s-fire-mh2")
GRUNT = ("Jötun Grunt", "csp", "8", "s-grunt-csp")
CERBERUS = ("Summon G.F. Cerberus", "fin", "1", "s-cerberus-fin")
SOL = ("Sol Ring", "c21", "263", "s-sol-c21")
RIFT = ("Cyclonic Rift", "2x2", "45", "s-rift-2x2")
BOLT = ("Lightning Bolt", "m10", "146", "s-bolt-m10")
COLLECTION = ("collection", "binder")
TRADE = ("trade", "binder")
DECK = ("Cerberus", "deck")


def row(
    name,
    set_code,
    number,
    scryfall_id,
    quantity,
    foil="normal",
    binder=COLLECTION,
    condition="near_mint",
    language="en",
    price="0.23",
):
    """One row of an export, every column filled as ManaBox fills it."""
    return {
        "Binder Name": binder[0],
        "Binder Type": binder[1],
        "Name": name,
        "Set code": set_code,
        "Set name": "",
        "Collector number": number,
        "Foil": foil,
        "Rarity": "rare",
        "Quantity": str(quantity),
        "ManaBox ID": "",
        "Scryfall ID": scryfall_id,
        "Purchase price": price,
        "Misprint": "false",
        "Altered": "false",
        "Signed": "false",
        "Condition": condition,
        "Language": language,
        "Proxy": "false",
        "Purchase price currency": "USD" if price else "",
        "Added": "2026-09-21 07:30:09",
    }


def export_text(rows, columns=COLUMNS):
    """The export's text: UTF-8 with a BOM, the columns given; a row's other cells are dropped."""
    out = io.StringIO()
    writer = csv.DictWriter(out, columns, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return "﻿" + out.getvalue()


def holding(r):
    """The row as manabox.load reads it."""
    return Holding(
        r["Name"],
        int(r["Quantity"]),
        r["Scryfall ID"] or None,
        r["Set code"].upper() or None,
        r["Collector number"] or None,
        r["Foil"] in {"foil", "etched"},
        binder_name=r["Binder Name"] or None,
        binder_type=r["Binder Type"] or None,
        condition=r["Condition"] or None,
        language=r["Language"] or None,
        purchase_price=r["Purchase price"] or None,
        purchase_currency=r["Purchase price currency"] or None,
    )


# the collection: 8 copies
OLD = [
    row(*FIRE, 2),
    row(*GRUNT, 1, foil="foil", binder=TRADE),
    row(*CERBERUS, 1, binder=TRADE),
    row(*SOL, 1),
    row(*RIFT, 3),
]
# the export before it: two Rifts fewer
BEFORE = [*OLD[:4], row(*RIFT, 1)]
# the export after it
NEW = [
    row(*FIRE, 4),
    row(*CERBERUS, 1, binder=DECK),
    row(*SOL, 1, binder=DECK),
    row(*RIFT, 1),
    row(*BOLT, 10),
]
CHANGED = "+12 copies, −3, 2 moved binders"
LINES = [
    "−2 Cyclonic Rift (2X2 45, near_mint, en) in binder collection",
    "+2 Fire // Ice (MH2 290, near_mint, en) in binder collection",
    "−1 Jötun Grunt (CSP 8, foil, near_mint, en) in binder trade",
    "+10 Lightning Bolt (M10 146, near_mint, en) in binder collection",
    "1 Sol Ring (C21 263, near_mint, en) moved from binder collection to deck Cerberus",
    "1 Summon G.F. Cerberus (FIN 1, near_mint, en) moved from binder trade to deck Cerberus",
]
TEN = [
    row(*RIFT, 3),
    row(*FIRE, 2),
    row(*SOL, 1),
    row(*GRUNT, 1, foil="foil", binder=TRADE),
    row(*CERBERUS, 1, binder=TRADE),
    row(*BOLT, 2),
]
# 6 of TEN's 10 copies dropped: more than half
FOUR = [row(*RIFT, 1), row(*FIRE, 2), row(*BOLT, 1)]
# 5 dropped: half, not more
FIVE = [row(*RIFT, 3), row(*FIRE, 2)]
# every copy in another binder: moves, not removals
MOVED = [{**r, "Binder Name": "Cerberus", "Binder Type": "deck"} for r in TEN]

# ---- times ------------------------------------------------------------------------------


def at(*ymdhms):
    return datetime(*ymdhms, tzinfo=UTC)


# the user's seven exports, 2026-09-21 to 2026-10-18, two days with two each
T1 = at(2026, 9, 21, 7, 30, 9)
T2 = at(2026, 9, 29, 7, 48, 0)
T3 = at(2026, 9, 29, 8, 29, 0)
T4 = at(2026, 10, 5, 7, 26, 0)
T5 = at(2026, 10, 5, 8, 17, 0)
T6 = at(2026, 10, 11, 7, 10, 14)
T7 = at(2026, 10, 18, 8, 41, 52)
T8 = at(2026, 10, 19, 9, 0, 0)


def dated(path, when):
    """path with its modification time set to when."""
    ns = int(when.replace(microsecond=0).timestamp()) * 1_000_000_000 + when.microsecond * 1_000
    os.utime(path, ns=(ns, ns))
    return path


def stamp_of(when):
    return f"{when:%Y-%m-%dT%H%M%SZ}"


def utc(when):
    return f"{when:%Y-%m-%d %H:%M} UTC"


def later(days=1):
    """A time after any ingest this test run makes: when the next export from ManaBox lands."""
    return datetime.now(UTC).replace(microsecond=0) + timedelta(days=days)


def fresh(path, since):
    """Whether path's time is an ingest's: set when it was written, between since and now."""
    return since - 1 <= path.stat().st_mtime <= time.time() + 1


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_export(path, rows, when, columns=COLUMNS):
    path.write_text(export_text(rows, columns), encoding="utf-8")
    return dated(path, when)


@dataclass(frozen=True)
class Export:
    name: str
    when: datetime
    rows: list[dict]
    columns: tuple[str, ...] = COLUMNS


SEVEN = (
    Export("ManaBox_Collection.csv", T1, [row(*SOL, 1)], EARLY_COLUMNS),
    Export("ManaBox_Collection 2.csv", T2, [row(*SOL, 2)]),
    Export("ManaBox_Collection 3.csv", T3, [row(*SOL, 3)]),
    Export("ManaBox_Collection 4.csv", T4, [row(*SOL, 4)]),
    Export("ManaBox_Collection 5.csv", T5, [row(*SOL, 5)]),
    Export("ManaBox_Collection 6.csv", T6, BEFORE),
    Export("ManaBox_Collection 7.csv", T7, OLD),
)


# ---- the stamp and the kept copies ------------------------------------------------------


def test_the_history_folder_is_manabox_in_the_data_folder():
    assert config.load().manabox_exports == data_dir() / "manabox"


def test_the_stamp_is_the_files_modification_time_in_utc_to_the_second(tmp_path, denver):
    export = write_export(tmp_path / "ManaBox_Collection 7.csv", OLD, T7.replace(microsecond=250_000))
    assert manabox.stamp(export) == "2026-10-18T084152Z"


def test_an_export_is_kept_as_its_stamp_with_its_sha256_beside_it_and_its_time(tmp_path):
    export = write_export(tmp_path / "ManaBox_Collection 7.csv", OLD, T7)
    folder = tmp_path / "manabox"
    kept, new = manabox.keep(export, folder)
    assert new is True
    assert kept == manabox.Kept(folder / "2026-10-18T084152Z.csv", sha256(export), T7)
    assert kept.path.read_bytes() == export.read_bytes()
    assert (folder / "2026-10-18T084152Z.sha256").read_text() == f"{sha256(export)}  2026-10-18T084152Z.csv\n"
    assert kept.path.stat().st_mtime == pytest.approx(export.stat().st_mtime, abs=1e-3)
    assert sorted(p.name for p in folder.iterdir()) == ["2026-10-18T084152Z.csv", "2026-10-18T084152Z.sha256"]
    assert export.exists()
    with pytest.raises(dataclasses.FrozenInstanceError):
        kept.sha256 = "0" * 64


def test_the_same_export_is_never_kept_twice(tmp_path):
    export = write_export(tmp_path / "ManaBox_Collection 7.csv", OLD, T7)
    folder = tmp_path / "manabox"
    first, _ = manabox.keep(export, folder)
    # the same bytes under another name and time
    copy = write_export(tmp_path / "put-back.csv", OLD, T8)
    assert manabox.keep(copy, folder) == (first, False)
    assert manabox.keep(first.path, folder) == (first, False)
    assert manabox.kept(folder) == [first]
    assert sorted(p.name for p in folder.iterdir()) == ["2026-10-18T084152Z.csv", "2026-10-18T084152Z.sha256"]


def test_two_exports_with_times_on_one_day_are_two_files(tmp_path):
    folder = tmp_path / "manabox"
    early = write_export(tmp_path / "ManaBox_Collection 2.csv", [row(*SOL, 2)], T2)
    late = write_export(tmp_path / "ManaBox_Collection 3.csv", [row(*SOL, 3)], T3)
    manabox.keep(late, folder)
    manabox.keep(early, folder)
    assert [k.path.name for k in manabox.kept(folder)] == ["2026-09-29T074800Z.csv", "2026-09-29T082900Z.csv"]


def test_a_different_export_with_the_same_time_to_the_second_gets_the_next_free_stamp(tmp_path):
    folder = tmp_path / "manabox"
    a = write_export(tmp_path / "a.csv", [row(*SOL, 1)], T7.replace(microsecond=100_000))
    b = write_export(tmp_path / "b.csv", [row(*SOL, 2)], T7.replace(microsecond=900_000))
    c = write_export(tmp_path / "c.csv", [row(*SOL, 3)], T7)
    kept_a, _ = manabox.keep(a, folder)
    kept_b, _ = manabox.keep(b, folder)
    kept_c, _ = manabox.keep(c, folder)
    assert kept_a.path.name == "2026-10-18T084152Z.csv"
    assert kept_b.path.name == "2026-10-18T084152.000001Z.csv"
    assert kept_c.path.name == "2026-10-18T084152.000002Z.csv"
    assert kept_a.path.read_bytes() == a.read_bytes()
    assert kept_b.path.read_bytes() == b.read_bytes()
    assert manabox.kept(folder) == [kept_a, kept_b, kept_c]
    assert [k.at for k in manabox.kept(folder)] == [T7, T7.replace(microsecond=1), T7.replace(microsecond=2)]
    assert (folder / "2026-10-18T084152.000001Z.sha256").read_text() == (
        f"{sha256(b)}  2026-10-18T084152.000001Z.csv\n"
    )


def test_kept_lists_the_exports_oldest_first_and_ignores_what_isnt_one(tmp_path):
    folder = tmp_path / "manabox"
    assert manabox.kept(folder) == []
    seven, _ = manabox.keep(write_export(tmp_path / "ManaBox_Collection 7.csv", OLD, T7), folder)
    # kept by hand, without its .sha256
    six = write_export(folder / "2026-10-11T071014Z.csv", BEFORE, T6)
    (folder / "2026-10-25T081200Z.csv.part").write_text("half an export")
    (folder / "2026-09-21T073009Z.sha256").write_text(f"{'0' * 64}  2026-09-21T073009Z.csv\n")
    (folder / "collection.csv").write_text(export_text(OLD))
    (folder / "notes.txt").write_text("stray")
    assert manabox.kept(folder) == [manabox.Kept(six, sha256(six), T6), seven]


def test_keep_raises_oserror_where_the_folder_cant_be_made(tmp_path):
    export = write_export(tmp_path / "ManaBox_Collection 7.csv", OLD, T7)
    folder = tmp_path / "manabox"
    folder.write_text("a file where the history folder would be")
    with pytest.raises(OSError):
        manabox.keep(export, folder)


def test_the_stamp_truncates_a_time_a_hair_short_of_the_next_second(tmp_path):
    export = write_export(tmp_path / "ManaBox_Collection 7.csv", OLD, T7)
    ns = int(T7.timestamp()) * 1_000_000_000 + 999_999_500
    os.utime(export, ns=(ns, ns))
    assert manabox.stamp(export) == "2026-10-18T084152Z"


def test_kept_passes_over_a_folder_and_hashes_past_a_sha256_it_cant_read(tmp_path):
    folder = tmp_path / "manabox"
    seven, _ = manabox.keep(write_export(tmp_path / "ManaBox_Collection 7.csv", OLD, T7), folder)
    (folder / "2026-01-01T000000Z.csv").mkdir()
    six = write_export(folder / "2026-10-11T071014Z.csv", BEFORE, T6)
    (folder / "2026-10-11T071014Z.sha256").write_bytes(b"\xff\xfe not UTF-8")
    five = write_export(folder / "2026-10-05T081700Z.csv", [row(*SOL, 5)], T5)
    (folder / "2026-10-05T081700Z.sha256").write_text(f"{'z' * 64}  2026-10-05T081700Z.csv\n")
    assert manabox.kept(folder) == [
        manabox.Kept(five, sha256(five), T5),
        manabox.Kept(six, sha256(six), T6),
        seven,
    ]


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read any file")
def test_kept_leaves_out_a_file_it_cant_read_and_has_no_sha256_for(tmp_path):
    folder = tmp_path / "manabox"
    seven, _ = manabox.keep(write_export(tmp_path / "ManaBox_Collection 7.csv", OLD, T7), folder)
    locked = write_export(folder / "2026-10-11T071014Z.csv", BEFORE, T6)
    locked.chmod(0)
    try:
        assert manabox.kept(folder) == [seven]
    finally:
        locked.chmod(0o644)


# ---- the columns read -------------------------------------------------------------------


def test_an_export_without_the_new_columns_loads_them_as_none(tmp_path):
    export = write_export(tmp_path / "ManaBox_Collection.csv", [row(*FIRE, 2)], T1, SIX_COLUMNS)
    (h,) = manabox.load(export)
    assert (h.name, h.quantity, h.scryfall_id, h.set_code, h.collector_number, h.foil) == (
        "Fire // Ice",
        2,
        "s-fire-mh2",
        "MH2",
        "290",
        False,
    )
    assert (h.binder_name, h.binder_type, h.condition, h.language, h.purchase_price, h.purchase_currency) == (
        None,
    ) * 6


def test_an_export_with_the_new_columns_fills_them_and_an_empty_cell_is_none(tmp_path):
    rows = [row(*GRUNT, 1, foil="foil", binder=TRADE), row(*SOL, 1, price="")]
    export = write_export(tmp_path / "ManaBox_Collection 7.csv", rows, T7)
    grunt, sol = manabox.load(export)
    assert grunt == Holding(
        "Jötun Grunt",
        1,
        "s-grunt-csp",
        "CSP",
        "8",
        True,
        binder_name="trade",
        binder_type="binder",
        condition="near_mint",
        language="en",
        purchase_price="0.23",
        purchase_currency="USD",
    )
    assert (sol.binder_name, sol.binder_type, sol.condition, sol.language) == (
        "collection",
        "binder",
        "near_mint",
        "en",
    )
    assert (sol.purchase_price, sol.purchase_currency) == (None, None)
    assert manabox.load(export) == [holding(r) for r in rows]


# ---- the change -------------------------------------------------------------------------


def test_the_summary_shows_both_counts_and_the_moves_only_when_there_are_any():
    assert str(manabox.Change(12, 3, 2, [])) == "+12 copies, −3, 2 moved binders"
    assert str(manabox.Change(0, 157, 0, [])) == "+0 copies, −157"
    assert str(manabox.Change(0, 0, 0, [])) == "no change"
    assert manabox.change([], []) == manabox.Change(0, 0, 0, [])


def test_a_quantity_going_from_2_to_3_is_one_copy_added():
    two, three = [holding(row(*FIRE, 2))], [holding(row(*FIRE, 3))]
    assert manabox.change(two, three) == manabox.Change(
        1, 0, 0, ["+1 Fire // Ice (MH2 290, near_mint, en) in binder collection"]
    )
    assert manabox.change(three, two) == manabox.Change(
        0, 1, 0, ["−1 Fire // Ice (MH2 290, near_mint, en) in binder collection"]
    )


def test_one_printing_in_two_binders_is_two_keys_and_the_rows_of_one_key_are_summed():
    in_two = [holding(row(*FIRE, 2)), holding(row(*FIRE, 1, binder=TRADE))]
    assert manabox.change(in_two, []) == manabox.Change(
        0,
        3,
        0,
        [
            "−2 Fire // Ice (MH2 290, near_mint, en) in binder collection",
            "−1 Fire // Ice (MH2 290, near_mint, en) in binder trade",
        ],
    )
    # two rows of one key, bought at different prices
    split = [holding(row(*FIRE, 1)), holding(row(*FIRE, 1, price="1.00"))]
    assert manabox.change(split, [holding(row(*FIRE, 2))]) == manabox.Change(0, 0, 0, [])
    assert manabox.change([holding(row(*FIRE, 2))], split) == manabox.Change(0, 0, 0, [])


def test_a_copy_moved_between_binders_is_one_move_not_a_removal_and_an_addition():
    in_trade = [holding(row(*CERBERUS, 1, binder=TRADE))]
    in_deck = [holding(row(*CERBERUS, 1, binder=DECK))]
    assert manabox.change(in_trade, in_deck) == manabox.Change(
        0, 0, 1, ["1 Summon G.F. Cerberus (FIN 1, near_mint, en) moved from binder trade to deck Cerberus"]
    )
    three = [holding(row(*GRUNT, 3, foil="foil", binder=TRADE))]
    two_moved = [holding(row(*GRUNT, 1, foil="foil", binder=TRADE)), holding(row(*GRUNT, 2, foil="foil"))]
    assert manabox.change(three, two_moved) == manabox.Change(
        0, 0, 2, ["2 Jötun Grunt (CSP 8, foil, near_mint, en) moved from binder trade to binder collection"]
    )


def test_a_copy_gone_from_one_binder_with_two_come_to_others_is_one_move_and_one_addition():
    in_trade = [holding(row(*CERBERUS, 1, binder=TRADE))]
    spread = [holding(row(*CERBERUS, 1)), holding(row(*CERBERUS, 1, binder=DECK))]
    assert manabox.change(in_trade, spread) == manabox.Change(
        1,
        0,
        1,
        [
            "1 Summon G.F. Cerberus (FIN 1, near_mint, en) moved from binder trade to binder collection",
            "+1 Summon G.F. Cerberus (FIN 1, near_mint, en) in deck Cerberus",
        ],
    )


def test_a_copy_in_another_binder_with_another_foil_or_condition_isnt_a_move():
    foil_in_trade = [holding(row(*GRUNT, 1, foil="foil", binder=TRADE))]
    assert manabox.change(foil_in_trade, [holding(row(*GRUNT, 1))]) == manabox.Change(
        1,
        1,
        0,
        [
            "+1 Jötun Grunt (CSP 8, near_mint, en) in binder collection",
            "−1 Jötun Grunt (CSP 8, foil, near_mint, en) in binder trade",
        ],
    )
    played = manabox.change(foil_in_trade, [holding(row(*GRUNT, 1, foil="foil", condition="played"))])
    assert (played.added, played.removed, played.moved) == (1, 1, 0)
    assert played.lines == [
        "+1 Jötun Grunt (CSP 8, foil, played, en) in binder collection",
        "−1 Jötun Grunt (CSP 8, foil, near_mint, en) in binder trade",
    ]


def test_a_card_without_a_scryfall_id_is_keyed_by_name_set_and_number():
    c21 = holding(row("Sol Ring", "c21", "263", "", 1))
    repriced = holding(row("Sol Ring", "c21", "263", "", 1, price="9.99"))
    assert manabox.change([c21], [repriced]) == manabox.Change(0, 0, 0, [])
    lea = holding(row("Sol Ring", "lea", "270", "", 1))
    assert manabox.change([c21], [c21, lea]) == manabox.Change(
        1, 0, 0, ["+1 Sol Ring (LEA 270, near_mint, en) in binder collection"]
    )
    # no binder, condition or language recorded
    bare = Holding("Sol Ring", 1, None, "C21", "263")
    assert manabox.change([], [bare]) == manabox.Change(1, 0, 0, ["+1 Sol Ring (C21 263) in no binder"])


def test_the_lines_are_sorted_by_name_then_set_and_number_then_binder():
    change = manabox.change([holding(r) for r in OLD], [holding(r) for r in NEW])
    assert str(change) == CHANGED
    assert change.lines == LINES
    sols = [holding(row("Sol Ring", "lea", "270", "s-sol-lea", 1)), holding(row(*SOL, 1, binder=TRADE))]
    assert manabox.change([], sols).lines == [
        "+1 Sol Ring (C21 263, near_mint, en) in binder trade",
        "+1 Sol Ring (LEA 270, near_mint, en) in binder collection",
    ]


# ---- the column guard -------------------------------------------------------------------


def test_a_column_the_diff_reads_gone_or_added_is_named_and_one_it_doesnt_read_is_not(tmp_path):
    full = write_export(tmp_path / "full.csv", OLD, T7)
    without = write_export(tmp_path / "without.csv", OLD, T8, WITHOUT_BINDER_NAME)
    assert manabox.columns_changed(full, without) == ["Binder Name missing"]
    assert manabox.columns_changed(without, full) == ["Binder Name added"]
    assert manabox.columns_changed(full, write_export(tmp_path / "next.csv", NEW, T8)) == []
    no_price = write_export(
        tmp_path / "no-price.csv", OLD, T8, tuple(c for c in COLUMNS if c != "Purchase price")
    )
    assert manabox.columns_changed(full, no_price) == []


def test_the_18_column_export_beside_a_20_column_one_is_not_a_change_of_columns(tmp_path):
    early = write_export(tmp_path / "ManaBox_Collection.csv", [row(*SOL, 1)], T1, EARLY_COLUMNS)
    late = write_export(tmp_path / "ManaBox_Collection 2.csv", [row(*SOL, 2)], T2)
    assert manabox.columns_changed(early, late) == []
    assert manabox.columns_changed(late, early) == []
    assert manabox.load(early)[0].binder_name == "collection"


# ---- the commands -----------------------------------------------------------------------

NOTE = (
    "---\ngame: mtg\nformat: commander\n---\n\n## Moxfield import\n\n```\n1 Sol Ring\n1 Cyclonic Rift\n```\n"
)
FIXES = "card,recorded as,corrected to,copies,why\nSol Ring,C21 263,FDC 286,1,box: Calling All Angels\n"
ALREADY = (
    "ManaBox_Collection 7.csv is the collection already (manabox/2026-10-18T084152Z.csv); nothing changed"
)


@dataclass
class Home:
    """A fake HOME: a vault with one deck note, ~/Downloads, and Riffle's data folder."""

    root: Path
    mtg: Path

    @property
    def downloads(self):
        return self.root / "Downloads"

    @property
    def collection(self):
        return data_dir() / "collection.csv"

    @property
    def history(self):
        return data_dir() / "manabox"

    def export(self, name, when, rows, columns=COLUMNS):
        """An export in Downloads, dated when."""
        return write_export(self.downloads / name, rows, when, columns)

    def collect(self, rows, when, columns=COLUMNS):
        """The collection as a sync before this patch left it: the export's bytes and its time."""
        return write_export(self.collection, rows, when, columns)

    def fixes(self, when):
        """A ManaBox*.csv in Downloads that isn't an export, dated when."""
        path = self.downloads / "ManaBox_Collection-5-fixes.csv"
        path.write_text(FIXES)
        return dated(path, when)

    def stamps(self):
        """The kept exports' file names, oldest first."""
        return [k.path.name for k in manabox.kept(self.history)]

    def files(self):
        """What the history folder holds, with each file's time: a check that nothing moved."""
        if not self.history.is_dir():
            return []
        return sorted((p.name, p.stat().st_mtime_ns) for p in self.history.iterdir())


@pytest.fixture
def home(tmp_path, monkeypatch, opened):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / ".local" / "share"))
    (tmp_path / ".config" / "riffle").mkdir(parents=True)
    (tmp_path / ".config" / "riffle" / "config.toml").write_text(
        'vault = "~/vault"\nnotes = "games/tcg"\ncard_notes = false\n'
    )
    mtg = tmp_path / "vault" / "games" / "tcg" / "mtg"
    (mtg / "commander").mkdir(parents=True)
    (mtg / "commander" / "rift.md").write_text(NOTE)
    (tmp_path / "Downloads").mkdir()
    data_dir().mkdir(parents=True)
    monkeypatch.setattr(scryfall, "snapshot_prices", lambda: (tmp_path / "2026-09-26.jsonl.gz", False))
    return Home(tmp_path, mtg)


def run(*args):
    return CliRunner().invoke(app, list(args))


def sync():
    return run("sync", "--offline")


# ---- the sync ---------------------------------------------------------------------------


def test_two_exports_picked_up_in_turn_are_both_kept_and_the_change_is_named(home):
    seven = home.export("ManaBox_Collection 7.csv", T7, OLD)
    # newer than the export, and not one
    home.fixes(T8)
    first = sync()
    assert first.exit_code == 0, first.output
    assert (
        "collection: not picked up: ManaBox_Collection-5-fixes.csv in Downloads isn't a ManaBox export "
        "(no Name column)"
    ) in first.output
    assert (
        "collection: picked up ManaBox_Collection 7.csv from Downloads (no collection yet), "
        "kept as manabox/2026-10-18T084152Z.csv; nothing to compare"
    ) in first.output
    assert home.collection.read_text(encoding="utf-8") == export_text(OLD)
    # a sync's pickup gives the collection the export's time: the next export taken is one made after it
    assert home.collection.stat().st_mtime_ns == seven.stat().st_mtime_ns
    when = later()
    eight = home.export("ManaBox_Collection 8.csv", when, NEW)
    second = sync()
    assert second.exit_code == 0, second.output
    assert (
        "collection: picked up ManaBox_Collection 8.csv from Downloads (newer than the collection), "
        f"kept as manabox/{stamp_of(when)}.csv; since the export of 2026-10-18 08:41 UTC: {CHANGED}"
    ) in second.output
    assert "collection: kept " not in second.output
    assert home.collection.read_bytes() == eight.read_bytes()
    assert home.collection.stat().st_mtime_ns == eight.stat().st_mtime_ns
    kept = manabox.kept(home.history)
    assert [k.path.name for k in kept] == ["2026-10-18T084152Z.csv", f"{stamp_of(when)}.csv"]
    assert [k.sha256 for k in kept] == [sha256(k.path) for k in kept]
    for k in kept:
        assert k.path.with_suffix(".sha256").read_text() == f"{k.sha256}  {k.path.name}\n"


def test_the_first_sync_after_the_patch_keeps_the_collection_as_the_first_entry(home):
    # as the sync of 2026-10-11 left it: the export's bytes and its time
    home.collect(BEFORE, T6)
    seven = home.export("ManaBox_Collection 7.csv", T7, OLD)
    result = sync()
    assert result.exit_code == 0, result.output
    assert "collection: kept 1 export in manabox/, 2026-10-11 07:10 UTC" in result.output
    assert (
        "collection: picked up ManaBox_Collection 7.csv from Downloads (newer than the collection), "
        "kept as manabox/2026-10-18T084152Z.csv; since the export of 2026-10-11 07:10 UTC: +2 copies, −0"
    ) in result.output
    assert result.output.index("kept 1 export") < result.output.index("picked up")
    kept = manabox.kept(home.history)
    assert [(k.path.name, k.at) for k in kept] == [
        ("2026-10-11T071014Z.csv", T6),
        ("2026-10-18T084152Z.csv", T7),
    ]
    assert kept[0].path.read_text(encoding="utf-8") == export_text(BEFORE)
    assert home.collection.read_text(encoding="utf-8") == export_text(OLD)
    assert home.collection.stat().st_mtime_ns == seven.stat().st_mtime_ns


def test_seven_exports_in_downloads_are_kept_once_each_by_the_first_sync_and_not_again(home):
    for e in SEVEN:
        home.export(e.name, e.when, e.rows, e.columns)
    # the newest export's bytes, as the sync that copied it left them
    home.collect(OLD, T7)
    result = sync()
    assert result.exit_code == 0, result.output
    assert (
        "collection: kept 7 exports in manabox/, 2026-09-21 07:30 UTC to 2026-10-18 08:41 UTC"
        in result.output
    )
    assert "picked up Mana" not in result.output
    kept = manabox.kept(home.history)
    assert [k.path.name for k in kept] == [f"{stamp_of(e.when)}.csv" for e in SEVEN]
    for k, e in zip(kept, SEVEN, strict=True):
        assert k.sha256 == sha256(home.downloads / e.name) == sha256(k.path)
        assert k.path.with_suffix(".sha256").read_text() == f"{k.sha256}  {k.path.name}\n"
        assert k.at == e.when
        assert k.path.stat().st_mtime == pytest.approx(e.when.timestamp(), abs=1e-3)
    assert home.collection.read_text(encoding="utf-8") == export_text(OLD)
    assert home.collection.stat().st_mtime == pytest.approx(T7.timestamp(), abs=1e-3)
    files = home.files()
    again = sync()
    assert again.exit_code == 0, again.output
    assert "collection: kept" not in again.output and "picked up Mana" not in again.output
    assert home.files() == files


def test_an_older_export_dropped_into_downloads_is_kept_and_not_picked_up(home):
    home.collect(OLD, T7)
    sync()
    home.export("ManaBox_Collection 6.csv", T6, BEFORE)
    result = sync()
    assert result.exit_code == 0, result.output
    assert "collection: kept 1 export in manabox/, 2026-10-11 07:10 UTC" in result.output
    assert "picked up Mana" not in result.output
    assert home.stamps() == ["2026-10-11T071014Z.csv", "2026-10-18T084152Z.csv"]
    assert home.collection.read_text(encoding="utf-8") == export_text(OLD)


def test_an_older_kept_export_put_back_by_hand_stays_the_collection_at_the_next_sync(home):
    home.collect(OLD, T7)
    six = home.export("ManaBox_Collection 6.csv", T6, BEFORE)
    home.export("ManaBox_Collection 7.csv", T7, OLD)
    sync()
    before = time.time()
    put_back = run("ingest", "manabox", str(six))
    assert put_back.exit_code == 0, put_back.output
    assert (
        "restored the export of 2026-10-11 07:10 UTC (manabox/2026-10-11T071014Z.csv); "
        "since the export of 2026-10-18 08:41 UTC: +0 copies, −2"
    ) in put_back.output
    assert "1 decks · " in put_back.output
    assert home.collection.read_text(encoding="utf-8") == export_text(BEFORE)
    assert fresh(home.collection, before)
    result = sync()
    assert result.exit_code == 0, result.output
    assert "picked up Mana" not in result.output
    assert home.collection.read_text(encoding="utf-8") == export_text(BEFORE)
    assert home.stamps() == ["2026-10-11T071014Z.csv", "2026-10-18T084152Z.csv"]


def test_a_sync_that_cant_keep_leaves_the_collection_and_fails_its_step_only(home):
    home.collect(OLD, T7)
    home.export("ManaBox_Collection 8.csv", later(), NEW)
    # a file where the history folder would be: nothing can be kept there
    home.history.write_text("a file where the history folder would be")
    written = home.collection.stat().st_mtime_ns
    result = sync()
    assert result.exit_code == 1, result.output
    assert "! collection: can't keep the export: " in result.output
    assert "; the collection was not replaced" in result.output
    assert "picked up Mana" not in result.output
    assert home.collection.read_text(encoding="utf-8") == export_text(OLD)
    assert home.collection.stat().st_mtime_ns == written
    assert "1 decks · " in result.output
    assert (home.mtg / "_generated").is_dir()
    assert "1 step failed: collection" in result.output


def test_an_export_dropping_more_than_half_the_copies_warns_and_is_still_the_collection(home):
    home.collect(TEN, T7)
    when = later()
    four = home.export("ManaBox_Collection 8.csv", when, FOUR)
    result = sync()
    assert result.exit_code == 0, result.output
    assert (
        "collection: picked up ManaBox_Collection 8.csv from Downloads (newer than the collection), "
        f"kept as manabox/{stamp_of(when)}.csv; since the export of 2026-10-18 08:41 UTC: +0 copies, −6"
    ) in result.output
    assert (
        f"warning: collection: the export of {utc(when)} drops 6 of 10 copies: "
        "check it's the whole collection (riffle ingest manabox --diff)"
    ) in result.output
    assert home.collection.read_bytes() == four.read_bytes()
    assert home.stamps() == ["2026-10-18T084152Z.csv", f"{stamp_of(when)}.csv"]


def test_a_sync_whose_export_lost_a_key_column_warns_and_makes_no_diff(home):
    home.collect(OLD, T7)
    when = later()
    eight = home.export("ManaBox_Collection 8.csv", when, NEW, WITHOUT_BINDER_NAME)
    result = sync()
    assert result.exit_code == 0, result.output
    picked = next(line for line in result.output.splitlines() if "picked up Mana" in line)
    assert (
        "collection: picked up ManaBox_Collection 8.csv from Downloads (newer than the collection), "
        f"kept as manabox/{stamp_of(when)}.csv"
    ) in picked
    assert "; since" not in picked
    assert (
        "warning: collection: the export's columns changed since the export of 2026-10-18 08:41 UTC: "
        "Binder Name missing; no diff"
    ) in result.output
    assert home.collection.read_bytes() == eight.read_bytes()
    assert home.stamps() == ["2026-10-18T084152Z.csv", f"{stamp_of(when)}.csv"]


def test_an_export_with_the_collections_bytes_is_taken_once_and_changes_nothing(home):
    home.collect(OLD, T7)
    home.export("ManaBox_Collection 7.csv", T7, OLD)
    sync()
    # exported again with nothing changed
    home.export("ManaBox_Collection 8.csv", later(), OLD)
    result = sync()
    assert result.exit_code == 0, result.output
    assert (
        "collection: picked up ManaBox_Collection 8.csv from Downloads (newer than the collection), "
        "already kept as manabox/2026-10-18T084152Z.csv; since the export of 2026-10-18 08:41 UTC: no change"
    ) in result.output
    assert "collection: kept " not in result.output
    assert "picked up Mana" not in sync().output
    assert home.stamps() == ["2026-10-18T084152Z.csv"]


def test_a_collection_put_in_by_hand_and_in_downloads_too_is_counted_once(home):
    # not kept yet, and older than the same bytes in Downloads
    home.collect(OLD, T6)
    home.export("ManaBox_Collection 7.csv", T7, OLD)
    result = sync()
    assert result.exit_code == 0, result.output
    assert "collection: kept " not in result.output
    assert (
        "collection: picked up ManaBox_Collection 7.csv from Downloads (newer than the collection), "
        "already kept as manabox/2026-10-11T071014Z.csv; since the export of 2026-10-11 07:10 UTC: no change"
    ) in result.output
    assert home.stamps() == ["2026-10-11T071014Z.csv"]


def test_an_export_that_lands_after_the_sync_that_took_an_older_one_is_still_taken(home):
    home.export("ManaBox_Collection 6.csv", T6, BEFORE)
    sync()
    # made after the export taken, though it lands after the sync that took it
    seven = home.export("ManaBox_Collection 7.csv", T7, OLD)
    result = sync()
    assert result.exit_code == 0, result.output
    assert "picked up ManaBox_Collection 7.csv from Downloads (newer than the collection)" in result.output
    assert home.collection.read_bytes() == seven.read_bytes()


def test_an_export_dated_ahead_of_the_clock_is_taken_once(home):
    home.collect(OLD, T7)
    home.export("ManaBox_Collection 8.csv", later(), NEW)
    assert "picked up ManaBox_Collection 8.csv" in sync().output
    assert "picked up Mana" not in sync().output


def test_an_export_put_back_by_hand_stays_the_collection_beside_one_dated_ahead_of_the_clock(home):
    home.collect(OLD, T7)
    six = home.export("ManaBox_Collection 6.csv", T6, BEFORE)
    home.export("ManaBox_Collection 8.csv", later(), NEW)
    sync()
    assert run("ingest", "manabox", str(six), "--no-sync").exit_code == 0
    assert "picked up Mana" not in sync().output
    assert home.collection.read_text(encoding="utf-8") == export_text(BEFORE)


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read any file")
def test_a_collection_that_cant_be_read_isnt_replaced(home):
    home.collect(OLD, T7)
    home.export("ManaBox_Collection 8.csv", later(), NEW)
    eight = write_export(home.root / "ManaBox_Collection 8.csv", NEW, T8)
    home.collection.chmod(0)
    try:
        result = sync()
        ingest = run("ingest", "manabox", str(eight), "--no-sync")
    finally:
        home.collection.chmod(0o644)
    assert result.exit_code == 1
    assert "! collection: can't keep the export: [Errno 13] Permission denied" in result.output
    assert ingest.exit_code == 1
    assert ingest.stderr.startswith("can't keep the export: [Errno 13] Permission denied")
    assert home.collection.read_text(encoding="utf-8") == export_text(OLD)
    assert home.stamps() == []


def test_a_collection_that_cant_be_written_fails_its_step_and_the_next_sync_takes_the_export(
    home, monkeypatch
):
    home.collect(OLD, T7)
    when = later()
    eight = home.export("ManaBox_Collection 8.csv", when, NEW)
    written = home.collection.stat().st_mtime_ns
    copy = cli._copy_manabox

    def full_disk(*_v):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(cli, "_copy_manabox", full_disk)
    unwritten = (
        "can't write the collection: [Errno 28] No space left on device; it's as it was, "
        f"and the export is kept as manabox/{stamp_of(when)}.csv"
    )
    result = sync()
    assert result.exit_code == 1, result.output
    assert f"! collection: {unwritten}" in result.output
    assert "1 decks · " in result.output
    ingest = run("ingest", "manabox", str(eight), "--no-sync")
    assert ingest.exit_code == 1
    assert ingest.stderr.splitlines() == [unwritten]
    assert home.collection.read_text(encoding="utf-8") == export_text(OLD)
    assert home.collection.stat().st_mtime_ns == written
    monkeypatch.setattr(cli, "_copy_manabox", copy)
    again = sync()
    assert (
        "collection: picked up ManaBox_Collection 8.csv from Downloads (newer than the collection), "
        f"already kept as manabox/{stamp_of(when)}.csv; since the export of 2026-10-18 08:41 UTC: {CHANGED}"
    ) in again.output
    assert home.collection.read_bytes() == eight.read_bytes()


# ---- ingest manabox ---------------------------------------------------------------------


def test_ingest_makes_a_new_export_the_collection_keeping_the_old_one_first(home):
    home.collect(OLD, T7)
    eight = write_export(home.root / "ManaBox_Collection 8.csv", NEW, T8)
    before = time.time()
    result = run("ingest", "manabox", str(eight), "--no-sync")
    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines() == [
        "ManaBox_Collection 8.csv is the collection now, kept as manabox/2026-10-19T090000Z.csv; "
        f"since the export of 2026-10-18 08:41 UTC: {CHANGED}"
    ]
    assert result.stderr == ""
    assert home.collection.read_bytes() == eight.read_bytes()
    assert fresh(home.collection, before)
    kept = manabox.kept(home.history)
    assert [(k.path.name, k.at) for k in kept] == [
        ("2026-10-18T084152Z.csv", T7),
        ("2026-10-19T090000Z.csv", T8),
    ]
    assert kept[0].path.read_text(encoding="utf-8") == export_text(OLD)


def test_ingest_with_no_collection_before_it_has_nothing_to_compare(home):
    home.export("ManaBox_Collection 7.csv", T7, OLD)
    # the newest ManaBox*.csv, passed over for the newest export
    home.fixes(T8)
    result = run("ingest", "manabox", "--no-sync")
    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines() == [
        "ManaBox_Collection 7.csv is the collection now, kept as manabox/2026-10-18T084152Z.csv; "
        "nothing to compare"
    ]
    assert home.collection.read_text(encoding="utf-8") == export_text(OLD)
    assert home.stamps() == ["2026-10-18T084152Z.csv"]


def test_diff_adds_every_line_of_the_change_indented_after_the_summary(home):
    home.collect(OLD, T7)
    eight = write_export(home.root / "ManaBox_Collection 8.csv", NEW, T8)
    result = run("ingest", "manabox", str(eight), "--diff", "--no-sync")
    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines() == [
        "ManaBox_Collection 8.csv is the collection now, kept as manabox/2026-10-19T090000Z.csv; "
        f"since the export of 2026-10-18 08:41 UTC: {CHANGED}",
        *(f"  {line}" for line in LINES),
    ]


def test_ingest_restores_a_kept_export_from_any_file_with_its_bytes(home):
    home.collect(OLD, T7)
    home.export("ManaBox_Collection 6.csv", T6, BEFORE)
    sync()
    put_back = write_export(home.root / "put-back.csv", BEFORE, T8)
    before = time.time()
    result = run("ingest", "manabox", str(put_back), "--no-sync")
    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines() == [
        "restored the export of 2026-10-11 07:10 UTC (manabox/2026-10-11T071014Z.csv); "
        "since the export of 2026-10-18 08:41 UTC: +0 copies, −2"
    ]
    assert home.collection.read_bytes() == put_back.read_bytes()
    assert fresh(home.collection, before)
    assert home.stamps() == ["2026-10-11T071014Z.csv", "2026-10-18T084152Z.csv"]


def test_ingest_restores_from_the_kept_file_itself_and_diff_lists_the_lines(home):
    home.collect(OLD, T7)
    home.export("ManaBox_Collection 6.csv", T6, BEFORE)
    sync()
    result = run("ingest", "manabox", str(home.history / "2026-10-11T071014Z.csv"), "--diff", "--no-sync")
    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines() == [
        "restored the export of 2026-10-11 07:10 UTC (manabox/2026-10-11T071014Z.csv); "
        "since the export of 2026-10-18 08:41 UTC: +0 copies, −2",
        "  −2 Cyclonic Rift (2X2 45, near_mint, en) in binder collection",
    ]
    assert home.collection.read_text(encoding="utf-8") == export_text(BEFORE)
    assert home.stamps() == ["2026-10-11T071014Z.csv", "2026-10-18T084152Z.csv"]


def test_restoring_an_older_export_never_loses_the_newer_one(home):
    # the collection isn't kept yet
    home.collect(OLD, T7)
    older = write_export(home.root / "ManaBox_Collection 6.csv", BEFORE, T6)
    assert run("ingest", "manabox", str(older), "--no-sync").exit_code == 0
    assert home.stamps() == ["2026-10-11T071014Z.csv", "2026-10-18T084152Z.csv"]
    result = run("ingest", "manabox", str(home.history / "2026-10-18T084152Z.csv"), "--no-sync")
    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines() == [
        "restored the export of 2026-10-18 08:41 UTC (manabox/2026-10-18T084152Z.csv); "
        "since the export of 2026-10-11 07:10 UTC: +2 copies, −0"
    ]
    assert home.collection.read_text(encoding="utf-8") == export_text(OLD)


def test_the_export_that_is_the_collection_already_is_kept_once_and_nothing_is_rewritten(home):
    home.collect(OLD, T7)
    seven = home.export("ManaBox_Collection 7.csv", T7, OLD)
    sync()
    files = home.files()
    written = home.collection.stat().st_mtime_ns
    result = run("ingest", "manabox", str(seven))
    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines() == [ALREADY]
    assert "decks · " not in result.output
    assert home.collection.stat().st_mtime_ns == written
    assert home.files() == files


def test_diff_on_the_collection_already_compares_it_with_the_kept_export_before_it(home):
    home.collect(OLD, T7)
    seven = home.export("ManaBox_Collection 7.csv", T7, OLD)
    sync()
    alone = run("ingest", "manabox", str(seven), "--diff", "--no-sync")
    assert alone.exit_code == 0, alone.output
    assert alone.stdout.splitlines() == [
        ALREADY,
        "the first export kept, nothing to compare",
    ]
    home.export("ManaBox_Collection 6.csv", T6, BEFORE)
    sync()
    result = run("ingest", "manabox", str(seven), "--diff", "--no-sync")
    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines() == [
        ALREADY,
        "since the export of 2026-10-11 07:10 UTC: +2 copies, −0",
        "  +2 Cyclonic Rift (2X2 45, near_mint, en) in binder collection",
    ]


@pytest.mark.parametrize(
    ("rows", "summary", "warning"),
    [
        (
            FOUR,
            "+0 copies, −6",
            "the export of 2026-10-19 09:00 UTC drops 6 of 10 copies: check it's the whole collection "
            "(riffle ingest manabox --diff)",
        ),
        (FIVE, "+0 copies, −5", None),
        (MOVED, "+0 copies, −0, 10 moved binders", None),
    ],
    ids=["more than half", "exactly half", "all moved"],
)
def test_ingest_warns_past_half_the_copies_dropped_and_moves_arent_removals(home, rows, summary, warning):
    home.collect(TEN, T7)
    eight = write_export(home.root / "ManaBox_Collection 8.csv", rows, T8)
    result = run("ingest", "manabox", str(eight), "--no-sync")
    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines() == [
        "ManaBox_Collection 8.csv is the collection now, kept as manabox/2026-10-19T090000Z.csv; "
        f"since the export of 2026-10-18 08:41 UTC: {summary}"
    ]
    assert result.stderr.splitlines() == ([warning] if warning else [])
    assert home.collection.read_bytes() == eight.read_bytes()


def test_ingest_that_cant_keep_leaves_the_collection_and_exits_1(home):
    home.collect(OLD, T7)
    eight = write_export(home.root / "ManaBox_Collection 8.csv", NEW, T8)
    # a file where the history folder would be: nothing can be kept there
    home.history.write_text("a file where the history folder would be")
    written = home.collection.stat().st_mtime_ns
    result = run("ingest", "manabox", str(eight), "--no-sync")
    assert result.exit_code == 1, result.output
    assert any(
        line.startswith("can't keep the export: ") and line.endswith("; the collection was not replaced")
        for line in result.stderr.splitlines()
    ), result.stderr
    assert result.stdout == ""
    assert home.collection.read_text(encoding="utf-8") == export_text(OLD)
    assert home.collection.stat().st_mtime_ns == written


def test_ingest_warns_when_the_exports_columns_changed_and_makes_no_diff(home):
    home.collect(OLD, T7)
    eight = write_export(home.root / "ManaBox_Collection 8.csv", NEW, T8, WITHOUT_BINDER_NAME)
    result = run("ingest", "manabox", str(eight), "--no-sync")
    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines() == [
        "ManaBox_Collection 8.csv is the collection now, kept as manabox/2026-10-19T090000Z.csv"
    ]
    assert result.stderr.splitlines() == [
        "the export's columns changed since the export of 2026-10-18 08:41 UTC: Binder Name missing; no diff"
    ]
    assert home.collection.read_bytes() == eight.read_bytes()
    assert home.stamps() == ["2026-10-18T084152Z.csv", "2026-10-19T090000Z.csv"]


def test_diff_on_the_collection_already_names_changed_columns_and_lists_nothing(home):
    home.export("ManaBox_Collection 6.csv", T6, BEFORE, WITHOUT_BINDER_NAME)
    seven = home.export("ManaBox_Collection 7.csv", T7, OLD)
    home.collect(OLD, T7)
    sync()
    result = run("ingest", "manabox", str(seven), "--diff", "--no-sync")
    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines() == [ALREADY]
    assert result.stderr.splitlines() == [
        "the export's columns changed since the export of 2026-10-11 07:10 UTC: Binder Name added; no diff"
    ]


def test_ingest_with_no_export_in_downloads_asks_for_a_path(home, plain):
    home.fixes(T8)
    result = run("ingest", "manabox")
    assert result.exit_code == 2
    assert "no ManaBox export in ~/Downloads — pass a path" in plain(result.output)
    assert not home.history.exists()


def test_ingest_refuses_a_csv_that_isnt_an_export_and_keeps_nothing(home, plain):
    home.collect(OLD, T7)
    fixes = home.fixes(T8)
    result = run("ingest", "manabox", str(fixes))
    assert result.exit_code == 2
    assert "ManaBox_Collection-5-fixes.csv isn't a ManaBox export: no Name column" in plain(result.output)
    assert home.collection.read_text(encoding="utf-8") == export_text(OLD)
    assert not home.history.exists()


# ---- check collection -------------------------------------------------------------------


def test_check_collection_says_no_exports_are_kept_yet_and_keeps_none(home):
    home.collect(OLD, T7)
    home.export("ManaBox_Collection 7.csv", T7, OLD)
    result = run("check", "collection")
    assert result.exit_code == 0, result.output
    assert result.output.splitlines()[:2] == [
        "collection: 8 copies in ~/.local/share/riffle/collection.csv",
        "exports kept: none yet",
    ]
    assert not home.history.exists()


def test_check_collection_counts_the_kept_exports_and_names_the_newest(home):
    home.collect(OLD, T7)
    manabox.keep(home.export("ManaBox_Collection 6.csv", T6, BEFORE), home.history)
    manabox.keep(home.collection, home.history)
    # not kept: check keeps nothing
    home.export("ManaBox_Collection 8.csv", T8, NEW)
    result = run("check", "collection")
    assert result.exit_code == 0, result.output
    assert result.output.splitlines()[:2] == [
        "collection: 8 copies in ~/.local/share/riffle/collection.csv",
        "exports kept: 2, newest 2026-10-18 08:41 UTC, in ~/.local/share/riffle/manabox",
    ]
    assert home.stamps() == ["2026-10-11T071014Z.csv", "2026-10-18T084152Z.csv"]


def test_on_a_terminal_the_newest_export_is_shown_in_the_macs_time(home, on_a_terminal):
    home.collect(OLD, T7)
    manabox.keep(home.collection, home.history)
    result = run("check", "collection")
    assert result.exit_code == 0, result.output
    assert "exports kept: 1, newest 2026-10-18 02:41 MDT, in ~/.local/share/riffle/manabox" in result.output
