"""ManaBox, Arena, and config: the files the tool reads."""

import pytest
from typer.testing import CliRunner

from riffle import config
from riffle.analysis.resolve import counts, resolve_holdings
from riffle.cli import app
from riffle.ingest import arena, manabox
from riffle.models import Holding

MANABOX = (
    "\ufeffName,Set code,Set name,Collector number,Foil,Rarity,Quantity,ManaBox ID,Scryfall ID,Language\n"
    "Sol Ring,m3c,MH3 Commander,283,normal,uncommon,2,1,s-sol-m3c,en\n"
    "Sol Ring,cmr,Commander Legends,472,foil,uncommon,1,2,,en\n"
    "Cyclonic Rift,2x2,Double Masters 2022,45,etched,rare,1,3,,en\n"
    ",,,,,,,,,\n"
    "Forest,m3c,MH3 Commander,300,normal,common,,4,,en\n"
)


def test_manabox_export_shape(tmp_path):
    p = tmp_path / "ManaBox_Collection.csv"
    p.write_text(MANABOX, encoding="utf-8")
    hs = manabox.load(p)
    assert [(h.name, h.quantity, h.set_code, h.collector_number, h.foil) for h in hs] == [
        ("Sol Ring", 2, "M3C", "283", False),
        ("Sol Ring", 1, "CMR", "472", True),
        ("Cyclonic Rift", 1, "2X2", "45", True),
        ("Forest", 1, "M3C", "300", False),  # blank quantity reads as 1
    ]
    assert hs[0].scryfall_id == "s-sol-m3c" and hs[1].scryfall_id is None
    assert all(h.source == "manabox" for h in hs)


def test_manabox_rows_resolve_and_sum_per_card(tmp_path, cat):
    p = tmp_path / "c.csv"
    p.write_text(MANABOX, encoding="utf-8")
    hs = manabox.load(p)
    assert resolve_holdings(hs, cat) == []
    c = counts(hs)
    assert c["o-sol"] == 3 and c["o-rift"] == 1 and c["o-forest"] == 1


def test_unresolved_holdings_are_reported_and_not_counted(cat):
    hs = [Holding("Not A Card", 2), Holding("Sol Ring", 1)]
    assert resolve_holdings(hs, cat) == ["Not A Card"]
    assert dict(counts(hs)) == {"o-sol": 1}


def test_bad_scryfall_id_falls_back_to_set_number_then_name(cat):
    hs = [
        Holding("x", 1, scryfall_id="nope", set_code="2X2", collector_number="45"),
        Holding("Sol Ring", 1, scryfall_id="nope", set_code="XXX", collector_number="1"),
    ]
    assert resolve_holdings(hs, cat) == []
    assert [h.card_id for h in hs] == ["o-rift", "o-sol"]


@pytest.mark.parametrize(
    "header, row",
    [
        ("Name,Count", "Sol Ring,4"),
        ("name,quantity", "Sol Ring,4"),
        ("Card Name,Qty", "Sol Ring,4"),
        (" Card , Owned ", "Sol Ring,4"),
    ],
)
def test_arena_csv_column_names(tmp_path, header, row):
    p = tmp_path / "a.csv"
    p.write_text(f"{header}\n{row}\n")
    assert [(h.name, h.quantity) for h in arena.load(p)] == [("Sol Ring", 4)]


def test_arena_csv_skips_bad_counts_zero_counts_and_blank_names(tmp_path):
    p = tmp_path / "a.csv"
    p.write_text("Name,Count\nSol Ring,4\n,3\nCyclonic Rift,x\nForest,\nIsland,0\n")
    assert [(h.name, h.quantity) for h in arena.load(p)] == [("Sol Ring", 4)]


def test_arena_csv_without_usable_columns_is_an_error(tmp_path):
    p = tmp_path / "a.csv"
    p.write_text("Card,Set\nSol Ring,CMR\n")
    with pytest.raises(ValueError, match="expected columns"):
        arena.load(p)


