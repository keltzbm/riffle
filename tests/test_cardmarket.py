"""Cardmarket price guides: every game's, kept once per createdAt day, not asked for while fresh."""

import gzip
import json
from datetime import UTC, date, datetime, timedelta

import pytest

from riffle import net
from riffle.ingest import cardmarket

B = cardmarket.BASE
STAMP = "2026-09-27T02:45:12+0200"
NOW = datetime(2026, 9, 27, 13, 0, tzinfo=UTC)  # the scheduled 07:00 sync in Denver
DAY = date(2026, 9, 27)
ONE = {"mtg": 1}


def guide(stamp: str = STAMP, products: int = 2) -> bytes:
    rows = [
        {"idProduct": n, "idCategory": 1, "avg": 1.5, "trend": None, "avg-foil": 3.0} for n in range(products)
    ]
    return json.dumps({"version": 1, "createdAt": stamp, "priceGuides": rows}).encode()


class Source:
    """Cardmarket as a download: url -> body, None (404), or an exception to raise. Records every
    url asked for; anything not listed is a 404."""

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


def url(gid: int) -> str:
    return f"{B}/price_guide_{gid}.json"


def every(stamp: str = STAMP, **overrides) -> dict[str, bytes | None | Exception]:
    found: dict[str, bytes | None | Exception] = {
        url(gid): guide(stamp) for gid in [*cardmarket.GAMES.values(), *cardmarket.OTHERS.values()]
    }
    found.update(overrides)
    return found


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    return tmp_path / "riffle" / "cardmarket"


@pytest.fixture
def few(monkeypatch):
    """Just Magic and Flesh and Blood played, and two other games."""
    monkeypatch.setattr(cardmarket, "GAMES", {"mtg": 1, "fab": 16})
    monkeypatch.setattr(cardmarket, "OTHERS", {"pokemon": 6, "yugioh": 3})


def run(source: Source, tracker=None, now: datetime = NOW) -> cardmarket.Snapshot:
    if tracker is None:
        return cardmarket.snapshot(download=source.download, now=now)
    return cardmarket.snapshot(download=source.download, tracker=tracker, now=now)


def test_every_game_is_kept_as_returned_under_its_created_day(data_dir, few, tracker):
    source = Source(every())
    snap = run(source, tracker)
    assert snap.fetched == ["mtg", "fab", "pokemon", "yugioh"] and not snap.failed
    path = data_dir / "daily" / "2026-09-27" / "mtg.json.gz"
    assert gzip.decompress(path.read_bytes()) == guide()  # as returned
    assert [s.label for s in tracker.steps] == [
        "Cardmarket mtg",
        "Cardmarket fab",
        "Cardmarket pokemon",
        "Cardmarket yugioh",
    ]
    assert tracker.outcomes()["Cardmarket mtg"] == ("ok", "kept 2026-09-27, 2 products")
    assert tracker.steps[0].unit == "bytes" and tracker.steps[0].updates == [(len(guide()), len(guide()))]
    assert not [p for p in data_dir.rglob("*") if p.name.endswith((".new", ".part"))]


def test_a_fresh_guide_is_not_asked_for_again(data_dir, few, tracker):
    run(Source(every()))
    source = Source(every())
    snap = run(source, tracker, now=NOW + timedelta(hours=5))  # 17 hours after Cardmarket made it
    assert source.asked == []
    assert snap.skipped == ["mtg", "fab", "pokemon", "yugioh"] and not snap.fetched
    assert tracker.outcomes() == {
        "Cardmarket mtg": ("ok", "already have 2026-09-27"),
        "Cardmarket fab": ("ok", "already have 2026-09-27"),
        "Cardmarket 2 more games": ("ok", "already have 2026-09-27"),
    }


def test_a_day_later_the_next_guide_is_kept(data_dir, few):
    run(Source(every()))
    snap = run(Source(every("2026-09-28T02:44:02+0200")), now=NOW + timedelta(days=1))
    assert snap.fetched == ["mtg", "fab", "pokemon", "yugioh"]
    assert (data_dir / "daily" / "2026-09-28" / "fab.json.gz").exists()
    assert cardmarket.newest("fab") == (date(2026, 9, 28), cardmarket.created_at("2026-09-28T02:44:02+0200"))


