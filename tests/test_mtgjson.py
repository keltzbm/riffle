"""MTGJSON prices: every build of AllPricesToday kept by the run rule, asked for when the next
is due (riffle.cadence) and checked against its .sha256, and 90 days to start from."""

import dataclasses
import hashlib
import json
import lzma
import re
from datetime import UTC, date, datetime, time, timedelta

import pytest

from riffle import locks, net, runs, watching
from riffle.ingest import mtgjson

B = mtgjson.BASE
EVERY_FILE = mtgjson.FILES  # before a test narrows them to AllPricesToday
DAY = date(2026, 9, 27)
NOW = datetime(2026, 9, 27, 13, 0, tzinfo=UTC)  # the 07:00 sync in Denver


def meta(day: date) -> bytes:
    stamp = {"date": day.isoformat(), "version": f"5.3.0+{day:%Y%m%d}"}
    return json.dumps({"meta": stamp, "data": stamp}).encode()


def doc(day: date, days: int = 1, price: float = 0.25) -> bytes:
    """A price file as MTGJSON streams it, unpacked: compact JSON, meta first."""
    head = {"date": day.isoformat(), "version": f"5.3.0+{day:%Y%m%d}"}
    series = {(day - timedelta(n)).isoformat(): price for n in range(days)}
    card = {"paper": {"cardkingdom": {"buylist": {}, "retail": {"normal": series}, "currency": "USD"}}}
    return json.dumps(
        {"meta": head, "data": {"0000cd33-1fbb-5ff1-a4d2-0f8e2b9e9b28": card}}, separators=(",", ":")
    ).encode()


def prices(day: date, days: int = 1, price: float = 0.25) -> bytes:
    """A price file as MTGJSON serves it: xz-compressed."""
    return lzma.compress(doc(day, days, price))


def sha(body: bytes) -> bytes:
    return hashlib.sha256(body).hexdigest().encode()


def built(day: date) -> datetime:
    """When MTGJSON builds a day's files: 06:12:38 UTC that day."""
    return datetime.combine(day, time(6, 12, 38), UTC)


AUTO = object()


class Source:
    """MTGJSON as a fetch, a download and net.fetch_new: url -> body, None (404), or an exception
    to raise, each body's ETag its hash, its Last-Modified when the day inside was built (or
    `modified`). Records every url asked for, and the ETags sent."""

    def __init__(self, answers: dict[str, bytes | None | Exception], modified=AUTO, tagged: bool = True):
        self.answers = answers
        self.asked: list[str] = []
        self.etags: list[str | None] = []
        self.modified = modified
        self.tagged = tagged

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

    def _stamp(self, body: bytes) -> datetime | None:
        if self.modified is not AUTO:
            return self.modified  # type: ignore[return-value]
        try:
            found = re.search(rb'"date":"(\d{4}-\d{2}-\d{2})"', lzma.decompress(body)[:512])
            return built(date.fromisoformat(found.group(1).decode()) if found else DAY)
        except (lzma.LZMAError, ValueError):
            return built(DAY)

    def fetch_new(self, url, dest, known, etag=None, accept="*/*", progress=None, missing=(404,)):
        self.etags.append(etag)
        body = self._answer(url)
        if body is None:
            return None
        tag = f'"{hash(body)}"'
        if etag == tag:
            return net.Fetched("unchanged", b"", etag)
        assert not known(body[: net.HEAD])
        dest.write_bytes(body)
        if progress:
            progress(len(body), None)
        return net.Fetched(
            "new", body[: net.HEAD], tag if self.tagged else None, len(body), self._stamp(body)
        )


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


def watch(source: Source, tracker=None, now: datetime = NOW, always: bool = True) -> watching.Watch:
    kw = {} if tracker is None else {"tracker": tracker}
    return mtgjson.watch(fetch=source.fetch_new, get=source.fetch, clock=lambda: now, always=always, **kw)


def refill(source: Source, tracker=None):
    if tracker is None:
        return mtgjson.snapshot(fetch=source.fetch, download=source.download)
    return mtgjson.snapshot(fetch=source.fetch, download=source.download, tracker=tracker)


