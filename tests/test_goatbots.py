"""GoatBots MTGO prices: the latest day kept once, card definitions after it, and every yearly
archive GoatBots still has."""

import io
import json
import os
import zipfile
from datetime import UTC, date, datetime

import pytest

from riffle import net
from riffle.ingest import goatbots

NEW, OLD = goatbots.BASES
DAY = date(2026, 9, 27)
DEFS = {"348": {"name": "Black Lotus", "cardset": "1E", "rarity": "Rare", "foil": 1}}


def zipped(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, body in files.items():
            zf.writestr(name, body)
    return buf.getvalue()


def latest(day: date = DAY, prices: dict | None = None) -> bytes:
    prices = {"348": 419.99, "47483": 0.02} if prices is None else prices
    return zipped({f"price-history-{day.isoformat()}.txt": json.dumps(prices).encode()})


def definitions(cards: dict | None = None) -> bytes:
    return zipped({"card-definitions.txt": json.dumps(DEFS if cards is None else cards).encode()})


def archive(year: int, days: int = 3, whole: bool = True) -> bytes:
    """A year's archive: its first days, and Dec 31 when it's whole."""
    names = [f"price-history-{year}-01-{n:02d}.txt" for n in range(1, days + 1)]
    return zipped(
        {name: b'{"348": 400.0}' for name in [*names, *[f"price-history-{year}-12-31.txt"] * whole]}
    )


class Source:
    """GoatBots as a download: url -> body, None (404), or an exception to raise. Records every
    url asked for."""

    def __init__(self, answers: dict[str, bytes | None | Exception]):
        self.answers = answers
        self.asked: list[str] = []

    def download(self, url, dest, progress=None):
        self.asked.append(url)
        body = self.answers.get(url)
        if isinstance(body, Exception):
            raise body
        if body is None:
            return None
        dest.write_bytes(body)
        if progress:
            progress(len(body), len(body))
        return len(body)


def answers(day: date = DAY, **overrides):
    found: dict[str, bytes | None | Exception] = {
        f"{NEW}/{goatbots.LATEST}": latest(day),
        f"{NEW}/{goatbots.DEFINITIONS}": definitions(),
        f"{NEW}/price-history-2026.zip": archive(2026, days=5, whole=False),
        f"{NEW}/price-history-2025.zip": archive(2025),
        f"{NEW}/price-history-2024.zip": archive(2024),
    }
    found.update(overrides)
    return found


def run(source: Source, tracker=None, today: date | None = None) -> goatbots.Snapshot:
    if tracker is None:
        return goatbots.snapshot(download=source.download, today=today)
    return goatbots.snapshot(download=source.download, tracker=tracker, today=today)


TODAY = date(2026, 9, 28)  # UTC


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setattr(goatbots.times, "today", lambda: TODAY)
    monkeypatch.setattr(goatbots.times, "now", lambda: datetime(2026, 9, 28, 13, 0, tzinfo=UTC))
    return tmp_path / "riffle" / "goatbots"


def test_the_first_run_keeps_the_day_the_cards_and_every_year_goatbots_has(data_dir, tracker):
    source = Source(answers())
    snap = run(source, tracker)
    assert snap.day == DAY
    assert snap.kept == [
        "daily/2026-09-27.zip",
        "card-definitions.zip",
        "yearly/2026-partial.zip",
        "yearly/2025.zip",
        "yearly/2024.zip",
    ]
    assert (data_dir / "daily" / "2026-09-27.zip").read_bytes() == latest()  # as returned
    assert (data_dir / "card-definitions.zip").read_bytes() == definitions()
    assert (data_dir / "yearly" / "2025.zip").read_bytes() == archive(2025)
    assert (data_dir / "yearly" / "2023.none").read_text() == "2026-09-28\n"  # GoatBots had nothing older
    outcomes = tracker.outcomes()
    assert outcomes["GoatBots prices"] == ("ok", "kept 2026-09-27, 2 prices")
    assert outcomes["GoatBots cards"] == ("ok", "1 card")
    assert outcomes["GoatBots 2026"][1].startswith("kept 5 days, ")
    assert outcomes["GoatBots 2023"] == ("ok", "none from GoatBots; asked again from 2026-10-05")
    assert tracker.steps[0].updates  # the download's progress reached the step
    assert not [p for p in data_dir.rglob("*") if p.name.endswith((".new", ".part"))]


def test_the_same_day_again_costs_one_download(data_dir, tracker):
    run(Source(answers()))
    source = Source(answers())
    snap = run(source, tracker)
    assert source.asked == [f"{NEW}/{goatbots.LATEST}"]  # no cards, no years, not even 2023's
    assert snap.kept == []
    assert tracker.outcomes() == {"GoatBots prices": ("ok", "already have 2026-09-27")}


def test_a_new_day_brings_new_card_definitions(data_dir):
    run(Source(answers()))
    nxt = date(2026, 9, 28)
    source = Source(
        answers(nxt, **{f"{NEW}/{goatbots.DEFINITIONS}": definitions({**DEFS, "1": {"name": "X"}})})
    )
    snap = run(source)
    assert snap.kept == ["daily/2026-09-28.zip", "card-definitions.zip"]
    with zipfile.ZipFile(data_dir / "card-definitions.zip") as zf:
        assert len(json.loads(zf.read("card-definitions.txt"))) == 2


def test_an_empty_price_file_keeps_nothing_and_is_asked_again(data_dir, tracker):
    run(Source(answers(**{f"{NEW}/{goatbots.LATEST}": latest(prices={})})), tracker)
    assert tracker.outcomes()["GoatBots prices"] == (
        "ok",
        "empty price file, nothing kept; asked again next run",
    )
    assert not (data_dir / "daily" / "2026-09-27.zip").exists()
    snap = run(Source(answers()))
    assert "daily/2026-09-27.zip" in snap.kept
    assert json.loads((data_dir.parent / "empty-answers.json").read_text()) == {}


def test_empty_card_definitions_dont_replace_the_kept_ones(data_dir, tracker):
    run(Source(answers()))
    nxt = date(2026, 9, 28)
    run(Source(answers(nxt, **{f"{NEW}/{goatbots.DEFINITIONS}": definitions({})})), tracker)
    assert tracker.outcomes()["GoatBots cards"] == (
        "ok",
        "empty card definitions, nothing kept; asked again next run",
    )
    with zipfile.ZipFile(data_dir / "card-definitions.zip") as zf:
        assert json.loads(zf.read("card-definitions.txt")) == DEFS  # the kept ones stay
    snap = run(Source(answers(nxt)))  # still behind the newest day: asked again
    assert snap.kept == ["card-definitions.zip"]


def test_once_a_year_is_over_its_whole_archive_is_kept_beside_the_partial_one(data_dir):
    run(Source(answers()))
    january = date(2027, 1, 2)
    source = Source(answers(january, **{f"{NEW}/price-history-2026.zip": archive(2026, days=31)}))
    snap = run(source)
    assert "yearly/2026.zip" in snap.kept
    assert (data_dir / "yearly" / "2026-partial.zip").read_bytes() == archive(2026, days=5, whole=False)
    assert f"{NEW}/price-history-2027.zip" in source.asked  # asked, not there yet
    assert not (data_dir / "yearly" / "2027.none").exists()  # so asked again next run
    assert f"{NEW}/price-history-2025.zip" not in source.asked


def test_the_old_download_path_is_tried_on_404(data_dir):
    moved = {f"{NEW}/{goatbots.LATEST}": None, f"{OLD}/{goatbots.LATEST}": latest()}
    source = Source(answers(**moved))
    snap = run(source)
    assert "daily/2026-09-27.zip" in snap.kept
    assert source.asked[:2] == [f"{NEW}/{goatbots.LATEST}", f"{OLD}/{goatbots.LATEST}"]


def test_a_refused_download_fails_its_step_and_the_years_still_run(data_dir, tracker):
    source = Source(answers(**{f"{NEW}/{goatbots.LATEST}": net.FetchError("HTTP 403")}))
    snap = run(source, tracker, today=DAY)
    assert f"{OLD}/{goatbots.LATEST}" not in source.asked  # only a 404 tries the old path
    assert tracker.outcomes()["GoatBots prices"] == ("fail", "HTTP 403")
    assert "GoatBots cards" not in tracker.outcomes()
    assert snap.day is None and "yearly/2025.zip" in snap.kept


def test_missing_everywhere_fails_the_day(data_dir, tracker):
    run(Source(answers(**{f"{NEW}/{goatbots.LATEST}": None})), tracker, today=DAY)
    assert tracker.outcomes()["GoatBots prices"] == ("fail", f"{goatbots.LATEST}: HTTP 404")


@pytest.mark.parametrize(
    ("body", "why"),
    [
        (b"<html>busy</html>", "not a zip"),
        (zipped({"readme.txt": b"hi"}), "no price-history-<day>.txt in it, only readme.txt"),
        (
            zipped({"price-history-2026-09-27.txt": b"<html>"}),
            "price-history-2026-09-27.txt isn't the expected JSON",
        ),
        (latest(prices={"348": "cheap"}), "price-history-2026-09-27.txt isn't MTGO IDs and prices"),
        (latest(prices={"348": True}), "price-history-2026-09-27.txt isn't MTGO IDs and prices"),
    ],
)
def test_a_zip_that_isnt_a_price_file_keeps_nothing(data_dir, tracker, body, why):
    run(Source(answers(**{f"{NEW}/{goatbots.LATEST}": body})), tracker, today=DAY)
    assert tracker.outcomes()["GoatBots prices"] == ("fail", f"{goatbots.LATEST}: {why}")
    assert not list((data_dir / "daily").iterdir())


def test_bad_card_definitions_are_retried_with_the_next_run(data_dir, tracker):
    run(
        Source(answers(**{f"{NEW}/{goatbots.DEFINITIONS}": zipped({"card-definitions.txt": b"[]"})})), tracker
    )
    assert tracker.outcomes()["GoatBots cards"] == (
        "fail",
        f"{goatbots.DEFINITIONS}: card-definitions.txt isn't MTGO IDs and cards",
    )
    assert not (data_dir / "card-definitions.zip").exists()
    snap = run(Source(answers()))  # same day, but the definitions are still missing
    assert snap.kept == ["card-definitions.zip"]


def test_card_definitions_without_their_file_fail(data_dir, tracker):
    run(Source(answers(**{f"{NEW}/{goatbots.DEFINITIONS}": zipped({"x.txt": b"{}"})})), tracker)
    assert tracker.outcomes()["GoatBots cards"] == (
        "fail",
        f"{goatbots.DEFINITIONS}: no card-definitions.txt in it, only x.txt",
    )


def test_a_failed_year_stops_the_walk_and_is_retried(data_dir, tracker):
    source = Source(answers(**{f"{NEW}/price-history-2025.zip": net.FetchError("HTTP 503")}))
    run(source, tracker)
    assert tracker.outcomes()["GoatBots 2025"] == ("fail", "HTTP 503")
    assert f"{NEW}/price-history-2024.zip" not in source.asked
    snap = run(Source(answers()))
    assert snap.kept == ["yearly/2025.zip", "yearly/2024.zip"]


def damage(body: bytes, at: int) -> bytes:
    """body with the byte `at` bytes into its first file's compressed data flipped."""
    with zipfile.ZipFile(io.BytesIO(body)) as zf:
        first = zf.infolist()[0]
    start = first.header_offset + 30 + len(first.filename.encode()) + len(first.extra)
    broken = bytearray(body)
    broken[start + at] ^= 0xFF
    return bytes(broken)


def test_an_archive_of_another_year_is_not_kept(data_dir, tracker):
    source = Source(answers(**{f"{NEW}/price-history-2026.zip": archive(2025, whole=False)}))
    run(source, tracker)
    assert tracker.outcomes()["GoatBots 2026"] == (
        "fail",
        "price-history-2026.zip: no price-history-2026-<month>-<day>.txt in it, only "
        "price-history-2025-01-01.txt, price-history-2025-01-02.txt, price-history-2025-01-03.txt",
    )
    assert not (data_dir / "yearly" / "2026-partial.zip").exists()
    assert f"{NEW}/price-history-2025.zip" not in source.asked  # the walk stopped


@pytest.mark.parametrize("at", range(8))
def test_a_damaged_archive_is_not_kept(data_dir, tracker, at):
    run(
        Source(answers(**{f"{NEW}/price-history-2025.zip": damage(archive(2025, days=1, whole=False), at)})),
        tracker,
    )
    outcome = tracker.outcomes()["GoatBots 2025"]
    assert outcome is not None and outcome[0] == "fail" and "damaged" in outcome[1]
    assert not (data_dir / "yearly" / "2025.zip").exists()


@pytest.mark.parametrize("at", range(4))
def test_a_damaged_price_file_is_not_kept(data_dir, tracker, at):
    run(Source(answers(**{f"{NEW}/{goatbots.LATEST}": damage(latest(), at)})), tracker, today=DAY)
    outcome = tracker.outcomes()["GoatBots prices"]
    assert outcome is not None and outcome[0] == "fail"
    assert not list((data_dir / "daily").iterdir())


def test_an_oversized_price_file_is_refused(data_dir, tracker, monkeypatch):
    monkeypatch.setattr(goatbots, "MAX_ENTRY", 10)
    run(Source(answers()), tracker)
    assert tracker.outcomes()["GoatBots prices"] == (
        "fail",
        f"{goatbots.LATEST}: price-history-2026-09-27.txt unpacks to 30 bytes, too big to be what it claims",
    )


def test_price_days_read_names_in_folders_and_skip_others():
    body = zipped(
        {
            "prices/price-history-2026-01-02.txt": b"{}",
            "price-history-2026-02-30.txt": b"{}",
            "price-history-2026-01-03.json": b"{}",
        }
    )
    with zipfile.ZipFile(io.BytesIO(body)) as zf:
        assert goatbots.price_days(zf) == {date(2026, 1, 2): "prices/price-history-2026-01-02.txt"}


def recorded(body: bytes, **fields: int) -> bytes:
    """body with its first entry's central-directory fields set: method (compression),
    flags (bit 0 is encryption)."""
    with zipfile.ZipFile(io.BytesIO(body)) as zf:
        first = zf.infolist()[0]
    at = body.index(b"PK\x01\x02")  # the first central-directory record
    broken = bytearray(body)
    for name, value in fields.items():
        offset = {"flags": 8, "method": 10}[name]
        broken[at + offset : at + offset + 2] = value.to_bytes(2, "little")
    assert first.filename  # the record belongs to a real entry
    return bytes(broken)


@pytest.mark.parametrize("fields", [{"method": 99}, {"flags": 1}, {"method": 14}])
def test_a_zip_zipfile_cant_read_fails_cleanly_everywhere(data_dir, tracker, fields):
    source = Source(
        answers(
            **{
                f"{NEW}/{goatbots.LATEST}": recorded(latest(), **fields),
                f"{NEW}/price-history-2026.zip": recorded(archive(2026, whole=False), **fields),
            }
        )
    )
    run(source, tracker, today=DAY)  # no crash
    outcomes = tracker.outcomes()
    assert outcomes["GoatBots prices"] is not None and outcomes["GoatBots prices"][0] == "fail"
    assert outcomes["GoatBots 2026"] is not None and outcomes["GoatBots 2026"][0] == "fail"
    assert not [p for p in data_dir.rglob("*") if p.name.endswith((".new", ".part"))]
    snap = run(
        Source(answers(**{f"{NEW}/{goatbots.DEFINITIONS}": recorded(definitions(), **fields)})), tracker
    )
    assert tracker.outcomes()["GoatBots cards"][0] == "fail"
    assert "card-definitions.zip" not in snap.kept


def test_card_definitions_that_failed_are_retried_while_behind_the_newest_day(data_dir, tracker):
    run(Source(answers()))
    nxt = date(2026, 9, 28)
    run(Source(answers(nxt, **{f"{NEW}/{goatbots.DEFINITIONS}": net.FetchError("HTTP 503")})), tracker)
    assert tracker.outcomes()["GoatBots cards"] == ("fail", "HTTP 503")
    assert goatbots.definitions_due()  # the old copy is behind the new day
    snap = run(Source(answers(nxt)))  # same day, nothing new, but the definitions are behind
    assert snap.kept == ["card-definitions.zip"]
    assert not goatbots.definitions_due()


def test_card_definitions_missing_everywhere_fail(data_dir, tracker):
    run(Source(answers(**{f"{NEW}/{goatbots.DEFINITIONS}": None})), tracker)
    assert tracker.outcomes()["GoatBots cards"] == ("fail", f"{goatbots.DEFINITIONS}: HTTP 404")


def test_card_definitions_without_names_are_refused(data_dir, tracker):
    run(Source(answers(**{f"{NEW}/{goatbots.DEFINITIONS}": definitions({"1": {"cardset": "X"}})})), tracker)
    assert tracker.outcomes()["GoatBots cards"] == (
        "fail",
        f"{goatbots.DEFINITIONS}: card-definitions.txt isn't MTGO IDs and cards",
    )


def test_a_latest_zip_with_two_days_is_the_later_one(data_dir):
    body = zipped(
        {
            "price-history-2026-09-26.txt": b'{"1": 1.0}',
            "price-history-2026-09-27.txt": b'{"1": 1.0, "2": 2.0}',
        }
    )
    snap = run(Source(answers(**{f"{NEW}/{goatbots.LATEST}": body})))
    assert snap.day == DAY and "daily/2026-09-27.zip" in snap.kept


def test_a_404_for_a_year_partly_kept_fails_and_is_retried(data_dir, tracker):
    run(Source(answers()))  # 2026's partial archive kept
    january = date(2027, 1, 2)
    source = Source(answers(january, **{f"{NEW}/price-history-2026.zip": None}))
    run(source, tracker)
    assert tracker.outcomes()["GoatBots 2026"] == (
        "fail",
        "price-history-2026.zip: HTTP 404, though a partial one is kept",
    )
    assert not (data_dir / "yearly" / "2026.none").exists()
    assert (data_dir / "yearly" / "2026-partial.zip").exists()
    snap = run(Source(answers(january, **{f"{NEW}/price-history-2026.zip": archive(2026, days=31)})))
    assert "yearly/2026.zip" in snap.kept and (data_dir / "yearly" / "2026-partial.zip").exists()


def test_a_failed_year_leaves_no_download_behind(data_dir):
    run(Source(answers(**{f"{NEW}/price-history-2025.zip": b"not a zip"})))
    assert not [p for p in (data_dir / "yearly").iterdir() if p.name.endswith((".new", ".part"))]
    assert not (data_dir / "yearly" / "2025.zip").exists()


# ---- whole years, and years GoatBots has none for ----------------------------------------


@pytest.mark.parametrize(
    ("whole_year", "lacks", "new"),
    [
        (archive(2026, days=31, whole=False), "2026-12-31", "26 days"),  # Jan 6-31
        (
            archive(2026, days=0),
            "2026-01-01, 2026-01-02, 2026-01-03, and 2 more days",
            "1 day",
        ),  # the partial's
    ],
)
def test_a_whole_year_that_came_short_is_kept_beside_the_partial_not_as_the_year(
    data_dir, tracker, whole_year, lacks, new
):
    run(Source(answers()))  # 2026's partial archive: Jan 1-5
    january = date(2027, 1, 2)
    snap = run(Source(answers(january, **{f"{NEW}/price-history-2026.zip": whole_year})), tracker)
    short = "2026-short-2026-09-28T130000Z.zip"
    assert tracker.outcomes()["GoatBots 2026"] == (
        "ok",
        f"lacks {lacks}; kept as {short} for {new} not kept before; the whole year asked again next run",
    )
    assert f"yearly/{short}" in snap.kept and (data_dir / "yearly" / short).read_bytes() == whole_year
    assert (
        not (data_dir / "yearly" / "2026.zip").exists()
        and (data_dir / "yearly" / "2026-partial.zip").exists()
    )
    assert not [p for p in (data_dir / "yearly").iterdir() if p.name.endswith(".new")]


def test_a_whole_year_short_seven_runs_in_a_row_warns_until_it_comes_whole(data_dir, tracker):
    run(Source(answers()))
    january = date(2027, 1, 2)
    short = Source(answers(january, **{f"{NEW}/price-history-2026.zip": archive(2026, days=31, whole=False)}))
    for _ in range(7):
        run(short, tracker)
    assert (
        len(list((data_dir / "yearly").glob("2026-short-*.zip"))) == 1
    )  # kept once: the same days again add nothing
    assert tracker.steps[-1].outcome == (
        "warn",
        "lacks 2026-12-31; no day in it not kept before; the whole year asked again next run"
        " (7 runs in a row, since 2026-09-28)",
    )
    whole = Source(answers(january, **{f"{NEW}/price-history-2026.zip": archive(2026, days=31)}))
    assert "yearly/2026.zip" in run(whole).kept
    assert len(list((data_dir / "yearly").glob("2026-*.zip"))) == 2  # the partial and the short one stay
    assert "goatbots/2026" not in json.loads((data_dir.parent / "empty-answers.json").read_text())


def test_a_year_goatbots_had_none_for_is_asked_again_a_week_later(data_dir, monkeypatch):
    run(Source(answers()))  # 2023.none, 2026-09-28
    monkeypatch.setattr(goatbots.times, "today", lambda: date(2026, 10, 4))
    source = Source(answers())
    run(source)
    assert f"{NEW}/price-history-2023.zip" not in source.asked
    monkeypatch.setattr(goatbots.times, "today", lambda: date(2026, 10, 5))
    source = Source(answers(**{f"{NEW}/price-history-2023.zip": archive(2023)}))
    snap = run(source)
    assert f"{NEW}/price-history-2023.zip" in source.asked and "yearly/2023.zip" in snap.kept
    assert not (data_dir / "yearly" / "2023.none").exists()
    assert (data_dir / "yearly" / "2022.none").read_text() == "2026-10-05\n"  # the walk goes on to the next


def test_a_none_file_written_before_its_day_was_goes_by_its_own_time(data_dir):
    yearly = data_dir / "yearly"
    run(Source(answers()))
    (yearly / "2023.none").write_text("")
    seen = datetime(2026, 9, 27, 20, 51, tzinfo=UTC).timestamp()
    os.utime(yearly / "2023.none", (seen, seen))
    assert goatbots._none_since(yearly / "2023.none") == date(2026, 9, 27)  # asked again from 2026-10-04
