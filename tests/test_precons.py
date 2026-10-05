"""Precon boxes from MTGJSON's deck files, the printings ManaBox misread from them, and a
collection that isn't a ManaBox export."""

import io
import json
import os
import tarfile
from datetime import UTC, datetime

import pytest
from typer.testing import CliRunner

from riffle import runs, vault
from riffle.analysis.resolve import resolve_holdings
from riffle.cli import app
from riffle.config import data_dir
from riffle.ingest import manabox, mtgjson, precons, scryfall
from riffle.ingest.precons import Box, Boxes, Fix, Short
from riffle.models import Holding

BUILT = datetime(2026, 10, 5, 6, 7, 25, tzinfo=UTC)
HEADER = "Name,Set code,Collector number,Foil,Quantity,Scryfall ID\n"
FIXES = "card,recorded as,corrected to,copies,why\nSol Ring,C21 263,FDC 286,1,box: Calling All Angels\n"


def card(sid, count=1):
    return {"name": "x", "count": count, "identifiers": {"scryfallId": sid}}


ANGELS = {
    "name": "Calling All Angels",
    "code": "FDC",
    "commander": [],
    "displayCommander": [card("s-sol-fdc")],  # the box's face card, held once already
    "mainBoard": [card("s-sol-fdc"), card("s-rift-fdc", 2), card("s-forest-fdc", 10)],
    "tokens": [],
}
REX = {
    "name": "Tramplesaurus Rex",
    "code": "FDC",
    "mainBoard": [card("s-sol-fdc"), {"name": "no ID", "count": 1}],
}
BLITZ = {"name": "Counter Blitz (FINAL FANTASY X)", "code": "FIC", "mainBoard": [card("s-sol-c21")]}
BLITZ_CE = {"name": "Counter Blitz Collector's Edition (FINAL FANTASY X)", "code": "FIC", "mainBoard": []}
DECKS = {
    "CallingAllAngels_FDC": ANGELS,
    "TramplesaurusRex_FDC": REX,
    "CounterBlitzFinalFantasyX_FIC": BLITZ,
    "CounterBlitzCollectorSEditionFinalFantasyX_FIC": BLITZ_CE,
}


def keep(decks=DECKS, at=BUILT, listed=None, tar=None):
    """MTGJSON's DeckList and AllDeckFiles kept as its watch keeps them."""
    entries = [{"name": d["name"], "code": d["code"], "fileName": f} for f, d in decks.items()]
    runs.keep(
        mtgjson.lists_dir("deck-list"), at, json.dumps({"meta": {}, "data": listed or entries}).encode()
    )
    if tar is None:
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as t:
            for f, d in decks.items():
                body = json.dumps({"meta": {}, "data": d}).encode()
                info = tarfile.TarInfo(f"AllDeckFiles/{f}.json")
                info.size = len(body)
                t.addfile(info, io.BytesIO(body))
        tar = buf.getvalue()
    runs.keep(mtgjson.lists_dir("all-deck-files"), at, tar)


def registry(*names, tmp_path=None):
    return {name: [(tmp_path or data_dir()) / f"{i}.md"] for i, name in enumerate(names)}


# ---- the export ----------------------------------------------------------------------


def test_a_csv_without_a_name_column_isnt_an_export(tmp_path):
    fixes, bad = tmp_path / "ManaBox_Collection-5-fixes.csv", tmp_path / "ManaBox_Latin1.csv"
    fixes.write_text(FIXES)
    bad.write_bytes(b"Name\n\xff\xfe\n")
    assert manabox.why_not(fixes) == "no Name column"
    assert manabox.why_not(bad).startswith("can't be read (")
    with pytest.raises(
        manabox.NotExport, match="ManaBox_Collection-5-fixes.csv isn't a ManaBox export: no Name"
    ):
        manabox.load(fixes)