def run(source: Source, tracker=None):
    """What the sync does: the watch once, then the refill."""
    watch(source, tracker)
    return refill(source, tracker)


def kept(data_dir) -> dict:
    return runs.kept(data_dir / "mtgjson" / "lists" / "prices-today")


def logged(data_dir) -> list[dict]:
    return watching.entries(mtgjson.STORE)


def have() -> set[date]:
    return mtgjson.covered(
        mtgjson.kept_days(mtgjson.daily_dir()) + watching.days("mtgjson", "prices-today"),
        mtgjson.kept_days(mtgjson.history_dir()),
    )


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setattr(mtgjson, "FILES", mtgjson.FILES[:1])  # the catalogs' tests set them back
    return tmp_path / "riffle"


def test_the_first_run_keeps_the_build_and_the_90_days_before_it(data_dir, tracker):
    source = Source(answers())
    snap = run(source, tracker)
    assert snap is not None and snap.day == DAY and snap.kept == [mtgjson.HISTORY]
    lists = kept(data_dir)
    assert list(lists) == ["2026-09-27T061238Z"]  # its Last-Modified
    assert runs.rebuild(lists["2026-09-27T061238Z"]) == doc(DAY)  # unpacked, as built
    history = data_dir / "mtgjson" / "90-days" / "2026-09-27.json.xz"
    assert history.read_bytes() == prices(DAY, days=90)  # as returned
    size = watching.size(lists["2026-09-27T061238Z"].stat().st_size)
    assert tracker.outcomes() == {
        "MTGJSON prices today": (
            "ok",
            f"kept the list built 2026-09-27 06:12 UTC (2026-09-27), {len(doc(DAY))} bytes: "
            f"a new run, {size} kept twice",
        ),
        "MTGJSON 90 days": ("ok", f"kept the 90 days to 2026-09-27, {history.stat().st_size / 1e6:,.1f} MB"),
    }
    assert [s.unit for s in tracker.steps] == ["bytes", "bytes"]
    assert tracker.steps[0].updates == [(len(prices(DAY)), None)]
    assert not [p for p in (data_dir / "mtgjson").rglob("*") if p.name.endswith((".new", ".part"))]
    entry = logged(data_dir)[0]
    assert entry == entry | {
        "at": "2026-09-27T130000Z",
        "list": "prices-today",
        "result": "kept",
        "made": "2026-09-27T061238Z",
        "kind": "base",
        "file": "mtgjson/lists/prices-today/2026-09-27T061238Z/2026-09-27T061238Z.json.zst",
        "day": "2026-09-27",
        "version": "5.3.0+20260927",
        "served_sha256": sha(prices(DAY)).decode(),
        "served_size": len(prices(DAY)),
        "etag": f'"{hash(prices(DAY))}"',
    }
    assert "seconds" in entry


def test_the_same_build_again_is_a_304_and_meta_json(data_dir, tracker):
    run(Source(answers()))
    source = Source(answers())
    snap = run(source, tracker)
    assert source.asked == [f"{B}/{mtgjson.TODAY}", f"{B}/Meta.json"]
    assert source.etags == [f'"{hash(prices(DAY))}"']
    assert snap is not None and snap.kept == []
    assert tracker.outcomes() == {
        "MTGJSON prices today": ("ok", "no new build since the last one kept"),
        "MTGJSON 90 days": ("drop",),
    }


def test_the_next_build_is_kept_as_a_difference(data_dir, tracker):
    run(Source(answers()))
    nxt = DAY + timedelta(1)
    source = Source(answers(nxt, today=prices(nxt, price=0.5)))
    snap = run(source, tracker)
    assert snap is not None and snap.kept == []
    assert f"{B}/{mtgjson.HISTORY}" not in source.asked
    lists = kept(data_dir)
    assert list(lists) == ["2026-09-27T061238Z", "2026-09-28T061238Z"]
    assert lists["2026-09-28T061238Z"].name.endswith(".diff.zst")
    assert runs.rebuild(lists["2026-09-28T061238Z"]) == doc(nxt, price=0.5)
    assert tracker.outcomes()["MTGJSON prices today"][1].startswith(
        "kept the list built 2026-09-28 06:12 UTC (2026-09-28), "
    )
    assert watching.days("mtgjson", "prices-today") == [DAY, nxt]


