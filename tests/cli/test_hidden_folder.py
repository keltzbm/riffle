"""The hidden folder: a deck under mtg/.data/ keeps its data note and version log there."""

import re
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from riffle import config, sync, vault
from riffle.cli import app
from riffle.cli.sync import _some
from riffle.export import obsidian
from riffle.ingest.remote import scryfall

IN_VIEW = ("Aesi, Tyrant of Gyre Strait", "Sol Ring", "Andúril, Flame of the West")
POOL = ("Fire // Ice", "Cyclonic Rift", "Andúril, Flame of the West")


def _note(*cards: str) -> str:
    lines = "\n".join(f"1 {c}" for c in cards)
    return f"---\ngame: mtg\nformat: commander\n---\n\n## Moxfield import\n\n```\n{lines}\n```\n"


def _vault(tmp_path: Path) -> Path:
    mtg = tmp_path / "library" / "tcg" / "mtg"
    (mtg / "commander").mkdir(parents=True)
    (mtg / "commander" / "aesi-lands.md").write_text(_note(*IN_VIEW))
    return mtg


def _files(folder: Path) -> dict[str, str]:
    return {p.name: p.read_text() for p in folder.iterdir() if p.is_file()} if folder.is_dir() else {}


def test_a_list_under_the_hidden_folder_is_found_and_its_notes_are_written_there(tmp_path, cat):
    """A list under .data/commander/ is found by decks() and find(); its data note is written under
    .data/_generated/ and its log under .data/_log/. The collection summary stays in mtg_dir/_generated/."""
    mtg = _vault(tmp_path)
    pool = mtg / ".data" / "commander" / "pool-fire.md"
    pool.parent.mkdir(parents=True)
    pool.write_text(_note(*POOL))
    assert [d.slug for d in vault.decks(mtg)] == ["pool-fire", "aesi-lands"]
    assert vault.find(mtg, "pool-fire").meta["path"] == str(pool)
    res = sync.run(mtg, sync.Inventory([]), cat, today="2026-09-21")
    assert (res.decks, res.moved, res.removed) == (["pool-fire", "aesi-lands"], [], [])
    assert set(_files(mtg / ".data" / "_generated")) == {"pool-fire-data.md"}
    assert set(_files(mtg / ".data" / "_log")) == {"pool-fire-versions.md"}
    assert set(_files(mtg / "_generated")) == {"aesi-lands-data.md", "collection-summary.md"}
    assert set(_files(mtg / "_log")) == {"aesi-lands-versions.md"}


def test_a_deck_moved_into_the_hidden_folder_and_back_takes_its_log_and_loses_its_old_data_note(
    tmp_path, cat
):
    """A deck moved from commander/ to .data/commander/ between two syncs: its old data note is
    pruned from _generated/, and its log moves with its earlier entries intact; and the same the
    other way. Before the vault moves anything, nothing changes: no folder is made under .data/."""
    mtg = _vault(tmp_path)
    in_view, hidden = mtg / "commander" / "aesi-lands.md", mtg / ".data" / "commander" / "aesi-lands.md"
    first = sync.run(mtg, sync.Inventory([]), cat, today="2026-09-21")
    assert first.versions == ["aesi-lands"] and not (mtg / ".data").exists()
    hidden.parent.mkdir(parents=True)
    in_view.rename(hidden)
    hidden.write_text(hidden.read_text().replace("1 Sol Ring\n", "1 Fire // Ice\n"))
    res = sync.run(mtg, sync.Inventory([]), cat, today="2026-09-22")
    assert (res.decks, res.versions, res.moved) == (
        ["aesi-lands"],
        ["aesi-lands"],
        ["aesi-lands-versions.md"],
    )
    assert res.removed == ["aesi-lands-data.md"]
    assert not (mtg / "_generated" / "aesi-lands-data.md").exists()
    assert (mtg / ".data" / "_generated" / "aesi-lands-data.md").exists()
    assert not (mtg / "_log" / "aesi-lands-versions.md").exists()
    log = (mtg / ".data" / "_log" / "aesi-lands-versions.md").read_text()
    assert "\n## 2026-09-21\n\nBaseline: 3 cards.\n" in log
    assert log.endswith("\n## 2026-09-22\n\n- \\+ 1 [[Fire|Fire // Ice]]\n- \\- 1 [[Sol Ring]]\n")
    hidden.rename(in_view)
    back = sync.run(mtg, sync.Inventory([]), cat, today="2026-09-23")
    assert (back.versions, back.moved) == ([], ["aesi-lands-versions.md"])
    assert back.removed == [".data/_generated/aesi-lands-data.md"]
    assert (mtg / "_generated" / "aesi-lands-data.md").exists()
    assert not (mtg / ".data" / "_generated" / "aesi-lands-data.md").exists()
    assert (mtg / "_log" / "aesi-lands-versions.md").read_text() == log
    assert not (mtg / ".data" / "_log" / "aesi-lands-versions.md").exists()