def test_the_newest_export_passes_over_a_manabox_csv_that_isnt_one(tmp_path):
    export, fixes = tmp_path / "ManaBox_Collection 5.csv", tmp_path / "ManaBox_Collection-5-fixes.csv"
    export.write_text(HEADER + "Sol Ring,C21,263,normal,1,s-sol-c21\n")
    fixes.write_text(FIXES)
    os.utime(export, (1_000, 1_000))
    assert manabox.exports(tmp_path) == [fixes, export]
    assert manabox.newest_export(tmp_path) == export


# ---- the registry --------------------------------------------------------------------


def test_deck_notes_register_the_boxes_they_name(tmp_path):
    (tmp_path / "commander").mkdir()
    head = "---\ngame: mtg\nsource: {}\n---\n"
    (tmp_path / "commander" / "a.md").write_text(head.format("precon:Calling All Angels"))
    (tmp_path / "commander" / "b.md").write_text(head.format('"precon:Calling All Angels"'))
    (tmp_path / "commander" / "c.md").write_text(head.format("precon:Counter Blitz"))
    (tmp_path / "commander" / "d.md").write_text(head.format("site:mtgjson.com"))
    (tmp_path / "commander" / "e.md").write_text(head.format("precon:"))
    (tmp_path / "commander" / "f.md").write_bytes(b"precon:\xff")
    found = vault.precon_boxes(tmp_path)
    assert {box: [p.name for p in notes] for box, notes in found.items()} == {
        "Calling All Angels": ["a.md", "b.md"],
        "Counter Blitz": ["c.md"],
    }


def test_a_box_is_named_as_mtgjson_names_it_or_without_its_parenthesised_part():
    decks = [{"name": d["name"], "code": d["code"]} for d in DECKS.values()]
    decks += [{"name": "Evasive Maneuvers", "code": "C13"}, {"name": "Evasive Maneuvers", "code": "CMA"}]
    assert [d["code"] for d in precons.match("Calling All Angels", decks)] == ["FDC"]
    assert [d["name"] for d in precons.match("Counter Blitz", decks)] == ["Counter Blitz (FINAL FANTASY X)"]
    assert precons.match("Counter Blitz (FINAL FANTASY X)", decks) == precons.match("Counter Blitz", decks)
    assert len(precons.match("Evasive Maneuvers", decks)) == 2
    assert precons.match("Evasive Maneuvers (cma)", decks) == [{"name": "Evasive Maneuvers", "code": "CMA"}]
    assert [d["name"] for d in precons.match("Counter Blitz Collector's Edition", decks)] == [
        BLITZ_CE["name"]
    ]
    assert precons.match("Counter", decks) == []


# ---- the boxes' lists ----------------------------------------------------------------


def test_without_deck_files_kept_no_printing_is_corrected():
    assert precons.load(registry("Calling All Angels")).problems == [precons.NOT_KEPT]
    assert precons.load({}) == Boxes()
    assert not (data_dir() / precons.CACHE).exists()


def test_the_registered_boxes_lists_are_read_once_and_kept(monkeypatch):
    keep()
    found = precons.load(registry("Calling All Angels", "Counter Blitz"))
    assert found.problems == []
    assert found.boxes == [
        Box(
            "Calling All Angels",
            "Calling All Angels",
            "FDC",
            (("s-sol-fdc", 1), ("s-rift-fdc", 2), ("s-forest-fdc", 10)),
        ),
        Box("Counter Blitz", "Counter Blitz (FINAL FANTASY X)", "FIC", (("s-sol-c21", 1),)),
    ]
    assert precons.cached() == Boxes(found.boxes)

    def unread(*_):
        raise AssertionError("read again")

    monkeypatch.setattr(precons, "_deck_files", unread)
    monkeypatch.setattr(precons.runs, "rebuild", unread)
    assert precons.load(registry("Counter Blitz")).boxes == found.boxes[1:]  # kept, and the cache follows
    assert precons.cached().boxes == found.boxes[1:]
    precons.load({})
    assert precons.cached() == Boxes() and not (data_dir() / precons.CACHE).exists()


