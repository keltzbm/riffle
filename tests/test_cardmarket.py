"""Cardmarket price guides: every guide each game publishes, kept by the run rule under its
createdAt, each asked for when its next is due (riffle.cadence)."""

import gzip
import json
from datetime import UTC, datetime, timedelta

import pytest

from riffle import locks, net, runs, watching
from riffle.ingest import cardmarket

B = cardmarket.BASE
STAMP = "2026-09-27T02:45:12+0200"
NOW = datetime(2026, 9, 27, 13, 0, tzinfo=UTC)  # the 07:00 sync in Denver


def guide(stamp: str = STAMP, products: int = 2, price: float = 1.5) -> bytes:
    rows = [
        {"idProduct": n, "idCategory": 1, "avg": price, "trend": None, "avg-foil": 3.0}
        for n in range(products)
    ]
    return json.dumps({"version": 1, "createdAt": stamp, "priceGuides": rows}).encode()


class Source:
    """Cardmarket as net.fetch_new: url -> body, None (missing), or an exception to raise, each
    body's ETag its hash. Records every (url, ETag sent) and the missing codes asked with."""

    def __init__(self, answers: dict[str, bytes | None | Exception]):
        self.answers = answers
        self.asked: list[tuple[str, str | None]] = []
        self.missing: set = set()
        self.tag_new, self.tag_known = True, False

    def fetch(self, url, dest, known, etag=None, accept="*/*", progress=None, missing=(404,)):
        self.asked.append((url, etag))
        self.missing.add(missing)
        body = self.answers.get(url)
        if isinstance(body, Exception):
            raise body
        if body is None:
            return None
        tag = f'"{hash(body)}"'
        if etag == tag:
            return net.Fetched("unchanged", b"", etag)
        head = body[: net.HEAD]
        if known(head):
            return net.Fetched("known", head, tag if self.tag_known else None)
        dest.write_bytes(body)
        if progress:
            progress(len(body), None)
        return net.Fetched("new", head, tag if self.tag_new else None, len(body))

    def urls(self) -> list[str]:
        return [u for u, _ in self.asked]


def url(gid: int | str) -> str:
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


def run(source: Source, tracker=None, now: datetime = NOW, always: bool = True) -> watching.Watch:
    kw = {} if tracker is None else {"tracker": tracker}
    return cardmarket.watch(fetch=source.fetch, clock=lambda: now, always=always, **kw)


def logged(data_dir) -> list[dict]:
    return [json.loads(line) for line in (data_dir / "watch.jsonl").read_text().splitlines()]


def test_every_game_is_kept_as_returned_under_its_created_time(data_dir, few, tracker):
    source = Source(every())
    res = run(source, tracker)
    games = ["Cardmarket mtg", "Cardmarket fab", "Cardmarket pokemon", "Cardmarket yugioh"]
    assert res.kept == games and not res.failed
    kept = runs.kept(data_dir / "lists" / "mtg")
    assert list(kept) == ["2026-09-27T004512Z"]  # createdAt in UTC
    assert runs.rebuild(kept["2026-09-27T004512Z"]) == guide()  # as returned
    assert [s.label for s in tracker.steps] == games
    size = watching.size(kept["2026-09-27T004512Z"].stat().st_size)
    assert tracker.outcomes()["Cardmarket mtg"] == (
        "ok",
        f"kept the guide made 2026-09-27 00:45 UTC, 2 products: a new run, {size} kept twice",
    )
    assert tracker.steps[0].unit == "bytes" and tracker.steps[0].updates == [(len(guide()), None)]
    assert source.missing == {(403, 404)}  # what the download server answers for a guide it lacks
    assert not [p for p in data_dir.rglob("*") if p.name.endswith((".new", ".part"))]
    entry = logged(data_dir)[0]
    assert (
        entry["list"] == "mtg"
        and entry["at"] == "2026-09-27T130000Z"
        and entry["made"] == "2026-09-27T004512Z"
    )
    assert entry["file"] == "cardmarket/lists/mtg/2026-09-27T004512Z/2026-09-27T004512Z.json.zst"
    assert entry["result"] == "kept" and entry["kind"] == "base" and "seconds" in entry


