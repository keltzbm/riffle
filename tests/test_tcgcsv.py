"""Daily tcgcsv price snapshots: one request per file, stored as returned, a day fetched once."""

import gzip
import json
import re
from datetime import UTC, date, datetime, timedelta

import pytest

from riffle import locks, net, runs, watching
from riffle.ingest import tcgcsv

B = tcgcsv.BASE
STAMP = b"2026-09-24T20:05:50+0000\n"
DAY = date(2026, 9, 24)
MODIFIED = "Thu, 24 Sep 2026 20:04:00 GMT"  # each price file is written a little before the stamp
NOW = datetime(2026, 9, 24, 22, 0, tzinfo=UTC)
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


def fake_fetch(answers: dict[str, bytes | net.Reply | None | Exception]):
    """answers: url -> body (last modified at MODIFIED), a whole reply, None (404), or an
    exception to raise. Records every url asked for."""
    asked: list[str] = []

    def fetch(url: str) -> net.Reply | None:
        asked.append(url)
        answer = answers.get(url)
        if isinstance(answer, list):  # one answer per ask, in turn
            answer = answer.pop(0)
        if isinstance(answer, Exception):
            raise answer
        if isinstance(answer, bytes):
            return net.Reply(answer, {"last-modified": MODIFIED})
        return answer

    return fetch, asked


def row(gid: int, body: bytes | None, modified: str | None = "2026-09-24T20:04:00+00:00") -> dict:
    """A set's line as kept: fetched at NOW."""
    response = None if body is None else json.loads(body)
    return {
        "groupId": gid,
        "fetched": "2026-09-24T22:00:00+00:00",
        "lastModified": modified,
        "response": response,
    }


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


def plain(outcome: tuple[str, str]) -> tuple[str, str]:
    """A game's step without how its day was kept, which is sized by zstd."""
    return outcome[0], re.sub(r", kept as [^;]*", "", outcome[1])


def lines(game_dir):
    """A finished game's day as kept: one set a line."""
    kept = tcgcsv.kept_game(game_dir)
    assert kept is not None, f"{game_dir.name} isn't finished"
    return [json.loads(line) for line in kept.decode().splitlines()]


@pytest.fixture(autouse=True)
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setattr(tcgcsv.times, "now", lambda: NOW)
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
    assert lines(day / "fab") == [row(100, prices(1, 2)), row(200, prices(3))]
    by_id = [f"{B}/tcgplayer/62/100/prices", f"{B}/tcgplayer/62/200/prices"]
    assert asked[-2:] == by_id  # sorted by id, not in the listing's order
    assert sleeps == [0.1, 0.1, 0.1]  # a pause before each request after the day's first two
    assert tcgcsv.stored_days(FAB) == [DAY]
    assert not list(day.rglob("*.part")) and not (day / "fab" / "missing.txt").exists()


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
    assert lines(tcgcsv.day_dir(DAY, "fab"))[1] == row(200, None, modified=None)


def test_a_failed_set_keeps_the_rest_and_the_next_run_asks_only_for_it(data_dir, sleeps, tracker):
    answers = fab_answers(
        **{
            f"{B}/tcgplayer/62/100/prices": net.FetchError("HTTP 503"),
            f"{B}/tcgplayer/68/groups": groups(7),
            f"{B}/tcgplayer/68/7/prices": prices(9),
        }
    )
    both = {"fab": "Flesh & Blood TCG", "op": "One Piece Card Game"}
    snap = tcgcsv.snapshot(both, fetch=fake_fetch(answers)[0], tracker=tracker)
    why = "1 of 2 sets kept; set 100 failed (HTTP 503), asked again next run"
    assert snap.failed == [("fab", why)] and snap.fetched == ["op"]
    assert tracker.outcomes()["tcgcsv fab"] == ("fail", why)
    fab = tcgcsv.day_dir(DAY, "fab")
    assert not tcgcsv.finished(fab) and (fab / "prices.jsonl.part").exists()
    assert tcgcsv.stored_days({"fab": "x", "op": "x"}) == []  # a day counts only when every game has it
    fetch, asked = fake_fetch(fab_answers())
    snap = tcgcsv.snapshot(both, fetch=fetch, tracker=tracker)
    assert asked == [f"{B}/last-updated.txt", f"{B}/tcgplayer/62/100/prices"]  # nothing kept is asked again
    assert snap.fetched == ["fab"] and plain(tracker.outcomes()["tcgcsv fab"]) == (
        "ok",
        "the last 1 set of 2",
    )
    assert lines(fab) == [row(100, prices(1, 2)), row(200, prices(3))]  # sorted by set
    assert not (fab / "prices.jsonl.part").exists()
    assert tcgcsv.stored_days({"fab": "x", "op": "x"}) == [DAY]