def keep_daily(*days: date) -> None:
    for day in days:
        run(Source(answers(day)))


def test_a_missed_day_is_filled_from_the_90_days_at_most_every_30_days(data_dir):
    run(Source(answers()))  # the 90 days to DAY
    later = DAY + timedelta(10)  # nine days since were missed
    source = Source(answers(later))
    snap = run(source)
    assert snap is not None and snap.kept == []  # the last 90 days came only 10 days ago
    source = Source(answers(DAY + timedelta(29)))
    run(source)
    assert f"{B}/{mtgjson.HISTORY}" not in source.asked  # 29 days: still too soon
    refill_day = DAY + timedelta(30)
    snap = run(Source(answers(refill_day)))
    assert snap is not None and snap.kept == [mtgjson.HISTORY]
    assert all(DAY + timedelta(n) in have() for n in range(31))  # the missed days are back


def test_no_missing_day_means_no_refill(data_dir):
    run(Source(answers()))
    keep_daily(*(DAY + timedelta(n) for n in range(1, 40)))
    source = Source(answers(DAY + timedelta(40)))
    snap = run(source)
    assert snap is not None and snap.kept == []
    assert f"{B}/{mtgjson.HISTORY}" not in source.asked


def test_days_kept_by_the_sync_before_the_watch_count_too(data_dir):
    run(Source(answers()))
    daily = mtgjson.daily_dir()
    daily.mkdir(parents=True)
    for n in range(1, 40):  # kept a day, before the watch
        (daily / f"{(DAY + timedelta(n)).isoformat()}.json.xz").write_bytes(b"")
    source = Source(answers(DAY + timedelta(40)))
    refill(source)
    assert source.asked == [f"{B}/Meta.json"]


def test_one_missing_day_brings_a_refill_once_due(data_dir):
    run(Source(answers()))
    keep_daily(*(DAY + timedelta(n) for n in range(1, 30) if n != 20))  # day 20 missed; too soon to refill
    assert mtgjson.kept_days(mtgjson.history_dir()) == [DAY]
    snap = run(Source(answers(DAY + timedelta(30))))  # due now
    assert snap is not None and snap.kept == [mtgjson.HISTORY]
    keep_daily(*(DAY + timedelta(n) for n in range(32, 35)))  # day 31 missed right after the refill
    source = Source(answers(DAY + timedelta(35)))
    run(source)
    assert f"{B}/{mtgjson.HISTORY}" not in source.asked  # it waits for the next


def test_the_90_days_are_retried_until_kept(data_dir, tracker):
    run(Source(answers(**{f"{B}/{mtgjson.HISTORY}": net.FetchError("HTTP 503")})))
    source = Source(answers())
    snap = run(source, tracker)
    assert snap is not None and snap.kept == [mtgjson.HISTORY]
    assert tracker.outcomes()["MTGJSON prices today"] == ("ok", "no new build since the last one kept")


def test_a_build_that_doesnt_match_its_sha256_is_not_kept_and_asked_again(data_dir, tracker):
    source = Source(answers(**{f"{B}/{mtgjson.TODAY}.sha256": b"0" * 64}))
    res = watch(source, tracker)
    aside = "mtgjson/aside/AllPricesToday-2026-09-27T130000Z.json.xz"
    why = (
        f"{mtgjson.TODAY} doesn't match its .sha256; MTGJSON may be mid-update"
        f"; set aside as {aside}, asked again next run"
    )
    assert res.failed == [("MTGJSON prices today", why)]
    assert tracker.outcomes()["MTGJSON prices today"] == ("fail", why)
    assert not kept(data_dir) and watching.load_tags("mtgjson") == {}  # no ETag: asked whole again
    assert not [p for p in (data_dir / "mtgjson").rglob("*") if p.name.endswith((".new", ".part"))]
    assert logged(data_dir)[0]["result"] == "failed" and logged(data_dir)[0]["aside"] == aside
    assert (data_dir / aside).exists()  # it may be the only copy of what MTGJSON served
    source = Source(answers())
    watch(source)
    assert source.etags == [None] and len(kept(data_dir)) == 1


