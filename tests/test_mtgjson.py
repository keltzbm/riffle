"""MTGJSON prices: a day kept once, checked against its .sha256, and 90 days to start from."""

import hashlib
import json
import lzma
from datetime import date, timedelta

import pytest

from riffle import net
from riffle.ingest import mtgjson

B = mtgjson.BASE
DAY = date(2026, 9, 27)


def meta(day: date) -> bytes:
    stamp = {"date": day.isoformat(), "version": f"5.3.0+{day:%Y%m%d}"}
    return json.dumps({"meta": stamp, "data": stamp}).encode()


def prices(day: date, days: int = 1) -> bytes:
    """A price file as MTGJSON streams it: compact JSON, meta first, xz-compressed."""
    head = {"date": day.isoformat(), "version": f"5.3.0+{day:%Y%m%d}"}
    series = {(day - timedelta(n)).isoformat(): 0.25 for n in range(days)}
    card = {"paper": {"cardkingdom": {"buylist": {}, "retail": {"normal": series}, "currency": "USD"}}}
    doc = {"meta": head, "data": {"0000cd33-1fbb-5ff1-a4d2-0f8e2b9e9b28": card}}
    return lzma.compress(json.dumps(doc, separators=(",", ":")).encode())


def sha(body: bytes) -> bytes:
    return hashlib.sha256(body).hexdigest().encode()


class Source:
    """MTGJSON as a fetch and a download: url -> body, None (404), or an exception to raise.
    Records every url asked for."""

    def __init__(self, answers: dict[str, bytes | None | Exception]):
        self.answers = answers
        self.asked: list[str] = []

    def _answer(self, url: str) -> bytes | None:
        self.asked.append(url)
        answer = self.answers.get(url)
        if isinstance(answer, Exception):
            raise answer
        return answer

    def fetch(self, url: str) -> bytes | None:
        return self._answer(url)

    def download(self, url, dest, progress=None):
        body = self._answer(url)
        if body is None:
            return None
        dest.write_bytes(body)
        if progress:
            progress(len(body), len(body))
        return len(body)


def answers(day: date = DAY, today: bytes | None = None, history: bytes | None = None, **overrides):
    today = prices(day) if today is None else today
    history = prices(day, days=90) if history is None else history
    found: dict[str, bytes | None | Exception] = {
        f"{B}/Meta.json": meta(day),
        f"{B}/{mtgjson.TODAY}": today,
        f"{B}/{mtgjson.TODAY}.sha256": sha(today),
        f"{B}/{mtgjson.HISTORY}": history,
        f"{B}/{mtgjson.HISTORY}.sha256": sha(history),
    }
    found.update(overrides)
    return found


def run(source: Source, tracker=None):
    if tracker is None:
        return mtgjson.snapshot(fetch=source.fetch, download=source.download)
    return mtgjson.snapshot(fetch=source.fetch, download=source.download, tracker=tracker)


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    return tmp_path / "riffle"


def test_the_first_run_keeps_the_day_and_the_90_days_before_it(data_dir, tracker):
    source = Source(answers())
    snap = run(source, tracker)
    assert snap is not None and snap.day == DAY and snap.kept == [mtgjson.TODAY, mtgjson.HISTORY]
    daily = data_dir / "mtgjson" / "daily" / "2026-09-27.json.xz"
    history = data_dir / "mtgjson" / "90-days" / "2026-09-27.json.xz"
    assert daily.read_bytes() == prices(DAY)  # as returned
    assert history.read_bytes() == prices(DAY, days=90)
    assert tracker.outcomes() == {
        "MTGJSON prices": ("ok", f"kept 2026-09-27, {daily.stat().st_size / 1e6:,.1f} MB"),
        "MTGJSON 90 days": ("ok", f"kept the 90 days to 2026-09-27, {history.stat().st_size / 1e6:,.1f} MB"),
    }
    assert [s.unit for s in tracker.steps] == ["bytes", "bytes"]
    assert tracker.steps[0].updates == [(len(prices(DAY)), len(prices(DAY)))]
    assert not [p for p in (data_dir / "mtgjson").rglob("*") if p.name.endswith((".new", ".part"))]


def test_a_day_already_kept_costs_one_request(data_dir, tracker):
    run(Source(answers()))
    source = Source(answers())
    snap = run(source, tracker)
    assert source.asked == [f"{B}/Meta.json"]
    assert snap is not None and snap.kept == []
    assert tracker.outcomes() == {"MTGJSON prices": ("ok", "already have 2026-09-27")}