def test_a_newer_build_or_a_new_box_reads_the_deck_files_again(monkeypatch):
    keep()
    precons.load(registry("Calling All Angels"))
    read = []
    real = precons._deck_files
    monkeypatch.setattr(
        precons, "_deck_files", lambda path, wanted: read.append(wanted) or real(path, wanted)
    )
    assert precons.load(registry("Calling All Angels", "Tramplesaurus Rex")).boxes[1].cards == (
        ("s-sol-fdc", 1),
    )
    keep(at=BUILT.replace(day=6))
    precons.load(registry("Calling All Angels", "Tramplesaurus Rex"))
    assert read == [{"TramplesaurusRex_FDC"}, {"CallingAllAngels_FDC", "TramplesaurusRex_FDC"}]


def test_a_box_that_cant_be_read_is_named_with_its_note(tmp_path):
    listed = [{"name": d["name"], "code": d["code"], "fileName": f} for f, d in DECKS.items()]
    listed += [
        {"name": "Evasive Maneuvers", "code": "C13", "fileName": "EvasiveManeuvers_C13"},
        {"name": "Evasive Maneuvers", "code": "CMA", "fileName": "EvasiveManeuvers_CMA"},
    ]
    keep(listed=listed)
    found = precons.load(
        registry("Calling All Angels", "Angels", "Evasive Maneuvers", "Evasive Maneuvers (C13)")
    )
    assert [b.name for b in found.boxes] == ["Calling All Angels"]
    assert found.problems == [
        """precon box "Angels" in 1.md isn't among MTGJSON's decks: write the name MTGJSON gives it""",
        """precon box "Evasive Maneuvers" in 2.md names 2 of MTGJSON's decks, Evasive Maneuvers (C13), """
        """Evasive Maneuvers (CMA): add the set of yours, e.g. Evasive Maneuvers (SET)""",
        """precon box "Evasive Maneuvers (C13)": EvasiveManeuvers_C13 isn't in MTGJSON's deck files""",
    ]


def test_deck_files_that_dont_read_correct_nothing():
    keep(tar=b"not a tar")
    found = precons.load(registry("Calling All Angels"))
    assert found.boxes == []
    assert found.problems[0].startswith("MTGJSON's deck files can't be read (")


def test_a_cache_in_another_shape_holds_no_boxes():
    (data_dir()).mkdir(parents=True)
    (data_dir() / precons.CACHE).write_text('{"boxes": {"Calling All Angels": {"deck": "x"}}}')
    assert precons.cached() == Boxes()
    (data_dir() / precons.CACHE).write_text("[]")
    assert precons.cached() == Boxes()


# ---- the correction ------------------------------------------------------------------

BOXES = Boxes(
    [
        Box("Calling All Angels", "", "FDC", (("s-sol-fdc", 1), ("s-rift-fdc", 2), ("s-forest-fdc", 10))),
        Box("Tramplesaurus Rex", "", "FDC", (("s-sol-fdc", 1),)),
    ]
)


def test_a_copy_recorded_with_the_box_printings_art_is_read_as_it(cat):
    holdings = [
        Holding("Sol Ring", 3, "s-sol-c21", "C21", "263"),  # the boxes hold 2: the row is split
        Holding("Sol Ring", 1, "s-sol-lea", "LEA", "270"),  # other art
        Holding("Cyclonic Rift", 1, "s-rift-fdc", "FDC", "100"),  # the box's own
        Holding("Cyclonic Rift", 1, "s-rift-2x2", "2X2", "45"),  # no art recorded: left as it is
        Holding("Forest", 2, "s-forest-m3c", "M3C", "300"),  # a basic land
        Holding("Sol Ring", 1, "s-sol-c21", source="arena"),
    ]
    resolve_holdings(holdings, cat)
    fixed = precons.correct(holdings, BOXES, cat)
    assert [(h.quantity, h.scryfall_id, h.set_code, h.collector_number) for h in fixed.holdings] == [
        (2, "s-sol-fdc", "FDC", "286"),
        (1, "s-sol-c21", "C21", "263"),
        (1, "s-sol-lea", "LEA", "270"),
        (1, "s-rift-fdc", "FDC", "100"),
        (1, "s-rift-2x2", "2X2", "45"),
        (2, "s-forest-m3c", "M3C", "300"),
        (1, "s-sol-c21", None, None),
    ]
    assert sum(h.quantity for h in fixed.holdings) == sum(h.quantity for h in holdings)
    assert fixed.fixes == [
        Fix("Sol Ring", "C21 263", "FDC 286", 2, ("Calling All Angels", "Tramplesaurus Rex"))
    ]
    assert fixed.short == [Short("Cyclonic Rift", "FDC 100", 1, ("Calling All Angels",), ("2X2 45",))]
    assert fixed.printings == 2