def test_a_sha256_file_may_name_the_file_too(data_dir):
    body = prices(DAY)
    res = watch(
        Source(answers(**{f"{B}/{mtgjson.TODAY}.sha256": sha(body).upper() + b"  " + b"x.json.xz\n"}))
    )
    assert res.kept == ["MTGJSON prices today"]


def test_the_day_comes_from_the_build_not_meta(data_dir, tracker):
    stale = DAY - timedelta(1)  # a cache still serving yesterday's file
    watch(Source(answers(today=prices(stale))), tracker)
    assert watching.days("mtgjson", "prices-today") == [stale]
    assert "(2026-09-26)" in tracker.outcomes()["MTGJSON prices today"][1]


def test_a_build_kept_already_is_had_not_kept_again(data_dir, tracker):
    watch(Source(answers()))
    (data_dir / "mtgjson" / "watch-etags.json").unlink()  # the ETag lost: the file comes whole again
    res = watch(Source(answers()), tracker)
    assert res.same == ["MTGJSON prices today"] and len(kept(data_dir)) == 1
    assert tracker.outcomes()["MTGJSON prices today"] == (
        "ok",
        "have the build made 2026-09-27 06:12 UTC (2026-09-27); the same as the one kept",
    )
    entry = logged(data_dir)[-1]
    assert {k: entry[k] for k in ("at", "list", "result", "made", "same", "day")} == {
        "at": "2026-09-27T130000Z",
        "list": "prices-today",
        "result": "known",
        "made": "2026-09-27T061238Z",
        "same": True,
        "day": "2026-09-27",
    }
    assert "aside" not in entry and not (data_dir / "mtgjson" / "aside").exists()
    assert watching.load_tags("mtgjson") == {"prices-today": f'"{hash(prices(DAY))}"'}


def test_a_build_fetched_again_that_isnt_the_one_kept_is_set_aside(data_dir, tracker):
    """The same Last-Modified with other contents: the copy may be the only one of what MTGJSON
    served, so it's kept aside, and the step warns."""
    watch(Source(answers()))
    (data_dir / "mtgjson" / "watch-etags.json").unlink()
    other = prices(DAY, price=0.5)
    res = watch(Source(answers(today=other), modified=built(DAY)), tracker)
    aside = "mtgjson/aside/AllPricesToday-2026-09-27T130000Z.json.xz"
    assert res.same == ["MTGJSON prices today"] and len(kept(data_dir)) == 1
    assert tracker.outcomes()["MTGJSON prices today"] == (
        "warn",
        f"have the build made 2026-09-27 06:12 UTC (2026-09-27)"
        f"; this copy isn't the one kept: set aside as {aside}",
    )
    assert (data_dir / aside).read_bytes() == other
    assert logged(data_dir)[-1]["same"] is False and logged(data_dir)[-1]["aside"] == aside


def test_a_build_with_no_last_modified_is_set_aside_and_fails(data_dir, tracker):
    watch(Source(answers(), modified=None), tracker)
    aside = "mtgjson/aside/AllPricesToday-2026-09-27T130000Z.json.xz"
    assert tracker.outcomes()["MTGJSON prices today"] == (
        "fail",
        f"{mtgjson.TODAY}: no Last-Modified, so no time it was built"
        f"; set aside as {aside}, asked again next run",
    )
    assert (data_dir / aside).read_bytes() == prices(DAY)
    assert not kept(data_dir) and watching.load_tags("mtgjson") == {}
    watch(Source(answers(), modified=None), tracker, now=NOW + timedelta(minutes=5))  # once a publish
    assert "a copy of this publish is set aside already" in tracker.outcomes()["MTGJSON prices today"][1]
    assert len(list((data_dir / "mtgjson" / "aside").iterdir())) == 1