def test_a_guide_not_new_costs_a_304_with_the_etag_last_kept(data_dir, few, tracker):
    run(Source(every()))
    source = Source(every())
    res = run(source, tracker, now=NOW + timedelta(hours=1))
    assert all(tag is not None for _, tag in source.asked)
    assert len(res.same) == 4 and not res.kept
    assert tracker.outcomes()["Cardmarket fab"] == ("ok", "no new guide since the last one kept")
    assert [e["result"] for e in logged(data_dir)][4:] == ["unchanged"] * 4


def test_a_guide_kept_already_is_known_by_its_first_bytes(data_dir, few, tracker):
    run(Source(every()))
    (data_dir / "watch-etags.json").unlink()  # no ETag to send
    res = run(Source(every()), tracker, now=NOW + timedelta(hours=1))
    assert len(res.same) == 4
    assert tracker.outcomes()["Cardmarket mtg"] == ("ok", "have the guide made 2026-09-27 00:45 UTC")
    assert logged(data_dir)[-1] | {"seconds": 0} == {
        "at": "2026-09-27T140000Z",
        "list": "yugioh",
        "result": "known",
        "made": "2026-09-27T004512Z",
        "seconds": 0,
    }


def test_the_next_days_guide_is_kept_as_a_difference(data_dir, few, tracker):
    run(Source(every()))
    later = "2026-09-28T02:44:02+0200"
    res = run(
        Source(every(later, **{url(1): guide(later, price=1.75)})), tracker, now=NOW + timedelta(days=1)
    )
    assert len(res.kept) == 4
    assert list(runs.kept(data_dir / "lists" / "fab")) == ["2026-09-27T004512Z", "2026-09-28T004402Z"]
    assert tracker.outcomes()["Cardmarket mtg"][1].startswith(
        "kept the guide made 2026-09-28 00:44 UTC, 2 products: a difference of"
    )
    path = runs.kept(data_dir / "lists" / "mtg")["2026-09-28T004402Z"]
    assert runs.rebuild(path) == guide(later, price=1.75)


def test_made_counts_the_days_kept_before_and_the_runs_since(data_dir, few):
    day = data_dir / "daily" / "2026-09-26"
    day.mkdir(parents=True)
    (day / "mtg.json.gz").write_bytes(gzip.compress(guide("2026-09-26T02:46:00+0200")))
    (day / "fab.json.gz").write_bytes(b"not gzip")  # unreadable: left out
    run(Source(every()))
    assert cardmarket.made("mtg") == [
        datetime(2026, 9, 26, 0, 46, tzinfo=UTC),
        datetime(2026, 9, 27, 0, 45, 12, tzinfo=UTC),
    ]
    assert cardmarket.made("fab") == [datetime(2026, 9, 27, 0, 45, 12, tzinfo=UTC)]
    assert cardmarket.stamp(day / "mtg.json.gz") == cardmarket.created_at("2026-09-26T02:46:00+0200")


def test_a_guide_not_due_is_not_asked_and_they_share_a_line(data_dir, few, tracker):
    run(Source(every()))  # one check each: with no gaps yet, every firing asks
    source = Source(every())
    res = run(source, tracker, now=NOW + timedelta(minutes=2), always=False)
    assert source.asked == [] and res.waiting == ["mtg", "fab", "pokemon", "yugioh"]
    assert tracker.outcomes() == {
        "Cardmarket 4 guides": ("ok", "none due; the next asked 2026-09-27 13:05 UTC")
    }
    run(source, now=NOW + timedelta(minutes=5), always=False)
    assert len(source.asked) == 4