def test_the_card_notes_fall_to_the_cards_linked_from_notes_in_view(tmp_path, cat):
    """linking() leaves the hidden folder out: a card linked only from a hidden list gets no card
    note, and one also linked from a note in view keeps it; the count says how many were removed."""
    mtg = _vault(tmp_path)
    (mtg / "commander" / "pool-fire.md").write_text(_note(*POOL) + "\n- [ ] [[Fire // Ice]] #mtg/buy\n")
    (mtg / "ideas.md").write_text("Try [[Cyclonic Rift]] in the pool deck.\n")
    how = sync.CardNotes(mtg.parent.parent, mtg.parent, "link")
    folder = mtg / "_generated" / "cards"
    sync.run(mtg, sync.Inventory([]), cat, today="2026-09-21", card_notes=how)
    assert sorted(p.name for p in folder.iterdir()) == [
        "Aesi, Tyrant of Gyre Strait.md",
        "Andúril, Flame of the West.md",
        "Cyclonic Rift.md",
        "Fire.md",
        "Sol Ring.md",
    ]
    (mtg / ".data" / "commander").mkdir(parents=True)
    (mtg / "commander" / "pool-fire.md").rename(mtg / ".data" / "commander" / "pool-fire.md")
    res = sync.run(mtg, sync.Inventory([]), cat, today="2026-09-22", card_notes=how)
    assert (res.card_notes.cards, res.card_notes.removed) == (4, 1)
    assert sorted(p.name for p in folder.iterdir()) == [
        "Aesi, Tyrant of Gyre Strait.md",
        "Andúril, Flame of the West.md",
        "Cyclonic Rift.md",
        "Sol Ring.md",
    ]


def test_linking_leaves_out_every_dot_folder_at_any_depth_and_riffle_s_notes_stay_in_view(tmp_path):
    """linking() leaves out every folder whose name starts with a dot, at any depth, besides
    _generated/, _log/ and conflict copies: Obsidian indexes none. riffle_notes() doesn't change."""
    notes = tmp_path / "tcg"
    for rel in [
        ".obsidian/workspace.md",
        "mtg/.data/commander/pool.md",
        "mtg/.data/_generated/pool-data.md",
        "mtg/.data/_log/pool-versions.md",
        "mtg/_generated/aesi-data.md",
        "mtg/_log/aesi-versions.md",
        "mtg/commander/.trash/old.md",
        "mtg/commander/aesi.md",
        "one-piece/zoro.md",
    ]:
        (notes / rel).parent.mkdir(parents=True, exist_ok=True)
        (notes / rel).write_text("")
    assert [p.relative_to(notes).as_posix() for p in vault.linking(notes)] == [
        "mtg/commander/aesi.md",
        "one-piece/zoro.md",
    ]
    assert [p.relative_to(notes).as_posix() for p in vault.riffle_notes(notes / "mtg")] == [
        "mtg/_generated/aesi-data.md",
        "mtg/_log/aesi-versions.md",
    ]