def test_a_busy_store_asks_nothing(data_dir, tracker):
    with locks.held(data_dir / "mtgjson" / "watch.lock") as mine:
        assert mine
        source = Source(answers())
        res = watch(source, tracker)
    assert res.busy and source.asked == []
    assert tracker.outcomes() == {"MTGJSON": ("ok", "another run is asking for its lists")}


def test_until_its_schedule_is_learned_the_watch_asks_at_every_firing(data_dir, tracker):
    watch(Source(answers()))
    source = Source(answers())
    res = watch(source, tracker, now=NOW + timedelta(minutes=4), always=False)  # a firing come early
    assert source.asked == [f"{B}/{mtgjson.TODAY}"] and res.waiting == []  # a 304
    assert tracker.outcomes()["MTGJSON prices today"] == (
        "ok",
        "no new build since the last one kept; asked at every firing until it has 14 gaps, 0 so far",
    )


def test_a_build_is_asked_from_when_it_goes_online_not_when_it_s_made(data_dir, tracker):
    folder = data_dir / "mtgjson" / "lists" / "prices-today"
    made = [datetime(2026, 9, 29, 6, 12, tzinfo=UTC) + timedelta(days=i) for i in range(17)]
    for m in made:  # each build not served 6 h 45 min after it was made, and got 5 minutes later
        stamp = runs.name(m)
        (folder / stamp).mkdir(parents=True)
        (folder / stamp / f"{stamp}{runs.BASE}").touch()
        miss, got = (
            runs.name(m + timedelta(hours=6, minutes=45)),
            runs.name(m + timedelta(hours=6, minutes=50)),
        )
        watching.log("mtgjson", {"at": miss, "list": "prices-today", "result": "unchanged"})
        watching.log("mtgjson", {"at": got, "list": "prices-today", "result": "kept", "made": stamp})
    now = made[-1] + timedelta(days=1, hours=1)  # 07:12 UTC: the next build made, not yet online
    watching.log(
        "mtgjson",
        {"at": runs.name(now - timedelta(minutes=5)), "list": "prices-today", "result": "unchanged"},
    )
    source = Source(answers())
    res = watch(source, tracker, now=now, always=False)
    assert source.asked == [] and res.waiting == ["MTGJSON prices today"]
    assert tracker.outcomes()["MTGJSON 1 list"] == (
        "ok",
        "none due; prices today next asked 2026-10-16 10:32 UTC"
        "; its next list expected 2026-10-16 06:12 UTC, "
        "online from about 2026-10-16 12:57 UTC",
    )


def test_a_90_day_file_already_kept_is_left_alone(data_dir, tracker):
    run(Source(answers()))
    later = DAY + timedelta(40)  # missing days, and a refill due, but the file served is the old one
    snap = run(Source(answers(later, history=prices(DAY, days=90))), tracker)
    assert snap is not None and snap.kept == []
    assert tracker.outcomes()["MTGJSON 90 days"] == ("ok", "already have the 90 days to 2026-09-27")


def test_a_file_cut_off_before_its_end_is_not_kept(data_dir, tracker):
    body = prices(DAY, days=90)[:-20]  # matches its .sha256, but it's not the whole stream
    run(Source(answers(history=body)), tracker)
    outcome, said = tracker.outcomes()["MTGJSON 90 days"]
    assert outcome == "fail" and said.startswith(f"{mtgjson.HISTORY}: cut off before its end; set aside as ")
    assert mtgjson.kept_days(mtgjson.history_dir()) == []
    (aside,) = (data_dir / "mtgjson" / "aside").iterdir()
    assert aside.read_bytes() == body and aside.name.startswith("AllPrices-")
    run(Source(answers(history=body)), tracker)
    assert "a copy of this publish is set aside already" in tracker.outcomes()["MTGJSON 90 days"][1]
    assert len(list((data_dir / "mtgjson" / "aside").iterdir())) == 1  # the same bytes: kept once


def test_a_full_disk_fails_the_step_and_the_90_days_still_run(data_dir, tracker):
    run(Source(answers(**{f"{B}/{mtgjson.TODAY}": OSError(28, "No space left on device")})), tracker)
    assert tracker.outcomes() == {
        "MTGJSON prices today": ("fail", "[Errno 28] No space left on device"),
        "MTGJSON 90 days": ("ok", tracker.outcomes()["MTGJSON 90 days"][1]),
    }
    assert mtgjson.kept_days(mtgjson.history_dir()) == [DAY]
    assert not [p for p in (data_dir / "mtgjson").rglob("*.new")]


