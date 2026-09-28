"""Daily tcgcsv price snapshots: one request per file, stored as returned, a day fetched once."""

import gzip
import json
from datetime import UTC, date, datetime

import pytest

from riffle import net
from riffle.ingest import tcgcsv

B = tcgcsv.BASE
STAMP = b"2026-09-24T20:05:50+0000\n"
DAY = date(2026, 9, 24)
CATS = json.dumps(
    {
        "success": True,
        "errors": [],
        "results": [
            {"categoryId": 1, "name": "Magic"},
            {"categoryId": 62, "name": "Flesh & Blood TCG"},
            {"categoryId": 68, "name": "One Piece Card Game"},
        ],
    }
).encode()
FAB = {"fab": "Flesh & Blood TCG"}


def groups(*ids: int) -> bytes:
    results = [{"groupId": i, "name": f"g{i}"} for i in ids]
    return json.dumps({"success": True, "errors": [], "results": results}).encode()


def prices(*product_ids: int) -> bytes:
    rows = [{"productId": p, "lowPrice": 1.5, "subTypeName": "Normal"} for p in product_ids]
    return json.dumps({"success": True, "errors": [], "results": rows}).encode()


def fake_fetch(answers: dict[str, bytes | None | Exception]):
    """answers: url -> body, None (404), or an exception to raise. Records every url asked for."""
    asked: list[str] = []

    def fetch(url: str) -> bytes | None:
        asked.append(url)
        answer = answers.get(url)
        if isinstance(answer, Exception):
            raise answer
        return answer

    return fetch, asked


def fab_answers(**overrides):
    answers = {
        f"{B}/last-updated.txt": STAMP,
        f"{B}/tcgplayer/categories": CATS,
        f"{B}/tcgplayer/62/groups": groups(200, 100),
        f"{B}/tcgplayer/62/100/prices": prices(1, 2),
        f"{B}/tcgplayer/62/200/prices": prices(3),
    }
    answers.update(overrides)
    return answers


def lines(path):
    with gzip.open(path, "rt", encoding="utf-8") as f:
        return [json.loads(line) for line in f]


@pytest.fixture(autouse=True)
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    return tmp_path / "riffle"


@pytest.fixture
def sleeps(monkeypatch):
    calls: list[float] = []
    monkeypatch.setattr(tcgcsv.time, "sleep", calls.append)
    return calls


def test_last_updated_parses_tcgcsv_stamp():
    fetch, _ = fake_fetch({f"{B}/last-updated.txt": STAMP})
    assert tcgcsv.last_updated(fetch) == datetime(2026, 9, 24, 20, 5, 50, tzinfo=UTC)
    fetch, _ = fake_fetch({f"{B}/last-updated.txt": b"<html>down</html>"})
    with pytest.raises(net.FetchError, match="last-updated"):
        tcgcsv.last_updated(fetch)


def test_resolve_matches_category_names_regardless_of_case():
    cats = [{"categoryId": 1, "name": "MAGIC"}, {"categoryId": 62, "name": "Flesh & Blood TCG"}, {"x": 1}]
    wanted = {"mtg": "Magic", "fab": "flesh & blood tcg", "op": "One Piece Card Game"}
    assert tcgcsv.resolve(cats, wanted) == {"mtg": 1, "fab": 62}


def test_snapshot_stores_a_game_as_returned_under_tcgcsv_day(data_dir, sleeps):
    fetch, asked = fake_fetch(fab_answers())
    snap = tcgcsv.snapshot(FAB, delay=0.1, fetch=fetch)
    assert (snap.day, snap.fetched, snap.skipped, snap.failed) == (DAY, ["fab"], [], [])
    assert snap.groups == {"fab": 2} and snap.requests == 5
    day = data_dir / "tcgcsv" / "daily" / "2026-09-24"
    assert (day / "last-updated.txt").read_bytes() == STAMP.strip()
    assert (day / "fab" / "groups.json").read_bytes() == groups(200, 100)
    assert lines(day / "fab" / "prices.jsonl.gz") == [
        {"groupId": 100, "response": json.loads(prices(1, 2))},
        {"groupId": 200, "response": json.loads(prices(3))},
    ]
    by_id = [f"{B}/tcgplayer/62/100/prices", f"{B}/tcgplayer/62/200/prices"]
    assert asked[-2:] == by_id  # sorted by id, not in the listing's order
    assert sleeps == [0.1, 0.1, 0.1]  # a pause before each request after the day's first two
    assert tcgcsv.stored_days(FAB) == [DAY]
    assert not list(day.rglob("*.part"))