def test_a_guide_not_yet_replaced_is_kept_once(data_dir, few, tracker):
    run(Source(every()))
    source = Source(every())  # a day on, Cardmarket still has yesterday's
    snap = run(source, tracker, now=NOW + timedelta(days=1))
    assert len(source.asked) == 4 and not snap.fetched
    assert tracker.outcomes()["Cardmarket mtg"] == ("ok", "already have 2026-09-27")


def test_the_day_is_cardmarkets_own(data_dir, few):
    run(Source(every("2026-09-27T00:45:00+0200")), now=datetime(2026, 9, 26, 23, 0, tzinfo=UTC))
    assert (data_dir / "daily" / "2026-09-27" / "mtg.json.gz").exists()  # the 26th in UTC


def test_a_missing_guide_fails_a_played_game_and_is_noted_for_the_others(data_dir, few, tracker):
    snap = run(Source(every(**{url(16): None, url(3): None})), tracker)
    assert snap.missing == ["fab", "yugioh"] and snap.fetched == ["mtg", "pokemon"]
    assert tracker.outcomes()["Cardmarket fab"] == ("fail", "Cardmarket has no price guide for game 16")
    assert tracker.outcomes()["Cardmarket yugioh"] == ("ok", "no guide, nothing kept; asked again next run")


def test_a_game_with_no_guide_is_asked_every_run_and_warns_after_a_week_of_runs(data_dir, few, tracker):
    for n in range(6):
        run(Source(every(**{url(16): None, url(3): None})), now=NOW + timedelta(days=n))
    source = Source(every(**{url(16): None, url(3): None}))
    run(source, tracker, now=NOW + timedelta(days=6))
    assert url(3) in source.asked and url(16) in source.asked
    assert tracker.outcomes()["Cardmarket yugioh"] == (
        "warn",
        "no guide since 2026-09-27 (7 runs in a row); asked again every run",
    )
    assert tracker.outcomes()["Cardmarket fab"] == (
        "fail",
        "Cardmarket has no price guide for game 16 since 2026-09-27 (7 runs in a row)",
    )
    run(Source(every()), now=NOW + timedelta(days=7))
    assert json.loads((data_dir.parent / "empty-answers.json").read_text()) == {}  # back: forgotten


def test_a_guide_with_no_rows_is_kept_nowhere_played_or_not(data_dir, few, tracker):
    snap = run(Source(every(**{url(1): guide(products=0), url(6): guide(products=0)})), tracker)
    assert snap.empty == ["mtg", "pokemon"] and snap.fetched == ["fab", "yugioh"] and not snap.failed
    assert tracker.outcomes()["Cardmarket mtg"] == ("ok", "empty guide, nothing kept; asked again next run")
    assert cardmarket.newest("mtg") is None and cardmarket.newest("pokemon") is None
    source = Source(every())
    run(source, now=NOW + timedelta(hours=1))
    assert source.asked == [url(1), url(6)]  # asked again; the rest are fresh


def test_a_403_means_cardmarket_has_no_guide(monkeypatch, tmp_path):
    asked = {}

    def download(url, dest, **kw):
        asked.update(kw)
        return None

    monkeypatch.setattr(cardmarket.net, "download", download)
    assert cardmarket._download(url(99), tmp_path / "x") is None
    assert asked["missing"] == (403, 404)


def test_a_game_that_fails_keeps_nothing_and_the_rest_carry_on(data_dir, few, tracker):
    snap = run(Source(every(**{url(1): net.FetchError("HTTP 503")})), tracker)
    assert snap.failed == [("mtg", "HTTP 503")] and snap.fetched == ["fab", "pokemon", "yugioh"]
    assert cardmarket.newest("mtg") is None
    snap = run(Source(every()), now=NOW + timedelta(hours=1))
    assert snap.fetched == ["mtg"]  # retried; the others are fresh


