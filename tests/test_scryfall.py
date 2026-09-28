import gzip
import json
import os
from datetime import UTC, date, datetime

import pytest

from riffle import net
from riffle.ingest import scryfall
from riffle.ingest.scryfall import download_url


def test_prefers_jsonl_link_after_the_2026_format_change():
    info = {"jsonl_download_uri": "https://x/default.jsonl.gz", "download_uri": "https://x/default.json"}
    assert download_url(info) == ("https://x/default.jsonl.gz", "default-cards.jsonl.gz")


def test_falls_back_to_the_old_array_link():
    assert download_url({"download_uri": "https://x/d.json"}) == ("https://x/d.json", "default-cards.json")


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


# ---- refresh --------------------------------------------------------------------

INFO = {"type": "default_cards", "updated_at": "2026-09-24T21:05:23.456+00:00"}


def test_refresh_reports_the_download_and_the_set_list(tmp_path, monkeypatch, tracker):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    bulk = tmp_path / "riffle" / "default-cards.jsonl.gz"  # download() writes into the data folder
    bulk.parent.mkdir()
    bulk.write_bytes(b"x" * 2_500_000)
    asked = []

    def download(progress, info):
        asked.append(info)
        progress(2_500_000, 2_500_000)
        return bulk, info

    monkeypatch.setattr(scryfall, "remote_info", lambda: INFO)
    monkeypatch.setattr(scryfall, "download", download)
    monkeypatch.setattr(net, "get", lambda url, accept: b'{"data": [{"code": "lea"}, {"code": "leb"}]}')
    scryfall.refresh(tracker=tracker)
    assert tracker.outcomes() == {
        "Scryfall bulk data": ("ok", "2.5 MB, Scryfall 2026-09-24"),
        "Scryfall set list": ("ok", "2 sets"),
    }
    assert asked == [INFO]  # the index is asked once, and the download reuses its answer
    assert tracker.steps[0].unit == "bytes" and tracker.steps[0].updates == [(2_500_000, 2_500_000)]
    meta = json.loads(scryfall.meta_path().read_text())
    assert meta["updated_at"] == "2026-09-24T21:05:23.456+00:00"
    assert scryfall.is_current(INFO)


@pytest.mark.parametrize(
    ("kept", "published", "current"),
    [
        ("2026-09-24T09:05:40.725+00:00", "2026-09-24T21:05:23.456+00:00", True),
        ("2026-09-24T21:05:23.456+00:00", "2026-09-25T09:02:11.001+00:00", False),
        ("2026-09-24T21:05:23.456+00:00", "2026-09-25T20:59:59.000+00:00", False),
        ("2026-09-25T09:02:11.001+00:00", "2026-09-24T21:05:23.456+00:00", True),
    ],
    ids=["same day, later file", "next day, earlier hour", "next day, same hour", "older on Scryfall"],
)
def test_a_new_day_is_downloaded_whatever_the_hour(tmp_path, monkeypatch, kept, published, current):
    """A job run at 07:00 each day starts a few seconds short of 24 hours after the last
    download finished; going by the download's age skipped every other day."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    write_bulk(tmp_path / "riffle", updated_at=kept)
    assert scryfall.is_current({"updated_at": published}) is current


def test_a_missing_or_unstamped_download_is_never_current(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    assert not scryfall.is_current(INFO)
    write_bulk(tmp_path / "riffle", updated_at=None)
    assert not scryfall.is_current(INFO)
    scryfall.meta_path().write_text(json.dumps({"updated_at": "not a time"}))
    assert not scryfall.is_current(INFO)
    write_bulk(tmp_path / "riffle")
    assert not scryfall.is_current({"updated_at": None})


@pytest.mark.parametrize("error", [net.FetchError("HTTP 503"), KeyError()], ids=["with a message", "without"])
def test_a_failed_download_is_reported_never_raised(tmp_path, monkeypatch, tracker, error):
    """A sync carries on with the last download."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))

    def download(progress, info):
        raise error

    monkeypatch.setattr(scryfall, "remote_info", lambda: INFO)
    monkeypatch.setattr(scryfall, "download", download)
    scryfall.refresh(tracker=tracker)
    assert tracker.outcomes() == {"Scryfall bulk data": ("fail", str(error) or "KeyError")}
    assert not scryfall.meta_path().exists()


def test_an_unreadable_bulk_index_is_reported_and_keeps_the_last_download(tmp_path, monkeypatch, tracker):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    bulk = write_bulk(tmp_path / "riffle")

    def remote_info():
        raise net.NoAnswer("no answer after 3 tries (timed out)")

    monkeypatch.setattr(scryfall, "remote_info", remote_info)
    scryfall.refresh(tracker=tracker)
    assert tracker.outcomes() == {"Scryfall bulk data": ("fail", "no answer after 3 tries (timed out)")}
    assert scryfall.bulk_file() == bulk


def test_refresh_skips_the_day_already_kept(tmp_path, monkeypatch, tracker):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    write_bulk(tmp_path / "riffle")
    scryfall.sets_path().parent.mkdir(parents=True)
    scryfall.sets_path().write_text('{"data": []}')
    monkeypatch.setattr(scryfall, "remote_info", lambda: INFO)
    monkeypatch.setattr(scryfall, "download", lambda **kw: pytest.fail("downloaded the day kept"))
    scryfall.refresh(tracker=tracker)
    assert tracker.outcomes() == {"Scryfall bulk data": ("ok", "current, Scryfall 2026-09-24")}