def test_unknown_category_is_reported_without_guessing(data_dir, sleeps):
    fetch, asked = fake_fetch(fab_answers())
    snap = tcgcsv.snapshot({"xx": "Nope"}, fetch=fetch)
    assert snap.failed == [("xx", "tcgcsv has no category named 'Nope'")]
    assert asked == [f"{B}/last-updated.txt", f"{B}/tcgplayer/categories"]


def test_an_unexpected_page_fails_the_game_cleanly(data_dir, sleeps):
    fetch, _ = fake_fetch(fab_answers(**{f"{B}/tcgplayer/62/groups": b"<html>maintenance</html>"}))
    snap = tcgcsv.snapshot(FAB, fetch=fetch)
    aside = "tcgcsv/aside/fab-groups-2026-09-24T220000Z.json"
    assert snap.failed == [
        ("fab", f"groups: not the expected JSON; set aside as {aside}, asked again next run")
    ]
    assert not (tcgcsv.day_dir(DAY, "fab") / "groups.json").exists()
    assert (data_dir / aside).read_bytes() == b"<html>maintenance</html>"


def test_a_set_list_with_no_sets_is_a_finished_game(data_dir, sleeps, tracker):
    fetch, _ = fake_fetch(fab_answers(**{f"{B}/tcgplayer/62/groups": groups()}))
    snap = tcgcsv.snapshot(FAB, fetch=fetch, tracker=tracker)
    assert snap.fetched == ["fab"] and plain(tracker.outcomes()["tcgcsv fab"]) == ("ok", "0 sets")
    assert lines(tcgcsv.day_dir(DAY, "fab")) == []


def test_pretty_printed_response_still_takes_one_line(data_dir, sleeps):
    pretty = json.dumps(json.loads(prices(1)), indent=2).encode()
    fetch, _ = fake_fetch(fab_answers(**{f"{B}/tcgplayer/62/100/prices": pretty}))
    tcgcsv.snapshot(FAB, fetch=fetch)
    game = tcgcsv.day_dir(DAY, "fab")
    assert len(tcgcsv.kept_game(game).splitlines()) == 2
    assert lines(game)[0] == row(100, prices(1))


def test_the_games_with_a_step_of_their_own_are_the_three_played():
    assert list(tcgcsv.GAMES) == ["mtg", "fab", "op"]


def test_each_game_is_a_step(data_dir, sleeps, tracker):
    answers = fab_answers(**{f"{B}/tcgplayer/68/groups": net.FetchError("HTTP 503")})
    fetch, _ = fake_fetch(answers)
    tcgcsv.snapshot({"fab": "Flesh & Blood TCG", "op": "One Piece Card Game"}, fetch=fetch, tracker=tracker)
    fab, op = tracker.steps
    assert (fab.label, fab.unit, fab.updates) == ("tcgcsv fab", "sets", [(0, 2), (1, None), (2, None)])
    assert plain(fab.outcome) == ("ok", "2 sets") and op.outcome == ("fail", "HTTP 503")


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
    assert lines(day / "warhammer-box-sets") == [row(40, prices(4))]
    assert not [url for url in asked if "/69/" in url or "/70/" in url]  # comics are never asked for
    yugioh = tracker.steps[-1]
    assert (yugioh.unit, yugioh.updates, plain(yugioh.outcome)) == (
        "sets",
        [(0, 2), (1, None), (2, None)],
        ("ok", "2 sets"),
    )
    assert plain(tracker.outcomes()["tcgcsv mtg"]) == ("ok", "1 set")


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
    why = "1 of 2 sets kept; set 21 failed (HTTP 503), asked again next run"
    assert snap.failed == [("yugioh", why)] and "warhammer-box-sets" in snap.fetched
    assert not tcgcsv.finished(tcgcsv.day_dir(DAY, "yugioh"))  # the next run tries again
    assert tracker.outcomes()["tcgcsv yugioh"] == ("fail", why)


