"""riffle check: every kept price file under the day its own stamp says, made before its fetch."""

import gzip
import io
import json
import lzma
import os
import zipfile
from datetime import UTC, datetime, timedelta

import pytest
from typer.testing import CliRunner

from riffle import net, runs, times, watching
from riffle.cli import app
from riffle.ingest import checks, pricelists

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


def test_goatbots_whole_years_run_to_dec_31(data):
    yearly = data / "goatbots" / "yearly"
    yearly.mkdir(parents=True)
    for name, body in [
        ("2021", zipped("price-history-2020-12-31.txt")),
        ("2022", b"not a zip"),
        ("2023", zipped("price-history-2023-12-31.txt")),
        ("2024", zipped("price-history-2024-01-03.txt")),
        ("2026-partial", zipped("price-history-2026-01-03.txt")),  # a partial year runs short
    ]:
        (yearly / f"{name}.zip").write_bytes(body)
    assert by_source()["GoatBots"].problems == [
        "goatbots/yearly/2021.zip: runs to no day of the year, short of Dec 31",
        "goatbots/yearly/2022.zip: not a zip",
        "goatbots/yearly/2024.zip: runs to 2024-01-03, short of Dec 31",
    ]


def price_lines(*sets: tuple[int, str | None]) -> bytes:
    """A tcgcsv price file's lines, as Riffle writes them: (set, Last-Modified)."""
    return b"".join(
        json.dumps(
            {"groupId": g, "fetched": "2026-09-28T22:00:00+00:00", "lastModified": m, "response": None}
        ).encode()
        + b"\n"
        for g, m in sets
    )


