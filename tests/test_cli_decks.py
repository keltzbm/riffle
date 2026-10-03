"""`riffle decks`: a line per deck note, each column as wide as its longest value, narrowed by
filters and sorted by any column."""

import pytest
from typer.testing import CliRunner

from riffle.cli import app

NOTE = """---
game: mtg
format: {fmt}
strategy: {strategy}
archetype: {archetype}
colors: {colors}
{checked}---

## Moxfield import

```
{cards}
```
"""


def _vault(tmp_path, monkeypatch, *notes: dict) -> None:
    """A vault under a scratch HOME (the default vault lives under it) holding these notes. Each
    is a dict: folder, slug, cards, and any of format, strategy, archetype, colors, checked."""
    monkeypatch.setenv("HOME", str(tmp_path))
    mtg = tmp_path / "atelier" / "library" / "games" / "tcg" / "mtg"
    for note in notes:
        path = mtg / note["folder"] / f"{note['slug']}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        checked = f"checked: {note['checked']}\n" if "checked" in note else ""
        text = NOTE.format(
            fmt=note.get("format", "modern"),
            strategy=note.get("strategy", "aggro"),
            archetype=note.get("archetype", "burn"),
            colors=note.get("colors", "[R]"),
            checked=checked,
            cards=note["cards"],
        )
        path.write_text(text, encoding="utf-8")


def decks(*args: str, code: int = 0) -> list[str]:
    result = CliRunner().invoke(app, ["decks", *args])
    assert result.exit_code == code, result.output
    return result.output.splitlines()


AESI = {
    "folder": "commander",
    "slug": "simic-aesi-tyrant-of-gyre-strait-lands",
    "format": "commander",
    "strategy": "ramp",
    "archetype": "lands",
    "colors": "[G, U]",
    "checked": "2026-09-30",
    "cards": "1 Aesi, Tyrant of Gyre Strait\n99 Forest",
}
BURN = {"folder": "modern", "slug": "burn", "cards": "60 Mountain"}
TWIN = {
    "folder": "legacy",
    "slug": "twin",
    "format": "legacy",
    "strategy": "combo",
    "archetype": "[combo, tempo]",
    "colors": "[U, R]",
    "checked": "2026-10-01",
    "cards": "4 Sol Ring\n4 Cyclonic Rift\n52 Island",
}
WASTES = {"folder": "modern", "slug": "eldrazi", "strategy": "ramp", "colors": "[]", "cards": "60 Wastes"}


def test_each_column_is_as_wide_as_its_longest_value_and_the_list_is_by_name(tmp_path, monkeypatch):
    _vault(tmp_path, monkeypatch, AESI, BURN)
    assert decks() == [
        "burn                                    modern     aggro  R    60 cards  not checked",
        "simic-aesi-tyrant-of-gyre-strait-lands  commander  ramp   GU  100 cards  checked 2026-09-30",
    ]


def test_widths_count_terminal_cells_not_characters(tmp_path, monkeypatch):
    decomposed = "séance"  # "séance" as macOS can return it: an e, then a combining accent
    _vault(
        tmp_path,
        monkeypatch,
        {"folder": "modern", "slug": decomposed, "cards": "60 Island"},
        {
            "folder": "modern",
            "slug": "wide-象",
            "format": "legacy",
            "cards": "60 Island",
        },  # 象 takes two cells
    )
    assert decks() == [
        f"{decomposed}   modern  aggro  R  60 cards  not checked",
        "wide-象  legacy  aggro  R  60 cards  not checked",
    ]


def test_colorless_is_c_and_a_list_value_is_joined(tmp_path, monkeypatch):
    _vault(tmp_path, monkeypatch, WASTES, {**BURN, "format": "[legacy, vintage]"})
    assert decks() == [
        "burn     legacy, vintage  aggro  R  60 cards  not checked",
        "eldrazi  modern           ramp   C  60 cards  not checked",
    ]


def test_an_empty_vault_prints_nothing(tmp_path, monkeypatch):
    _vault(tmp_path, monkeypatch)
    assert decks() == []
    assert decks("--format", "modern") == []  # nothing to match isn't a typo