def test_a_category_without_a_group_list_is_skipped_and_noted(data_dir, sleeps, tracker):
    snap = tcgcsv.snapshot(
        fetch=fake_fetch(every_game_answers(**{f"{B}/tcgplayer/85/groups": None}))[0], tracker=tracker
    )
    assert snap.empty == ["pokemon-japan"] and not snap.failed
    assert tracker.outcomes()["tcgcsv pokemon-japan"] == ("ok", "no sets on tcgcsv (HTTP 404); skipped")
    assert not tcgcsv.finished(tcgcsv.day_dir(DAY, "pokemon-japan"))
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


def test_a_game_the_budget_has_no_room_for_isn_t_started(data_dir, sleeps, monkeypatch):
    monkeypatch.setattr(tcgcsv, "DAILY_REQUESTS", 2)  # last-updated.txt and the category list
    fetch, asked = fake_fetch(fab_answers())
    snap = tcgcsv.snapshot(FAB, fetch=fetch)
    assert snap.failed == [("fab", tcgcsv.OVER_BUDGET)] and len(asked) == 2


def test_a_last_updated_that_isnt_utf8_is_set_aside(data_dir):
    utf16 = "2026-09-24T20:05:50+0000".encode("utf-16")
    fetch, _ = fake_fetch({f"{B}/last-updated.txt": utf16})
    with pytest.raises(
        net.FetchError, match="unexpected last-updated.txt: .*; set aside as tcgcsv/aside/last-updated-"
    ):
        tcgcsv.last_updated(fetch)
    (aside,) = (data_dir / "tcgcsv" / "aside").iterdir()
    assert aside.read_bytes() == utf16 and logged(data_dir)[-1]["list"] == "last-updated.txt"


def test_a_group_without_a_whole_number_group_id_fails_its_game_not_the_run(data_dir, sleeps):
    bad = json.dumps({"success": True, "errors": [], "results": [{"groupId": "x1", "name": "g"}]}).encode()
    fetch, _ = fake_fetch(fab_answers(**{f"{B}/tcgplayer/62/groups": bad}))
    snap = tcgcsv.snapshot(FAB, fetch=fetch)
    aside = "tcgcsv/aside/fab-groups-2026-09-24T220000Z.json"
    why = f"groups: a group without a whole-number groupId; set aside as {aside}, asked again next run"
    assert snap.failed == [("fab", why)]


def test_a_price_file_that_isnt_utf8_fails_its_game_not_the_run(data_dir, sleeps):
    utf16 = prices(1, 2).decode().encode("utf-16")  # json.loads reads it; the file must be UTF-8
    fetch, _ = fake_fetch(fab_answers(**{f"{B}/tcgplayer/62/100/prices": utf16}))
    snap = tcgcsv.snapshot(FAB, fetch=fetch)
    aside = "tcgcsv/aside/fab-100-2026-09-24T220000Z.json"
    why = f"1 of 2 sets kept; set 100 failed (not UTF-8; set aside as {aside}), asked again next run"
    assert snap.failed == [("fab", why)] and (data_dir / aside).read_bytes() == utf16


def test_a_price_file_served_broken_again_is_set_aside_once_a_publish(data_dir, sleeps):
    for body in (b'{"results": [', b'{"results": [{'):  # two broken copies, one Last-Modified
        fetch, _ = fake_fetch(fab_answers(**{f"{B}/tcgplayer/62/100/prices": body}))
        snap = tcgcsv.snapshot(FAB, fetch=fetch)
    aside = "tcgcsv/aside/fab-100-2026-09-24T220000Z.json"
    why = f"not the expected JSON; a copy of this publish is set aside already as {aside}"
    assert snap.failed == [("fab", f"1 of 2 sets kept; set 100 failed ({why}), asked again next run")]
    assert [p.name for p in (data_dir / "tcgcsv" / "aside").iterdir()] == ["fab-100-2026-09-24T220000Z.json"]
    first, again = [entry for entry in logged(data_dir) if entry["list"] == "fab/100.json"]
    assert first["aside"] == aside and again["not_kept"] is True
    assert first["publish"] == again["publish"] == runs.name(net.http_time(MODIFIED))