def test_force_downloads_the_day_already_kept(tmp_path, monkeypatch, tracker):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    bulk = write_bulk(tmp_path / "riffle")
    monkeypatch.setattr(scryfall, "remote_info", lambda: INFO)
    monkeypatch.setattr(scryfall, "download", lambda progress, info: (bulk, info))
    monkeypatch.setattr(net, "get", lambda url, accept: b'{"data": []}')
    scryfall.refresh(force=True, tracker=tracker)
    assert tracker.outcomes()["Scryfall bulk data"][0] == "ok"
    assert tracker.outcomes()["Scryfall bulk data"][1].endswith("Scryfall 2026-09-24")


def test_a_current_download_without_a_set_list_fetches_one(tmp_path, monkeypatch, tracker):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    write_bulk(tmp_path / "riffle")
    monkeypatch.setattr(scryfall, "remote_info", lambda: INFO)
    monkeypatch.setattr(net, "get", lambda url, accept: b'{"data": [{"code": "lea"}]}')
    scryfall.refresh(tracker=tracker)
    assert tracker.outcomes() == {
        "Scryfall bulk data": ("ok", "current, Scryfall 2026-09-24"),
        "Scryfall set list": ("ok", "1 sets"),
    }


def test_a_failed_set_list_fetch_is_reported_never_raised(tmp_path, monkeypatch, tracker):
    """Only the Postgres catalog needs the set list: it mustn't stop a sync."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    write_bulk(tmp_path / "riffle")

    def get(url, accept):
        raise net.FetchError("HTTP 503")

    monkeypatch.setattr(scryfall, "remote_info", lambda: INFO)
    monkeypatch.setattr(net, "get", get)
    scryfall.refresh(tracker=tracker)
    assert tracker.outcomes() == {
        "Scryfall bulk data": ("ok", "current, Scryfall 2026-09-24"),
        "Scryfall set list": ("fail", "HTTP 503"),
    }


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
    with pytest.raises(scryfall.CorruptBulk, match="cut short or corrupt.*set aside"):
        scryfall.snapshot_prices()
    assert scryfall.bulk_file() is None  # so the next online refresh downloads it again
    assert (tmp_path / "riffle" / "default-cards.jsonl.gz.bad").exists()
    assert not scryfall.is_current(INFO)
    assert not list((tmp_path / "riffle" / "scryfall" / "daily").glob("*"))


@pytest.mark.parametrize("meta", ["{not json", "[]", '"2026-09-24"', "\xff"])
def test_an_unreadable_bulk_meta_counts_as_missing(tmp_path, monkeypatch, meta):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    bulk = write_bulk(tmp_path / "riffle")
    scryfall.meta_path().write_text(meta, encoding="latin-1")
    assert not scryfall.is_current(INFO)  # the next online refresh downloads the bulk file again
    stamp = datetime(2026, 9, 24, 12, tzinfo=UTC).timestamp()
    os.utime(bulk, (stamp, stamp))
    assert scryfall.bulk_day(bulk) == date(2026, 9, 24)


def test_a_new_download_removes_a_set_aside_file(tmp_path, monkeypatch):
    (tmp_path / "default-cards.jsonl.gz.bad").write_bytes(b"cut short")

    def download(url, dest, progress=None, **kwargs):
        dest.write_bytes(gzip.compress(b"{}\n"))
        return dest.stat().st_size

    monkeypatch.setattr(net, "download", download)
    path, _ = scryfall.download(dest_dir=tmp_path, info={"jsonl_download_uri": "https://x/default.jsonl.gz"})
    assert [p.name for p in tmp_path.iterdir()] == [path.name] == ["default-cards.jsonl.gz"]


def test_a_download_that_isnt_gzip_or_is_missing_keeps_the_last_good_file(tmp_path, monkeypatch):
    info = {"jsonl_download_uri": "https://x/default.jsonl.gz"}
    kept = tmp_path / "default-cards.jsonl.gz"
    kept.write_bytes(gzip.compress(b"{}\n"))

    def html(url, dest, progress=None, **kwargs):
        dest.write_bytes(b"<html>maintenance</html>")
        return 24

    monkeypatch.setattr(net, "download", html)
    with pytest.raises(RuntimeError, match="isn't gzip"):
        scryfall.download(dest_dir=tmp_path, info=info)
    monkeypatch.setattr(net, "download", lambda url, dest, progress=None: None)
    with pytest.raises(RuntimeError, match="bulk file is missing"):
        scryfall.download(dest_dir=tmp_path, info=info)
    assert [p.name for p in tmp_path.iterdir()] == [kept.name]


def test_the_bulk_index_must_list_the_file(monkeypatch):
    monkeypatch.setattr(net, "get", lambda url, accept: b'{"data": [{"type": "oracle_cards"}]}')
    with pytest.raises(RuntimeError, match="no bulk file of type default_cards"):
        scryfall.remote_info()
    assert scryfall.remote_info("oracle_cards") == {"type": "oracle_cards"}
    monkeypatch.setattr(net, "get", lambda url, accept: None)
    with pytest.raises(RuntimeError, match="bulk index is missing"):
        scryfall.remote_info()


def test_an_error_while_keeping_prices_leaves_no_partial_file(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    bulk = write_bulk(tmp_path / "riffle")
    with gzip.open(bulk, "wt", encoding="utf-8") as f:
        f.write('{"name": "no id"}\n')
    with pytest.raises(KeyError):
        scryfall.snapshot_prices()
    assert not list((tmp_path / "riffle" / "scryfall" / "daily").glob("*"))
    assert scryfall.bulk_file() == bulk  # a file that reads whole isn't set aside
