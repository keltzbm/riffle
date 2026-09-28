"""The vault is authored by hand in Obsidian: frontmatter and layout vary."""

import pytest

from riffle import vault


@pytest.mark.parametrize(
    "text, expected",
    [
        ("---\ngame: mtg\n---\n", {"game": "mtg"}),
        ("---\r\ngame: mtg\r\ntags: [a, b]\r\n---\r\n", {"game": "mtg", "tags": ["a", "b"]}),
        ('---\ngame: "mtg"\n---\n', {"game": "mtg"}),
        ("---\ntitle: 'a # b'\nx: y # c\n---\n", {"title": "a # b", "x": "y"}),
        ("---\nsource: https://moxfield.com/decks/abc\n---\n", {"source": "https://moxfield.com/decks/abc"}),
        ("---\ncommander: Y'shtola, Night's Blessed\n---\n", {"commander": "Y'shtola, Night's Blessed"}),
        ("---\ntags:\n  - mtg\n  - 'commander'\n---\n", {"tags": ["mtg", "commander"]}),
        ("---\nempty:\n---\n", {"empty": []}),
        ("---\ncolors: []\n---\n", {"colors": []}),
        ("---\n---\n", {}),
        ("no frontmatter\n", {}),
        ("---\ngame: mtg\nnever closed\n", {}),
        ("---\n  indented: ignored\ngame: mtg\n---\n", {"game": "mtg"}),
    ],
)
def test_frontmatter(text, expected):
    assert vault.frontmatter(text) == expected


def _note(body, game="mtg"):
    return f"---\ngame: {game}\n---\n\n{body}"


def test_heading_containing_important_is_not_an_import_heading():
    body = (
        "## Important interactions\n\n```\n1 Combo Piece\n```\n\n"
        "## Moxfield import\n\n```\nDeck\n1 Sol Ring\n```\n"
    )
    assert vault.decklist_block(_note(body)).strip() == "Deck\n1 Sol Ring"


@pytest.mark.parametrize(
    "heading", ["## Moxfield import", "### Moxfield Import", "# IMPORT", "## Arena import list"]
)
def test_import_heading_variants(heading):
    body = f"```\nprose, not cards\n```\n\n{heading}\n\n```text\n1 Sol Ring\n```\n"
    assert vault.decklist_block(_note(body)).strip() == "1 Sol Ring"


def test_without_import_heading_the_first_mostly_card_block_wins():
    body = "```python\nprint('hi')\n```\n\n```\n1 Sol Ring\n2 Forest\nnote: fine\n```\n"
    assert vault.decklist_block(_note(body)).strip().startswith("1 Sol Ring")


def test_no_block_means_no_deck(tmp_path):
    p = tmp_path / "x.md"
    p.write_text(_note("Just prose.\n"))
    assert vault.read_deck(p) is None


def test_other_games_are_not_mtg_decks(tmp_path):
    p = tmp_path / "x.md"
    p.write_text(_note("## Moxfield import\n\n```\n1 Sol Ring\n```\n", game="fab"))
    assert vault.read_deck(p) is None


def test_meta_carries_frontmatter_and_path(tmp_path):
    p = tmp_path / "aesi-lands.md"
    p.write_text("---\ngame: mtg\nformat: modern\n---\n\n## Moxfield import\n\n```\n1 Sol Ring\n```\n")
    d = vault.read_deck(p)
    assert d.format == "modern" and d.meta["path"] == str(p) and d.slug == "aesi-lands"


def test_format_defaults_to_commander(tmp_path):
    p = tmp_path / "d.md"
    p.write_text(_note("## Moxfield import\n\n```\n1 Sol Ring\n```\n"))
    assert vault.read_deck(p).format == "commander"


def test_find_by_txt_path_md_path_and_slug(tmp_path):
    mtg = tmp_path / "tcg" / "mtg" / "modern"
    mtg.mkdir(parents=True)
    note = mtg / "murktide.md"
    note.write_text(_note("## Moxfield import\n\n```\n4 Murktide Regent\n```\n"))
    txt = tmp_path / "list.txt"
    txt.write_text("4 Bolt\n")
    root = tmp_path / "tcg" / "mtg"
    assert vault.find(root, "murktide").count() == 4
    assert vault.find(root, str(note)).count() == 4
    assert vault.find(root, str(txt)).entries[0].name == "Bolt"


def test_find_errors_are_specific(tmp_path):
    bad = tmp_path / "bad.md"
    bad.write_text("no frontmatter")
    with pytest.raises(ValueError, match="no game: mtg"):
        vault.find(tmp_path, str(bad))
    with pytest.raises(LookupError, match="no deck note named 'nope'"):
        vault.find(tmp_path, "nope")


def test_skip_dirs_only_apply_to_folders_not_file_names(tmp_path):
    mtg = tmp_path / "mtg"
    (mtg / "commander").mkdir(parents=True)
    (mtg / "commander" / "_log.md").write_text("x")
    (mtg / "_log").mkdir()
    (mtg / "_log" / "prices.md").write_text("x")
    names = [p.name for p in vault.deck_notes(mtg)]
    assert names == ["_log.md"]


@pytest.mark.parametrize(
    "line, found",
    [
        ("- [ ] [[Sol Ring]] #mtg/buy", ["Sol Ring"]),
        ("- [ ] 🟢 **[[Sol Ring|the ring]]** · ~$1 #mtg/buy", ["Sol Ring"]),
        ("- [ ] [[Sol Ring#Rulings]] #mtg/buy", ["Sol Ring"]),
        ("  - [ ] [[Sol Ring]] #mtg/buy", ["Sol Ring"]),
        ("- [x] [[Sol Ring]] #mtg/buy", []),
        ("- [ ] [[Sol Ring]] #mtg/buyer", []),
        ("- [ ] [[Sol Ring]]", []),
        ("[[Sol Ring]] #mtg/buy", []),
    ],
)
def test_buy_line_shapes(tmp_path, line, found):
    (tmp_path / "n.md").write_text(line + "\n")
    assert vault.buy_cards(tmp_path) == found


def test_buy_cards_skip_machine_zones_and_dedupe(tmp_path):
    for sub in ("mtg/_generated", "mtg/_log", "mtg/commander", "fab"):
        (tmp_path / sub).mkdir(parents=True, exist_ok=True)
        (tmp_path / sub / "n.md").write_text(
            "- [ ] [[Sol Ring]] #mtg/buy\n- [ ] [[Cyclonic Rift]] #mtg/buy\n"
        )
    assert sorted(vault.buy_cards(tmp_path)) == ["Cyclonic Rift", "Sol Ring"]


def test_an_unreadable_note_raises_unless_the_caller_collects_it(tmp_path):
    mtg = tmp_path / "tcg" / "mtg"
    mtg.mkdir(parents=True)
    (mtg / "bad.md").write_bytes(b"---\ngame: mtg\n---\ncaf\xe9\n")
    with pytest.raises(UnicodeDecodeError):
        vault.decks(mtg)
    with pytest.raises(UnicodeDecodeError):
        vault.buy_cards(mtg.parent)
    unreadable: vault.Unreadable = []
    assert vault.decks(mtg, unreadable) == [] and vault.buy_cards(mtg.parent, unreadable) == []
    assert unreadable == [(mtg / "bad.md", "not UTF-8")] * 2