@pytest.mark.parametrize(
    "body",
    [
        b"<html>AccessDenied</html>",
        b'{"version": 1, "createdAt": "2026-09-27T02:45:12+0200"}',
        b'{"version": 1, "createdAt": "2026-09-27", "priceGuides": []}',
        b'{"version": 1, "createdAt": null, "priceGuides": []}',
        b'{"createdAt": "2026-09-27T02:45:12+0200", "priceGuides": {}}',
        b"[]",
        b"[" * 100_000,
    ],
)
def test_a_file_that_isnt_a_price_guide_fails_cleanly(data_dir, few, tracker, body):
    snap = run(Source(every(**{url(1): body})), tracker)
    assert snap.failed == [("mtg", "price_guide_1.json: not the expected JSON")]
    assert cardmarket.newest("mtg") is None
    assert not [p for p in data_dir.rglob("*") if p.name.endswith((".new", ".part"))]


def test_an_unreadable_kept_guide_is_replaced(data_dir, few, tracker):
    run(Source(every()))
    (data_dir / "daily" / "2026-09-27" / "mtg.json.gz").write_bytes(b"not gzip")
    assert cardmarket.newest("mtg") == (DAY, None)
    source = Source(every())
    run(source, tracker, now=NOW + timedelta(hours=1))
    assert source.asked == [url(1)]  # only the one that can't be read
    assert gzip.decompress((data_dir / "daily" / "2026-09-27" / "mtg.json.gz").read_bytes()) == guide()
    assert tracker.outcomes()["Cardmarket mtg"] == ("ok", "kept 2026-09-27, 2 products")


def test_no_answer_from_cardmarket_fails_the_rest_without_asking(data_dir, few, tracker):
    source = Source(every(**{url(1): net.NoAnswer("no answer after 3 tries (timed out)")}))
    snap = run(source, tracker)
    assert source.asked == [url(1)]
    assert snap.failed == [
        ("mtg", "no answer after 3 tries (timed out)"),
        ("fab", "not asked: Cardmarket gave no answer"),
        ("pokemon", "not asked: Cardmarket gave no answer"),
        ("yugioh", "not asked: Cardmarket gave no answer"),
    ]
    assert tracker.outcomes()["Cardmarket yugioh"] == ("fail", "not asked: Cardmarket gave no answer")


def test_another_failure_doesnt_stop_the_rest(data_dir, few):
    snap = run(Source(every(**{url(1): net.FetchError("HTTP 503")})))
    assert snap.fetched == ["fab", "pokemon", "yugioh"]


def test_a_guide_20_hours_old_is_asked_for_again(data_dir, few):
    run(Source(every()))
    created = cardmarket.created_at(STAMP)
    source = Source(every())
    run(source, now=created + timedelta(hours=19, minutes=59))
    assert source.asked == []
    run(source, now=created + timedelta(hours=20))
    assert len(source.asked) == 4


def test_newest_ignores_other_folders(data_dir):
    folder = cardmarket.daily_dir()
    (folder / "notes").mkdir(parents=True)
    (folder / "notes" / "mtg.json.gz").write_bytes(gzip.compress(guide()))
    assert cardmarket.newest("mtg") is None


def test_the_games_played_come_first_then_the_rest_by_name():
    assert list(cardmarket.GAMES) == ["mtg", "fab", "op"]
    assert list(cardmarket.OTHERS) == sorted(cardmarket.OTHERS)
    ids = [*cardmarket.GAMES.values(), *cardmarket.OTHERS.values()]
    assert len(ids) == len(set(ids)) == 21  # 20 games and the accessories


def test_accessories_are_kept_like_a_game(data_dir, monkeypatch, tracker):
    monkeypatch.setattr(cardmarket, "GAMES", {"mtg": 1})
    monkeypatch.setattr(cardmarket, "OTHERS", {"accessories": "accessories"})
    source = Source({url(1): guide(), f"{B}/price_guide_accessories.json": guide()})
    snap = run(source, tracker)
    assert snap.fetched == ["mtg", "accessories"]
    assert (data_dir / "daily" / "2026-09-27" / "accessories.json.gz").exists()
    assert tracker.outcomes()["Cardmarket accessories"] == ("ok", "kept 2026-09-27, 2 products")