@pytest.mark.parametrize(
    ("body", "why"),
    [
        (b"<html>busy</html>", "not xz"),
        (lzma.compress(b"[1, 2, 3]"), "not the expected JSON"),
        (lzma.compress(b'{"meta":{"date":"2026-13-45","version":"5"}}'), "not the expected JSON"),
        (lzma.compress(b'{"meta":{"date":"2026-09-27"}}'), "not the expected JSON"),
        (lzma.compress(b'{"meta":{"date":"2026-09-27","version":5}}'), "not the expected JSON"),
        (lzma.compress(b'[{"date":"2026-09-27"}]'), "not the expected JSON"),
        (prices(DAY)[:-20], "cut off before its end"),
    ],
)
def test_a_file_that_isnt_a_price_file_fails_cleanly(data_dir, tracker, body, why):
    watch(Source(answers(today=body)), tracker)
    aside = "mtgjson/aside/AllPricesToday-2026-09-27T130000Z.json.xz"
    said = f"{mtgjson.TODAY}: {why}; set aside as {aside}, asked again next run"
    assert tracker.outcomes()["MTGJSON prices today"] == ("fail", said)
    assert not kept(data_dir) and (data_dir / aside).read_bytes() == body


def test_an_oversized_build_is_refused(data_dir, tracker, monkeypatch):
    monkeypatch.setattr(mtgjson, "FILES", (dataclasses.replace(mtgjson.FILES[0], most=100),))
    watch(Source(answers()), tracker)
    aside = "mtgjson/aside/AllPricesToday-2026-09-27T130000Z.json.xz"
    assert tracker.outcomes()["MTGJSON prices today"] == (
        "fail",
        f"{mtgjson.TODAY}: unpacks to more than 100 bytes, too big to be what it claims; "
        f"set aside as {aside}, asked again next run",
    )


def test_missing_files_fail_their_step(data_dir, tracker):
    run(Source(answers(**{f"{B}/{mtgjson.TODAY}": None, f"{B}/{mtgjson.HISTORY}.sha256": None})), tracker)
    assert tracker.outcomes() == {
        "MTGJSON prices today": ("fail", f"{mtgjson.TODAY}: HTTP 404"),
        "MTGJSON 90 days": ("fail", f"{mtgjson.HISTORY}.sha256: HTTP 404"),
    }


def test_a_missing_sha256_fails_the_build(data_dir, tracker):
    watch(Source(answers(**{f"{B}/{mtgjson.TODAY}.sha256": None})), tracker)
    assert tracker.outcomes()["MTGJSON prices today"] == ("fail", f"{mtgjson.TODAY}.sha256: HTTP 404")


@pytest.mark.parametrize(
    ("body", "why"),
    [
        (None, "Meta.json: HTTP 404"),
        (b"<html></html>", "Meta.json: not the expected JSON"),
        (b"[" * 100_000, "Meta.json: not the expected JSON"),
    ],
)
def test_without_meta_the_refill_asks_nothing_else(data_dir, tracker, body, why):
    source = Source(answers(**{f"{B}/Meta.json": body}))
    assert refill(source, tracker) is None
    assert source.asked == [f"{B}/Meta.json"]
    assert tracker.outcomes() == {"MTGJSON 90 days": ("fail", why)}


def test_an_unreachable_mtgjson_fails_its_steps(data_dir, tracker):
    down = net.FetchError("no answer after 3 tries")
    run(Source(answers(**{f"{B}/Meta.json": down, f"{B}/{mtgjson.TODAY}": down})), tracker)
    assert tracker.outcomes() == {
        "MTGJSON prices today": ("fail", "no answer after 3 tries"),
        "MTGJSON 90 days": ("fail", "no answer after 3 tries"),
    }


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