def test_a_set_with_no_last_modified_is_named_by_its_etag(data_dir, sleeps):
    for n in (1, 2):
        broken = net.Reply(b"<html>%d</html>" % n, {"etag": '"e1"'})
        fetch, _ = fake_fetch(fab_answers(**{f"{B}/tcgplayer/62/100/prices": broken}))
        tcgcsv.snapshot(FAB, fetch=fetch)
    assert len(list((data_dir / "tcgcsv" / "aside").iterdir())) == 1
    assert [entry["publish"] for entry in logged(data_dir) if entry["list"] == "fab/100.json"] == ['"e1"'] * 2


def test_a_category_list_that_isnt_json_is_set_aside(data_dir, sleeps):
    fetch, _ = fake_fetch(fab_answers(**{f"{B}/tcgplayer/categories": b"<html>busy</html>"}))
    with pytest.raises(
        net.FetchError, match="categories: not the expected JSON; set aside as tcgcsv/aside/categories-"
    ):
        tcgcsv.snapshot(FAB, fetch=fetch)


# ---- resuming, no answer, and tcgcsv's next day ------------------------------------------


def test_no_answer_fails_the_rest_of_tcgcsv_at_once_keeping_what_each_has(data_dir, sleeps, tracker):
    silent = net.NoAnswer("no answer after 3 tries (timed out)")
    fetch, asked = fake_fetch(every_game_answers(**{f"{B}/tcgplayer/62/200/prices": silent}))
    snap = tcgcsv.snapshot(fetch=fetch, tracker=tracker)
    why = f"1 of 2 sets kept; tcgcsv gave no answer ({silent}), the rest asked again next run"
    assert snap.fetched == ["mtg"] and snap.failed[0] == ("fab", why)
    assert snap.failed[1:] == [
        (game, tcgcsv.NO_ANSWER) for game in ("op", "pokemon-japan", "warhammer-box-sets", "yugioh")
    ]
    assert asked[-1] == f"{B}/tcgplayer/62/200/prices"  # nothing asked after it
    assert (tcgcsv.day_dir(DAY, "fab") / "prices.jsonl.part").exists()  # set 100 kept for the next run


def test_no_answer_for_a_set_list_fails_the_rest_too(data_dir, sleeps):
    silent = net.NoAnswer("no answer after 3 tries (reset)")
    fetch, _ = fake_fetch(every_game_answers(**{f"{B}/tcgplayer/68/groups": silent}))
    snap = tcgcsv.snapshot(fetch=fetch)
    assert snap.failed[0] == ("op", str(silent)) and snap.failed[1] == ("pokemon-japan", tcgcsv.NO_ANSWER)


def test_sets_that_fail_are_counted_and_the_first_named(data_dir, sleeps, monkeypatch):
    append = tcgcsv._append

    def full_disk(part, gid, *rest):
        if gid == 200:
            raise OSError(28, "No space left on device")
        append(part, gid, *rest)

    monkeypatch.setattr(tcgcsv, "_append", full_disk)
    fetch, _ = fake_fetch(fab_answers(**{f"{B}/tcgplayer/62/100/prices": net.FetchError("HTTP 500")}))
    snap = tcgcsv.snapshot(FAB, fetch=fetch)
    assert snap.failed == [
        ("fab", "0 of 2 sets kept; set 100 failed (HTTP 500), and 1 more set, asked again next run")
    ]


def test_a_set_with_no_last_modified_is_kept_under_the_run_s_day_and_flagged(data_dir, sleeps, tracker):
    fetch, _ = fake_fetch(fab_answers(**{f"{B}/tcgplayer/62/100/prices": net.Reply(prices(1, 2), {})}))
    tcgcsv.snapshot(FAB, fetch=fetch, tracker=tracker)
    assert plain(tracker.outcomes()["tcgcsv fab"]) == (
        "warn",
        "2 sets; 1 without a Last-Modified, kept under 2026-09-24",
    )
    assert lines(tcgcsv.day_dir(DAY, "fab"))[0] == row(100, prices(1, 2), modified=None)


