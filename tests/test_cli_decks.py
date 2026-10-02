"""`riffle decks`: a line per deck note, each column as wide as its longest value."""

from typer.testing import CliRunner

from riffle.cli import app

NOTE = "---\ngame: mtg\nformat: {fmt}\nstatus: {status}\n---\n\n## Moxfield import\n\n```\n{cards}\n```\n"


def decks(tmp_path, monkeypatch, *notes: tuple[str, str, str, str, str]) -> list[str]:
    """`riffle decks` over a vault holding these notes: (folder, slug, format, status, list)."""
    monkeypatch.setenv("HOME", str(tmp_path))  # the default vault lives under it
    mtg = tmp_path / "atelier" / "library" / "games" / "tcg" / "mtg"
    for folder, slug, fmt, status, cards in notes:
        path = mtg / folder / f"{slug}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(NOTE.format(fmt=fmt, status=status, cards=cards), encoding="utf-8")
    result = CliRunner().invoke(app, ["decks"])
    assert result.exit_code == 0, result.output
    return result.output.splitlines()


def test_each_column_is_as_wide_as_its_longest_value(tmp_path, monkeypatch):
    aesi = (
        "commander",
        "simic-aesi-tyrant-of-gyre-strait-lands",
        "commander",
        "assembled",
        "1 Aesi\n99 Forest",
    )
    burn = ("modern", "burn", "modern", "idea", "60 Mountain")
    assert decks(tmp_path, monkeypatch, aesi, burn) == [
        "simic-aesi-tyrant-of-gyre-strait-lands  commander  assembled  100 cards",
        "burn                                    modern     idea        60 cards",
    ]


def test_widths_count_terminal_cells_not_characters(tmp_path, monkeypatch):
    decomposed = "se\u0301ance"  # "séance" as macOS can return it: an e, then a combining accent
    lines = decks(
        tmp_path,
        monkeypatch,
        ("modern", decomposed, "modern", "idea", "60 Island"),
        ("modern", "wide-象", "legacy", "idea", "60 Island"),  # 象 takes two cells
    )
    assert lines == [f"{decomposed}   modern  idea  60 cards", "wide-象  legacy  idea  60 cards"]


def test_a_list_or_an_empty_value_still_prints(tmp_path, monkeypatch):
    lines = decks(tmp_path, monkeypatch, ("legacy", "twin", "[legacy, vintage]", "", "60 Island"))
    assert lines == ["twin  legacy, vintage    60 cards"]


def test_an_empty_vault_prints_nothing(tmp_path, monkeypatch):
    assert decks(tmp_path, monkeypatch) == []
