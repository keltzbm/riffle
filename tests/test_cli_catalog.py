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
    assert "○  4  Lightning Bolt" in result.output and "unmatched: Not A Card" in result.output
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
    assert "1 decks · 2 notes updated · versions changed: burn" in result.output
    assert "card notes: 1 (1 written, 0 removed)" in result.output  # the buy table's Lightning Bolt
    assert (mtg / "_generated" / "burn-data.md").exists()
    assert opened == ["open", "close"]


@pytest.fixture
def notes(tmp_path, monkeypatch):
    """The default vault under a fake HOME, with a deck note that links cards in its text."""
    monkeypatch.setenv("HOME", str(tmp_path))
    mtg = tmp_path / "atelier" / "library" / "games" / "tcg" / "mtg"
    (mtg / "modern").mkdir(parents=True)
    note = "---\ngame: mtg\nformat: modern\n---\n\n[[Sol Ring]] [[Fire|Fire // Ice]]\n\n"
    note += "```\n4 Lightning Bolt\n```\n"
    (mtg / "modern" / "burn.md").write_text(note)
    return mtg


def test_check_vault_says_every_link_opens_its_card(notes, opened):
    result = run("check", "vault")
    assert result.exit_code == 0, result.output
    assert (
        result.output == "card links: 2 cards linked in ~/atelier/library/games/tcg\n"
        "every card link opens the card's note\n"
    )


def test_check_vault_names_each_link_that_cant_and_exits_1(notes, opened, tmp_path, monkeypatch, cat):
    (notes / "ideas.md").write_text(
        "[[Fire // Ice]] [[Summon: Bahamut]] [[Delver of Secrets // Insectile Aberration]]"
    )
    (notes.parent.parent / "Summon Bahamut.md").write_text("")
    real = cat.resolve
    monkeypatch.setattr(cat, "resolve", lambda name: "o-bolt" if name == "Delver of Secrets" else real(name))
    result = run("check", "vault")
    assert result.exit_code == 1
    assert result.output.splitlines() == [
        "card links: 2 cards linked in ~/atelier/library/games/tcg",
        "  ! [[Fire // Ice]] in 1 note: write [[Fire|Fire // Ice]]",
        "  ! Summon Bahamut: ~/atelier/library/games/Summon Bahamut.md has that name, "
        "so the card has no note",
        "  ! Delver of Secrets // Insectile Aberration: its note would be Delver of Secrets, which names "
        "Lightning Bolt; no note",
        "1 link can't open the card's note; 2 linked cards without a note",
    ]


def test_check_takes_prices_vault_or_collection(plain):
    result = run("check", "decks")
    assert result.exit_code == 2
    assert "what to check: prices, vault, or collection, not 'decks'" in plain(result.output)


def test_card_prints_what_its_note_holds(notes, opened, tmp_path, monkeypatch):
    import webbrowser

    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    (tmp_path / "data" / "riffle").mkdir(parents=True)
    (tmp_path / "data" / "riffle" / "collection.csv").write_text(
        "Name,Set code,Collector number,Foil,Quantity,Scryfall ID\nFire // Ice,DMR,215,normal,2,\n"
    )
    opens: list[str] = []
    monkeypatch.setattr(webbrowser, "open", opens.append)
    result = run("card", "fire", "--open")
    assert result.exit_code == 0, result.output
    assert result.output.splitlines() == [
        "Fire // Ice",
        "Fire {1}{R}",
        "  Instant",
        "  Fire deals 2 damage.",
        "Ice {1}{U}",
        "  Instant",
        "  Tap.",
        "  Draw.",
        "Price on 2026-09-21: $1.00 paper, 0.10 tix "
        "(Scryfall's, at the cheapest printing that can be played).",
        "Legal in modern.",  # the one list's format
        "Owned: 2 (2 DMR 215).",
        "https://scryfall.com/card/dmr/215",
    ]
    assert opens == ["https://scryfall.com/card/dmr/215"]
    one = run("card", "Sol Ring")
    assert one.output.splitlines()[:3] == ["Sol Ring", "  Artifact", "  {T}: Add {C}{C}."]


def test_card_says_when_no_card_has_the_name(notes, opened):
    result = run("card", "Fyre")
    assert result.exit_code == 1
    assert result.output == "no card named 'Fyre'\n"