def test_a_line_cut_off_mid_write_is_dropped_and_its_set_asked_again(data_dir, sleeps):
    fab = tcgcsv.day_dir(DAY, "fab")
    fab.mkdir(parents=True)
    (fab / "groups.json").write_bytes(groups(200, 100))
    (fab / "prices.jsonl.part").write_text(json.dumps(row(100, prices(1, 2))) + '\n{"groupId": 200, "fetc')
    fetch, asked = fake_fetch(fab_answers())
    tcgcsv.snapshot(FAB, fetch=fetch)
    assert asked[-1] == f"{B}/tcgplayer/62/200/prices" and f"{B}/tcgplayer/62/100/prices" not in asked
    assert lines(fab) == [row(100, prices(1, 2)), row(200, prices(3))]


NEXT = b"2026-09-25T20:05:10+0000"
NEXT_MODIFIED = "Fri, 25 Sep 2026 20:04:30 GMT"


def refreshing(stamp_again):
    """tcgcsv publishes the 25th while set 200's file is being asked for."""
    return fab_answers(
        **{
            f"{B}/last-updated.txt": [STAMP, stamp_again],
            f"{B}/tcgplayer/62/200/prices": net.Reply(prices(3), {"last-modified": NEXT_MODIFIED}),
        }
    )


def test_a_set_from_the_next_refresh_moves_the_run_to_its_day(data_dir, sleeps, tracker):
    fetch, asked = fake_fetch(refreshing(NEXT))
    snap = tcgcsv.snapshot(FAB, fetch=fetch, tracker=tracker)
    assert snap.refreshed == date(2026, 9, 25) and snap.fetched == ["fab"]
    assert asked[5:] == [f"{B}/last-updated.txt", f"{B}/tcgplayer/62/groups", f"{B}/tcgplayer/62/100/prices"]
    old, new = tcgcsv.day_dir(DAY, "fab"), tcgcsv.day_dir(date(2026, 9, 25), "fab")
    assert lines(old) == [row(100, prices(1, 2))]  # finished as it stood
    assert (old / "missing.txt").read_text() == "200\n"
    assert lines(new) == [
        row(100, prices(1, 2)),
        row(200, prices(3), "2026-09-25T20:04:30+00:00"),
    ]
    assert (new.parent / "last-updated.txt").read_bytes() == NEXT
    assert {label: plain(said) for label, said in tracker.outcomes().items()} == {
        "tcgcsv fab": (
            "warn",
            "1 of 2 sets kept for 2026-09-24; tcgcsv published 2026-09-25 mid-run, and the rest go under it; "
            "the last 1 set of 2",
        ),
        "tcgcsv 2026-09-24": (
            "warn",
            "finished as it stood; sets never fetched, named in missing.txt: fab 1",
        ),
    }


def test_the_next_day_s_stamp_is_kept_only_once_tcgcsv_has_written_it(data_dir, sleeps):
    snap = tcgcsv.snapshot(FAB, fetch=fake_fetch(refreshing(net.FetchError("HTTP 503")))[0])
    assert snap.refreshed == date(2026, 9, 25) and snap.fetched == ["fab"]
    assert not (tcgcsv.daily_dir() / "2026-09-25" / "last-updated.txt").exists()


def test_an_earlier_day_left_unfinished_is_finished_as_it_stood(data_dir, sleeps, tracker):
    before = tcgcsv.daily_dir() / "2026-09-23"
    (before / "fab").mkdir(parents=True)
    (before / "fab" / "groups.json").write_bytes(groups(1, 2))
    (before / "fab" / "prices.jsonl.part").write_text(
        "\n" + json.dumps(row(2, prices(5))) + "\n"
    )  # a blank line too
    (before / "op").mkdir()
    (before / "op" / "prices.jsonl.part").write_text(json.dumps(row(7, prices(9))) + "\n")  # no set list kept
    (tcgcsv.daily_dir() / "2026-09-22" / "fab").mkdir(parents=True)  # nothing to finish
    (tcgcsv.daily_dir() / "notes").mkdir()  # not a day
    tcgcsv.snapshot(FAB, fetch=fake_fetch(fab_answers())[0], tracker=tracker)
    assert tracker.outcomes()["tcgcsv 2026-09-23"] == (
        "warn",
        "finished as it stood; sets never fetched, named in missing.txt: fab 1, op, which unknown",
    )
    assert lines(before / "fab") == [row(2, prices(5))]
    assert (before / "fab" / "missing.txt").read_text() == "1\n"
    assert (before / "op" / "missing.txt").read_text() == "unknown: the day's set list wasn't kept\n"
    assert not list(before.rglob("*.part"))


