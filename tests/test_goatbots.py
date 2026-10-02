"""GoatBots MTGO prices: every day's price file kept by the run rule, asked for when the next
is due (riffle.cadence), the card definitions the same way, and every yearly archive GoatBots
still has."""

import dataclasses
import hashlib
import io
import json
import os
import zipfile
from datetime import UTC, date, datetime, time, timedelta

import pytest

from riffle import locks, net, runs, watching
from riffle.ingest import goatbots

NEW, OLD = goatbots.BASES
DAY = date(2026, 9, 27)
DEFS = {"348": {"name": "Black Lotus", "cardset": "1E", "rarity": "Rare", "foil": 1}}
NOW = datetime(2026, 9, 28, 13, 0, tzinfo=UTC)  # the 07:00 sync in Denver
PRICES = {"348": 419.99, "47483": 0.02}


def zipped(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, body in files.items():
            zf.writestr(zipfile.ZipInfo(name, date_time=(2026, 9, 28, 5, 15, 20)), body)
    return buf.getvalue()


def price_file(prices: dict | None = None) -> bytes:
    return json.dumps(PRICES if prices is None else prices).encode()


def latest(day: date = DAY, prices: dict | None = None) -> bytes:
    return zipped({f"price-history-{day.isoformat()}.txt": price_file(prices)})


def definitions(cards: dict | None = None) -> bytes:
    return zipped({"card-definitions.txt": json.dumps(DEFS if cards is None else cards).encode()})


def archive(year: int, days: int = 3, whole: bool = True) -> bytes:
    """A year's archive: its first days, and Dec 31 when it's whole."""
    names = [f"price-history-{year}-01-{n:02d}.txt" for n in range(1, days + 1)]
    return zipped(
        {name: b'{"348": 400.0}' for name in [*names, *[f"price-history-{year}-12-31.txt"] * whole]}
    )


def made(day: date) -> datetime:
    """When GoatBots makes a day's zip: 03:15:20 UTC the next day."""
    return datetime.combine(day + timedelta(1), time(3, 15, 20), UTC)


AUTO = object()


class Source:
    """GoatBots as a download and as net.fetch_new: url -> body, None (404), or an exception to
    raise, each body's ETag its hash, its Last-Modified when its newest price file's day was
    made (or `modified`). Records every url asked for, and the ETags sent."""

    def __init__(self, answers: dict[str, bytes | None | Exception], modified=AUTO, tagged: bool = True):
        self.answers = answers
        self.asked: list[str] = []
        self.etags: list[str | None] = []
        self.modified = modified
        self.tagged = tagged

    def _answer(self, url: str) -> bytes | None:
        self.asked.append(url)
        body = self.answers.get(url)
        if isinstance(body, Exception):
            raise body
        return body

    def download(self, url, dest, progress=None):
        body = self._answer(url)
        if body is None:
            return None
        dest.write_bytes(body)
        if progress:
            progress(len(body), len(body))
        return len(body)

    def _stamp(self, body: bytes) -> datetime | None:
        if self.modified is not AUTO:
            return self.modified  # type: ignore[return-value]
        try:
            with zipfile.ZipFile(io.BytesIO(body)) as zf:
                return made(max(goatbots.price_days(zf), default=DAY))
        except (zipfile.BadZipFile, ValueError):
            return made(DAY)

    def fetch(self, url, dest, known, etag=None, accept="*/*", progress=None, missing=(404,)):
        self.etags.append(etag)
        body = self._answer(url)
        if body is None:
            return None
        tag = f'"{hash(body)}"'
        if etag == tag:
            return net.Fetched("unchanged", b"", etag)
        assert accept == "application/zip" and not known(body[: net.HEAD])
        dest.write_bytes(body)
        if progress:
            progress(len(body), None)
        return net.Fetched(
            "new", body[: net.HEAD], tag if self.tagged else None, len(body), self._stamp(body)
        )


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


def watch(source: Source, tracker=None, now: datetime = NOW, always: bool = True) -> watching.Watch:
    kw = {} if tracker is None else {"tracker": tracker}
    return goatbots.watch(fetch=source.fetch, clock=lambda: now, always=always, **kw)


def run(source: Source, tracker=None, today: date | None = None) -> goatbots.Snapshot:
    """What the sync does: the watch once, then the years."""
    watch(source, tracker)
    if tracker is None:
        return goatbots.snapshot(download=source.download, today=today)
    return goatbots.snapshot(download=source.download, tracker=tracker, today=today)


def kept(data_dir) -> dict:
    return runs.kept(data_dir / "lists" / "prices")


def cards(data_dir) -> dict:
    return runs.kept(data_dir / "lists" / "cards")


def logged(data_dir) -> list[dict]:
    return [json.loads(line) for line in (data_dir / "watch.jsonl").read_text().splitlines()]


TODAY = date(2026, 9, 28)  # UTC


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setattr(goatbots.times, "today", lambda: TODAY)
    monkeypatch.setattr(goatbots.times, "now", lambda: NOW)
    monkeypatch.setattr(goatbots, "LISTS", (goatbots.LIST,))  # the cards' tests set them back
    return tmp_path / "riffle" / "goatbots"


def test_the_first_run_keeps_the_day_and_every_year_goatbots_has(data_dir, tracker):
    source = Source(answers())
    snap = run(source, tracker)
    assert snap.day == DAY
    assert snap.kept == [
        "yearly/2026-partial.zip",
        "yearly/2025.zip",
        "yearly/2024.zip",
    ]
    lists = kept(data_dir)
    assert list(lists) == ["2026-09-28T031520Z"]  # the zip's Last-Modified
    assert runs.rebuild(lists["2026-09-28T031520Z"]) == price_file()  # the price file's own bytes
    assert (data_dir / "yearly" / "2025.zip").read_bytes() == archive(2025)
    assert (data_dir / "yearly" / "2023.none").read_text() == "2026-09-28\n"  # GoatBots had nothing older
    outcomes = tracker.outcomes()
    size = watching.size(lists["2026-09-28T031520Z"].stat().st_size)
    assert outcomes["GoatBots prices"] == (
        "ok",
        f"kept 2026-09-27's list, made 2026-09-28 03:15 UTC, 2 prices: a new run, {size} kept twice",
    )
    assert outcomes["GoatBots 2026"][1].startswith("kept 5 days, ")
    assert outcomes["GoatBots 2023"] == ("ok", "none from GoatBots; asked again from 2026-10-05")
    assert tracker.steps[0].unit == "bytes" and tracker.steps[0].updates  # the download's progress
    assert not [p for p in data_dir.rglob("*") if p.name.endswith((".new", ".part"))]
    entry = logged(data_dir)[0]
    assert entry == entry | {
        "at": "2026-09-28T130000Z",
        "list": "prices",
        "result": "kept",
        "made": "2026-09-28T031520Z",
        "kind": "base",
        "file": "goatbots/lists/prices/2026-09-28T031520Z/2026-09-28T031520Z.json.zst",
        "day": "2026-09-27",
        "entry": "price-history-2026-09-27.txt",
        "entry_time": "2026-09-28T05:15:20",  # Central European local time, as the zip says it
        "served_sha256": hashlib.sha256(latest()).hexdigest(),
        "served_size": len(latest()),
        "etag": f'"{hash(latest())}"',
    }
    assert "seconds" in entry


def test_the_same_day_again_is_a_304_and_nothing_more(data_dir, tracker):
    run(Source(answers()))
    source = Source(answers())
    snap = run(source, tracker)
    assert source.asked == [f"{NEW}/{goatbots.LATEST}"]  # no cards, no years, not even 2023's
    assert source.etags == [f'"{hash(latest())}"']
    assert snap.kept == []
    assert tracker.outcomes() == {"GoatBots prices": ("ok", "no new list since the last one kept")}
    assert logged(data_dir)[-1]["result"] == "unchanged"


def test_the_next_day_is_kept_as_a_difference(data_dir, tracker):
    run(Source(answers()))
    nxt = DAY + timedelta(1)
    res = watch(
        Source(answers(nxt, **{f"{NEW}/{goatbots.LATEST}": latest(nxt, {**PRICES, "1": 0.5})})), tracker
    )
    assert res.kept == ["GoatBots prices"]
    lists = kept(data_dir)
    assert list(lists) == ["2026-09-28T031520Z", "2026-09-29T031520Z"]
    assert lists["2026-09-29T031520Z"].name.endswith(".diff.zst")
    assert runs.rebuild(lists["2026-09-29T031520Z"]) == price_file({**PRICES, "1": 0.5})
    assert tracker.outcomes()["GoatBots prices"][1].startswith(
        "kept 2026-09-28's list, made 2026-09-29 03:15 UTC, 3 prices: a difference of "
    )
    assert goatbots.newest_day() == nxt


def test_an_empty_price_file_keeps_nothing_and_is_asked_again(data_dir, tracker):
    source = Source(answers(**{f"{NEW}/{goatbots.LATEST}": latest(prices={})}))
    res = watch(source, tracker)
    assert res.empty == ["GoatBots prices"]
    assert tracker.outcomes()["GoatBots prices"] == (
        "ok",
        "empty price file, nothing kept; asked again next run",
    )
    assert not kept(data_dir) and watching.load_tags("goatbots") == {}  # so the next run asks again
    run(Source(answers()))
    assert list(kept(data_dir)) == ["2026-09-28T031520Z"]
    assert json.loads((data_dir.parent / "empty-answers.json").read_text()) == {}


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
    res = watch(source)
    assert res.kept == ["GoatBots prices"]
    assert source.asked == [f"{NEW}/{goatbots.LATEST}", f"{OLD}/{goatbots.LATEST}"]


def test_a_refused_download_fails_its_step_and_the_years_still_run(data_dir, tracker):
    source = Source(answers(**{f"{NEW}/{goatbots.LATEST}": net.FetchError("HTTP 403")}))
    snap = run(source, tracker, today=DAY)
    assert f"{OLD}/{goatbots.LATEST}" not in source.asked  # only a 404 tries the old path
    assert tracker.outcomes()["GoatBots prices"] == ("fail", "HTTP 403")
    assert "GoatBots cards" not in tracker.outcomes()
    assert snap.day is None and "yearly/2025.zip" in snap.kept
    assert logged(data_dir)[0] | {"seconds": 0} == {
        "at": "2026-09-28T130000Z",
        "list": "prices",
        "result": "failed",
        "why": "HTTP 403",
        "seconds": 0,
    }


def test_missing_everywhere_fails_the_day(data_dir, tracker):
    res = watch(Source(answers(**{f"{NEW}/{goatbots.LATEST}": None})), tracker)
    assert res.failed == [("GoatBots prices", f"{goatbots.LATEST}: HTTP 404")]
    assert tracker.outcomes()["GoatBots prices"] == ("fail", f"{goatbots.LATEST}: HTTP 404")


@pytest.mark.parametrize(
    ("body", "why"),
    [
        (b"<html>busy</html>", "not a zip"),
        (
            zipped({"price-history-2026-09-27.txt": b"<html>"}),
            "price-history-2026-09-27.txt isn't the expected JSON",
        ),
        (latest(prices={"348": "cheap"}), "price-history-2026-09-27.txt isn't MTGO IDs and prices"),
        (latest(prices={"348": True}), "price-history-2026-09-27.txt isn't MTGO IDs and prices"),
    ],
)
def test_a_zip_that_isnt_a_price_file_is_set_aside_once_a_publish(data_dir, tracker, body, why):
    watch(Source(answers(**{f"{NEW}/{goatbots.LATEST}": body})), tracker)
    aside = "goatbots/aside/price-history-2026-09-28T130000Z.zip"
    said = f"{goatbots.LATEST}: {why}; set aside as {aside}, asked again next run"
    assert tracker.outcomes()["GoatBots prices"] == ("fail", said)
    assert (data_dir.parent / aside).read_bytes() == body
    assert not kept(data_dir) and watching.load_tags("goatbots") == {}
    watch(Source(answers(**{f"{NEW}/{goatbots.LATEST}": body})), tracker, now=NOW + timedelta(minutes=5))
    assert "a copy of this publish is set aside already" in tracker.outcomes()["GoatBots prices"][1]
    assert len(list((data_dir / "aside").iterdir())) == 1


def test_a_zip_with_no_price_file_is_set_aside_and_fails(data_dir, tracker):
    body = zipped({"readme.txt": b"hi"})
    watch(Source(answers(**{f"{NEW}/{goatbots.LATEST}": body})), tracker)
    aside = "goatbots/aside/price-history-2026-09-28T130000Z.zip"
    assert tracker.outcomes()["GoatBots prices"] == (
        "fail",
        f"{goatbots.LATEST}: no price-history-<day>.txt in it, only readme.txt"
        f"; set aside as {aside}, asked again next run",
    )
    assert (data_dir.parent / aside).read_bytes() == body and not kept(data_dir)


def test_a_zip_with_no_last_modified_is_set_aside_and_fails(data_dir, tracker):
    watch(Source(answers(), modified=None), tracker)
    aside = "goatbots/aside/price-history-2026-09-28T130000Z.zip"
    assert tracker.outcomes()["GoatBots prices"] == (
        "fail",
        f"{goatbots.LATEST}: no Last-Modified, so no time it was made"
        f"; set aside as {aside}, asked again next run",
    )
    assert (data_dir.parent / aside).read_bytes() == latest()
    assert not kept(data_dir) and watching.load_tags("goatbots") == {}
    watch(Source(answers(), modified=None), tracker, now=NOW + timedelta(minutes=5))  # once a publish
    assert "a copy of this publish is set aside already" in tracker.outcomes()["GoatBots prices"][1]
    assert len(list((data_dir / "aside").iterdir())) == 1


def test_a_list_kept_already_is_had_not_kept_again(data_dir, tracker):
    watch(Source(answers()))
    (data_dir / "watch-etags.json").unlink()  # the ETag lost: the zip comes whole again
    res = watch(Source(answers()), tracker)
    assert res.same == ["GoatBots prices"] and len(kept(data_dir)) == 1
    assert tracker.outcomes()["GoatBots prices"] == (
        "ok",
        "have 2026-09-27's list, made 2026-09-28 03:15 UTC; the same as the one kept",
    )
    assert logged(data_dir)[-1]["result"] == "known"
    assert watching.load_tags("goatbots") == {"prices": f'"{hash(latest())}"'}


def test_a_list_that_fails_to_keep_saves_no_etag(data_dir, tracker, monkeypatch):
    def broken(folder, at, data):
        raise runs.Unverified("the disk is at fault")

    monkeypatch.setattr(goatbots.runs, "keep", broken)
    watch(Source(answers()), tracker)
    assert tracker.outcomes()["GoatBots prices"] == ("fail", "the disk is at fault")
    assert watching.load_tags("goatbots") == {}  # so the next run asks for it whole
    monkeypatch.undo()
    monkeypatch.setenv("XDG_DATA_HOME", str(data_dir.parent.parent))
    monkeypatch.setattr(goatbots, "LISTS", (goatbots.LIST,))
    source = Source(answers())
    watch(source)
    assert source.etags == [None] and len(kept(data_dir)) == 1


def test_a_server_that_sends_no_etag_gets_the_whole_zip_each_time(data_dir, tracker):
    watch(Source(answers(), tagged=False))
    source = Source(answers(), tagged=False)
    res = watch(source, tracker)
    assert source.etags == [None] and res.same == ["GoatBots prices"]
    assert watching.load_tags("goatbots") == {} and len(kept(data_dir)) == 1


def test_a_busy_store_asks_nothing(data_dir, tracker):
    with locks.held(data_dir / "watch.lock") as mine:
        assert mine
        source = Source(answers())
        res = watch(source, tracker)
    assert res.busy and source.asked == []
    assert tracker.outcomes() == {"GoatBots": ("ok", "another run is asking for its lists")}


def test_until_its_schedule_is_learned_the_watch_asks_at_every_firing(data_dir, tracker):
    watch(Source(answers()))
    source = Source(answers())
    res = watch(source, tracker, now=NOW + timedelta(minutes=4), always=False)  # a firing come early
    assert source.asked == [f"{NEW}/price-history.zip"] and res.waiting == []
    assert tracker.outcomes()["GoatBots prices"] == (
        "ok",
        "no new list since the last one kept; asked at every firing until it has 14 gaps, 0 so far",
    )


def test_newest_day_counts_the_daily_zips_kept_before(data_dir):
    daily = data_dir / "daily"
    daily.mkdir(parents=True)
    for name in ("2026-09-26.zip", "notes.zip"):
        (daily / name).write_bytes(b"")
    assert goatbots.newest_day() == date(2026, 9, 26)


def test_no_list_kept_means_no_newest_day(data_dir):
    assert goatbots.newest_day() is None


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
    (aside,) = (data_dir / "aside").iterdir()  # named by when the sync ran
    assert tracker.outcomes()["GoatBots 2026"] == (
        "fail",
        "price-history-2026.zip: no price-history-2026-<month>-<day>.txt in it, only "
        "price-history-2025-01-01.txt, price-history-2025-01-02.txt, price-history-2025-01-03.txt"
        f"; set aside as goatbots/aside/{aside.name}, asked again next run",
    )
    assert aside.read_bytes() == archive(2025, whole=False)
    assert not (data_dir / "yearly" / "2026-partial.zip").exists()
    run(Source(answers(**{f"{NEW}/price-history-2026.zip": archive(2025, whole=False)})), tracker)
    assert "a copy of this publish is set aside already" in tracker.outcomes()["GoatBots 2026"][1]
    assert len(list((data_dir / "aside").iterdir())) == 1  # the same bytes: kept once
    assert logged(data_dir)[-1]["list"] == "price-history-2026.zip" and logged(data_dir)[-1]["not_kept"]
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
    watch(Source(answers(**{f"{NEW}/{goatbots.LATEST}": damage(latest(), at)})), tracker)
    outcome = tracker.outcomes()["GoatBots prices"]
    assert outcome is not None and outcome[0] == "fail"
    assert not kept(data_dir)


def test_an_oversized_price_file_is_refused(data_dir, tracker, monkeypatch):
    monkeypatch.setattr(goatbots, "MAX_ENTRY", 10)
    watch(Source(answers()), tracker)
    assert tracker.outcomes()["GoatBots prices"] == (
        "fail",
        f"{goatbots.LATEST}: price-history-2026-09-27.txt unpacks to 30 bytes, too big to be what it claims"
        "; set aside as goatbots/aside/price-history-2026-09-28T130000Z.zip, asked again next run",
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
    goatbots.LISTS = goatbots.LIST, goatbots.CARDS  # put back by the fixture's monkeypatch
    watch(Source(answers(**{f"{NEW}/{goatbots.DEFINITIONS}": recorded(definitions(), **fields)})), tracker)
    assert tracker.outcomes()["GoatBots cards"][0] == "fail"
    assert not cards(data_dir)


def test_a_latest_zip_with_two_days_keeps_the_later_and_sets_the_zip_aside(data_dir, tracker):
    body = zipped(
        {
            "price-history-2026-09-26.txt": b'{"1": 1.0}',
            "price-history-2026-09-27.txt": b'{"1": 1.0, "2": 2.0}',
        }
    )
    res = watch(Source(answers(**{f"{NEW}/{goatbots.LATEST}": body})), tracker)
    assert res.kept == ["GoatBots prices"]
    assert runs.rebuild(kept(data_dir)["2026-09-28T031520Z"]) == b'{"1": 1.0, "2": 2.0}'
    aside = "goatbots/aside/price-history-2026-09-28T130000Z.zip"
    outcome = tracker.outcomes()["GoatBots prices"]
    assert outcome[0] == "warn" and outcome[1].endswith(
        f"; the zip also held 2026-09-26; set aside whole as {aside}"
    )
    assert (data_dir.parent / aside).read_bytes() == body
    entry = logged(data_dir)[0]
    assert entry["days"] == ["2026-09-26", "2026-09-27"] and entry["aside"] == aside
    assert goatbots.newest_day() == DAY


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


@pytest.fixture
def with_cards(data_dir, monkeypatch):
    """The card definitions asked for too, as the watch does."""
    monkeypatch.setattr(goatbots, "LISTS", (goatbots.LIST, goatbots.CARDS))
    return data_dir


def test_the_card_definitions_are_kept_by_the_run_rule_under_the_zip_s_last_modified(with_cards, tracker):
    data_dir = with_cards
    res = watch(Source(answers()), tracker)
    assert res.kept == ["GoatBots prices", "GoatBots cards"] and not res.failed
    found = cards(data_dir)
    assert list(found) == ["2026-09-28T031520Z"]
    assert runs.rebuild(found["2026-09-28T031520Z"]) == json.dumps(DEFS).encode()  # the file's own bytes
    size = watching.size(found["2026-09-28T031520Z"].stat().st_size)
    assert tracker.outcomes()["GoatBots cards"] == (
        "ok",
        f"kept the definitions made 2026-09-28 03:15 UTC, 1 card: a new run, {size} kept twice",
    )
    entry = logged(data_dir)[-1]
    assert entry == entry | {
        "list": "cards",
        "result": "kept",
        "made": "2026-09-28T031520Z",
        "day": "2026-09-28",
        "served_sha256": hashlib.sha256(definitions()).hexdigest(),
    }
    assert watching.load_tags("goatbots")["cards"] == f'"{hash(definitions())}"'


def test_new_definitions_are_a_difference_and_the_same_ones_a_304(with_cards, tracker):
    data_dir = with_cards
    watch(Source(answers()))
    more = definitions({**DEFS, "1": {"name": "X"}})
    later = made(DAY) + timedelta(days=1)
    watch(Source(answers(**{f"{NEW}/{goatbots.DEFINITIONS}": more}), modified=later))
    assert list(cards(data_dir)) == ["2026-09-28T031520Z", "2026-09-29T031520Z"]
    assert cards(data_dir)["2026-09-29T031520Z"].name.endswith(".diff.zst")
    source = Source(answers(**{f"{NEW}/{goatbots.DEFINITIONS}": more}), modified=later)
    res = watch(source, tracker)
    assert tracker.outcomes()["GoatBots cards"] == ("ok", "no new definitions since the last kept")
    assert "GoatBots cards" in res.same and len(cards(data_dir)) == 2


def test_empty_card_definitions_are_kept_nowhere_and_asked_again(with_cards, tracker):
    data_dir = with_cards
    res = watch(Source(answers(**{f"{NEW}/{goatbots.DEFINITIONS}": definitions({})})), tracker)
    assert res.empty == ["GoatBots cards"] and not cards(data_dir)
    assert tracker.outcomes()["GoatBots cards"] == (
        "ok",
        "empty card definitions, nothing kept; asked again next run",
    )
    assert "cards" not in watching.load_tags("goatbots")


@pytest.mark.parametrize(
    ("body", "why"),
    [
        (zipped({"card-definitions.txt": b"[]"}), "card-definitions.txt isn't MTGO IDs and cards"),
        (zipped({"x.txt": b"{}"}), "no card-definitions.txt in it, only x.txt"),
        (definitions({"1": {"cardset": "X"}}), "card-definitions.txt isn't MTGO IDs and cards"),
        (b"<html>busy</html>", "not a zip"),
    ],
)
def test_definitions_that_arent_whole_are_set_aside_once_a_publish(with_cards, tracker, body, why):
    data_dir = with_cards
    watch(Source(answers(**{f"{NEW}/{goatbots.DEFINITIONS}": body})), tracker)
    aside = "goatbots/aside/card-definitions-2026-09-28T130000Z.zip"
    said = f"{goatbots.DEFINITIONS}: {why}; set aside as {aside}, asked again next run"
    assert tracker.outcomes()["GoatBots cards"] == ("fail", said)
    assert (data_dir.parent / aside).read_bytes() == body and not cards(data_dir)
    assert logged(data_dir)[-1]["publish"] == "2026-09-28T031520Z"
    watch(Source(answers(**{f"{NEW}/{goatbots.DEFINITIONS}": body})), tracker, now=NOW + timedelta(minutes=5))
    again = (
        f"{goatbots.DEFINITIONS}: {why}"
        f"; a copy of this publish is set aside already as {aside}, asked again next run"
    )
    assert tracker.outcomes()["GoatBots cards"] == ("fail", again)
    assert len(list((data_dir / "aside").iterdir())) == 1  # kept once, not at every run
    assert logged(data_dir)[-1]["not_kept"] is True


def test_definitions_missing_everywhere_fail(with_cards, tracker):
    watch(Source(answers(**{f"{NEW}/{goatbots.DEFINITIONS}": None})), tracker)
    assert tracker.outcomes()["GoatBots cards"] == ("fail", f"{goatbots.DEFINITIONS}: HTTP 404")


def test_definitions_with_no_last_modified_are_set_aside_and_fail(with_cards, tracker):
    data_dir = with_cards
    watch(Source(answers(), modified=None), tracker)
    aside = "goatbots/aside/card-definitions-2026-09-28T130000Z.zip"
    assert tracker.outcomes()["GoatBots cards"] == (
        "fail",
        f"{goatbots.DEFINITIONS}: no Last-Modified, so no time they were made"
        f"; set aside as {aside}, asked again next run",
    )
    assert (data_dir.parent / aside).read_bytes() == definitions()


def test_definitions_fetched_again_are_compared_with_the_ones_kept(with_cards, tracker):
    data_dir = with_cards
    watch(Source(answers()))
    (data_dir / "watch-etags.json").unlink()  # the ETag lost: the zip comes whole again
    watch(Source(answers()), tracker)
    assert tracker.outcomes()["GoatBots cards"] == (
        "ok",
        "have the definitions made 2026-09-28 03:15 UTC; the same as the one kept",
    )
    other = definitions({"2": {"name": "Y"}})
    watch(Source(answers(**{f"{NEW}/{goatbots.DEFINITIONS}": other}), modified=made(DAY)), tracker)
    aside = "goatbots/aside/card-definitions-2026-09-28T130000Z.zip"
    assert tracker.outcomes()["GoatBots cards"] == (
        "warn",
        f"have the definitions made 2026-09-28 03:15 UTC; this copy isn't the one kept: set aside as {aside}",
    )
    assert (data_dir.parent / aside).read_bytes() == other and len(cards(data_dir)) == 1


def test_a_price_list_fetched_again_that_isnt_the_one_kept_is_set_aside(data_dir, tracker):
    watch(Source(answers()))
    (data_dir / "watch-etags.json").unlink()
    other = latest(prices={"348": 1.0})
    watch(Source(answers(**{f"{NEW}/{goatbots.LATEST}": other})), tracker)
    aside = "goatbots/aside/price-history-2026-09-28T130000Z.zip"
    assert tracker.outcomes()["GoatBots prices"] == (
        "warn",
        f"have 2026-09-27's list, made 2026-09-28 03:15 UTC"
        f"; this copy isn't the one kept: set aside as {aside}",
    )
    assert (data_dir.parent / aside).read_bytes() == other and len(kept(data_dir)) == 1


def test_the_sync_leaves_the_definitions_it_kept_before_alone(data_dir):
    """Before 0037 the sync kept the latest definitions as card-definitions.zip, replacing the
    last; the watch keeps them now, and that file stays as it is."""
    old = data_dir / "card-definitions.zip"
    old.parent.mkdir(parents=True)
    old.write_bytes(b"kept before")
    source = Source(answers())
    run(source)
    assert old.read_bytes() == b"kept before"
    assert f"{NEW}/{goatbots.DEFINITIONS}" not in source.asked


def test_definitions_from_a_server_with_no_etag_are_still_kept_and_had(with_cards, tracker):
    data_dir = with_cards
    watch(Source(answers(), tagged=False))
    watch(Source(answers(), tagged=False), tracker)
    assert len(cards(data_dir)) == 1 and "cards" not in watching.load_tags("goatbots")
    assert tracker.outcomes()["GoatBots cards"][1].endswith("the same as the one kept")


def test_damage_found_keeping_definitions_is_a_warning(with_cards, tracker, monkeypatch):
    keep = runs.keep

    def keep_and_repair(folder, at, data):
        return dataclasses.replace(keep(folder, at, data), notes=("a copy was damaged",))

    monkeypatch.setattr(goatbots.runs, "keep", keep_and_repair)
    watch(Source(answers()), tracker)
    assert tracker.outcomes()["GoatBots cards"][0] == "warn"
    assert tracker.outcomes()["GoatBots cards"][1].endswith("; a copy was damaged")