def test_a_guide_learned_daily_is_asked_from_its_expected_time(data_dir, monkeypatch, tracker):
    monkeypatch.setattr(cardmarket, "GAMES", {"mtg": 1})
    monkeypatch.setattr(cardmarket, "OTHERS", {})
    first = datetime(2026, 9, 1, 0, 45, tzinfo=UTC)
    for n in range(15):  # 14 gaps of a day: the next is expected a day after the last
        day = data_dir / "daily" / (first + timedelta(days=n)).date().isoformat()
        day.mkdir(parents=True)
        made = (first + timedelta(days=n)).strftime("%Y-%m-%dT%H:%M:%S%z")
        (day / "mtg.json.gz").write_bytes(gzip.compress(guide(made)))
    expected = first + timedelta(days=15)
    checks = [expected - timedelta(hours=1, minutes=5 * n) for n in range(300)]  # a clean week
    (data_dir / "watch.jsonl").write_text(
        "".join(
            json.dumps({"at": runs.name(at), "list": "mtg", "result": "unchanged"}) + "\n" for at in checks
        )
    )
    later = expected.strftime("%Y-%m-%dT%H:%M:%S%z")
    source = Source({url(1): guide(later)})
    run(source, tracker, now=expected - timedelta(minutes=1), always=False)
    assert source.asked == []
    assert tracker.outcomes()["Cardmarket 1 guide"] == ("ok", "none due; the next asked 2026-09-16 00:45 UTC")
    res = run(source, now=expected + timedelta(seconds=30), always=False)
    assert len(source.asked) == 1 and res.kept == ["Cardmarket mtg"]


def test_a_missing_guide_fails_a_played_game_and_is_noted_for_the_others(data_dir, few, tracker):
    res = run(Source(every(**{url(16): None, url(3): None})), tracker)
    assert res.empty == ["Cardmarket fab", "Cardmarket yugioh"]
    assert res.kept == ["Cardmarket mtg", "Cardmarket pokemon"]
    assert res.failed == [("Cardmarket fab", "Cardmarket has no price guide for game 16")]
    assert tracker.outcomes()["Cardmarket fab"] == ("fail", "Cardmarket has no price guide for game 16")
    assert tracker.outcomes()["Cardmarket yugioh"] == ("ok", "no guide, nothing kept; asked again next run")
    assert [e["result"] for e in logged(data_dir)] == ["kept", "missing", "kept", "missing"]


def test_a_game_with_no_guide_warns_after_seven_times_in_a_row(data_dir, few, tracker):
    for n in range(6):
        run(Source(every(**{url(16): None, url(3): None})), now=NOW + timedelta(days=n))
    source = Source(every(**{url(16): None, url(3): None}))
    run(source, tracker, now=NOW + timedelta(days=6))
    assert url(3) in source.urls() and url(16) in source.urls()
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
    res = run(Source(every(**{url(1): guide(products=0), url(6): guide(products=0)})), tracker)
    assert res.empty == ["Cardmarket mtg", "Cardmarket pokemon"] and not res.failed
    assert tracker.outcomes()["Cardmarket mtg"] == ("ok", "empty guide, nothing kept; asked again next run")
    assert cardmarket.made("mtg") == [] and cardmarket.made("pokemon") == []
    source = Source(every())
    run(source, now=NOW + timedelta(hours=1))
    assert dict(source.asked)[url(1)] is None  # no ETag kept for it: asked whole again


def test_a_game_that_fails_keeps_nothing_and_the_rest_carry_on(data_dir, few, tracker):
    res = run(Source(every(**{url(1): net.FetchError("HTTP 503")})), tracker)
    assert res.failed == [("Cardmarket mtg", "HTTP 503")] and len(res.kept) == 3
    assert cardmarket.made("mtg") == []
    assert logged(data_dir)[0] | {"seconds": 0} == {
        "at": "2026-09-27T130000Z",
        "list": "mtg",
        "result": "failed",
        "why": "HTTP 503",
        "seconds": 0,
    }
    res = run(Source(every()), now=NOW + timedelta(hours=1))
    assert res.kept == ["Cardmarket mtg"]  # retried; the others are the guides kept


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
    res = run(Source(every(**{url(1): body})), tracker)
    assert res.failed == [("Cardmarket mtg", "price_guide_1.json: not the expected JSON")]
    assert cardmarket.made("mtg") == []
    assert not [p for p in data_dir.rglob("*") if p.name.endswith((".new", ".part"))]


