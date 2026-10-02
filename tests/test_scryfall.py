import gzip
import hashlib
import json
import os
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from riffle import locks, net, watching
from riffle.cli import app
from riffle.ingest import scryfall
from riffle.ingest.scryfall import download_url
from riffle.progress import SILENT


def test_prefers_jsonl_link_after_the_2026_format_change():
    info = {"jsonl_download_uri": "https://x/default.jsonl.gz", "download_uri": "https://x/default.json"}
    assert download_url(info) == ("https://x/default.jsonl.gz", ".jsonl.gz")


def test_falls_back_to_the_old_array_link():
    assert download_url({"download_uri": "https://x/d.json"}) == ("https://x/d.json", ".json")


def test_an_entry_without_a_link_is_an_error():
    with pytest.raises(RuntimeError, match="no download link"):
        download_url({"type": "rulings"})


# ---- daily price snapshots ----------------------------------------------------

CARDS = [
    {"id": "aaa", "name": "Sol Ring", "prices": {"usd": "1.50", "usd_foil": None, "eur": "1.20"}},
    {"id": "bbb", "name": "Island", "prices": {"usd": "0.05", "usd_foil": "0.30", "tix": None}},
]


def write_bulk(data_dir, name="default-cards.jsonl.gz", updated_at="2026-09-24T09:05:40.725+00:00"):
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / name
    if name.endswith(".jsonl.gz"):
        with gzip.open(path, "wt", encoding="utf-8") as f:
            f.writelines(json.dumps(c) + "\n" for c in CARDS)
    else:
        path.write_text(json.dumps(CARDS))
    if updated_at:
        (data_dir / "bulk-meta.json").write_text(json.dumps({"updated_at": updated_at, "rows": 2}))
    return path


def snapshot_lines(path):
    with gzip.open(path, "rt", encoding="utf-8") as f:
        return [json.loads(line) for line in f]