def test_a_server_that_sends_no_etag_gets_the_whole_build_each_time(data_dir, tracker):
    watch(Source(answers(), tagged=False))
    source = Source(answers(), tagged=False)
    res = watch(source, tracker)
    assert source.etags == [None] and res.same == ["MTGJSON prices today"]
    assert watching.load_tags("mtgjson") == {} and len(kept(data_dir)) == 1


def test_damage_found_while_keeping_is_a_warning(data_dir, tracker, monkeypatch):
    keep = runs.keep

    def keep_and_repair(folder, at, data):
        return dataclasses.replace(keep(folder, at, data), notes=("x.copy.json.zst was damaged: set aside",))

    monkeypatch.setattr(mtgjson.runs, "keep", keep_and_repair)
    watch(Source(answers()), tracker)
    outcome = tracker.outcomes()["MTGJSON prices today"]
    assert outcome[0] == "warn" and outcome[1].endswith("; x.copy.json.zst was damaged: set aside")


@pytest.mark.parametrize(
    ("overrides", "why"),
    [
        ({f"{B}/{mtgjson.HISTORY}": None}, f"{mtgjson.HISTORY}: HTTP 404"),
        (
            {f"{B}/{mtgjson.HISTORY}.sha256": b"0" * 64},
            f"{mtgjson.HISTORY} doesn't match its .sha256; MTGJSON may be mid-update",
        ),
    ],
)
def test_a_90_day_file_missing_or_not_matching_is_not_kept(data_dir, tracker, overrides, why):
    refill(Source(answers(**overrides)), tracker)
    outcome, said = tracker.outcomes()["MTGJSON 90 days"]
    assert outcome == "fail" and said.split("; set aside as ")[0] == why
    assert ("set aside" in said) == ("sha256" in why)  # a copy fetched whole is set aside
    assert mtgjson.kept_days(mtgjson.history_dir()) == []


CSV = b"uuid,scryfallId\n0000cd33,5f8287b1\n"


def catalog(file: mtgjson.File, day: date = DAY) -> bytes:
    """A catalog file's list: JSON with its meta first, or a CSV or tar's bytes."""
    return meta(day) if file.meta else CSV


def catalogs(day: date = DAY, **overrides) -> dict:
    """Every file of the build as MTGJSON serves it, each beside its .sha256 if it has one."""
    found = answers(day)
    for file in EVERY_FILE[1:]:
        served = lzma.compress(catalog(file, day)) if file.path.endswith(".xz") else catalog(file, day)
        found[f"{B}/{file.path}"] = served
        if file.sha256:
            found[f"{B}/{file.path}.sha256"] = sha(served)
    found.update(overrides)
    return found


@pytest.fixture
def every_file(data_dir, monkeypatch):
    monkeypatch.setattr(mtgjson, "FILES", EVERY_FILE)
    return data_dir


def test_every_file_of_the_build_is_kept_as_a_list_of_its_own(every_file, tracker):
    source = Source(catalogs())
    res = watch(source, tracker)
    assert res.kept == [f"MTGJSON {file.label}" for file in EVERY_FILE] and not res.failed
    for file in EVERY_FILE[1:]:
        found = runs.kept(mtgjson.lists_dir(file.list))
        assert list(found) == ["2026-09-27T061238Z"]  # its Last-Modified
        assert runs.rebuild(found["2026-09-27T061238Z"]) == catalog(file)  # unpacked, as served inside
    assert f"{B}/BuildManifest.json.sha256" not in source.asked  # MTGJSON serves none for it
    entries = {entry["list"]: entry for entry in logged(every_file)}
    assert (
        entries["all-printings"]["day"] == "2026-09-27"
        and entries["all-printings"]["version"] == "5.3.0+20260927"
    )
    assert entries["card-identifiers"]["day"] == "2026-09-27" and "version" not in entries["card-identifiers"]
    assert tracker.outcomes()["MTGJSON AllPrintings"][1].startswith(
        "kept the list built 2026-09-27 06:12 UTC (2026-09-27)"
    )


def test_a_catalog_not_new_is_a_304(every_file):
    watch(Source(catalogs()))
    source = Source(catalogs())
    res = watch(source)
    assert res.same == [f"MTGJSON {file.label}" for file in EVERY_FILE] and not res.kept
    assert None not in source.etags