def test_a_createdat_that_isnt_a_time_is_not_a_guide_kept(data_dir):
    assert cardmarket._created(b'{"createdAt": "yesterday"') is None
    assert cardmarket._created(b'{"version": 1}') is None


def test_no_answer_from_cardmarket_fails_the_rest_without_asking(data_dir, few, tracker):
    source = Source(every(**{url(1): net.NoAnswer("no answer after 3 tries (timed out)")}))
    res = run(source, tracker)
    assert source.urls() == [url(1)]
    assert res.failed == [
        ("Cardmarket mtg", "no answer after 3 tries (timed out)"),
        ("Cardmarket fab", "not asked: Cardmarket gave no answer"),
        ("Cardmarket pokemon", "not asked: Cardmarket gave no answer"),
        ("Cardmarket yugioh", "not asked: Cardmarket gave no answer"),
    ]
    assert tracker.outcomes()["Cardmarket yugioh"] == ("fail", "not asked: Cardmarket gave no answer")
    assert [e["result"] for e in logged(data_dir)] == ["failed"] * 4


def test_a_run_that_finds_the_lock_held_asks_nothing(data_dir, few, tracker):
    source = Source(every())
    with locks.held(data_dir / "watch.lock"):
        res = run(source, tracker)
    assert res.busy and source.asked == []
    assert tracker.outcomes() == {"Cardmarket guides": ("ok", "another run is asking for them")}


def test_a_list_that_doesnt_read_back_fails_its_game(data_dir, few, tracker, monkeypatch):
    def unverified(folder, at, data):
        raise runs.Unverified("x.json.zst didn't read back as the list written; set aside as x")

    monkeypatch.setattr(cardmarket.runs, "keep", unverified)
    res = run(Source(every()), tracker)
    assert res.failed[0] == (
        "Cardmarket mtg",
        "x.json.zst didn't read back as the list written; set aside as x",
    )


def test_damage_found_on_the_way_is_a_warning(data_dir, few, tracker):
    run(Source(every()))
    base = data_dir / "lists" / "mtg" / "2026-09-27T004512Z" / "2026-09-27T004512Z.copy.json.zst"
    base.write_bytes(b"damaged")
    later = "2026-09-28T02:44:02+0200"
    run(Source(every(later)), tracker, now=NOW + timedelta(days=1))
    outcome, note = tracker.outcomes()["Cardmarket mtg"]
    assert outcome == "warn" and "was damaged: set aside as" in note


def test_the_games_played_come_first_then_the_rest_by_name():
    assert list(cardmarket.GAMES) == ["mtg", "fab", "op"]
    assert list(cardmarket.OTHERS) == sorted(cardmarket.OTHERS)
    ids = [*cardmarket.GAMES.values(), *cardmarket.OTHERS.values()]
    assert len(ids) == len(set(ids)) == 21  # 20 games and the accessories


def test_accessories_are_kept_like_a_game(data_dir, monkeypatch, tracker):
    monkeypatch.setattr(cardmarket, "GAMES", {"mtg": 1})
    monkeypatch.setattr(cardmarket, "OTHERS", {"accessories": "accessories"})
    res = run(Source({url(1): guide(), url("accessories"): guide()}), tracker)
    assert res.kept == ["Cardmarket mtg", "Cardmarket accessories"]
    assert list(runs.kept(data_dir / "lists" / "accessories")) == ["2026-09-27T004512Z"]


def test_a_guide_sent_without_an_etag_is_asked_whole_next_time_and_known_by_its_stamp(data_dir, few):
    source = Source(every())
    source.tag_new = False
    run(source)
    assert json.loads((data_dir / "watch-etags.json").read_text()) == {}
    source = Source(every())
    source.tag_known = True
    res = run(source, now=NOW + timedelta(hours=1))
    assert len(res.same) == 4 and all(tag is None for _, tag in source.asked)
    assert set(json.loads((data_dir / "watch-etags.json").read_text())) == {"mtg", "fab", "pokemon", "yugioh"}
