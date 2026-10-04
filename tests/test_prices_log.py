"""`riffle prices log`: each card's price on every Scryfall day the store keeps, read on demand
in place of the vault's _log/prices.md, by the rule deck prices use."""

import gzip
import json
from datetime import date

import pytest
from typer.testing import CliRunner

from riffle.analysis import pricing
from riffle.cli import app
from riffle.export import obsidian
from riffle.ingest import scryfall
from riffle.models import PricedPrinting, Prices


def keep_day(day: str, prices: dict[str, dict]) -> None:
    """A price day as snapshot_prices keeps it: a line a printing, {"id", "prices"}."""
    folder = scryfall.prices_dir()
    folder.mkdir(parents=True, exist_ok=True)
    with gzip.open(folder / f"{day}.jsonl.gz", "wt", encoding="utf-8") as f:
        for sid, found in prices.items():
            f.write(json.dumps({"id": sid, "prices": found}, separators=(",", ":")) + "\n")


def sid(n: int) -> str:
    return f"{n:08d}-0000-0000-0000-000000000000"  # 36 characters, as Scryfall's are


def run(*args):
    return CliRunner().invoke(app, ["prices", "log", *args])


def test_kept_days_are_listed_oldest_first_and_other_files_ignored():
    keep_day("2026-09-25", {})
    keep_day("2026-09-24", {})
    (scryfall.prices_dir() / "2026-09-26.jsonl.gz.part").write_text("")
    (scryfall.prices_dir() / "9999-99-99.jsonl.gz").write_text("")
    assert [day for day, _ in scryfall.price_days()] == [date(2026, 9, 24), date(2026, 9, 25)]


def test_a_day_s_prices_are_read_only_for_the_printings_asked():
    keep_day("2026-09-24", {sid(1): {"usd": "1.00"}, sid(2): {"usd": "2.00"}, sid(3): None})
    [(_, path)] = scryfall.price_days()
    assert scryfall.day_prices(path, {sid(2), sid(3), sid(4)}) == {sid(2): {"usd": "2.00"}, sid(3): {}}


@pytest.mark.parametrize(
    "body", [b"not gzip", gzip.compress(b'{"id":"00000001-0000-0000-0000-000000000000",')]
)
def test_a_day_that_cant_be_read_says_which(body):
    scryfall.prices_dir().mkdir(parents=True)
    path = scryfall.prices_dir() / "2026-09-24.jsonl.gz"
    path.write_bytes(body)
    with pytest.raises(scryfall.Unreadable, match="2026-09-24.jsonl.gz can't be read"):
        scryfall.day_prices(path, {sid(1)})


def test_a_day_s_price_follows_the_catalog_s_rule():
    printings = {
        "tomb": [PricedPrinting("tomb-1", True, False), PricedPrinting("tomb-wc", False, False)],
        "hans": [PricedPrinting("hans-1", False, False), PricedPrinting("hans-mo", False, True)],
        "lotus": [PricedPrinting("lotus-1", True, False), PricedPrinting("lotus-ce", False, False)],
        "bolt": [PricedPrinting("bolt-1", True, False), PricedPrinting("bolt-mo", False, True)],
    }
    day = {
        "tomb-1": {"usd": "123.01"},
        "tomb-wc": {"usd": "56.21"},  # gold-bordered: not a copy that can be played
        "hans-1": {"usd": "0.50"},
        "hans-mo": {"usd": "0.01", "tix": "0.03"},  # digital: no paper price of its own
        "lotus-ce": {"usd": "3000.00"},
        "bolt-1": {"usd": "1.25", "tix": "bad"},
        "bolt-mo": {"tix": "0.02"},
    }
    assert pricing.on_day(printings, day) == {
        "tomb": Prices(usd=123.01),
        "hans": Prices(usd=0.5, tix=0.03),  # no playable printing: its cheapest paper one
        "lotus": Prices(),  # its playable printing has no price that day
        "bolt": Prices(usd=1.25, tix=0.02),  # MTGO: the cheapest tix of any printing
    }
    assert pricing.scryfall_ids(printings) == set(day) | {"lotus-1"}
    assert pricing.on_day(printings, {"tomb-1": {"usd": "NaN"}})["tomb"] == Prices()