def test_a_catalog_that_isnt_whole_is_set_aside_once_a_build_and_the_rest_still_kept(every_file, tracker):
    broken = b"<html>busy</html>"
    overrides = {f"{B}/AllPrintings.json.xz": broken, f"{B}/AllPrintings.json.xz.sha256": sha(broken)}
    res = watch(Source(catalogs(**overrides)), tracker)
    aside = "mtgjson/aside/AllPrintings-2026-09-27T130000Z.json.xz"
    assert res.failed == [
        ("MTGJSON AllPrintings", f"AllPrintings.json.xz: not xz; set aside as {aside}, asked again next run")
    ]
    assert len(res.kept) == len(EVERY_FILE) - 1 and (every_file / aside).read_bytes() == broken
    watch(Source(catalogs(**overrides)), tracker, now=NOW + timedelta(minutes=5))
    said = (
        f"AllPrintings.json.xz: not xz"
        f"; a copy of this publish is set aside already as {aside}, asked again next run"
    )
    assert tracker.outcomes()["MTGJSON AllPrintings"] == ("fail", said)
    assert len(list((every_file / "mtgjson" / "aside").iterdir())) == 1
    last = [entry for entry in logged(every_file) if entry["list"] == "all-printings"][-1]
    assert last["not_kept"] is True and last["publish"] == "2026-09-27T061238Z"


def test_a_manifest_that_isnt_json_is_set_aside(every_file, tracker):
    watch(Source(catalogs(**{f"{B}/BuildManifest.json": b"<html>busy</html>"})), tracker)
    aside = "mtgjson/aside/BuildManifest-2026-09-27T130000Z.json"
    assert tracker.outcomes()["MTGJSON BuildManifest"] == (
        "fail",
        f"BuildManifest.json: not the expected JSON; set aside as {aside}, asked again next run",
    )


def test_a_file_with_no_etag_or_time_is_set_aside_once_its_bytes(every_file, tracker):
    """With nothing to say which publish a broken copy is, a copy the same byte for byte isn't
    kept again, and any other is: nothing is lost."""
    for minutes, body in ((0, b"<html>busy</html>"), (5, b"<html>busy</html>"), (10, b"<html>down</html>")):
        overrides = {f"{B}/AllPrintings.json.xz": body, f"{B}/AllPrintings.json.xz.sha256": sha(body)}
        watch(
            Source(catalogs(**overrides), modified=None, tagged=False), now=NOW + timedelta(minutes=minutes)
        )
    names = [
        p.name for p in (every_file / "mtgjson" / "aside").iterdir() if p.name.startswith("AllPrintings")
    ]
    assert sorted(names) == [
        "AllPrintings-2026-09-27T130000Z.json.xz",
        "AllPrintings-2026-09-27T131000Z.json.xz",
    ]


def learned(name: str, last: datetime) -> None:
    """14 gaps of a day kept for a list, the last made at last, and a clean check a minute ago."""
    folder = mtgjson.lists_dir(name)
    for i in range(15):
        stamp = runs.name(last - timedelta(days=i))
        (folder / stamp).mkdir(parents=True)
        (folder / stamp / f"{stamp}{runs.BASE}").touch()
    checked = runs.name(NOW - timedelta(minutes=1))
    watching.log("mtgjson", {"at": checked, "list": name, "result": "unchanged"})


def test_lists_not_due_share_one_line_naming_the_next(every_file, tracker):
    for file in EVERY_FILE:
        learned(file.list, datetime(2026, 9, 27, 6, 12, tzinfo=UTC))  # today's build
    source = Source(catalogs())
    res = watch(source, tracker, now=NOW, always=False)
    assert source.asked == [] and len(res.waiting) == len(EVERY_FILE)
    assert tracker.outcomes() == {
        f"MTGJSON {len(EVERY_FILE)} lists": (
            "ok",
            "none due; prices today next asked 2026-09-27 13:04 UTC"
            "; its next list expected 2026-09-28 06:12 UTC",
        )
    }