def test_an_earlier_day_with_every_set_is_finished_whole(data_dir, sleeps, tracker):
    before = tcgcsv.day_dir(date(2026, 9, 23), "fab")
    before.mkdir(parents=True)
    (before / "groups.json").write_bytes(groups(2))
    (before / "prices.jsonl.part").write_text(json.dumps(row(2, prices(5))) + "\n")
    tcgcsv.snapshot(FAB, fetch=fake_fetch(fab_answers())[0], tracker=tracker)
    assert tracker.outcomes()["tcgcsv 2026-09-23"] == ("ok", "finished 1 game")
    assert not (before / "missing.txt").exists()


def test_a_day_that_can_t_be_finished_fails_its_step_and_keeps_its_part_file(
    data_dir, sleeps, tracker, monkeypatch
):
    before = tcgcsv.day_dir(date(2026, 9, 23), "fab")
    before.mkdir(parents=True)
    (before / "prices.jsonl.part").write_text(json.dumps(row(2, prices(5))) + "\n")

    def full_disk(game_dir, stamp):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(tcgcsv, "_finish", full_disk)
    tcgcsv.snapshot(FAB, fetch=fake_fetch(fab_answers())[0], tracker=tracker)
    assert tracker.outcomes()["tcgcsv 2026-09-23"] == ("fail", "[Errno 28] No space left on device")
    assert (before / "prices.jsonl.part").exists()


# ---- each finished game kept by the run rule -------------------------------------


def test_a_finished_game_is_kept_in_its_runs_under_the_day_s_stamp(data_dir, sleeps, tracker):
    tcgcsv.snapshot(FAB, fetch=fake_fetch(fab_answers())[0], tracker=tracker)
    fab = tcgcsv.day_dir(DAY, "fab")
    record = json.loads((fab / "kept.json").read_text())
    assert (record["made"], record["kind"], record["day"], record["sets"]) == (
        "2026-09-24T200550Z",
        "base",
        "2026-09-24",
        2,
    )
    assert record["file"] == "tcgcsv/lists/fab/2026-09-24T200550Z/2026-09-24T200550Z.json.zst"
    assert not (fab / "prices.jsonl.gz").exists() and not (fab / "prices.jsonl.part").exists()
    size = watching.size((data_dir.parent / "riffle" / record["file"]).stat().st_size)
    assert tracker.outcomes()["tcgcsv fab"] == ("ok", f"2 sets, kept as a new run, {size} kept twice")


def test_the_next_day_is_kept_as_a_difference(data_dir, sleeps, tracker):
    tcgcsv.snapshot(FAB, fetch=fake_fetch(fab_answers())[0])
    tcgcsv.snapshot(FAB, fetch=fake_fetch(fab_answers(**{f"{B}/last-updated.txt": NEXT}))[0], tracker=tracker)
    fab = tcgcsv.day_dir(date(2026, 9, 25), "fab")
    record = json.loads((fab / "kept.json").read_text())
    assert (record["made"], record["kind"]) == ("2026-09-25T200510Z", "diff")
    assert tracker.outcomes()["tcgcsv fab"][1].startswith("2 sets, kept as a difference of")
    assert lines(fab) == [row(100, prices(1, 2)), row(200, prices(3))]
    assert list(runs.kept(tcgcsv.lists_dir("fab"))) == ["2026-09-24T200550Z", "2026-09-25T200510Z"]