def test_conflicts_finds_a_conflict_copy_under_the_hidden_log_folder(tmp_path):
    """conflicts(mtg_dir, hidden) also lists the conflict copies in mtg_dir/<hidden>/_log/."""
    mtg = tmp_path / "mtg"
    for rel in [
        "_log/aesi-lands-versions [conflicted].md",
        "_log/aesi-lands-versions.md",
        ".pool/_log/pool-versions.sync-conflict-20261002-061151-ABCDEFG.md",
        ".pool/_log/pool-versions.md",
        ".pool/commander/pool (conflicted copy 2026-10-02 061151).md",
    ]:
        (mtg / rel).parent.mkdir(parents=True, exist_ok=True)
        (mtg / rel).write_text("")
    assert vault.conflicts(mtg, hidden=".pool") == [
        mtg / ".pool" / "_log" / "pool-versions.sync-conflict-20261002-061151-ABCDEFG.md",
        mtg / ".pool" / "commander" / "pool (conflicted copy 2026-10-02 061151).md",
        mtg / "_log" / "aesi-lands-versions [conflicted].md",
    ]


def test_doubled_logs_names_every_log_in_both_folders_sorted_even_when_its_deck_is_gone(tmp_path):
    """doubled_logs: the sorted file names of every *-versions.md in both mtg/_log/ and
    mtg/.data/_log/, whether or not its deck note still exists; [] when either folder is missing."""
    mtg = tmp_path / "mtg"
    (mtg / "commander").mkdir(parents=True)
    (mtg / "commander" / "zz-top.md").write_text(_note(*IN_VIEW))
    log, hidden = mtg / "_log", mtg / ".data" / "_log"
    hidden.mkdir(parents=True)
    for name in ("zz-top-versions.md", "gone-versions.md", "only-there-versions.md", "prices.md"):
        (hidden / name).write_text("")
    assert vault.doubled_logs(mtg) == []
    log.mkdir()
    for name in ("zz-top-versions.md", "gone-versions.md", "only-here-versions.md", "prices.md"):
        (log / name).write_text("")
    assert vault.doubled_logs(mtg) == ["gone-versions.md", "zz-top-versions.md"]
    shutil.rmtree(hidden)
    assert vault.doubled_logs(mtg) == []


@pytest.mark.parametrize(
    ("here", "there", "moved", "after"),
    [
        ({}, {"aesi-lands-versions.md": "history\n"}, True, {"aesi-lands-versions.md": "history\n"}),
        ({}, {}, False, {}),
        (
            {"aesi-lands-versions.md": "here\n"},
            {"aesi-lands-versions.md": "there\n"},
            False,
            {"aesi-lands-versions.md": "here\n"},
        ),
        ({"aesi-lands.versions.md": "dotted\n"}, {}, False, {"aesi-lands-versions.md": "dotted\n"}),
        ({}, {"aesi-lands.versions.md": "dotted\n"}, True, {"aesi-lands-versions.md": "dotted\n"}),
    ],
    ids=["moved", "nothing to move", "both present", "the dotted legacy name", "a dotted log elsewhere"],
)
def test_move_log_brings_a_log_from_the_other_folder_when_its_own_has_none(
    tmp_path, here, there, moved, after
):
    """move_log renames the dotted name 0.2 wrote, then moves <slug>-versions.md from elsewhere into
    log_dir when log_dir has none, making log_dir if it isn't there: True when a log came from
    elsewhere; when both folders have one, nothing moves and it returns False."""
    log_dir, elsewhere = tmp_path / ".data" / "_log", tmp_path / "_log"
    for folder, files in ((log_dir, here), (elsewhere, there)):
        if files:
            folder.mkdir(parents=True)
        for name, text in files.items():
            (folder / name).write_text(text)
    assert obsidian.move_log(log_dir, elsewhere, "aesi-lands") is moved
    assert _files(log_dir) == after
    assert _files(elsewhere) == ({} if moved else there)
    # a folder with nothing to give is never made: before the vault moves anything, nothing changes
    assert there or not elsewhere.exists()