def test_a_card_s_history_a_line_a_day(opened):
    keep_day("2026-09-24", {"s-o-rift": {"usd": "30.00", "tix": "2.00"}})
    keep_day("2026-09-25", {"s-o-rift": {"usd": "31.50"}, "s-o-sol": {"usd": "1.00"}})
    result = run("cyclonic rift", "Sol Ring")
    assert result.exit_code == 0, result.output
    assert result.output.splitlines() == [
        "2026-09-24 | Cyclonic Rift | $30.00 | 2.00",
        "2026-09-24 | Sol Ring | — | —",
        "2026-09-25 | Cyclonic Rift | $31.50 | —",
        "2026-09-25 | Sol Ring | $1.00 | —",
    ]
    assert obsidian.price_line("2026-09-24", "X", Prices(1234.5, 0.1)) == "2026-09-24 | X | $1,234.50 | 0.10"


def test_with_no_names_every_buy_line_s_card(tmp_path, monkeypatch, opened):
    monkeypatch.setenv("HOME", str(tmp_path))  # the default vault lives under it
    notes = tmp_path / "atelier" / "library" / "games" / "tcg"
    (notes / "mtg" / "_generated").mkdir(parents=True)
    (notes / "shopping.md").write_text(
        "- [ ] [[Sol Ring]] #mtg/buy\n- [ ] [[Not A Card]] #mtg/buy\n"
        "- [ ] [[Summon Bahamut|Summon: Bahamut]] #mtg/buy\n"  # a card's link, by its safe name
    )
    (notes / "mtg" / "_generated" / "x-data.md").write_text("- [ ] [[Cyclonic Rift]] #mtg/buy\n")  # Riffle's
    (notes / "latin.md").write_bytes(b"caf\xe9\n")
    keep_day("2026-09-24", {"s-o-sol": {"usd": "1.00", "tix": "0.05"}, "s-o-summon": {"usd": "4.00"}})
    result = run()
    assert result.exit_code == 0, result.output
    assert "2026-09-24 | Sol Ring | $1.00 | 0.05" in result.output.splitlines()
    assert "2026-09-24 | Summon: Bahamut | $4.00 | —" in result.output.splitlines()
    assert "Cyclonic Rift" not in result.output
    assert "  ! buy list: unmatched Not A Card" in result.output
    assert f"  ! can't read {notes / 'latin.md'} (not UTF-8); its buy lines are left out" in result.output


def test_a_name_that_isnt_a_card_is_named_and_exits_1(opened):
    keep_day("2026-09-24", {"s-o-sol": {"usd": "1.00"}})
    result = run("Sol Ring", "Sol Rign")
    assert result.exit_code == 1
    assert result.output.splitlines() == ["no card named 'Sol Rign'", "2026-09-24 | Sol Ring | $1.00 | —"]


def test_no_days_kept_says_how_one_is_and_exits_1(opened):
    result = run("Sol Ring")
    assert result.exit_code == 1
    assert result.output == "no Scryfall price days kept yet; riffle sync keeps one a day\n"
    assert opened == []  # the catalog isn't opened for nothing


def test_a_day_that_cant_be_read_is_left_out_and_the_rest_print(opened):
    keep_day("2026-09-25", {"s-o-sol": {"usd": "1.10"}})
    (scryfall.prices_dir() / "2026-09-24.jsonl.gz").write_bytes(b"cut short")
    result = run("Sol Ring")
    assert result.exit_code == 1
    lines = result.output.splitlines()
    assert lines[0].startswith("  ! 2026-09-24: 2026-09-24.jsonl.gz can't be read (")
    assert lines[0].endswith("); that day is left out")
    assert lines[1:] == ["2026-09-25 | Sol Ring | $1.10 | —"]