def test_a_box_copy_the_collection_lacks_is_short_with_nowhere_else(cat):
    fixed = precons.correct([], BOXES, cat)
    assert fixed.holdings == [] and fixed.fixes == []
    assert [(s.name, s.copies, s.elsewhere) for s in fixed.short] == [
        ("Cyclonic Rift", 2, ()),
        ("Sol Ring", 2, ()),
    ]


# ---- the commands --------------------------------------------------------------------

NOTE = (
    "---\ngame: mtg\nformat: commander\nsource: precon:Calling All Angels\n---\n\n"
    "## Moxfield import\n\n```\n1 Sol Ring\n1 Cyclonic Rift\n```\n"
)
COLLECTION = (
    HEADER + "Sol Ring,C21,263,normal,1,s-sol-c21\n"
    "Cyclonic Rift,FDC,100,normal,1,s-rift-fdc\n"
    "Cyclonic Rift,2X2,45,normal,1,s-rift-2x2\n"
)


@pytest.fixture
def home(tmp_path, monkeypatch, opened):
    """A vault under a fake HOME with a note registering a box, the box's deck files kept, a
    collection, the in-memory catalog and a price snapshot kept."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / ".local" / "share"))
    (tmp_path / ".config" / "riffle").mkdir(parents=True)
    (tmp_path / ".config" / "riffle" / "config.toml").write_text('notes = "games/tcg"\ncard_notes = false\n')
    mtg = tmp_path / "atelier" / "library" / "games" / "tcg" / "mtg"
    (mtg / "commander").mkdir(parents=True)
    (mtg / "commander" / "angels.md").write_text(NOTE)
    (tmp_path / "Downloads").mkdir()
    data_dir().mkdir(parents=True)
    (data_dir() / "collection.csv").write_text(COLLECTION)
    keep()
    monkeypatch.setattr(scryfall, "snapshot_prices", lambda: (tmp_path / "2026-09-26.jsonl.gz", False))
    return mtg


def run(*args):
    return CliRunner().invoke(app, list(args))


def test_a_sync_reads_each_misread_copy_as_its_box_printing(home):
    result = run("sync", "--offline")
    assert result.exit_code == 0, result.output
    assert (
        "precon boxes: 1 registered; 1 copy read as a box's printing; "
        "1 copy the boxes hold not recorded as theirs (riffle check collection)"
    ) in result.output
    assert "1 Sol Ring (FDC) 286" in (home / "_generated" / "angels-data.md").read_text()
    assert (data_dir() / "collection.csv").read_text() == COLLECTION  # the export as it was


def test_check_collection_lists_what_it_corrected_and_what_it_couldnt(home):
    result = run("check", "collection")
    assert result.exit_code == 0, result.output
    assert result.output.splitlines() == [
        "collection: 3 copies in ~/.local/share/riffle/collection.csv",
        "precon boxes: 1, 2 printings, basic lands aside",
        "read as their box's printing, recorded under another with the same art: 1",
        "  Sol Ring: C21 263 → FDC 286, 1 (Calling All Angels)",
        "not found as their box's printing: 1",
        "  Cyclonic Rift: FDC 100, 1 (Calling All Angels); recorded as 2X2 45, other art",
    ]


def test_check_collection_fails_on_a_box_it_cant_find(home):
    (home / "commander" / "rex.md").write_text("---\ngame: mtg\nsource: precon:Rex\n---\n")
    (home / "commander" / "angels.md").unlink()
    result = run("check", "collection")
    assert result.exit_code == 1
    assert result.output.splitlines()[1:] == [
        """  ! precon box "Rex" in rex.md isn't among MTGJSON's decks: write the name MTGJSON gives it""",
        "1 precon box can't be read",
    ]


def test_check_collection_says_how_to_register_a_box(home):
    (home / "commander" / "angels.md").unlink()
    result = run("check", "collection")
    assert result.exit_code == 0, result.output
    assert result.output.splitlines()[1:] == [
        "precon boxes: none registered (a deck note's source: precon:<box> registers one)"
    ]


def test_check_collection_without_a_collection_says_how_to_get_one(home):
    (data_dir() / "collection.csv").unlink()
    result = run("check", "collection")
    assert result.exit_code == 1
    assert "no collection yet — export from ManaBox to ~/Downloads" in result.output


def test_a_sync_leaves_a_csv_that_isnt_an_export_in_downloads(home, plain):
    (data_dir() / "collection.csv").unlink()
    export, fixes = (
        home.parents[4] / "Downloads" / "ManaBox_Collection 5.csv",
        home.parents[4] / "Downloads" / "ManaBox_Collection-5-fixes.csv",
    )
    export.write_text(COLLECTION)
    fixes.write_text(FIXES)
    os.utime(export, (1_000, 1_000))
    result = run("sync", "--offline")
    assert result.exit_code == 0, result.output
    assert (
        "collection: not picked up: ManaBox_Collection-5-fixes.csv in Downloads isn't a ManaBox export "
        "(no Name column)"
    ) in result.output
    assert "picked up ManaBox_Collection 5.csv from Downloads" in result.output
    assert (data_dir() / "collection.csv").read_text() == COLLECTION
    again = run("sync", "--offline")  # the fixes are newer still than the collection kept
    assert (
        "not picked up: ManaBox_Collection-5-fixes.csv" in again.output
        and "picked up Mana" not in again.output
    )


def test_ingest_refuses_a_csv_that_isnt_an_export(home, plain):
    fixes = home.parents[4] / "Downloads" / "ManaBox_Collection-5-fixes.csv"
    fixes.write_text(FIXES)
    result = run("ingest", "manabox", str(fixes))
    assert result.exit_code == 2
    assert "ManaBox_Collection-5-fixes.csv isn't a ManaBox export: no Name column" in plain(result.output)
    assert (data_dir() / "collection.csv").read_text() == COLLECTION


def test_a_collection_that_isnt_an_export_writes_no_notes(home):
    (data_dir() / "collection.csv").write_text(FIXES)
    result = run("sync", "--offline")
    assert result.exit_code == 1
    assert (
        "vault: not written: ~/.local/share/riffle/collection.csv isn't a ManaBox export (no Name column); "
        "riffle ingest manabox <export>"
    ) in result.output
    assert not (home / "_generated").exists()
    own = run("own", str(home / "commander" / "angels.md"))
    assert own.exit_code == 1
    assert (
        "collection.csv isn't a ManaBox export: no Name column; export the collection from ManaBox again"
        in own.output
    )


def test_the_syncs_line_leaves_out_what_isnt_missing():
    from riffle import cli

    fixed = precons.Corrected([], 2, [Fix("Sol Ring", "C21 263", "FDC 286", 2, ("Calling All Angels",))])
    assert cli._corrected(fixed, 1) == "1 registered; 2 copies read as a box's printing"


def test_a_sync_names_a_box_it_cant_find_and_corrects_by_the_rest(home):
    (home / "commander" / "rex.md").write_text("---\ngame: mtg\nsource: precon:Rex\n---\n")
    result = run("sync", "--offline")
    assert result.exit_code == 0, result.output
    assert (
        """precon boxes: precon box "Rex" in rex.md isn't among MTGJSON's decks: """
        "write the name MTGJSON gives it"
    ) in result.output
    assert "precon boxes: 1 registered; 1 copy read as a box's printing" in result.output