@pytest.mark.parametrize(
    ("args", "slugs"),
    [
        (["--format", "MODERN"], ["burn", "eldrazi"]),
        (["--strategy", "ramp"], ["eldrazi", "simic-aesi-tyrant-of-gyre-strait-lands"]),
        (["--strategy", "ramp", "--format", "commander"], ["simic-aesi-tyrant-of-gyre-strait-lands"]),
        (["--archetype", "tempo"], ["twin"]),  # any item of a list
        (["--colors", "ug"], ["simic-aesi-tyrant-of-gyre-strait-lands"]),  # any order, exactly these
        (["--colors", "u"], []),
        (["--colors", "c"], ["eldrazi"]),
    ],
)
def test_filters_narrow_the_list(tmp_path, monkeypatch, args, slugs):
    _vault(tmp_path, monkeypatch, AESI, BURN, TWIN, WASTES)
    lines = decks(*args, code=0 if slugs else 1)
    assert [line.split()[0] for line in lines if not line.startswith("no decks")] == slugs


def test_filters_that_match_no_deck_together_say_so_and_exit_1(tmp_path, monkeypatch):
    _vault(tmp_path, monkeypatch, AESI, BURN)
    assert decks("--format", "modern", "--strategy", "ramp", code=1) == ["no decks match"]


def test_a_value_no_deck_has_names_the_ones_there_are(tmp_path, monkeypatch, plain):
    _vault(tmp_path, monkeypatch, AESI, BURN)
    result = CliRunner().invoke(app, ["decks", "--format", "moden"])
    assert result.exit_code == 2
    said = "Invalid value for '--format': no deck has format moden; the vault has commander, modern"
    assert said in plain(result.output)


def test_among_many_values_a_typo_gets_the_close_ones(tmp_path, monkeypatch, plain):
    notes = [{**BURN, "slug": f"deck-{n}", "archetype": f"theme{n}"} for n in range(13)]
    _vault(tmp_path, monkeypatch, *notes, {**BURN, "slug": "tribal", "archetype": "tribal"})
    result = CliRunner().invoke(app, ["decks", "--archetype", "tribel"])
    assert result.exit_code == 2
    assert "no deck has archetype tribel; close: tribal" in plain(result.output)
    result = CliRunner().invoke(app, ["decks", "--archetype", "zzz"])
    assert "no deck has archetype zzz" in plain(result.output) and "close" not in result.output


@pytest.mark.parametrize(
    ("args", "why"), [(["--colors", "wx"], "wubrg"), (["--sort", "price"], "one of name")]
)
def test_a_bad_color_or_sort_is_refused(tmp_path, monkeypatch, plain, args, why):
    _vault(tmp_path, monkeypatch, BURN)
    result = CliRunner().invoke(app, ["decks", *args])
    assert result.exit_code == 2 and why in plain(result.output)


@pytest.mark.parametrize(
    ("key", "slugs"),
    [
        ("cards", ["burn", "eldrazi", "twin", "simic-aesi-tyrant-of-gyre-strait-lands"]),
        ("checked", ["burn", "eldrazi", "simic-aesi-tyrant-of-gyre-strait-lands", "twin"]),
        ("colors", ["eldrazi", "simic-aesi-tyrant-of-gyre-strait-lands", "burn", "twin"]),
        ("format", ["simic-aesi-tyrant-of-gyre-strait-lands", "twin", "burn", "eldrazi"]),
        ("strategy", ["burn", "twin", "eldrazi", "simic-aesi-tyrant-of-gyre-strait-lands"]),
    ],
)
def test_sort_by_a_column_then_by_name(tmp_path, monkeypatch, key, slugs):
    _vault(tmp_path, monkeypatch, AESI, BURN, TWIN, WASTES)
    assert [line.split()[0] for line in decks("--sort", key)] == slugs


def test_to_buy_reads_the_collection_and_keeps_decks_with_at_most_n_left(tmp_path, monkeypatch, opened):
    """No collection: every card but the basics is to buy. Twin needs Sol Ring and Cyclonic Rift."""
    _vault(tmp_path, monkeypatch, {**TWIN, "cards": "4 Sol Ring\n4 Cyclonic Rift\n52 Forest"}, AESI)
    assert decks("--to-buy", "1") == [
        "simic-aesi-tyrant-of-gyre-strait-lands  commander  ramp  GU  100 cards  checked 2026-09-30  1 to buy"
    ]
    assert [line.split()[0] for line in decks("--sort", "to-buy")] == [
        "simic-aesi-tyrant-of-gyre-strait-lands",
        "twin",
    ]
    assert decks("--to-buy", "0", code=1) == ["no decks match"]
    assert opened == ["open", "close"] * 3


def test_a_note_that_names_no_colors_shows_none(tmp_path, monkeypatch):
    _vault(tmp_path, monkeypatch, BURN)
    note = tmp_path / "atelier" / "library" / "games" / "tcg" / "mtg" / "modern" / "burn.md"
    note.write_text(note.read_text().replace("colors: [R]\n", ""))
    assert decks() == ["burn  modern  aggro    60 cards  not checked"]  # an empty column keeps its place