def test_config_defaults_and_xdg(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    cfg = config.load()
    assert cfg.obsolete is None and cfg.old_notes is None and cfg.vault.name == "library"
    assert config.config_path() == tmp_path / "cfg" / "riffle" / "config.toml"
    assert config.data_dir() == tmp_path / "data" / "riffle"
    assert cfg.notes == cfg.vault / "games" / "tcg"
    assert cfg.mtg_dir == cfg.vault / "games" / "tcg" / "mtg"
    assert cfg.collection_csv == tmp_path / "data" / "riffle" / "collection.csv"


def test_config_file_is_read_and_never_overwritten(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    path = config.write_default()
    path.write_text('vault = "~/elsewhere"\nprecons = ["M3C-tricky-terrain"]\n')
    assert config.write_default() == path
    cfg = config.load()
    assert set(cfg.obsolete) == {"precons"}
    assert "~" not in str(cfg.vault) and cfg.vault.name == "elsewhere"


def test_the_notes_folder_is_read_against_the_vault_unless_absolute(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    path = config.write_default()
    assert 'notes = "games/tcg"' in path.read_text()
    path.write_text(f'vault = "{tmp_path / "v"}"\nnotes = "cards"\n')
    cfg = config.load()
    assert cfg.old_notes is None and cfg.mtg_dir == tmp_path / "v" / "cards" / "mtg"
    path.write_text(f'vault = "{tmp_path / "v"}"\nnotes = "{tmp_path / "away"}"\n')
    assert config.load().notes == tmp_path / "away"


def _old_config(tmp_path, monkeypatch, vault: str) -> config.Config:
    """A config written before the notes setting: vault named the folder holding tcg/."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    config.write_default().write_text(f'vault = "{vault}"\n')
    return config.load()


def test_an_old_config_reads_tcg_in_the_vault_and_says_what_to_set(tmp_path, monkeypatch):
    (tmp_path / "atelier" / "library" / ".obsidian").mkdir(parents=True)
    cfg = _old_config(tmp_path, monkeypatch, "~/atelier/library/games")
    assert cfg.mtg_dir == tmp_path / "atelier" / "library" / "games" / "tcg" / "mtg"
    assert cfg.old_notes == (
        "~/.config/riffle/config.toml names no notes folder, so it's ~/atelier/library/games/tcg: "
        'set vault = "~/atelier/library" and add notes = "games/tcg"'
    )


def test_an_old_config_already_naming_the_vault_only_needs_a_notes_line(tmp_path, monkeypatch):
    (tmp_path / "atelier" / "library" / ".obsidian").mkdir(parents=True)
    cfg = _old_config(tmp_path, monkeypatch, "~/atelier/library")
    assert cfg.mtg_dir == tmp_path / "atelier" / "library" / "tcg" / "mtg"
    assert str(cfg.old_notes).endswith('so it\'s ~/atelier/library/tcg: add notes = "tcg"')


def test_an_old_config_outside_any_vault_keeps_its_folder(tmp_path, monkeypatch):
    elsewhere = tmp_path.parent / f"{tmp_path.name}-elsewhere"  # not under HOME, no .obsidian above it
    cfg = _old_config(tmp_path, monkeypatch, str(elsewhere))
    assert cfg.notes == elsewhere / "tcg"
    assert str(cfg.old_notes).endswith(f'so it\'s {elsewhere / "tcg"}: add notes = "tcg"')


def test_init_shows_the_vault_and_the_notes_folder(tmp_path, monkeypatch):
    (tmp_path / "atelier" / "library" / ".obsidian").mkdir(parents=True)
    _old_config(tmp_path, monkeypatch, "~/atelier/library/games")
    config.config_path().write_text('vault = "~/atelier/library/games"\nprecons = []\n')
    result = CliRunner().invoke(app, ["init"])
    assert result.exit_code == 0, result.output
    assert "  ! `precons` in config is ignored: " in result.output
    assert f"vault      {tmp_path / 'atelier' / 'library' / 'games'}\n" in result.output
    assert f"notes      {tmp_path / 'atelier' / 'library' / 'games' / 'tcg'}\n" in result.output
    assert "  ! ~/.config/riffle/config.toml names no notes folder, so it's " in result.output
    config.config_path().write_text('vault = "~/atelier/library"\nnotes = "games/tcg"\n')
    result = CliRunner().invoke(app, ["init"])
    assert f"notes      {tmp_path / 'atelier' / 'library' / 'games' / 'tcg'}\n" in result.output
    assert "!" not in result.output