def test_the_next_day_is_just_its_own_file(data_dir, tracker):
    run(Source(answers()))
    nxt = DAY + timedelta(1)
    source = Source(answers(nxt))
    snap = run(source, tracker)
    assert snap is not None and snap.kept == [mtgjson.TODAY]
    assert f"{B}/{mtgjson.HISTORY}" not in source.asked
    assert mtgjson.kept_days(mtgjson.daily_dir()) == [DAY, nxt]
    assert list(tracker.outcomes()) == ["MTGJSON prices"]


def keep_daily(*days: date) -> None:
    for day in days:
        run(Source(answers(day)))


def test_a_missed_day_is_filled_from_the_90_days_at_most_every_30_days(data_dir):
    run(Source(answers()))  # the 90 days to DAY
    later = DAY + timedelta(10)  # nine days since were missed
    source = Source(answers(later))
    snap = run(source)
    assert snap is not None and snap.kept == [mtgjson.TODAY]  # the last 90 days came only 10 days ago
    source = Source(answers(DAY + timedelta(29)))
    run(source)
    assert f"{B}/{mtgjson.HISTORY}" not in source.asked  # 29 days: still too soon
    refill = DAY + timedelta(30)
    snap = run(Source(answers(refill)))
    assert snap is not None and snap.kept == [mtgjson.TODAY, mtgjson.HISTORY]
    have = mtgjson.covered(mtgjson.kept_days(mtgjson.daily_dir()), mtgjson.kept_days(mtgjson.history_dir()))
    assert all(DAY + timedelta(n) in have for n in range(31))  # the missed days are back


def test_no_missing_day_means_no_refill(data_dir):
    run(Source(answers()))
    keep_daily(*(DAY + timedelta(n) for n in range(1, 40)))
    source = Source(answers(DAY + timedelta(40)))
    snap = run(source)
    assert snap is not None and snap.kept == [mtgjson.TODAY]
    assert f"{B}/{mtgjson.HISTORY}" not in source.asked


def test_one_missing_day_brings_a_refill_once_due(data_dir):
    run(Source(answers()))
    keep_daily(*(DAY + timedelta(n) for n in range(1, 30) if n != 20))  # day 20 missed; too soon to refill
    assert mtgjson.kept_days(mtgjson.history_dir()) == [DAY]
    snap = run(Source(answers(DAY + timedelta(30))))  # due now
    assert snap is not None and snap.kept == [mtgjson.TODAY, mtgjson.HISTORY]
    keep_daily(*(DAY + timedelta(n) for n in range(32, 35)))  # day 31 missed right after the refill
    source = Source(answers(DAY + timedelta(35)))
    run(source)
    assert f"{B}/{mtgjson.HISTORY}" not in source.asked  # it waits for the next


def test_the_90_days_are_retried_until_kept(data_dir, tracker):
    run(Source(answers(**{f"{B}/{mtgjson.HISTORY}": net.FetchError("HTTP 503")})))
    source = Source(answers())
    snap = run(source, tracker)
    assert snap is not None and snap.kept == [mtgjson.HISTORY]
    assert tracker.outcomes()["MTGJSON prices"] == ("ok", "already have 2026-09-27")


def test_a_file_that_doesnt_match_its_sha256_is_not_kept(data_dir, tracker):
    source = Source(answers(**{f"{B}/{mtgjson.TODAY}.sha256": b"0" * 64}))
    run(source, tracker)
    assert tracker.outcomes()["MTGJSON prices"] == (
        "fail",
        f"{mtgjson.TODAY} doesn't match its .sha256; MTGJSON may be mid-update",
    )
    assert mtgjson.kept_days(mtgjson.daily_dir()) == []
    assert not list(mtgjson.daily_dir().iterdir())  # nothing half-kept


def test_a_sha256_file_may_name_the_file_too(data_dir):
    body = prices(DAY)
    snap = run(Source(answers(**{f"{B}/{mtgjson.TODAY}.sha256": sha(body).upper() + b"  " + b"x.json.xz\n"})))
    assert snap is not None and mtgjson.TODAY in snap.kept


def test_the_day_comes_from_the_file_not_meta(data_dir, tracker):
    stale = DAY - timedelta(1)  # a cache still serving yesterday's file
    source = Source(answers(today=prices(stale)))
    run(source, tracker)
    assert mtgjson.kept_days(mtgjson.daily_dir()) == [stale]
    assert tracker.outcomes()["MTGJSON prices"][1].startswith("kept 2026-09-26")


def test_a_file_already_kept_under_its_own_day_is_left_alone(data_dir, tracker):
    stale = DAY - timedelta(1)
    run(Source(answers(stale)))
    kept = mtgjson.daily_dir() / "2026-09-26.json.xz"
    kept.write_bytes(b"what was kept first")
    run(Source(answers(today=prices(stale))), tracker)
    assert tracker.outcomes()["MTGJSON prices"] == ("ok", "already have 2026-09-26")
    assert kept.read_bytes() == b"what was kept first"