def test_a_stored_day_costs_one_request(data_dir, sleeps):
    fetch, asked = fake_fetch(fab_answers())
    tcgcsv.snapshot(FAB, fetch=fetch)
    fetch, asked = fake_fetch(fab_answers())
    snap = tcgcsv.snapshot(FAB, fetch=fetch)
    assert asked == [f"{B}/last-updated.txt"]
    assert (snap.fetched, snap.skipped, snap.requests) == ([], ["fab"], 1)


def test_missing_price_file_is_kept_as_null(data_dir, sleeps):
    fetch, _ = fake_fetch(fab_answers(**{f"{B}/tcgplayer/62/200/prices": None}))
    snap = tcgcsv.snapshot(FAB, fetch=fetch)
    assert snap.fetched == ["fab"]
    assert lines(tcgcsv.day_dir(DAY, "fab") / "prices.jsonl.gz")[1] == {"groupId": 200, "response": None}


def test_a_failed_game_keeps_nothing_and_the_others_continue(data_dir, sleeps):
    answers = fab_answers(
        **{
            f"{B}/tcgplayer/62/200/prices": net.FetchError("HTTP 503"),
            f"{B}/tcgplayer/68/groups": groups(7),
            f"{B}/tcgplayer/68/7/prices": prices(9),
        }
    )
    fetch, _ = fake_fetch(answers)
    snap = tcgcsv.snapshot({"fab": "Flesh & Blood TCG", "op": "One Piece Card Game"}, fetch=fetch)
    assert snap.failed == [("fab", "HTTP 503")] and snap.fetched == ["op"]
    assert not (tcgcsv.day_dir(DAY, "fab") / "prices.jsonl.gz").exists()
    assert not list(tcgcsv.daily_dir().rglob("*.part"))
    assert (tcgcsv.day_dir(DAY, "op") / "prices.jsonl.gz").exists()
    assert tcgcsv.stored_days({"fab": "x", "op": "x"}) == []  # a day counts only when every game has it


def test_unknown_category_is_reported_without_guessing(data_dir, sleeps):
    fetch, asked = fake_fetch(fab_answers())
    snap = tcgcsv.snapshot({"xx": "Nope"}, fetch=fetch)
    assert snap.failed == [("xx", "tcgcsv has no category named 'Nope'")]
    assert asked == [f"{B}/last-updated.txt", f"{B}/tcgplayer/categories"]


def test_an_unexpected_page_fails_the_game_cleanly(data_dir, sleeps):
    fetch, _ = fake_fetch(fab_answers(**{f"{B}/tcgplayer/62/groups": b"<html>maintenance</html>"}))
    snap = tcgcsv.snapshot(FAB, fetch=fetch)
    assert snap.failed == [("fab", "groups: not the expected JSON")]
    assert not (tcgcsv.day_dir(DAY, "fab") / "groups.json").exists()


def test_pretty_printed_response_still_takes_one_line(data_dir, sleeps):
    pretty = json.dumps(json.loads(prices(1)), indent=2).encode()
    fetch, _ = fake_fetch(fab_answers(**{f"{B}/tcgplayer/62/100/prices": pretty}))
    tcgcsv.snapshot(FAB, fetch=fetch)
    path = tcgcsv.day_dir(DAY, "fab") / "prices.jsonl.gz"
    with gzip.open(path, "rt", encoding="utf-8") as f:
        assert len(f.read().splitlines()) == 2
    assert lines(path)[0] == {"groupId": 100, "response": json.loads(prices(1))}


def test_the_games_with_a_step_of_their_own_are_the_three_played():
    assert list(tcgcsv.GAMES) == ["mtg", "fab", "op"]


def test_each_game_is_a_step(data_dir, sleeps, tracker):
    answers = fab_answers(**{f"{B}/tcgplayer/68/groups": net.FetchError("HTTP 503")})
    fetch, _ = fake_fetch(answers)
    tcgcsv.snapshot({"fab": "Flesh & Blood TCG", "op": "One Piece Card Game"}, fetch=fetch, tracker=tracker)
    fab, op = tracker.steps
    assert (fab.label, fab.unit, fab.updates) == ("tcgcsv fab", "groups", [(0, 2), (1, None), (2, None)])
    assert fab.outcome == ("ok", "2 groups") and op.outcome == ("fail", "HTTP 503")


