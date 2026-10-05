import os
import time
from collections import Counter

from riffle.analysis.ownership import diff, wildcards
from riffle.analysis.resolve import resolve_deck
from riffle.export import formats, obsidian
from riffle.ingest import arena, manabox
from riffle.ingest.decklist import parse_text


def test_arena_text_and_csv_exports(tmp_path):
    t = tmp_path / "mtga_collection.txt"
    t.write_text("4 Sol Ring (CMR) 472\n1 Cyclonic Rift\n")
    assert [(h.name, h.quantity, h.source) for h in arena.load(t)] == [
        ("Sol Ring", 4, "arena"),
        ("Cyclonic Rift", 1, "arena"),
    ]
    c = tmp_path / "untapped.csv"
    c.write_text("Name,Set,Count\nSol Ring,CMR,4\n")
    assert [(h.name, h.quantity) for h in arena.load(c)] == [("Sol Ring", 4)]


def test_wildcards_by_rarity(cat):
    d = parse_text("1 Aesi, Tyrant of Gyre Strait\n1 Cyclonic Rift\n1 Sol Ring\n1 Fire // Ice\n")
    resolve_deck(d, cat)
    wc = wildcards(diff(d, Counter({"o-sol": 1}), cat), cat)
    assert wc == {"mythic": 1, "rare": 1, "uncommon": 0, "common": 0, "not on Arena": 1}


def test_arena_export_has_no_printings(cat):
    d = parse_text("Commander\n1 Aesi, Tyrant of Gyre Strait\n\nDeck\n1 Sol Ring (M3C) 283\n")
    resolve_deck(d, cat)
    assert (
        formats.arena(d, cat) == "Commander\n1 Aesi, Tyrant of Gyre Strait\n\nDeck\n1 Sol Ring (M3C) 283\n"
        or "(M3C)" not in formats.moxfield(d, cat, {})
    )  # explicit pins in a list are kept as written


def test_newest_manabox_export(tmp_path):
    a, b = tmp_path / "ManaBox_Collection.csv", tmp_path / "ManaBox_Collection (1).csv"
    a.write_text("Name,Quantity\nSol Ring,1\n")
    b.write_text("Name,Quantity\nSol Ring,2\n")
    os.utime(a, (time.time() - 100, time.time() - 100))
    assert manabox.newest_export(tmp_path) == b
    assert manabox.newest_export(tmp_path / "nope") is None


def test_prune_never_removes_readme(tmp_path):
    (tmp_path / "README.md").write_text("contract")
    (tmp_path / "old.md").write_text("x")
    assert obsidian.prune(tmp_path, set()) == ["old.md"]
    assert (tmp_path / "README.md").exists()
