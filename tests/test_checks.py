"""riffle check: every kept price file under the day its own stamp says, made before its fetch."""

import gzip
import io
import json
import lzma
import os
import zipfile
from datetime import UTC, datetime

import pytest
from typer.testing import CliRunner

from riffle.cli import app
from riffle.ingest import checks

FETCHED = datetime(2026, 9, 27, 20, 51, 50, tzinfo=UTC)


@pytest.fixture
def data(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    return tmp_path / "riffle"


def gz(path, body: bytes, fetched: datetime | None = FETCHED) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(gzip.compress(body, mtime=int(fetched.timestamp()) if fetched else 0))


def at(path, when: datetime) -> None:
    os.utime(path, (when.timestamp(), when.timestamp()))


def ck(made: str | None = "2026-09-27 13:08:38") -> bytes:
    meta = {"created_at": made} if made else {}
    return json.dumps({"meta": meta, "data": [{"id": 1}]}).encode()


def guide(stamp: str = "2026-09-27T02:45:12+0200") -> bytes:
    return json.dumps({"version": 1, "createdAt": stamp, "priceGuides": [{"idProduct": 1}]}).encode()


def zipped(name: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(name, b'{"348": 1.0}')
    return buf.getvalue()


def by_source() -> dict[str, checks.Report]:
    return {rep.source: rep for rep in checks.run()}


def test_nothing_kept_is_nothing_wrong(data):
    reports = checks.run()
    assert [r.source for r in reports] == [
        "Card Kingdom",
        "Mana Pool",
        "Cardmarket",
        "MTGJSON",
        "GoatBots",
        "tcgcsv",
        "Scryfall",
    ]
    assert all(r.summary() == "nothing kept yet" and not r.problems for r in reports)


def test_store_lists_filed_right_and_made_before_their_fetch(data):
    gz(data / "cardkingdom" / "daily" / "2026-09-27" / "singles.json.gz", ck())
    gz(data / "cardkingdom" / "daily" / "2026-09-27" / "sealed.json.gz", ck("2026-09-27 13:08:45"))
    gz(
        data / "cardkingdom" / "daily" / "2026-09-27" / "stock.json.gz", b"not a list"
    )  # not a list Riffle keeps
    gz(data / "cardkingdom" / "aside" / "singles-2026-09-28T130004Z.json.gz", ck(None))
    gz(
        data / "manapool" / "daily" / "2026-09-27" / "singles.json.gz",
        b'{"meta":{"as_of":"2026-09-27T20:24:16Z"}}',
    )
    found = by_source()
    kingdom = found["Card Kingdom"]
    assert kingdom.summary() == "2 lists over 1 day: all right"
    assert kingdom.notes == [
        "read as Pacific time, each list was made 43m 05s to 43m 12s before it was fetched",
        "1 set aside in cardkingdom/aside, not as any day's",
    ]
    assert found["Mana Pool"].summary() == "1 list: all right"
    assert found["Mana Pool"].notes == ["read as UTC, each list was made 27m 34s before it was fetched"]


def test_every_way_a_store_list_can_be_wrong(data):
    daily = data / "cardkingdom" / "daily"
    gz(daily / "2026-09-28" / "singles.json.gz", ck())  # the 27th's list under the 28th
    gz(daily / "2026-09-29" / "singles.json.gz", ck(None))
    gz(
        daily / "2026-09-30" / "singles.json.gz",
        ck("2026-09-30 13:08:38"),
        datetime(2026, 9, 30, 13, 0, tzinfo=UTC),
    )
    gz(daily / "2026-10-01" / "singles.json.gz", ck("2026-10-01 13:08:38"), fetched=None)
    problems = by_source()["Card Kingdom"].problems
    assert problems == [
        "cardkingdom/daily/2026-09-28/singles.json.gz: made on 2026-09-27, kept under 2026-09-28",
        "cardkingdom/daily/2026-09-29/singles.json.gz: no readable created_at",
        "cardkingdom/daily/2026-09-30/singles.json.gz: its created_at 2026-09-30 13:08:38, read as Pacific"
        " time, is 2026-09-30 20:08 UTC, after it was fetched at 2026-09-30 13:00 UTC: Card Kingdom's clock"
        " isn't Pacific time",
        "cardkingdom/daily/2026-10-01/singles.json.gz: no time it was fetched",
    ]
    assert by_source()["Card Kingdom"].summary() == "4 lists: 4 wrong"  # one a day: no "over"


def test_cardmarket_guides(data):
    daily = data / "cardmarket" / "daily"
    gz(daily / "2026-09-27" / "mtg.json.gz", guide())
    gz(daily / "2026-09-28" / "fab.json.gz", guide())  # the 27th's
    gz(daily / "2026-09-28" / "op.json.gz", b'{"version": 1}')
    gz(daily / "2026-09-29" / "op.json.gz", guide("2026-09-29T02:45:12+0200"), fetched=None)
    gz(
        daily / "2026-09-28" / "lorcana.json.gz",
        guide("2026-09-28T02:45:12+0200"),
        datetime(2026, 9, 27, 23, 0, tzinfo=UTC),
    )
    rep = by_source()["Cardmarket"]
    assert rep.summary() == "5 guides over 3 days: 4 wrong"
    assert rep.problems == [
        "cardmarket/daily/2026-09-28/fab.json.gz: made on 2026-09-27, kept under 2026-09-28",
        "cardmarket/daily/2026-09-28/lorcana.json.gz: made 2026-09-28 00:45 UTC, after it was fetched at"
        " 2026-09-27 23:00 UTC",
        "cardmarket/daily/2026-09-28/op.json.gz: no readable createdAt",
        "cardmarket/daily/2026-09-29/op.json.gz: no time it was fetched",
    ]


def test_mtgjson_files(data):
    for folder, name, inside in [
        ("daily", "2026-09-27", b'{"meta": {"date": "2026-09-27", "version": "5.3.0"}, "data": {}}'),
        ("90-days", "2026-09-27", b'{"meta": {"date": "2026-09-27"}}'),
        ("daily", "2026-09-28", b'{"meta": {"date": "2026-09-26"}}'),
        ("daily", "2026-09-29", b"{}"),
        ("daily", "2026-09-30", b'{"meta": {"date": "2026-09-30"}}'),
    ]:
        path = data / "mtgjson" / folder / f"{name}.json.xz"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(lzma.compress(inside))
        at(path, FETCHED if name != "2026-09-30" else datetime(2026, 9, 29, 23, 0, tzinfo=UTC))
    (data / "mtgjson" / "daily" / "2026-10-01.json.xz").write_bytes(b"not xz")
    rep = by_source()["MTGJSON"]
    assert rep.summary() == "6 files: 4 wrong"
    assert rep.problems == [
        "mtgjson/daily/2026-09-28.json.xz: its meta says 2026-09-26",
        "mtgjson/daily/2026-09-29.json.xz: no readable meta date",
        "mtgjson/daily/2026-09-30.json.xz: dated after it was fetched, 2026-09-29 23:00 UTC",
        "mtgjson/daily/2026-10-01.json.xz: no readable meta date",
    ]


def test_goatbots_days(data):
    daily = data / "goatbots" / "daily"
    daily.mkdir(parents=True)
    for name, body in [
        ("2026-09-27", zipped("price-history-2026-09-27.txt")),
        ("2026-09-28", zipped("price-history-2026-09-26.txt")),
        ("2026-09-29", zipped("readme.txt")),
        ("2026-09-30", b"not a zip"),
        ("2026-10-05", zipped("price-history-2026-10-05.txt")),
    ]:
        (daily / f"{name}.zip").write_bytes(body)
        at(daily / f"{name}.zip", FETCHED)
    rep = by_source()["GoatBots"]
    assert rep.problems == [
        "goatbots/daily/2026-09-28.zip: holds 2026-09-26",
        "goatbots/daily/2026-09-29.zip: holds no price file",
        "goatbots/daily/2026-09-30.zip: not a zip",
        "goatbots/daily/2026-10-05.zip: dated after it was fetched, 2026-09-27 20:51 UTC",
    ]


def test_tcgcsv_days(data):
    daily = data / "tcgcsv" / "daily"
    for name, stamp, fetched in [
        ("2026-09-26", "2026-09-26T20:05:50+0000", FETCHED),
        ("2026-09-27", "2026-09-26T20:05:50+0000", FETCHED),
        ("2026-09-28", "2026-09-28T20:04:59+0000", datetime(2026, 9, 28, 13, 0, tzinfo=UTC)),
    ]:
        (daily / name).mkdir(parents=True)
        (daily / name / "last-updated.txt").write_text(stamp)
        at(daily / name / "last-updated.txt", fetched)
    (daily / "2026-09-29").mkdir()
    (daily / "categories.json").write_text("{}")  # not a day
    (daily / "notes").mkdir()
    rep = by_source()["tcgcsv"]
    assert rep.summary() == "4 days: 3 wrong"
    assert rep.problems == [
        "tcgcsv/daily/2026-09-27/last-updated.txt: says 2026-09-26",
        "tcgcsv/daily/2026-09-28/last-updated.txt: made 2026-09-28 20:04 UTC, after it was fetched at"
        " 2026-09-28 13:00 UTC",
        "tcgcsv/daily/2026-09-29/last-updated.txt: missing or unreadable",
    ]


def test_scryfall_days(data):
    daily = data / "scryfall" / "daily"
    gz(daily / "2026-09-27.jsonl.gz", b"{}\n")
    gz(daily / "2026-09-28.jsonl.gz", b"{}\n")  # kept on the 27th
    gz(daily / "2026-09-29.jsonl.gz", b"{}\n", fetched=None)
    gz(daily / "latest.jsonl.gz", b"{}\n")
    rep = by_source()["Scryfall"]
    assert rep.problems == [
        "scryfall/daily/2026-09-28.jsonl.gz: kept 2026-09-27 20:51 UTC, before its day began",
        "scryfall/daily/2026-09-29.jsonl.gz: no time it was kept",
        "scryfall/daily/latest.jsonl.gz: not named by a day",
    ]


def test_riffle_check_says_all_right(data):
    gz(data / "cardkingdom" / "daily" / "2026-09-27" / "singles.json.gz", ck())
    result = CliRunner().invoke(app, ["check"])
    assert result.exit_code == 0
    lines = result.output.splitlines()
    assert lines[0] == "Card Kingdom  1 list: all right"
    assert lines[1] == "              read as Pacific time, each list was made 43m 12s before it was fetched"
    assert "Scryfall      nothing kept yet" in lines


def test_riffle_check_names_each_file_dated_wrong_and_exits_1(data):
    gz(data / "cardkingdom" / "daily" / "2026-09-28" / "singles.json.gz", ck())
    gz(data / "cardmarket" / "daily" / "2026-09-28" / "fab.json.gz", guide())
    result = CliRunner().invoke(app, ["check"])
    assert result.exit_code == 1
    assert (
        "  ! cardkingdom/daily/2026-09-28/singles.json.gz: made on 2026-09-27, kept under 2026-09-28"
        in result.output
    )
    assert result.output.rstrip().endswith("2 price files dated wrong")