def test_a_stored_game_is_a_finished_step(data_dir, sleeps, tracker):
    tcgcsv.snapshot(FAB, fetch=fake_fetch(fab_answers())[0])
    tcgcsv.snapshot(FAB, fetch=fake_fetch(fab_answers())[0], tracker=tracker)
    assert tracker.outcomes() == {"tcgcsv fab": ("ok", "already have 2026-09-24")}


def test_an_unknown_category_fails_its_step(data_dir, sleeps, tracker):
    tcgcsv.snapshot({"xx": "Nope"}, fetch=fake_fetch(fab_answers())[0], tracker=tracker)
    assert tracker.outcomes() == {"tcgcsv xx": ("fail", "tcgcsv has no category named 'Nope'")}


# ---- everything but comics --------------------------------------------------------------

EVERY_CAT = json.dumps(
    {
        "success": True,
        "errors": [],
        "results": [
            {"categoryId": 1, "name": "Magic"},
            {"categoryId": 62, "name": "Flesh & Blood TCG"},
            {"categoryId": 68, "name": "One Piece Card Game"},
            {"categoryId": 2, "name": "YuGiOh"},
            {"categoryId": 85, "name": "Pokemon Japan"},
            {"categoryId": 41, "name": "Warhammer Box Sets"},
            {"categoryId": 69, "name": "Marvel Comics"},
            {"categoryId": 70, "name": "DC Comics"},
        ],
    }
).encode()


def every_game_answers(**overrides):
    answers = fab_answers(
        **{
            f"{B}/tcgplayer/categories": EVERY_CAT,
            f"{B}/tcgplayer/1/groups": groups(10),
            f"{B}/tcgplayer/1/10/prices": prices(1),
            f"{B}/tcgplayer/68/groups": groups(7),
            f"{B}/tcgplayer/68/7/prices": prices(9),
            f"{B}/tcgplayer/2/groups": groups(21, 20),
            f"{B}/tcgplayer/2/20/prices": prices(5),
            f"{B}/tcgplayer/2/21/prices": prices(6),
            f"{B}/tcgplayer/85/groups": groups(30),
            f"{B}/tcgplayer/85/30/prices": prices(8),
            f"{B}/tcgplayer/41/groups": groups(40),
            f"{B}/tcgplayer/41/40/prices": prices(4),
        }
    )
    answers.update(overrides)
    return answers


def test_everything_but_comics_is_kept():
    cats = [*json.loads(EVERY_CAT)["results"], {"x": 1}]
    assert tcgcsv.kept(cats) == {
        "magic": 1,
        "flesh-blood-tcg": 62,
        "one-piece-card-game": 68,
        "yugioh": 2,
        "pokemon-japan": 85,
        "warhammer-box-sets": 41,
    }


def test_by_default_every_game_is_a_step_of_its_own(data_dir, sleeps, tracker):
    fetch, asked = fake_fetch(every_game_answers())
    snap = tcgcsv.snapshot(fetch=fetch, tracker=tracker)
    games = [
        "mtg",
        "fab",
        "op",
        "pokemon-japan",
        "warhammer-box-sets",
        "yugioh",
    ]  # played first, then by name
    assert snap.fetched == games and not snap.failed
    assert [step.label for step in tracker.steps] == [f"tcgcsv {game}" for game in games]
    day = tcgcsv.daily_dir() / "2026-09-24"
    assert (day / "categories.json").read_bytes() == EVERY_CAT
    assert lines(day / "warhammer-box-sets" / "prices.jsonl.gz") == [
        {"groupId": 40, "response": json.loads(prices(4))}
    ]
    assert not [url for url in asked if "/69/" in url or "/70/" in url]  # comics are never asked for
    yugioh = tracker.steps[-1]
    assert (yugioh.unit, yugioh.updates, yugioh.outcome) == (
        "groups",
        [(0, 2), (1, None), (2, None)],
        ("ok", "2 groups"),
    )
    assert tracker.outcomes()["tcgcsv mtg"] == ("ok", "1 group")