def test_snapshot_keeps_each_printing_prices_for_the_bulk_day(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    write_bulk(tmp_path / "riffle")
    path, written = scryfall.snapshot_prices()
    assert written and path == tmp_path / "riffle" / "scryfall" / "daily" / "2026-09-24.jsonl.gz"
    assert snapshot_lines(path) == [{"id": c["id"], "prices": c["prices"]} for c in CARDS]
    assert scryfall.snapshot_prices() == (path, False)


def test_snapshot_reads_the_old_array_format_and_dates_by_mtime_without_meta(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    bulk = write_bulk(tmp_path / "riffle", name="default-cards.json", updated_at=None)
    stamp = datetime(2026, 3, 4, 12, tzinfo=UTC).timestamp()
    os.utime(bulk, (stamp, stamp))
    assert scryfall.bulk_day(bulk) == date(2026, 3, 4)
    path, written = scryfall.snapshot_prices()
    assert written and path.name == "2026-03-04.jsonl.gz" and len(snapshot_lines(path)) == 2


def test_snapshot_needs_a_bulk_file(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    with pytest.raises(FileNotFoundError):
        scryfall.snapshot_prices()
    (tmp_path / "riffle").mkdir()
    (tmp_path / "riffle" / "default-cards.jsonl.gz.part").write_bytes(b"")  # a half download doesn't count
    assert scryfall.bulk_file() is None


# ---- every bulk file, kept by publish time ---------------------------------------------------

AM = "2026-09-24T09:05:40.725+00:00"
PM = "2026-09-24T21:05:23.456+00:00"
SET_LIST = b'{"data": [{"code": "lea"}, {"code": "leb"}]}'
RULES = [{"oracle_id": "o1", "comment": "It does."}]


def entry(kind, updated_at, url=None):
    return {"type": kind, "updated_at": updated_at, "jsonl_download_uri": url or f"https://x/{kind}.jsonl.gz"}


def gz(rows):
    return gzip.compress("".join(json.dumps(r) + "\n" for r in rows).encode())


def lines(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


class Scryfall:
    """Scryfall as a sync sees it: the bulk index and set list from api.scryfall.com (net.get),
    and the files themselves (net.download), each by URL."""

    def __init__(self, monkeypatch, *entries, files=None, sets=SET_LIST):
        self.entries, self.files, self.sets = list(entries), dict(files or {}), sets
        self.downloaded: list[str] = []
        self.asked: list[str] = []  # each api.scryfall.com request
        monkeypatch.setattr(net, "get", self.get)
        monkeypatch.setattr(net, "download", self.download)

    def get(self, url, accept):
        self.asked.append(url)
        if url == scryfall.BULK_INDEX:
            return json.dumps({"object": "list", "data": self.entries}).encode()
        assert url == scryfall.SETS
        return self.sets

    def download(self, url, dest, progress=None, **kwargs):
        self.downloaded.append(url)
        body = self.files.get(url)
        if body is None:
            return None
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(body)
        if progress:
            progress(len(body), len(body))
        return len(body)


@pytest.fixture
def clock(monkeypatch):
    """times.now, set by the test: clock.now = ..."""
    clock = SimpleNamespace(now=datetime(2026, 9, 24, 13, 0, 6, tzinfo=UTC))
    monkeypatch.setattr(scryfall.times, "now", lambda: clock.now)
    return clock


def test_refresh_keeps_every_file_scryfall_lists(monkeypatch, tracker, clock):
    default, rulings = entry("default_cards", AM), entry("rulings", "2026-09-24T09:00:37.501+00:00")
    files = {default["jsonl_download_uri"]: gz(CARDS), rulings["jsonl_download_uri"]: gz(RULES)}
    Scryfall(monkeypatch, default, rulings, files=files)
    scryfall.refresh(tracker=tracker)
    assert tracker.outcomes() == {
        "Scryfall default cards": ("ok", "kept the 2026-09-24 09:05 UTC file, 0.0 MB"),
        "Scryfall set list": ("ok", "2 sets, kept"),
        "Scryfall rulings": ("ok", "kept the 2026-09-24 09:00 UTC file, 0.0 MB"),
    }
    assert scryfall.bulk_file() == scryfall.bulk_dir() / "2026-09-24T090540Z.jsonl.gz"
    assert scryfall.bulk_file().read_bytes() == files[default["jsonl_download_uri"]]  # kept as served
    assert [p.name for p in scryfall.kept_files("rulings")] == ["2026-09-24T090037Z.jsonl.gz"]
    assert tracker.steps[0].unit == "bytes" and tracker.steps[0].updates
    first, second = lines(scryfall.checks_path())
    assert (
        first["type"] == "default_cards"
        and first["published"] == AM
        and first["fetched"] == "2026-09-24T130006Z"
    )
    assert first["kept"] == "2026-09-24T090540Z.jsonl.gz" and len(first["sha256"]) == 64
    assert second["type"] == "rulings" and second["kept"] == "2026-09-24T090037Z.jsonl.gz"
    assert scryfall.bulk_updated_at(scryfall.bulk_file()) == datetime(2026, 9, 24, 9, 5, 40, tzinfo=UTC)
    assert [p.name for p in scryfall.sets_dir().glob("*.json")] == ["2026-09-24T130006Z.json"]


def test_both_files_of_a_day_are_kept(monkeypatch, tracker, clock):
    """Scryfall publishes about twice a day; before 0.4.0 the second file of a day was skipped."""
    am, pm = (
        entry("default_cards", AM, "https://x/am.jsonl.gz"),
        entry("default_cards", PM, "https://x/pm.jsonl.gz"),
    )
    served = Scryfall(
        monkeypatch, am, files={am["jsonl_download_uri"]: gz(CARDS), pm["jsonl_download_uri"]: gz(CARDS[:1])}
    )
    scryfall.refresh()
    served.entries = [pm]
    clock.now = datetime(2026, 9, 24, 22, 0, 5, tzinfo=UTC)
    scryfall.refresh(tracker=tracker)
    assert tracker.outcomes() == {
        "Scryfall default cards": ("ok", "kept the 2026-09-24 21:05 UTC file, 0.0 MB"),
        "Scryfall set list": ("ok", "2 sets, unchanged since 2026-09-24 13:00 UTC"),
    }
    assert [p.name for p in scryfall.kept_files()] == [
        "2026-09-24T090540Z.jsonl.gz",
        "2026-09-24T210523Z.jsonl.gz",
    ]
    assert scryfall.bulk_file().name == "2026-09-24T210523Z.jsonl.gz"
    assert lines(scryfall.sets_dir() / "checks.jsonl")[-1]["same_as"] == "2026-09-24T130006Z.json"


def test_a_publish_already_seen_isnt_downloaded_again(monkeypatch, tracker, clock):
    default, tags = entry("default_cards", AM), entry("oracle_tags", AM)
    files = {default["jsonl_download_uri"]: gz(CARDS), tags["jsonl_download_uri"]: gz([{"label": "ramp"}])}
    served = Scryfall(monkeypatch, default, tags, files=files)
    scryfall.refresh()
    scryfall.refresh(tracker=tracker)
    assert tracker.outcomes() == {
        "Scryfall default cards": ("ok", "current, the 2026-09-24 09:05 UTC file"),
        "Scryfall oracle tags": ("ok", "current, the 2026-09-24 09:05 UTC file"),
    }
    assert len(served.downloaded) == 2


def test_the_same_contents_are_not_kept_twice(monkeypatch, tracker, clock):
    """The check log records the publish; a file of the same contents isn't written again."""
    am, pm = (
        entry("default_cards", AM, "https://x/am.jsonl.gz"),
        entry("default_cards", PM, "https://x/pm.jsonl.gz"),
    )
    same = gz(CARDS)
    served = Scryfall(monkeypatch, am, files={am["jsonl_download_uri"]: same, pm["jsonl_download_uri"]: same})
    scryfall.refresh()
    served.entries = [pm]
    scryfall.refresh(tracker=tracker)
    assert tracker.outcomes()["Scryfall default cards"] == (
        "ok",
        "the same as the 2026-09-24 09:05 UTC file; not kept twice",
    )
    assert [p.name for p in scryfall.kept_files()] == ["2026-09-24T090540Z.jsonl.gz"]
    last = lines(scryfall.checks_path())[-1]
    assert last["published"] == PM and last["same_as"] == "2026-09-24T090540Z.jsonl.gz" and "kept" not in last
    assert scryfall.is_current(pm)
    assert watching.entries(scryfall.STORE)[-1] == watching.entries(scryfall.STORE)[-1] | {
        "result": "new",  # a new publish all the same, for when it went online (riffle.watching.online)
        "made": "2026-09-24T210523Z",
        "same_as": "2026-09-24T090540Z.jsonl.gz",
    }
    assert not list(scryfall.bulk_dir().glob("*.new"))
    scryfall.refresh(tracker=tracker)
    assert tracker.outcomes()["Scryfall default cards"] == ("ok", "current, the 2026-09-24 21:05 UTC file")


@pytest.mark.parametrize(
    ("published", "current"),
    [(AM, True), (PM, False), ("2026-09-23T21:05:23.456+00:00", False)],
    ids=["the file kept", "later the same day", "an earlier file never seen"],
)
def test_current_means_this_very_publish_is_kept(monkeypatch, clock, published, current):
    default = entry("default_cards", AM)
    Scryfall(monkeypatch, default, files={default["jsonl_download_uri"]: gz(CARDS)})
    scryfall.refresh()
    assert scryfall.is_current({"updated_at": published}) is current


def test_a_missing_or_unstamped_download_is_never_current(monkeypatch, clock):
    assert not scryfall.is_current({"updated_at": AM})
    default = entry("default_cards", AM)
    Scryfall(monkeypatch, default, files={default["jsonl_download_uri"]: gz(CARDS)})
    scryfall.refresh()
    assert not scryfall.is_current({"updated_at": None})
    assert not scryfall.is_current({"updated_at": "not a time"})


@pytest.mark.parametrize(
    ("error", "why"),
    [
        (net.FetchError("HTTP 503"), "HTTP 503"),
        (KeyError(), "KeyError (unexpected; details in {log}); asked again next run"),
    ],
    ids=["the source's", "a bug"],
)
def test_a_failed_download_fails_only_its_own_step(monkeypatch, tracker, clock, error, why):
    """A sync carries on with the files kept, and the other files are still asked for. A bug's
    traceback goes to errors.log, as in every watch."""
    default, art = entry("default_cards", AM), entry("art_tags", AM)
    served = Scryfall(monkeypatch, default, art, files={art["jsonl_download_uri"]: gz([{"label": "cat"}])})
    download = served.download

    def failing(url, dest, progress=None, **kwargs):
        if url == default["jsonl_download_uri"]:
            raise error
        return download(url, dest, progress)

    monkeypatch.setattr(net, "download", failing)
    scryfall.refresh(tracker=tracker)
    assert tracker.outcomes() == {
        "Scryfall default cards": ("fail", why.format(log=scryfall.data_dir() / "errors.log")),
        "Scryfall art tags": ("ok", "kept the 2026-09-24 09:05 UTC file, 0.0 MB"),
    }
    assert scryfall.bulk_file() is None


def test_another_file_failing_fails_only_its_own_step(monkeypatch, tracker, clock):
    default, cards = entry("default_cards", AM), entry("all_cards", AM)
    Scryfall(monkeypatch, default, cards, files={default["jsonl_download_uri"]: gz(CARDS)})
    scryfall.refresh(tracker=tracker)
    assert tracker.outcomes() == {
        "Scryfall default cards": ("ok", "kept the 2026-09-24 09:05 UTC file, 0.0 MB"),
        "Scryfall set list": ("ok", "2 sets, kept"),
        "Scryfall all cards": ("fail", "Scryfall's all_cards file is missing (https://x/all_cards.jsonl.gz)"),
    }


def test_a_file_scryfall_doesnt_serve_fails_its_step(monkeypatch, tracker, clock):
    default = entry("default_cards", AM)
    Scryfall(monkeypatch, default)
    scryfall.refresh(tracker=tracker)
    assert tracker.outcomes() == {
        "Scryfall default cards": (
            "fail",
            "Scryfall's default_cards file is missing (https://x/default_cards.jsonl.gz)",
        )
    }
    with pytest.raises(RuntimeError, match="rulings entry has no publish time"):
        scryfall.download({"type": "rulings", "jsonl_download_uri": "https://x/r.jsonl.gz"})


@pytest.mark.parametrize(
    ("answer", "why"),
    [
        (None, "Scryfall's bulk index is missing (https://api.scryfall.com/bulk-data)"),
        (b'{"object": "error"}', "Scryfall's bulk index isn't the expected JSON"),
        (b'{"data": [{"name": "no type"}]}', "Scryfall's bulk index isn't the expected JSON"),
        (b"<html>maintenance</html>", "Scryfall's bulk index isn't the expected JSON"),
        (b"[]", "Scryfall's bulk index isn't the expected JSON"),
    ],
    ids=["missing", "no list", "an entry without a type", "not JSON", "not an object"],
)
def test_an_unreadable_bulk_index_fails_and_keeps_what_is_kept(monkeypatch, tracker, answer, why):
    bulk = write_bulk(scryfall.data_dir())
    monkeypatch.setattr(net, "get", lambda url, accept: answer)
    scryfall.refresh(tracker=tracker)
    assert tracker.outcomes() == {"Scryfall default cards": ("fail", why)}
    assert scryfall.bulk_file() == scryfall.bulk_dir() / "2026-09-24T090540Z.jsonl.gz"
    assert (
        scryfall.bulk_file().read_bytes()
        == bulk.with_name(bulk.name)
        .parent.joinpath("scryfall/bulk/default_cards/2026-09-24T090540Z.jsonl.gz")
        .read_bytes()
    )


def test_a_bulk_index_that_doesnt_answer_fails_the_step(monkeypatch, tracker):
    """No answer at all: the types after the first aren't asked."""
    asked = []

    def get(url, accept):
        asked.append(url)
        raise net.NoAnswer("no answer after 3 tries (timed out)")

    monkeypatch.setattr(net, "get", get)
    scryfall.bulk_dir("rulings").mkdir(parents=True)
    scryfall.refresh(tracker=tracker)
    assert tracker.outcomes() == {
        "Scryfall default cards": ("fail", "Scryfall's bulk index: no answer after 3 tries (timed out)"),
        "Scryfall rulings": ("fail", "not asked: Scryfall gave no answer"),
    }
    assert asked == [scryfall.BULK_INDEX]


def test_a_bulk_index_that_fails_is_asked_once_a_run(monkeypatch, tracker):
    """An error from the index fails the first type with it, and each type after says so."""
    asked = []

    def get(url, accept):
        asked.append(url)
        raise net.FetchError("HTTP 503")

    monkeypatch.setattr(net, "get", get)
    scryfall.bulk_dir("rulings").mkdir(parents=True)
    scryfall.refresh(tracker=tracker)
    assert tracker.outcomes() == {
        "Scryfall default cards": ("fail", "Scryfall's bulk index: HTTP 503"),
        "Scryfall rulings": ("fail", "not asked: Scryfall's bulk index failed"),
    }
    assert asked == [scryfall.BULK_INDEX]


def test_an_index_without_default_cards_still_keeps_the_rest(monkeypatch, tracker, clock):
    rulings = entry("rulings", AM)
    Scryfall(monkeypatch, rulings, files={rulings["jsonl_download_uri"]: gz(RULES)})
    scryfall.refresh(tracker=tracker)
    assert tracker.outcomes() == {
        "Scryfall default cards": ("fail", "no bulk file of type default_cards"),
        "Scryfall rulings": ("ok", "kept the 2026-09-24 09:05 UTC file, 0.0 MB"),
    }


@pytest.mark.parametrize(
    ("body", "why"),
    [
        (b"<html>maintenance</html>", "isn't gzip — Scryfall's format may have changed again"),
        (
            gz(CARDS)[:-12],
            "is cut short or corrupt (Compressed file ended before the end-of-stream marker was reached)",
        ),
    ],
    ids=["not gzip", "cut short"],
)
def test_a_bad_download_is_set_aside_and_asked_again(monkeypatch, tracker, clock, body, why):
    default = entry("default_cards", AM)
    served = Scryfall(monkeypatch, default, files={default["jsonl_download_uri"]: body})
    scryfall.refresh(tracker=tracker)
    aside = "2026-09-24T090540Z-2026-09-24T130006Z.jsonl.gz.bad"
    assert tracker.outcomes() == {
        "Scryfall default cards": (
            "fail",
            f"the default_cards file {why}; set aside as {aside}, asked again next run",
        )
    }
    assert (scryfall.bulk_dir() / aside).read_bytes() == body  # kept, never deleted
    assert scryfall.bulk_file() is None and not scryfall.is_current(default)
    served.files[default["jsonl_download_uri"]] = gz(CARDS)
    scryfall.refresh(tracker=tracker)
    assert scryfall.bulk_file().name == "2026-09-24T090540Z.jsonl.gz"


def test_a_file_served_broken_again_is_set_aside_once_a_publish(monkeypatch, tracker, clock):
    """A publish served broken at every refresh is kept once, not twice a day; each later copy
    goes in the check log."""
    default = entry("default_cards", AM)
    body = b"<html>maintenance</html>"
    Scryfall(monkeypatch, default, files={default["jsonl_download_uri"]: body})
    scryfall.refresh()
    scryfall.refresh(tracker=tracker)
    aside = "2026-09-24T090540Z-2026-09-24T130006Z.jsonl.gz.bad"
    why = "isn't gzip — Scryfall's format may have changed again"
    assert tracker.outcomes()["Scryfall default cards"] == (
        "fail",
        f"the default_cards file {why}"
        f"; a copy of this publish is set aside already as {aside}, asked again next run",
    )
    assert [p.name for p in scryfall.bulk_dir().iterdir()] == [aside]
    line = json.loads(scryfall.checks_path().read_text().splitlines()[-1])
    assert line == line | {
        "type": "default_cards",
        "stamp": "2026-09-24T090540Z",
        "not_kept": True,
        "aside": aside,
    }
    assert line["served_sha256"] == hashlib.sha256(body).hexdigest() and line["size"] == len(body)


def test_force_downloads_default_cards_again(monkeypatch, tracker, clock):
    default = entry("default_cards", AM)
    served = Scryfall(monkeypatch, default, files={default["jsonl_download_uri"]: gz(CARDS)})
    scryfall.refresh()
    scryfall.refresh(force=True, tracker=tracker)
    assert tracker.outcomes()["Scryfall default cards"] == (
        "ok",
        "the same as the 2026-09-24 09:05 UTC file; not kept twice",
    )
    served.files[default["jsonl_download_uri"]] = gz(CARDS[:1])  # the same publish, served different
    clock.now = datetime(2026, 9, 24, 14, 0, tzinfo=UTC)
    scryfall.refresh(force=True, tracker=tracker)
    assert [p.name for p in scryfall.kept_files()] == [
        "2026-09-24T090540Z.jsonl.gz",
        "2026-09-24T090540Z-2026-09-24T140000Z.jsonl.gz",
    ]
    assert scryfall.bulk_file().name == "2026-09-24T090540Z-2026-09-24T140000Z.jsonl.gz"


def test_the_old_json_array_is_kept_as_json(monkeypatch, clock):
    info = {"type": "default_cards", "updated_at": AM, "download_uri": "https://x/d.json"}
    Scryfall(monkeypatch, info, files={"https://x/d.json": json.dumps(CARDS).encode()})
    path, kept = scryfall.download(info)
    assert kept and path.name == "2026-09-24T090540Z.json"
    assert [c["id"] for c in scryfall.cards(path)] == ["aaa", "bbb"]


def test_blank_lines_in_a_bulk_file_are_skipped(tmp_path):
    path = tmp_path / "2026-09-24T090540Z.jsonl.gz"
    path.write_bytes(gzip.compress(b'{"id": "aaa"}\n\n{"id": "bbb"}\n'))
    assert [c["id"] for c in scryfall.cards(path)] == ["aaa", "bbb"]


# ---- the watch --------------------------------------------------------------------------------


def watched(clock, tracker=None):
    """One run of `riffle watch scryfall` at clock.now."""
    return scryfall.watch(tracker=tracker or SILENT, clock=lambda: clock.now)


def published(kind, n, last, every=timedelta(hours=12)):
    """n publishes of a type in the check log, every 12 hours to last; the last one's file kept."""
    for i in range(n):
        stamp = scryfall._stamp(last - every * (n - 1 - i))
        _append(scryfall.checks_path(), {"type": kind, "stamp": stamp, "kept": f"{stamp}.jsonl.gz"})
    scryfall.bulk_dir(kind).mkdir(parents=True, exist_ok=True)
    (scryfall.bulk_dir(kind) / f"{stamp}.jsonl.gz").write_bytes(gz(CARDS))


def _append(path, line):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(line) + "\n")


def test_the_watch_asks_the_index_once_a_run_for_every_type_due(monkeypatch, tracker, clock):
    default, rulings = entry("default_cards", AM), entry("rulings", AM)
    files = {default["jsonl_download_uri"]: gz(CARDS), rulings["jsonl_download_uri"]: gz(RULES)}
    served = Scryfall(monkeypatch, default, rulings, files=files)
    learning = "asked at every firing until it has 14 gaps, 0 so far"
    watched(clock, tracker)
    assert tracker.outcomes() == {
        "Scryfall default cards": ("ok", f"kept the 2026-09-24 09:05 UTC file, 0.0 MB; {learning}"),
        "Scryfall rulings": ("ok", "kept the 2026-09-24 09:05 UTC file, 0.0 MB"),  # first seen in the index
        "Scryfall set list": ("ok", "2 sets, kept"),
    }
    assert served.asked == [scryfall.BULK_INDEX, scryfall.SETS]
    clock.now = datetime(2026, 9, 24, 13, 5, 6, tzinfo=UTC)
    tracker.steps.clear()
    watched(clock, tracker)
    assert tracker.outcomes() == {
        "Scryfall default cards": ("ok", f"current, the 2026-09-24 09:05 UTC file; {learning}"),
        "Scryfall rulings": ("ok", f"current, the 2026-09-24 09:05 UTC file; {learning}"),
    }
    assert served.asked == [scryfall.BULK_INDEX, scryfall.SETS, scryfall.BULK_INDEX]
    assert len(served.downloaded) == 2
    found = watching.entries(scryfall.STORE)
    assert [(e["list"], e["result"]) for e in found] == [
        ("default_cards", "kept"),
        ("rulings", "kept"),
        ("default_cards", "known"),
        ("rulings", "known"),
    ]
    assert found[0] == found[0] | {
        "at": "2026-09-24T130006Z",
        "made": "2026-09-24T090540Z",
        "file": "scryfall/bulk/default_cards/2026-09-24T090540Z.jsonl.gz",
        "size": len(gz(CARDS)),
    }
    assert found[2]["made"] == "2026-09-24T090540Z"


def test_a_type_is_asked_when_its_next_publish_is_due(monkeypatch, tracker, clock):
    """Once a type has 14 gaps, a firing asks for it only when it's due, and asks nothing else."""
    last = datetime(2026, 9, 24, 9, 5, 40, tzinfo=UTC)
    published("default_cards", 16, last)
    scryfall.sets_path().write_bytes(SET_LIST)
    watching.log(scryfall.STORE, {"at": "2026-09-24T130000Z", "list": "default_cards", "result": "known"})
    served = Scryfall(monkeypatch, entry("default_cards", AM))
    clock.now = datetime(2026, 9, 24, 13, 1, tzinfo=UTC)
    watched(clock, tracker)
    assert tracker.outcomes() == {
        "Scryfall 1 file": (
            "ok",
            "none due; default cards next asked 2026-09-24 13:05 UTC;"
            " its next file expected 2026-09-24 21:05 UTC",
        )
    }
    assert served.asked == []
    clock.now = datetime(2026, 9, 24, 13, 6, tzinfo=UTC)
    tracker.steps.clear()
    watched(clock, tracker)
    assert tracker.outcomes() == {"Scryfall default cards": ("ok", "current, the 2026-09-24 09:05 UTC file")}
    assert served.asked == [scryfall.BULK_INDEX]


def test_a_sync_that_finds_the_watch_asking_asks_nothing(monkeypatch, tracker, clock):
    default = entry("default_cards", AM)
    served = Scryfall(monkeypatch, default, files={default["jsonl_download_uri"]: gz(CARDS)})
    with locks.held(scryfall.data_dir() / "scryfall" / "watch.lock") as mine:
        assert mine
        scryfall.refresh(tracker=tracker)
    assert tracker.outcomes() == {"Scryfall": ("ok", "another run is asking for its lists")}
    assert not served.asked and not served.downloaded


def test_a_type_the_index_stops_listing_warns_and_one_without_a_time_fails(monkeypatch, tracker, clock):
    default, art = entry("default_cards", AM), entry("art_tags", None)
    scryfall.bulk_dir("rulings").mkdir(parents=True)
    Scryfall(monkeypatch, default, art, files={default["jsonl_download_uri"]: gz(CARDS)})
    scryfall.refresh(tracker=tracker)
    assert tracker.outcomes() == {
        "Scryfall default cards": ("ok", "kept the 2026-09-24 09:05 UTC file, 0.0 MB"),
        "Scryfall rulings": ("warn", "not in Scryfall's bulk index any more"),
        "Scryfall art tags": ("fail", "Scryfall's art_tags entry has no publish time"),
        "Scryfall set list": ("ok", "2 sets, kept"),
    }
    assert [(e["list"], e["result"]) for e in watching.entries(scryfall.STORE)] == [
        ("default_cards", "kept"),
        ("rulings", "missing"),
        ("art_tags", "failed"),
    ]


def test_the_types_and_their_publishes_are_learned_from_the_store():
    assert scryfall.kinds() == ["default_cards"]
    scryfall.bulk_dir("unique_artwork").mkdir(parents=True)
    rulings = scryfall.bulk_dir("rulings")
    rulings.mkdir(parents=True)
    for name in (
        "2026-09-24T090037Z.jsonl.gz",
        "2026-09-24T090037Z-2026-09-24T140000Z.jsonl.gz",  # a second copy of one publish
        "2026-09-24T210000Z.jsonl.gz.bad",  # set aside, not kept
    ):
        (rulings / name).write_bytes(gz(RULES))
    for line in (
        {"type": "rulings", "stamp": "2026-09-24T210033Z", "same_as": "2026-09-24T090037Z.jsonl.gz"},
        {"type": "rulings", "stamp": "2026-09-25T090031Z", "not_kept": True},
        {"type": "rulings", "stamp": "not a time", "kept": "x.jsonl.gz"},
        {"type": "art_tags", "stamp": "2026-09-24T090114Z", "kept": "2026-09-24T090114Z.jsonl.gz"},
        {"type": 7},
    ):
        _append(scryfall.checks_path(), line)
    assert scryfall.kinds() == ["default_cards", "art_tags", "rulings", "unique_artwork"]
    assert scryfall.publishes("rulings") == [
        datetime(2026, 9, 24, 9, 0, 37, tzinfo=UTC),
        datetime(2026, 9, 24, 21, 0, 33, tzinfo=UTC),
    ]
    assert scryfall.publishes("unique_artwork") == []


def test_riffle_watch_scryfall_keeps_a_new_publish_and_exits_1_on_a_failure(monkeypatch, tmp_path, clock):
    monkeypatch.setenv("HOME", str(tmp_path))
    default = entry("default_cards", AM)
    served = Scryfall(monkeypatch, default, files={default["jsonl_download_uri"]: gz(CARDS)})
    result = CliRunner().invoke(app, ["watch", "scryfall"])
    assert result.exit_code == 0, result.output
    assert "Scryfall default cards" in result.output and "kept the 2026-09-24 09:05 UTC file" in result.output
    served.entries = []
    result = CliRunner().invoke(app, ["watch", "scryfall"])
    assert result.exit_code == 1 and "no bulk file of type default_cards" in result.output


# ---- the file kept before 0.4.0 ----------------------------------------------------------------


def test_the_first_refresh_moves_the_file_kept_before(monkeypatch, tracker, clock):
    root = scryfall.data_dir()
    bulk = write_bulk(root)
    content = bulk.read_bytes()
    bad = root / "default-cards.jsonl.gz.bad"
    bad.write_bytes(b"cut short")
    stamp = datetime(2026, 9, 20, 7, 0, tzinfo=UTC).timestamp()
    os.utime(bad, (stamp, stamp))
    assert scryfall.bulk_file() == bulk  # read where it is until then
    default = entry("default_cards", AM)
    served = Scryfall(monkeypatch, default)
    scryfall.sets_path().parent.mkdir(parents=True, exist_ok=True)
    scryfall.sets_path().write_text('{"data": []}')
    scryfall.refresh(tracker=tracker)
    assert tracker.outcomes() == {"Scryfall default cards": ("ok", "current, the 2026-09-24 09:05 UTC file")}
    assert not served.downloaded
    assert scryfall.bulk_file() == scryfall.bulk_dir() / "2026-09-24T090540Z.jsonl.gz"
    assert scryfall.bulk_file().read_bytes() == content
    assert (scryfall.bulk_dir() / "2026-09-20T070000Z.jsonl.gz.bad").read_bytes() == b"cut short"
    assert not bulk.exists() and not bad.exists()


def test_a_file_kept_before_is_compared_by_its_contents(monkeypatch, tracker, clock):
    """The moved file has no line in the check log, so its contents are read to compare."""
    write_bulk(scryfall.data_dir())
    pm = entry("default_cards", PM)
    with gzip.open(scryfall.data_dir() / "default-cards.jsonl.gz", "rb") as f:
        same = gzip.compress(f.read())
    Scryfall(monkeypatch, pm, files={pm["jsonl_download_uri"]: same})
    scryfall.checks_path().parent.mkdir(parents=True)
    scryfall.checks_path().write_text('not json\n[1]\n{"type": "default_cards"}\n')  # skipped
    scryfall.refresh(tracker=tracker)
    assert tracker.outcomes()["Scryfall default cards"] == (
        "ok",
        "the same as the 2026-09-24 09:05 UTC file; not kept twice",
    )


def test_a_file_kept_before_without_its_publish_time_goes_by_its_file_time(monkeypatch, clock):
    bulk = write_bulk(scryfall.data_dir(), updated_at=None)
    stamp = datetime(2026, 9, 22, 13, 0, 6, tzinfo=UTC).timestamp()
    os.utime(bulk, (stamp, stamp))
    Scryfall(monkeypatch)
    scryfall.refresh()
    assert scryfall.bulk_file() == scryfall.bulk_dir() / "2026-09-22T130006Z.jsonl.gz"


def test_a_name_already_taken_leaves_the_file_where_it_is(monkeypatch, clock):
    bulk = write_bulk(scryfall.data_dir())
    taken = scryfall.bulk_dir() / "2026-09-24T090540Z.jsonl.gz"
    taken.parent.mkdir(parents=True)
    taken.write_bytes(gz(CARDS[:1]))
    (scryfall.data_dir() / "default-cards.jsonl.gz.new").write_bytes(b"mid-download")
    Scryfall(monkeypatch)
    scryfall.refresh()
    assert bulk.exists() and taken.read_bytes() == gz(CARDS[:1])
    assert (scryfall.data_dir() / "default-cards.jsonl.gz.new").exists()


# ---- the set list -----------------------------------------------------------------

PAGE_2 = "https://api.scryfall.com/sets?page=2"


def test_fetch_sets_saves_every_page_as_one_list(monkeypatch):
    pages = {
        scryfall.SETS: {"object": "list", "has_more": True, "next_page": PAGE_2, "data": [{"code": "lea"}]},
        PAGE_2: {"object": "list", "has_more": False, "data": [{"code": "leb"}]},
    }
    asked, pauses = [], []
    monkeypatch.setattr(net, "get", lambda url, accept: asked.append(url) or json.dumps(pages[url]).encode())
    monkeypatch.setattr(scryfall.time, "sleep", pauses.append)
    assert scryfall.fetch_sets() == scryfall.sets_path()
    assert scryfall.read_sets() == [{"code": "lea"}, {"code": "leb"}]
    assert asked == [scryfall.SETS, PAGE_2] and pauses == [scryfall.API_PAUSE]
    assert [f.name for f in scryfall.sets_path().parent.iterdir()] == ["sets.json"]


@pytest.mark.parametrize("body", [None, b'{"object": "error", "status": 500}'], ids=["missing", "not a list"])
def test_fetch_sets_keeps_nothing_that_isnt_a_set_list(monkeypatch, body):
    monkeypatch.setattr(net, "get", lambda url, accept: body)
    with pytest.raises(RuntimeError, match="set list"):
        scryfall.fetch_sets()
    assert not scryfall.sets_path().exists()


def test_there_are_no_sets_before_the_first_fetch():
    assert scryfall.read_sets() == []


def test_each_set_list_that_changes_is_kept_by_fetch_time(monkeypatch, tracker, clock):
    served = Scryfall(monkeypatch)
    scryfall.refresh_sets(tracker)
    clock.now = datetime(2026, 9, 24, 22, 0, 5, tzinfo=UTC)
    scryfall.refresh_sets(tracker)
    served.sets = b'{"data": [{"code": "lea"}, {"code": "leb"}, {"code": "2ed"}]}'
    clock.now = datetime(2026, 9, 25, 13, 0, 4, tzinfo=UTC)
    scryfall.refresh_sets(tracker)
    assert [s.outcome for s in tracker.steps] == [
        ("ok", "2 sets, kept"),
        ("ok", "2 sets, unchanged since 2026-09-24 13:00 UTC"),
        ("ok", "3 sets, changed, kept"),
    ]
    assert [p.name for p in sorted(scryfall.sets_dir().glob("*.json"))] == [
        "2026-09-24T130006Z.json",
        "2026-09-25T130004Z.json",
    ]
    assert [
        (line["fetched"], line.get("kept") or line["same_as"])
        for line in lines(scryfall.sets_dir() / "checks.jsonl")
    ] == [
        ("2026-09-24T130006Z", "2026-09-24T130006Z.json"),
        ("2026-09-24T220005Z", "2026-09-24T130006Z.json"),
        ("2026-09-25T130004Z", "2026-09-25T130004Z.json"),
    ]


def test_a_set_list_kept_before_is_copied_first_by_its_file_time(monkeypatch, tracker, clock):
    scryfall.sets_path().parent.mkdir(parents=True)
    scryfall.sets_path().write_text('{"data": [{"code": "lea"}]}')
    stamp = datetime(2026, 9, 21, 13, 0, 2, tzinfo=UTC).timestamp()
    os.utime(scryfall.sets_path(), (stamp, stamp))
    Scryfall(monkeypatch)
    scryfall.refresh_sets(tracker)
    assert tracker.outcomes() == {"Scryfall set list": ("ok", "2 sets, changed, kept")}
    assert (scryfall.sets_dir() / "2026-09-21T130002Z.json").read_text() == '{"data": [{"code": "lea"}]}'


def test_a_current_download_without_a_set_list_fetches_one(monkeypatch, tracker, clock):
    default = entry("default_cards", AM)
    Scryfall(monkeypatch, default, files={default["jsonl_download_uri"]: gz(CARDS)})
    scryfall.refresh()
    scryfall.sets_path().unlink()
    scryfall.refresh(tracker=tracker)
    assert tracker.outcomes() == {
        "Scryfall default cards": ("ok", "current, the 2026-09-24 09:05 UTC file"),
        "Scryfall set list": ("ok", "2 sets, unchanged since 2026-09-24 13:00 UTC"),
    }


def test_a_failed_set_list_fetch_is_reported_never_raised(monkeypatch, tracker, clock):
    """Only the Postgres catalog needs the set list: it mustn't stop a sync."""
    default = entry("default_cards", AM)
    served = Scryfall(monkeypatch, default, files={default["jsonl_download_uri"]: gz(CARDS)})
    get = served.get

    def failing(url, accept):
        if url == scryfall.SETS:
            raise net.FetchError("HTTP 503")
        return get(url, accept)

    monkeypatch.setattr(net, "get", failing)
    scryfall.refresh(tracker=tracker)
    assert tracker.outcomes() == {
        "Scryfall default cards": ("ok", "kept the 2026-09-24 09:05 UTC file, 0.0 MB"),
        "Scryfall set list": ("fail", "HTTP 503"),
    }


# ---- the bulk file's day, and a kept file found corrupt ---------------------------------------------


def test_the_bulk_file_s_publication_time(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    bulk = write_bulk(tmp_path / "riffle")
    assert scryfall.bulk_updated_at(bulk) == datetime(2026, 9, 24, 9, 5, 40, 725000, tzinfo=UTC)
    write_bulk(tmp_path / "riffle", updated_at="2026-09-24T09:05:40")
    assert scryfall.bulk_updated_at(bulk) == datetime(2026, 9, 24, 9, 5, 40, tzinfo=UTC)


def test_a_bulk_file_cut_short_is_set_aside_and_fetched_again(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    bulk = write_bulk(tmp_path / "riffle")
    bulk.write_bytes(bulk.read_bytes()[:-12])  # the gzip trailer and some data gone
    with pytest.raises(
        scryfall.CorruptBulk, match="cut short or corrupt.*set aside as default-cards.jsonl.gz.bad"
    ):
        scryfall.snapshot_prices()
    assert scryfall.bulk_file() is None  # so the next online refresh downloads it again
    assert (tmp_path / "riffle" / "default-cards.jsonl.gz.bad").exists()
    assert not scryfall.is_current({"updated_at": AM})
    assert not list((tmp_path / "riffle" / "scryfall" / "daily").glob("*"))


def test_a_kept_file_found_corrupt_twice_keeps_both(monkeypatch, clock):
    """A publish downloaded again and found corrupt again: the second is set aside beside the
    first, never over it."""
    folder = scryfall.bulk_dir()
    folder.mkdir(parents=True)
    kept = folder / "2026-09-24T090540Z.jsonl.gz"
    for _ in range(2):
        kept.write_bytes(gz(CARDS)[:-12])
        with pytest.raises(scryfall.CorruptBulk):
            scryfall.snapshot_prices()
    assert sorted(p.name for p in folder.iterdir()) == [
        "2026-09-24T090540Z-2026-09-24T130006Z.jsonl.gz.bad",
        "2026-09-24T090540Z.jsonl.gz.bad",
    ]


@pytest.mark.parametrize("meta", ["{not json", "[]", '"2026-09-24"', "\xff"])
def test_an_unreadable_bulk_meta_counts_as_missing(tmp_path, monkeypatch, meta):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    bulk = write_bulk(tmp_path / "riffle")
    scryfall.meta_path().write_text(meta, encoding="latin-1")
    stamp = datetime(2026, 9, 24, 12, tzinfo=UTC).timestamp()
    os.utime(bulk, (stamp, stamp))
    assert scryfall.bulk_day(bulk) == date(2026, 9, 24)


def test_an_error_while_keeping_prices_leaves_no_partial_file(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    bulk = write_bulk(tmp_path / "riffle")
    with gzip.open(bulk, "wt", encoding="utf-8") as f:
        f.write('{"name": "no id"}\n')
    with pytest.raises(KeyError):
        scryfall.snapshot_prices()
    assert not list((tmp_path / "riffle" / "scryfall" / "daily").glob("*"))
    assert scryfall.bulk_file() == bulk  # a file that reads whole isn't set aside