def test_a_day_kept_before_the_runs_still_counts_as_finished(data_dir, sleeps, tracker):
    fab = tcgcsv.day_dir(DAY, "fab")
    fab.mkdir(parents=True)
    body = (json.dumps(row(100, prices(1, 2))) + "\n").encode()
    (fab / "prices.jsonl.gz").write_bytes(gzip.compress(body))
    tcgcsv.snapshot(FAB, fetch=fake_fetch(fab_answers())[0], tracker=tracker)
    assert tracker.outcomes()["tcgcsv fab"] == ("ok", "already have 2026-09-24")
    assert tcgcsv.kept_game(fab) == body and tcgcsv.stored_days(FAB) == [DAY]
    assert tcgcsv.kept_game(tcgcsv.day_dir(DAY, "op")) is None


def test_made_is_when_each_day_kept_was_published(data_dir):
    for day, stamp in (("2026-09-24", STAMP), ("2026-09-25", NEXT), ("2026-09-26", b"garbled")):
        folder = data_dir / "tcgcsv" / "daily" / day
        folder.mkdir(parents=True)
        (folder / "last-updated.txt").write_bytes(stamp)
    assert tcgcsv.made() == [
        datetime(2026, 9, 24, 20, 5, 50, tzinfo=UTC),
        datetime(2026, 9, 25, 20, 5, 10, tzinfo=UTC),
    ]


def test_an_earlier_day_with_no_stamp_is_kept_under_its_midnight(data_dir, sleeps):
    before = tcgcsv.day_dir(date(2026, 9, 23), "fab")
    before.mkdir(parents=True)
    (before / "prices.jsonl.part").write_text(json.dumps(row(2, prices(5))) + "\n")
    tcgcsv.snapshot(FAB, fetch=fake_fetch(fab_answers())[0])
    assert json.loads((before / "kept.json").read_text())["made"] == "2026-09-23T000000Z"


# ---- the watch: asked when due -------------------------------------------------------


def watch(fetch, tracker=None, at: datetime = NOW, **kw) -> tcgcsv.Watched:
    if tracker is not None:
        kw["tracker"] = tracker
    return tcgcsv.watch(FAB, fetch=fetch, clock=lambda: at, **kw)


def logged(data_dir) -> list[dict]:
    return [json.loads(line) for line in (data_dir / "tcgcsv" / "watch.jsonl").read_text().splitlines()]


def test_the_first_watch_asks_and_fetches_the_day(data_dir, sleeps):
    fetch, asked = fake_fetch(fab_answers())
    res = watch(fetch)
    assert res.asked and res.snap is not None and res.snap.fetched == ["fab"]
    assert asked.count(f"{B}/last-updated.txt") == 1  # the check is the day's first request
    check, day = logged(data_dir)
    assert check == {
        "at": "2026-09-24T220000Z",
        "list": "last-updated",
        "result": "new",
        "made": "2026-09-24T200550Z",
    }
    assert day | {"seconds": 0} == {
        "at": "2026-09-24T220000Z",
        "list": "day",
        "made": "2026-09-24T200550Z",
        "result": "whole",
        "requests": 5,
        "seconds": 0,
    }


def test_a_watch_before_the_next_check_is_due_asks_nothing(data_dir, sleeps, tracker):
    watch(fake_fetch(fab_answers())[0])
    fetch, asked = fake_fetch(fab_answers())
    res = watch(fetch, tracker, at=NOW + timedelta(minutes=2))
    assert asked == [] and not res.asked and res.plan is not None and not res.plan.ask
    said = "next asked 2026-09-24 22:05 UTC; its next day's time is learned from 14 gaps, 0 so far"
    assert tracker.outcomes() == {"tcgcsv": ("ok", said)}


def test_a_due_check_that_finds_the_same_day_asks_once(data_dir, sleeps, tracker):
    watch(fake_fetch(fab_answers())[0])
    fetch, asked = fake_fetch(fab_answers())
    res = watch(fetch, tracker, at=NOW + timedelta(minutes=5))
    assert asked == [f"{B}/last-updated.txt"] and res.snap is None
    assert tracker.outcomes() == {"tcgcsv": ("ok", "no new day since the one made 2026-09-24 20:05 UTC")}
    assert logged(data_dir)[-1] == {
        "at": "2026-09-24T220500Z",
        "list": "last-updated",
        "result": "same",
        "made": "2026-09-24T200550Z",
    }