def test_a_rerun_the_same_day_costs_one_request(data_dir, sleeps, tracker):
    tcgcsv.snapshot(fetch=fake_fetch(every_game_answers())[0])
    fetch, asked = fake_fetch(every_game_answers())
    snap = tcgcsv.snapshot(fetch=fetch, tracker=tracker)
    assert asked == [f"{B}/last-updated.txt"]  # the day's category list is kept too
    assert tracker.outcomes() == {
        "tcgcsv mtg": ("ok", "already have 2026-09-24"),
        "tcgcsv fab": ("ok", "already have 2026-09-24"),
        "tcgcsv op": ("ok", "already have 2026-09-24"),
        "tcgcsv 3 more games": ("ok", "already have 2026-09-24"),
    }
    assert len(snap.skipped) == 6 and not snap.fetched


def test_one_game_failing_leaves_the_rest(data_dir, sleeps, tracker):
    fetch, _ = fake_fetch(every_game_answers(**{f"{B}/tcgplayer/2/21/prices": net.FetchError("HTTP 503")}))
    snap = tcgcsv.snapshot(fetch=fetch, tracker=tracker)
    assert snap.failed == [("yugioh", "HTTP 503")] and "warhammer-box-sets" in snap.fetched
    assert not tcgcsv.day_dir(DAY, "yugioh").joinpath("prices.jsonl.gz").exists()  # the next run tries again
    assert tracker.outcomes()["tcgcsv yugioh"] == ("fail", "HTTP 503")


def test_a_category_without_a_group_list_is_skipped_and_noted(data_dir, sleeps, tracker):
    snap = tcgcsv.snapshot(
        fetch=fake_fetch(every_game_answers(**{f"{B}/tcgplayer/85/groups": None}))[0], tracker=tracker
    )
    assert snap.empty == ["pokemon-japan"] and not snap.failed
    assert tracker.outcomes()["tcgcsv pokemon-japan"] == ("ok", "no sets on tcgcsv (HTTP 404); skipped")
    assert not tcgcsv.day_dir(DAY, "pokemon-japan").joinpath("prices.jsonl.gz").exists()
    assert "warhammer-box-sets" in snap.fetched  # the rest carry on


def test_a_played_game_without_a_group_list_fails(data_dir, sleeps, tracker):
    snap = tcgcsv.snapshot(
        FAB, fetch=fake_fetch(fab_answers(**{f"{B}/tcgplayer/62/groups": None}))[0], tracker=tracker
    )
    assert snap.failed == [("fab", "groups: HTTP 404")]
    assert tracker.outcomes()["tcgcsv fab"] == ("fail", "groups: HTTP 404")


def test_games_past_the_daily_request_budget_wait_a_day(data_dir, sleeps, monkeypatch):
    monkeypatch.setattr(tcgcsv, "DAILY_REQUESTS", 14)  # yugioh's price files would be requests 15 and 16
    snap = tcgcsv.snapshot(fetch=fake_fetch(every_game_answers())[0])
    assert snap.failed == [("yugioh", tcgcsv.OVER_BUDGET)] and "warhammer-box-sets" in snap.fetched
    assert snap.requests == 14


def test_a_last_updated_that_isnt_utf8_is_a_fetch_error():
    fetch, _ = fake_fetch({f"{B}/last-updated.txt": "2026-09-24T20:05:50+0000".encode("utf-16")})
    with pytest.raises(net.FetchError, match="last-updated"):
        tcgcsv.last_updated(fetch)


def test_a_group_without_a_whole_number_group_id_fails_its_game_not_the_run(data_dir, sleeps):
    bad = json.dumps({"success": True, "errors": [], "results": [{"groupId": "x1", "name": "g"}]}).encode()
    fetch, _ = fake_fetch(fab_answers(**{f"{B}/tcgplayer/62/groups": bad}))
    snap = tcgcsv.snapshot(FAB, fetch=fetch)
    assert snap.failed == [("fab", "groups: a group without a whole-number groupId")]


def test_a_price_file_that_isnt_utf8_fails_its_game_not_the_run(data_dir, sleeps):
    utf16 = prices(1, 2).decode().encode("utf-16")  # json.loads reads it; the file must be UTF-8
    fetch, _ = fake_fetch(fab_answers(**{f"{B}/tcgplayer/62/100/prices": utf16}))
    snap = tcgcsv.snapshot(FAB, fetch=fetch)
    assert snap.failed == [("fab", "group 100: not UTF-8")]
    assert not list(tcgcsv.daily_dir().rglob("*.part"))
