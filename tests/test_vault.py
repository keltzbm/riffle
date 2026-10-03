from pathlib import Path

from riffle import vault

NOTE = """---
game: mtg
format: commander
colors: [U, G]
aliases: [Aesi Lands, Tricky Terrain]   # comment
commander: Aesi, Tyrant of Gyre Strait
tags:
  - mtg
---

# Aesi Lands

```
not a decklist
```

## Moxfield import

```
Commander
1 Aesi, Tyrant of Gyre Strait

Deck
1 Sol Ring
2 Forest
```

- [ ] 🟢 **[[Sol Ring]]** · ~$1 #mtg/buy
- [x] 🟢 **[[Cyclonic Rift]]** · ~$30 #mtg/buy
- [ ] 🟢 **[[buy-list|Buy List]]** is not a card
"""


def test_frontmatter_subset():
    fm = vault.frontmatter(NOTE)
    assert fm["colors"] == ["U", "G"]
    assert fm["aliases"] == ["Aesi Lands", "Tricky Terrain"]
    assert fm["commander"] == "Aesi, Tyrant of Gyre Strait"
    assert fm["tags"] == ["mtg"]


def test_reads_the_import_block_not_the_first_block(tmp_path):
    p = tmp_path / "tcg" / "mtg" / "commander" / "aesi-lands.md"
    p.parent.mkdir(parents=True)
    p.write_text(NOTE)
    d = vault.find(tmp_path / "tcg" / "mtg", "aesi-lands")
    assert d.count() == 4
    assert d.board("commander")[0].name == "Aesi, Tyrant of Gyre Strait"


def test_generated_and_archetype_notes_are_not_decks(tmp_path):
    mtg = tmp_path / "tcg" / "mtg"
    for sub in ("_generated", "archetypes", "commander"):
        (mtg / sub).mkdir(parents=True)
        (mtg / sub / "x.md").write_text(NOTE)
    assert [d.meta["path"].split("/")[-2] for d in vault.decks(mtg)] == ["commander"]


def test_buy_cards_only_unticked_and_only_tagged(tmp_path):
    (tmp_path / "a.md").write_text(NOTE)
    assert vault.buy_cards(tmp_path) == ["Sol Ring"]


def test_stub_with_empty_list_is_not_a_deck(tmp_path):
    p = tmp_path / "stub.md"
    p.write_text("---\ngame: mtg\n---\n\n## Moxfield import\n\n```\n\n```\n")
    assert vault.read_deck(p) is None


def test_the_walk_never_enters_riffle_s_folders(tmp_path, monkeypatch):
    """S4: listing the deck notes walked _generated/ and _log/ too, thousands of files, and
    threw them away; sync --watch did it every 5 seconds."""
    import os

    mtg = tmp_path / "mtg"
    for folder in ("commander", "_generated/cards", "_log", "commander/archetypes"):
        (mtg / folder).mkdir(parents=True)
        (mtg / folder / "x.md").write_text("")
    (mtg / "commander" / "notes.txt").write_text("")
    entered = []
    walk = os.walk

    def recorded(top, *args, **kwargs):
        for folder, dirs, files in walk(top, *args, **kwargs):
            entered.append(os.path.relpath(folder, mtg))
            yield folder, dirs, files

    monkeypatch.setattr(os, "walk", recorded)
    assert vault.deck_notes(mtg) == [mtg / "commander" / "x.md"]
    assert sorted(entered) == [".", "commander"]


def test_conflict_copies_are_told_by_name():
    copies = [
        "aesi-lands [conflicted].md",  # pCloud
        "aesi-lands-data [conflicted 2].md",
        "aesi-lands (Brandon's conflicted copy 2026-10-02).md",  # Dropbox
        "aesi-lands (conflicted copy 2026-10-02 061151).md",  # Nextcloud
        "aesi-lands.sync-conflict-20261002-061151-ABCDEFG.md",  # Syncthing
    ]
    notes = ["Zuko, Conflicted.md", "conflicted-copy.md", "aesi-lands.md", "a [conflicted].txt"]
    assert [vault.is_conflict(Path(n)) for n in copies] == [True] * len(copies)
    assert [vault.is_conflict(Path(n)) for n in notes] == [False] * len(notes)


def test_buy_lines_in_a_conflict_copy_or_riffle_s_folders_are_left_out(tmp_path):
    (tmp_path / "mtg" / "_log").mkdir(parents=True)
    (tmp_path / "buy.md").write_text("- [ ] [[Sol Ring]] #mtg/buy\n")
    (tmp_path / "buy [conflicted].md").write_text("- [ ] [[Mana Crypt]] #mtg/buy\n")
    (tmp_path / "mtg" / "_log" / "x.md").write_text("- [ ] [[Cyclonic Rift]] #mtg/buy\n")
    assert vault.buy_cards(tmp_path) == ["Sol Ring"]


def test_a_vault_without_logs_has_no_conflicts_there(tmp_path):
    (tmp_path / "commander").mkdir()
    (tmp_path / "commander" / "x [conflicted].md").write_text("")
    assert vault.conflicts(tmp_path) == [tmp_path / "commander" / "x [conflicted].md"]