def test_a_90_day_file_already_kept_is_left_alone(data_dir, tracker):
    run(Source(answers()))
    later = DAY + timedelta(40)  # missing days, and a refill due, but the file served is the old one
    snap = run(Source(answers(later, history=prices(DAY, days=90))), tracker)
    assert snap is not None and snap.kept == [mtgjson.TODAY]
    assert tracker.outcomes()["MTGJSON 90 days"] == ("ok", "already have the 90 days to 2026-09-27")


def test_a_file_cut_off_before_its_end_is_not_kept(data_dir, tracker):
    body = prices(DAY, days=90)[:-20]  # matches its .sha256, but it's not the whole stream
    run(Source(answers(history=body)), tracker)
    assert tracker.outcomes()["MTGJSON 90 days"] == ("fail", f"{mtgjson.HISTORY}: cut off before its end")
    assert mtgjson.kept_days(mtgjson.history_dir()) == []


def test_a_full_disk_fails_the_step_and_the_90_days_still_run(data_dir, tracker):
    run(Source(answers(**{f"{B}/{mtgjson.TODAY}": OSError(28, "No space left on device")})), tracker)
    assert tracker.outcomes() == {
        "MTGJSON prices": ("fail", "[Errno 28] No space left on device"),
        "MTGJSON 90 days": ("ok", tracker.outcomes()["MTGJSON 90 days"][1]),
    }
    assert mtgjson.kept_days(mtgjson.history_dir()) == [DAY]
    assert not [p for p in (data_dir / "mtgjson").rglob("*.new")]


@pytest.mark.parametrize(
    ("body", "why"),
    [
        (b"<html>busy</html>", "not xz"),
        (lzma.compress(b"[1, 2, 3]"), "not the expected JSON"),
        (lzma.compress(b'{"meta":{"date":"2026-13-45"}}'), "not the expected JSON"),
        (lzma.compress(b'[{"date":"2026-09-27"}]'), "not the expected JSON"),
    ],
)
def test_a_file_that_isnt_a_price_file_fails_cleanly(data_dir, tracker, body, why):
    run(Source(answers(today=body)), tracker)
    assert tracker.outcomes()["MTGJSON prices"] == ("fail", f"{mtgjson.TODAY}: {why}")
    assert not list(mtgjson.daily_dir().iterdir())


def test_missing_files_fail_their_step(data_dir, tracker):
    run(Source(answers(**{f"{B}/{mtgjson.TODAY}": None, f"{B}/{mtgjson.HISTORY}.sha256": None})), tracker)
    assert tracker.outcomes() == {
        "MTGJSON prices": ("fail", f"{mtgjson.TODAY}: HTTP 404"),
        "MTGJSON 90 days": ("fail", f"{mtgjson.HISTORY}.sha256: HTTP 404"),
    }


@pytest.mark.parametrize(
    ("body", "why"),
    [
        (None, "Meta.json: HTTP 404"),
        (b"<html></html>", "Meta.json: not the expected JSON"),
        (b"[" * 100_000, "Meta.json: not the expected JSON"),
    ],
)
def test_without_meta_nothing_else_is_asked_for(data_dir, tracker, body, why):
    source = Source(answers(**{f"{B}/Meta.json": body}))
    assert run(source, tracker) is None
    assert source.asked == [f"{B}/Meta.json"]
    assert tracker.outcomes() == {"MTGJSON prices": ("fail", why)}


def test_an_unreachable_mtgjson_fails_its_step(data_dir, tracker):
    run(Source(answers(**{f"{B}/Meta.json": net.FetchError("no answer after 3 tries")})), tracker)
    assert tracker.outcomes() == {"MTGJSON prices": ("fail", "no answer after 3 tries")}


def test_kept_days_ignore_other_files(data_dir):
    folder = mtgjson.daily_dir()
    folder.mkdir(parents=True)
    for name in ("2026-09-25.json.xz", "notes.json.xz", "2026-09-26.json.xz.new", "2026-09-24.json.xz"):
        (folder / name).write_bytes(b"")
    assert mtgjson.kept_days(folder) == [date(2026, 9, 24), date(2026, 9, 25)]
    assert mtgjson.kept_days(folder / "missing") == []


def test_ninety_days_cover_their_own_date_and_the_89_before():
    days = mtgjson.covered([], [DAY])
    assert len(days) == 90 and min(days) == date(2026, 6, 30) and max(days) == DAY
