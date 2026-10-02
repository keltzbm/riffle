"""The deck commands read the catalog through open_catalog, and exit 1 saying what to run without one."""

import pytest
from typer.testing import CliRunner

from riffle.cli import app
from riffle.ingest import scryfall
from riffle.store import postgres

DECK = "4 Lightning Bolt\n1 Sol Ring\n2 Not A Card\n\n1 Cyclonic Rift\n"


@pytest.fixture
def deck(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))  # no real ~/Downloads or vault
    path = tmp_path / "burn.txt"
    path.write_text(DECK)
    return str(path)


def run(*args):
    return CliRunner().invoke(app, list(args))


def test_own_lists_what_to_buy(deck, opened):
    result = run("own", deck)
    assert result.exit_code == 0, result.output
    assert "🟥  4  Lightning Bolt" in result.output and "unmatched: Not A Card" in result.output
    assert opened == ["open", "close"]


def test_own_on_arena_counts_wildcards(deck, opened, tmp_path, monkeypatch):
    arena = tmp_path / "data" / "riffle" / "arena-collection.txt"
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    arena.parent.mkdir(parents=True)
    arena.write_text("1 Sol Ring\n")
    result = run("own", deck, "--arena")
    assert result.exit_code == 0, result.output
    assert "wildcards: 1 mythic · 4 not on Arena" in result.output


def test_price(deck, opened):
    result = run("price", deck)
    assert result.exit_code == 0, result.output
    assert "paper, whole deck    $35.00" in result.output


def test_legal(deck, opened):
    result = run("legal", deck, "-f", "modern")
    assert result.exit_code == 1
    assert "Not A Card — not matched to a card" in result.output
    assert "Sol Ring — not legal in modern" in result.output


def test_export(deck, opened):
    result = run("export", deck, "--to", "mtgo")
    assert result.exit_code == 0, result.output
    assert result.output.startswith("4 Lightning Bolt\n1 Sol Ring\n")


@pytest.mark.parametrize("command", [["own"], ["price"], ["legal"], ["export"]])
def test_without_a_catalog_commands_say_what_to_run(deck, monkeypatch, command):
    def open_catalog():
        raise postgres.Unavailable("no Magic cards in Postgres yet — run: riffle ingest scryfall")

    monkeypatch.setattr(postgres, "open_catalog", open_catalog)
    result = run(*command, deck)
    assert result.exit_code == 1
    assert "no Magic cards in Postgres yet — run: riffle ingest scryfall" in result.output


def test_an_offline_sync_writes_the_vault_from_the_catalog(tmp_path, monkeypatch, opened):
    monkeypatch.setenv("HOME", str(tmp_path))
    mtg = tmp_path / "atelier" / "library" / "games" / "tcg" / "mtg"
    (mtg / "modern").mkdir(parents=True)
    note = "---\ngame: mtg\nformat: modern\n---\n\n## Moxfield import\n\n```\n4 Lightning Bolt\n```\n"
    (mtg / "modern" / "burn.md").write_text(note)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))  # the default vault, under HOME
    monkeypatch.setattr(scryfall, "snapshot_prices", lambda: (tmp_path / "2026-09-26.jsonl.gz", False))
    result = run("sync", "--offline")
    assert result.exit_code == 0, result.output
    assert (
        "1 decks · 2 notes updated · 0 prices logged for 2026-09-21 · " in result.output
    )  # the catalog's day
    assert (mtg / "_generated" / "burn-data.md").exists()
    assert opened == ["open", "close"]