def test_a_log_in_both_folders_is_left_as_it_is_and_the_deck_logs_into_its_own(tmp_path, cat):
    """A log in both folders: both kept, the warning, the deck's own folder written; a doubled log
    is named even when its deck is gone, more than one joined by a comma."""
    mtg = _vault(tmp_path)
    hidden = mtg / ".data"
    (hidden / "commander").mkdir(parents=True)
    (mtg / "commander" / "aesi-lands.md").rename(hidden / "commander" / "aesi-lands.md")
    for folder, text in ((mtg / "_log", "in view\n"), (hidden / "_log", "hidden\n")):
        folder.mkdir()
        (folder / "aesi-lands-versions.md").write_text(text)
        (folder / "gone-versions.md").write_text(text)
    res = sync.run(mtg, sync.Inventory([]), cat, today="2026-09-21")
    assert (res.versions, res.moved) == (["aesi-lands"], [])
    assert res.warnings == [
        "version logs in both mtg/_log/ and mtg/.data/_log/: each deck logs into the one beside its "
        "list, and the other is left as it is; merge each pair by hand: "
        "aesi-lands-versions.md, gone-versions.md"
    ]
    assert _files(mtg / "_log") == {"aesi-lands-versions.md": "in view\n", "gone-versions.md": "in view\n"}
    assert _files(hidden / "_log") == {
        "aesi-lands-versions.md": "hidden\n\n## 2026-09-21\n\nBaseline: 3 cards.\n",
        "gone-versions.md": "hidden\n",
    }


def test_an_unreadable_note_keeps_its_data_note_in_the_folder_the_note_is_in(tmp_path, cat):
    """A note that can't be read keeps its data note as it was, in the _generated/ of the folder
    the note is in (in view or hidden)."""
    mtg = _vault(tmp_path)
    pool = mtg / ".data" / "commander" / "pool-fire.md"
    pool.parent.mkdir(parents=True)
    pool.write_text(_note(*POOL))
    sync.run(mtg, sync.Inventory([]), cat, today="2026-09-21")
    for note in (mtg / "commander" / "aesi-lands.md", pool):
        # saved as Latin-1
        note.write_bytes(note.read_bytes() + b"caf\xe9\n")
    res = sync.run(mtg, sync.Inventory([]), cat, today="2026-09-22")
    assert (res.decks, res.removed) == ([], [])
    assert (mtg / "_generated" / "aesi-lands-data.md").exists()
    assert (mtg / ".data" / "_generated" / "pool-fire-data.md").exists()
    assert res.failed == [
        (
            "vault notes",
            "skipped, can't read mtg/.data/commander/pool-fire.md (not UTF-8), "
            "mtg/commander/aesi-lands.md (not UTF-8)",
        )
    ]


def test_a_list_moved_while_unreadable_keeps_its_data_note_until_it_reads(tmp_path, cat):
    """A note that can't be read keeps its data note as it was: a list moved into the hidden folder
    and unreadable keeps the one in view, until it reads and its notes move beside it."""
    mtg = _vault(tmp_path)
    sync.run(mtg, sync.Inventory([]), cat, today="2026-09-21")
    hidden = mtg / ".data" / "commander" / "aesi-lands.md"
    hidden.parent.mkdir(parents=True)
    (mtg / "commander" / "aesi-lands.md").rename(hidden)
    text = hidden.read_bytes()
    hidden.write_bytes(text + b"caf\xe9\n")
    res = sync.run(mtg, sync.Inventory([]), cat, today="2026-09-22")
    assert (res.decks, res.moved, res.removed) == ([], [], [])
    assert (mtg / "_generated" / "aesi-lands-data.md").exists()
    hidden.write_bytes(text)
    res = sync.run(mtg, sync.Inventory([]), cat, today="2026-09-23")
    assert (res.moved, res.removed) == (["aesi-lands-versions.md"], ["aesi-lands-data.md"])
    assert (mtg / ".data" / "_generated" / "aesi-lands-data.md").exists()