def test_a_failed_check_is_logged_and_fails_the_step(data_dir, sleeps, tracker):
    fetch, _ = fake_fetch({f"{B}/last-updated.txt": net.NoAnswer("no answer after 3 tries")})
    res = watch(fetch, tracker)
    assert res.asked and res.snap is None
    assert tracker.outcomes() == {"tcgcsv": ("fail", "no answer after 3 tries")}
    assert logged(data_dir) == [
        {
            "at": "2026-09-24T220000Z",
            "list": "last-updated",
            "result": "failed",
            "why": "no answer after 3 tries",
        }
    ]


def test_a_day_not_yet_whole_is_asked_at_every_run(data_dir, sleeps):
    watch(fake_fetch(fab_answers(**{f"{B}/tcgplayer/62/100/prices": net.FetchError("HTTP 503")}))[0])
    assert logged(data_dir)[-1]["result"] == "short"
    fetch, asked = fake_fetch(fab_answers())
    res = watch(fetch, at=NOW + timedelta(minutes=1))  # not due by the schedule, but the day isn't whole
    assert res.snap is not None and res.snap.fetched == ["fab"]
    assert asked == [f"{B}/last-updated.txt", f"{B}/tcgplayer/62/100/prices"]
    assert logged(data_dir)[-1]["result"] == "whole"


def test_the_sync_asks_whether_or_not_a_day_is_due(data_dir, sleeps):
    watch(fake_fetch(fab_answers())[0])
    fetch, asked = fake_fetch(fab_answers())
    watch(fetch, at=NOW + timedelta(minutes=1), always=True)
    assert asked == [f"{B}/last-updated.txt"]


def test_a_watch_that_finds_tcgcsv_s_lock_held_asks_nothing(data_dir, tracker):
    fetch, asked = fake_fetch(fab_answers())
    with locks.held(data_dir / "tcgcsv" / "watch.lock"):
        res = watch(fetch, tracker)
    assert res.busy and asked == []
    assert tracker.outcomes() == {"tcgcsv": ("ok", "another run is asking for it")}


def test_a_day_that_can_t_be_fetched_is_logged_short(data_dir, sleeps, tracker):
    res = watch(fake_fetch(fab_answers(**{f"{B}/tcgplayer/categories": None}))[0], tracker)
    assert res.snap is None and tracker.outcomes()["tcgcsv"] == ("fail", "categories: HTTP 404")
    assert logged(data_dir)[-1] | {"seconds": 0} == {
        "at": "2026-09-24T220000Z",
        "list": "day",
        "made": "2026-09-24T200550Z",
        "result": "short",
        "seconds": 0,
    }


def test_a_day_tcgcsv_moves_past_mid_run_is_short(data_dir, sleeps):
    res = watch(fake_fetch(refreshing(NEXT))[0])
    assert res.snap is not None and res.snap.refreshed == date(2026, 9, 25)
    assert logged(data_dir)[-1]["result"] == "short"


def test_a_learned_day_is_asked_from_its_expected_time(data_dir, sleeps, tracker):
    last = datetime(2026, 9, 24, 20, 5, 50, tzinfo=UTC)
    for n in range(15):  # 14 gaps of a day
        folder = tcgcsv.daily_dir() / (last - timedelta(days=n)).date().isoformat()
        folder.mkdir(parents=True)
        (folder / "last-updated.txt").write_bytes((last - timedelta(days=n)).strftime(tcgcsv.STAMP).encode())
    expected = last + timedelta(days=1)
    checks = [expected - timedelta(hours=2 + 4 * n) for n in range(42)]  # a clean week
    (data_dir / "tcgcsv" / "watch.jsonl").write_text(
        "".join(
            json.dumps({"at": runs.name(at), "list": "last-updated", "result": "same"}) + "\n"
            for at in checks
        )
    )
    fetch, asked = fake_fetch(fab_answers())
    watch(fetch, tracker, at=expected - timedelta(minutes=1))
    assert asked == []
    said = "next asked 2026-09-25 20:05 UTC; its next day expected 2026-09-25 20:05 UTC"
    assert tracker.outcomes() == {"tcgcsv": ("ok", said)}
    assert watch(fetch, at=expected + timedelta(minutes=1)).asked
