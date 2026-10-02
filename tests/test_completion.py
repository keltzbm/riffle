"""Tab completion as zsh asks for it: deck names from the vault, option values from their lists."""

import pytest
from typer.testing import CliRunner

from riffle.cli import app
from riffle.export import formats
from riffle.ingest import mtgo

DECK = "---\ngame: mtg\nformat: commander\n---\n\n## Moxfield import\n\n```\n1 Sol Ring\n```\n"


def offered(line: str) -> list[str] | None:
    """What zsh is offered for a command line typed up to the cursor; None means file names."""
    env = {"_RIFFLE_COMPLETE": "complete_zsh", "_TYPER_COMPLETE_ARGS": line}
    out = CliRunner().invoke(app, [], env=env, prog_name="riffle").output.strip()
    if out == "_files":
        return None
    head, tail = "_arguments '*: :((", "))'"
    assert out.startswith(head) and out.endswith(tail), out
    return [item.split('":"')[0].strip('"') for item in out[len(head) : -len(tail)].splitlines()]


@pytest.fixture
def vault(tmp_path, monkeypatch):
    """Three deck notes, a note that isn't a deck, and a generated note, in the default vault."""
    monkeypatch.setenv("HOME", str(tmp_path))
    mtg = tmp_path / "atelier" / "library" / "games" / "tcg" / "mtg"
    for rel, text in {
        "commander/simic-aesi-lands.md": DECK,
        "commander/selesnya-emmara-tokens.md": DECK,
        "modern/izzet-murktide.md": DECK,
        "moc-mtg.md": "# Magic\n",
        "_generated/simic-aesi-lands-data.md": DECK,
    }.items():
        (mtg / rel).parent.mkdir(parents=True, exist_ok=True)
        (mtg / rel).write_text(text, encoding="utf-8")
    return mtg


def test_a_deck_completes_from_the_deck_notes(vault):
    assert offered("riffle own ") == ["selesnya-emmara-tokens", "simic-aesi-lands", "izzet-murktide"]
    assert offered("riffle own si") == ["simic-aesi-lands"]
    assert offered("riffle price iz") == ["izzet-murktide"]


@pytest.mark.parametrize("command", ["legal", "export"])
def test_legal_and_export_offer_all_too(vault, command):
    assert offered(f"riffle {command} ")[-1] == "all"
    assert offered(f"riffle {command} a") == ["all"]


def test_a_path_or_no_match_completes_as_a_file(vault):
    assert offered("riffle own ~/Downloads/li") is None
    assert offered("riffle own a") is None  # own takes no "all"


def test_an_unreadable_vault_offers_files(vault):
    (vault / "modern" / "broken.md").write_bytes(b"---\ngame: mtg\n\xff\n")
    assert offered("riffle own ") is None


@pytest.mark.parametrize(
    ("line", "values"),
    [
        ("riffle export x --to ", list(formats.FORMATS)),
        ("riffle export x --to m", ["moxfield", "manabox", "mtgo"]),
        ("riffle export x --pin ", ["owned", "none"]),
        ("riffle own x --show ", ["buy", "own", "all"]),
        ("riffle own x -s o", ["own"]),
        ("riffle legal x -f pau", ["pauper", "paupercommander"]),
        ("riffle meta cards --board ", ["all", "main", "side"]),
        ("riffle meta decks -k ", list(mtgo.KINDS)),
        ("riffle meta decks -f ", [*mtgo.FORMATS, "all"]),
        ("riffle meta cards -f du", ["duel-commander"]),
    ],
)
def test_option_values_complete_from_their_lists(line, values):
    assert offered(line) == values