def test_tcgcsv_sets_from_a_later_refresh_and_games_unfinished(data):
    day = data / "tcgcsv" / "daily" / "2026-09-27"
    day.mkdir(parents=True)
    (day / "last-updated.txt").write_text("2026-09-27T20:04:59+0000")
    at(day / "last-updated.txt", FETCHED)
    late = price_lines(
        (1, "2026-09-27T20:04:00+00:00"),  # the day's
        (2, None),  # no Last-Modified: can't tell
        (3, "2026-09-27T21:30:00+00:00"),  # late, but still the day's own date
        (4, "2026-09-28T20:04:30+00:00"),
        (5, "2026-09-28T20:05:00+00:00"),
    )
    gz(day / "mtg" / "prices.jsonl.gz", late)
    (day / "fab").mkdir()
    (day / "fab" / "prices.jsonl.part").write_bytes(price_lines((6, "2026-09-28T20:04:00+00:00")))
    gz(day / "op" / "prices.jsonl.gz", price_lines((7, "2026-09-27T20:04:00+00:00")))
    (day / "op" / "missing.txt").write_text("8\n9\n")
    gz(day / "yugioh" / "prices.jsonl.gz", b"")
    (day / "yugioh" / "missing.txt").write_text("unknown: the day's set list wasn't kept\n")
    (day / "lorcana").mkdir()
    (day / "lorcana" / "prices.jsonl.gz").write_bytes(b"not gzip")
    for game in ("pokemon", "starwars", "zombie-world-order-tcg"):
        (day / game).mkdir()
        (day / game / "groups.json").write_text("{}")
    unstamped = data / "tcgcsv" / "daily" / "2026-09-29" / "mtg"
    gz(unstamped / "prices.jsonl.gz", late)  # no stamp for the day: sets can't be checked
    rep = by_source()["tcgcsv"]
    assert rep.problems == [
        "tcgcsv/daily/2026-09-27/fab/prices.jsonl.part: set 6 is from a later refresh, 2026-09-28 20:04 UTC",
        "tcgcsv/daily/2026-09-27/lorcana/prices.jsonl.gz: unreadable (Not a gzipped file (b'no'))",
        "tcgcsv/daily/2026-09-27/mtg/prices.jsonl.gz: set 4 is from a later refresh, 2026-09-28 20:04 UTC,"
        " and 1 more",
        "tcgcsv/daily/2026-09-29/last-updated.txt: missing or unreadable",
    ]
    assert rep.notes == [
        "6 games unfinished: 2026-09-27 fab (being fetched), 2026-09-27 op (2 sets never fetched),"
        " 2026-09-27 pokemon (no prices kept), 2026-09-27 starwars (no prices kept),"
        " 2026-09-27 yugioh (which unknown), and 1 more"
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


# ---- the lists kept since 2026-09-29, every one a store publishes -----------------------------


def watched(made: str, fetched: datetime, price: str = "0.39") -> None:
    """Card Kingdom's singles as `riffle watch cardkingdom` keeps them."""
    rows = [{"id": n, "price_retail": price if n % 5 == 0 else "0.39", "qty_retail": n} for n in range(60)]
    body = json.dumps({"meta": {"created_at": made}, "data": rows}).encode()

    def fetch(url, dest, known, etag=None, accept="*/*", progress=None):
        dest.write_bytes(body)
        return net.Fetched("new", body[: net.HEAD], None, len(body))

    pricelists.watch(pricelists.CARD_KINGDOM[:1], fetch=fetch, clock=lambda: fetched)


def three_lists() -> dict:
    for hour, price in ((13, "0.39"), (16, "0.41"), (19, "0.45")):
        watched(f"2026-09-27 {hour}:08:38", datetime(2026, 9, 27, hour, 40, tzinfo=pricelists.PACIFIC), price)
    return runs.kept(pricelists.lists_dir(pricelists.CARD_KINGDOM[0]))


def test_watched_lists_kept_as_logged_are_all_right(data, monkeypatch):
    monkeypatch.setattr(times, "now", lambda: datetime(2026, 9, 28, 2, 40, tzinfo=UTC))
    assert len(three_lists()) == 3
    rep = by_source()["Card Kingdom"]
    assert rep.files == 3 and rep.problems == [] and rep.summary() == "3 lists over 1 day: all right"
    assert rep.notes == ["singles: 3 kept in 1 run; lateness judged from 14 gaps, 2 so far"]


def every_three_hours(count: int, longer: dict[int, int] | None = None) -> datetime:
    """Card Kingdom's singles every 3 hours, from 2026-09-25 01:08:38 Pacific, the gap before list n
    longer[n] hours instead. When the last was made."""
    made = datetime(2026, 9, 25, 1, 8, 38, tzinfo=pricelists.PACIFIC)
    for n in range(count):
        if n:
            made += timedelta(hours=(longer or {}).get(n, 3))
        watched(f"{made:%Y-%m-%d %H:%M:%S}", made + timedelta(minutes=30), f"0.{40 + n}")
    return made


def singles_note(at: datetime) -> str:
    folder = pricelists.lists_dir(pricelists.CARD_KINGDOM[0])
    n = len(runs.runs(folder))
    (note,) = by_source()["Card Kingdom"].notes
    return note.replace(f"in {n} run{'s' * (n != 1)}", "in N runs")


def test_a_list_is_late_past_its_margin_times_its_longest_gap_in_30_days(data, monkeypatch):
    last = every_three_hours(16)  # 15 gaps of 3 hours: the bar is 1.25 x 3 h = 3 h 45 m
    monkeypatch.setattr(times, "now", lambda: last + timedelta(hours=3, minutes=44))
    assert singles_note(last) == "singles: 16 kept in N runs, one every 3h 00m lately"
    monkeypatch.setattr(times, "now", lambda: last + timedelta(hours=4))
    assert singles_note(last) == (
        "singles: 16 kept in N runs, one every 3h 00m lately; late: the last was made 2026-09-27 05:08 UTC, "
        "4h 00m ago, past 1.25 × its longest gap in 30 days (3h 45m)"
    )


def test_a_list_that_often_runs_past_its_longest_gap_learns_a_wider_margin(data, monkeypatch):
    last = every_three_hours(18, longer={15: 5, 17: 7})  # 5 h is 1.67 x the longest before it, 7 h 1.4 x
    monkeypatch.setattr(times, "now", lambda: last + timedelta(hours=9))
    assert singles_note(last) == (
        "singles: 18 kept in N runs, one every 3h 00m lately, its margin 1.40 from its own gaps"
    )  # 9 h is under 1.40 x 7 h: an irregular list, not a late one


def test_a_kept_file_changed_or_missing_is_named(data):
    files = three_lists()
    diff = files["2026-09-27T230838Z"]
    diff.write_bytes(diff.read_bytes() + b"x")
    files["2026-09-28T020838Z"].unlink()
    rel = lambda path: path.relative_to(data).as_posix()  # noqa: E731
    assert by_source()["Card Kingdom"].problems == [
        f"{rel(diff)}: changed since it was kept",
        f"{rel(files['2026-09-28T020838Z'])}: missing",
    ]


def test_a_damaged_copy_of_a_base_says_the_other_is_whole(data):
    files = three_lists()
    copy = runs.copies(files["2026-09-27T200838Z"].parent)[1]
    copy.write_bytes(b"damaged")
    assert by_source()["Card Kingdom"].problems == [
        f"{copy.relative_to(data).as_posix()}: changed since it was kept; its other copy is whole, "
        "and the next list kept writes it again"
    ]


def test_a_watched_list_made_after_its_fetch_means_the_clock_isn_t_pacific(data):
    watched("2026-09-27 13:08:38", datetime(2026, 9, 27, 19, 0, tzinfo=UTC))
    (problem,) = by_source()["Card Kingdom"].problems
    assert problem.endswith(
        ".json.zst: made 2026-09-27 20:08 UTC, after it was fetched at 2026-09-27 19:00 UTC: "
        "Card Kingdom's clock isn't Pacific time"
    )


def test_a_watch_log_it_can_t_read_is_named(data):
    three_lists()
    log = watching.log_path("cardkingdom")
    with log.open("a") as f:
        f.write(json.dumps({"result": "unchanged", "list": "singles"}) + "\n")  # a check, not a list
        f.write("{not json\n")
        f.write(json.dumps({"result": "kept", "list": "singles"}) + "\n")
    problems = by_source()["Card Kingdom"].problems
    assert problems == [
        "cardkingdom/watch.jsonl: line 5 isn't JSON",
        "cardkingdom/watch.jsonl: an entry it can't read: {'result': 'kept', 'list': 'singles'}",
    ]


# ---- tcgcsv and Cardmarket kept in runs (0033) --------------------------------------


def test_a_tcgcsv_game_kept_in_its_runs_is_checked_as_its_record_says(data):
    from riffle.ingest import tcgcsv

    day = data / "tcgcsv" / "daily" / "2026-09-27"
    (day / "mtg").mkdir(parents=True)
    (day / "last-updated.txt").write_text("2026-09-27T20:04:59+0000")
    at(day / "last-updated.txt", FETCHED)
    (day / "mtg" / "prices.jsonl.part").write_bytes(
        price_lines((1, "2026-09-27T20:04:00+00:00"), (4, "2026-09-28T20:04:30+00:00"))
    )
    tcgcsv._finish(day / "mtg", datetime(2026, 9, 27, 20, 4, 59, tzinfo=UTC))
    (day / "op").mkdir()
    (day / "op" / "kept.json").write_text("not JSON")
    rep = by_source()["tcgcsv"]
    assert rep.problems == [
        "tcgcsv/daily/2026-09-27/mtg/kept.json: set 4 is from a later refresh, 2026-09-28 20:04 UTC",
        "tcgcsv/daily/2026-09-27/op/kept.json: unreadable",
    ]
    record = json.loads((day / "mtg" / "kept.json").read_text())
    (data / record["file"]).write_bytes(b"damaged")
    (data / record["file"]).with_name("2026-09-27T200459Z.copy.json.zst").unlink()
    problems = by_source()["tcgcsv"].problems
    assert (
        "tcgcsv/lists/mtg/2026-09-27T200459Z/2026-09-27T200459Z.json.zst: changed since it was kept"
        in problems
    )
    assert "tcgcsv/lists/mtg/2026-09-27T200459Z/2026-09-27T200459Z.copy.json.zst: missing" in problems
    assert any(p.startswith("tcgcsv/daily/2026-09-27/mtg/kept.json: unreadable (") for p in problems)


def tcgcsv_days(n: int, last: datetime) -> None:
    for k in range(n):
        made = last - timedelta(days=k)
        folder = data_dir_of() / "tcgcsv" / "daily" / made.date().isoformat()
        folder.mkdir(parents=True)
        (folder / "last-updated.txt").write_text(made.strftime("%Y-%m-%dT%H:%M:%S%z"))
        at(folder / "last-updated.txt", made + timedelta(hours=2))


def data_dir_of():
    from riffle.config import data_dir

    return data_dir()


def test_tcgcsv_s_days_are_judged_for_lateness(data, monkeypatch):
    last = datetime(2026, 9, 27, 20, 5, tzinfo=UTC)
    tcgcsv_days(5, last)
    monkeypatch.setattr(times, "now", lambda: last + timedelta(hours=1))
    assert by_source()["tcgcsv"].notes == ["lateness judged from 14 gaps, 4 so far"]
    tcgcsv_days(11, last - timedelta(days=5))
    assert by_source()["tcgcsv"].notes == ["one every 24h 00m lately"]
    monkeypatch.setattr(times, "now", lambda: last + timedelta(hours=31))
    assert by_source()["tcgcsv"].notes == [
        "late: the last was made 2026-09-27 20:05 UTC, 31h 00m ago,"
        " past 1.25 × its longest gap in 30 days (30h 00m)"
    ]


def cardmarket_guides(games: tuple[str, ...], days: int, last: datetime) -> None:
    """Each game's guide every day, the last made at last, kept in its runs and logged."""
    from riffle.ingest import cardmarket

    for game in games:
        for k in reversed(range(days)):
            made = last - timedelta(days=k)
            kept = runs.keep(cardmarket.lists_dir(game), made, guide(made.strftime("%Y-%m-%dT%H:%M:%S%z")))
            watching.log(
                "cardmarket",
                {"at": runs.name(made + timedelta(minutes=5)), "list": game} | watching.record(kept),
            )


def test_cardmarket_guides_kept_in_runs_are_checked_as_their_log_says(data, monkeypatch):
    last = datetime(2026, 9, 28, 0, 47, 45, tzinfo=UTC)
    cardmarket_guides(("mtg", "fab"), 2, last)
    monkeypatch.setattr(times, "now", lambda: last + timedelta(hours=1))
    rep = by_source()["Cardmarket"]
    assert rep.files == 4 and rep.problems == [] and rep.summary() == "4 guides over 2 days: all right"
    assert rep.notes == ["2 guides: lateness judged from 14 gaps, 1 so far at the fewest"]
    watching.log("cardmarket", {"at": "2026-09-28T010000Z", "list": "op", "result": "kept"})
    assert by_source()["Cardmarket"].problems == [
        "cardmarket/watch.jsonl: an entry it can't read:"
        " {'at': '2026-09-28T010000Z', 'list': 'op', 'result': 'kept'}"
    ]


def test_each_cardmarket_game_is_judged_for_lateness_alone(data, monkeypatch):
    last = datetime(2026, 9, 28, 0, 47, 45, tzinfo=UTC)
    cardmarket_guides(("mtg", "fab"), 16, last)
    cardmarket_guides(("op",), 16, last - timedelta(days=2))
    monkeypatch.setattr(times, "now", lambda: last + timedelta(hours=1))
    notes = by_source()["Cardmarket"].notes
    assert notes == [
        "op: late: the last was made 2026-09-26 00:47 UTC, 49h 00m ago,"
        " past 1.25 × its longest gap in 30 days (30h 00m)",
        "3 guides judged for lateness",
    ]