def test_two_notes_with_one_name_leave_their_logs_where_they_are_and_are_named(tmp_path, cat):
    """Two deck notes with one name, one in view and one hidden, would take one log back and forth
    on every sync: neither moves it, and a warning names both notes."""
    mtg = _vault(tmp_path)
    sync.run(mtg, sync.Inventory([]), cat, today="2026-09-21")
    history = (mtg / "_log" / "aesi-lands-versions.md").read_text()
    copy = mtg / ".data" / "commander" / "aesi-lands.md"
    copy.parent.mkdir(parents=True)
    copy.write_text(_note(*POOL))
    for day in ("2026-09-22", "2026-09-23"):
        res = sync.run(mtg, sync.Inventory([]), cat, today=day)
        assert res.moved == []
        assert res.warnings[-1] == (
            "deck notes sharing a name, whose version logs mix and aren't moved; rename one of each: "
            "aesi-lands (mtg/.data/commander/aesi-lands.md, mtg/commander/aesi-lands.md)"
        )
    assert (mtg / "_log" / "aesi-lands-versions.md").read_text().startswith(history)


def test_the_sync_reads_conflicts_and_doubled_logs_in_the_folder_it_is_given(tmp_path, cat):
    """conflicts(mtg_dir, hidden) and doubled_logs(mtg_dir, hidden) read the hidden folder sync.run is
    given, not only .data."""
    mtg = _vault(tmp_path)
    logs = mtg / ".pool" / "_log"
    logs.mkdir(parents=True)
    (mtg / "_log").mkdir()
    (logs / "gone-versions.sync-conflict-20261002-061151-ABCDEFG.md").write_text("x")
    for folder in (mtg / "_log", logs):
        (folder / "gone-versions.md").write_text("x")
    res = sync.run(mtg, sync.Inventory([]), cat, today="2026-09-21", hidden=".pool")
    assert res.warnings == [
        "sync-conflict copies, not read; merge each by hand: "
        "mtg/.pool/_log/gone-versions.sync-conflict-20261002-061151-ABCDEFG.md",
        "version logs in both mtg/_log/ and mtg/.pool/_log/: each deck logs into the one beside its "
        "list, and the other is left as it is; merge each pair by hand: gone-versions.md",
    ]


def test_ten_names_are_shown_whole_and_eleven_are_cut_to_ten():
    """Each of those two lines shows at most 10 names: past 10, the first 10 and then and N more."""
    names = [f"n{i}" for i in range(11)]
    assert _some(names[:10]) == ", ".join(names[:10])
    assert _some(names) == ", ".join(names[:10]) + " and 1 more"


def test_the_hidden_folder_setting_is_dot_data_unless_the_config_names_another(tmp_path, monkeypatch):
    """config.HIDDEN_FOLDER is ".data"; load() returns a Config whose hidden_folder is ".data" when
    the config names none; the config riffle init writes has a hidden_folder = ".data" line."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert config.HIDDEN_FOLDER == ".data"
    assert config.load().hidden_folder == ".data"
    path = config.write_default()
    assert 'hidden_folder = ".data"' in path.read_text()
    cfg = config.load()
    assert (cfg.hidden_folder, cfg.unread) == (".data", [])
    path.write_text('hidden_folder = ".pool"\n')
    cfg = config.load()
    assert (cfg.hidden_folder, cfg.unread) == (".pool", [])


@pytest.mark.parametrize(
    ("value", "why"),
    [
        ('"data"', 'is "data", which doesn\'t start with a dot, so Obsidian would index it'),
        ('""', 'is "", not a folder\'s name'),
        ('"."', 'is ".", not a folder\'s name'),
        ('".."', 'is "..", not a folder\'s name'),
        ('".a/b"', 'is ".a/b", not a folder\'s name'),
        ("5", "is 5, not a folder's name"),
    ],
)
def test_a_hidden_folder_that_cant_be_used_is_named_and_dot_data_used(tmp_path, monkeypatch, value, why):
    """A hidden_folder that can't be used adds one line to Config.unread, and .data is used: a string
    that doesn't start with a dot, or not one folder's name ("", ".", "..", a name with a / in it, or
    a value that isn't a string); a string is shown in double quotes, anything else as Python's repr."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    config.write_default().write_text(f"hidden_folder = {value}\n")
    cfg = config.load()
    assert cfg.hidden_folder == ".data"
    assert cfg.unread == [f'hidden_folder in {config.tilde(config.config_path())} {why}; ".data" is used']


@pytest.fixture
def home(tmp_path, monkeypatch, opened):
    """A one-deck vault under a fake HOME, the in-memory catalog, and a price snapshot kept."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / ".local" / "share"))
    (tmp_path / ".config" / "riffle").mkdir(parents=True)
    monkeypatch.setattr(scryfall, "snapshot_prices", lambda: (Path("/d/2026-09-26.jsonl.gz"), False))
    mtg = tmp_path / "vault" / "games" / "tcg" / "mtg"
    (mtg / "commander").mkdir(parents=True)
    (mtg / "commander" / "aesi-lands.md").write_text(_note(*IN_VIEW))
    return mtg


def _settings(line: str) -> None:
    """The config riffle sync reads, with one more line."""
    config.config_path().write_text(f'vault = "~/vault"\nnotes = "games/tcg"\ncard_images = "link"\n{line}\n')


def test_the_sync_names_the_logs_moved_and_the_notes_removed_ten_at_most(home):
    """riffle sync passes the config's hidden_folder to sync.run, and prints, after the versions
    changed line, moved version logs beside their lists: <names>, then removed stale generated
    notes: <names>; each shows at most 10 names: past 10, the first 10 and then and N more, N with
    a thousands comma."""
    _settings('hidden_folder = ".pool"')
    pool, log, gen = home / ".pool" / "commander", home / "_log", home / "_generated"
    for folder in (pool, log, gen):
        folder.mkdir(parents=True)
    for n in range(11):
        (pool / f"pool-{n:02}.md").write_text(_note(*POOL))
        (log / f"pool-{n:02}-versions.md").write_text(f"history {n}\n")
    for n in range(1011):
        (gen / f"stale-{n:04}-data.md").write_text("stale")
    result = CliRunner().invoke(app, ["sync", "--offline"])
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    versions = next(
        i for i, line in enumerate(lines) if line.startswith("12 decks · ") and "versions changed: " in line
    )
    moved = lines.index(
        "moved version logs beside their lists: "
        + ", ".join(f"pool-{n:02}-versions.md" for n in range(10))
        + " and 1 more"
    )
    [removed] = [i for i, line in enumerate(lines) if line.startswith("removed stale generated notes: ")]
    assert versions < moved < removed
    shown = re.fullmatch(
        r"removed stale generated notes: ((?:stale-\d{4}-data\.md, ){9}stale-\d{4}-data\.md) and 1,001 more",
        lines[removed],
    )
    assert shown and len(set(shown.group(1).split(", "))) == 10
    assert set(_files(home / ".pool" / "_generated")) == {f"pool-{n:02}-data.md" for n in range(11)}
    assert _files(home / ".pool" / "_log")["pool-10-versions.md"].startswith("history 10\n")
    assert set(_files(log)) == {"aesi-lands-versions.md"} and not (home / ".data").exists()


def test_a_hidden_folder_without_a_leading_dot_is_named_and_dot_data_used(home, plain):
    """hidden_folder = "data" prints setting: hidden_folder in ~/.config/riffle/config.toml is "data",
    which doesn't start with a dot, so Obsidian would index it; ".data" is used."""
    _settings('hidden_folder = "data"')
    pool = home / ".data" / "commander" / "pool-fire.md"
    pool.parent.mkdir(parents=True)
    pool.write_text(_note(*POOL))
    result = CliRunner().invoke(app, ["sync", "--offline"])
    assert result.exit_code == 0, result.output
    assert (
        'setting: hidden_folder in ~/.config/riffle/config.toml is "data", which doesn\'t start with a '
        'dot, so Obsidian would index it; ".data" is used'
    ) in plain(result.output)
    assert "2 decks · " in result.output
    assert (home / ".data" / "_generated" / "pool-fire-data.md").exists()
    assert not (home / "data").exists()
