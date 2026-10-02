"""What every store's watch shares: its log, read back for the checks it made and how long its
fetches took; one list's failure kept to its own step; and the ETags kept saved however a run
ends."""

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from riffle import cadence, watching

NOW = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)


@pytest.fixture
def data(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    return tmp_path / "riffle"


def test_a_log_line_that_isnt_an_entry_is_left_out(data):
    assert watching.entries("tcgcsv") == []
    watching.log("tcgcsv", {"at": "2026-09-29T115500Z", "list": "last-updated", "result": "same"})
    with watching.log_path("tcgcsv").open("a") as f:
        f.write('not JSON\n["a list"]\n{"at": "2026-09-29T115')  # the last cut off mid-write
    kept = {"at": "2026-09-29T115500Z", "list": "last-updated", "result": "same"}
    assert watching.entries("tcgcsv") == [kept]


def test_checks_and_the_longest_fetch_come_from_the_log(data):
    for entry in (
        {"at": "2026-09-29T100000Z", "list": "mtg", "result": "kept", "seconds": 2.5},
        {"at": "2026-09-29T110000Z", "list": "mtg", "result": "failed", "why": "HTTP 503", "seconds": 60},
        {"at": "2026-09-29T113000Z", "list": "fab", "result": "unchanged", "seconds": 90},
        {"at": "2026-08-01T000000Z", "list": "mtg", "result": "kept", "seconds": 600},  # over 30 days old
        {"at": "not a time", "list": "mtg", "result": "kept"},
        {"at": "2026-09-29T115000Z", "list": "mtg"},  # no result: not a check
    ):
        watching.log("cardmarket", entry)
    found = watching.entries("cardmarket")
    assert watching.checks(found, "mtg") == [
        (datetime(2026, 9, 29, 10, tzinfo=UTC), False),
        (datetime(2026, 9, 29, 11, tzinfo=UTC), True),
        (datetime(2026, 8, 1, tzinfo=UTC), False),
    ]
    assert watching.longest(found, "mtg", NOW, timedelta(days=30)) == timedelta(seconds=60)
    assert watching.longest(found, "op", NOW, timedelta(days=30)) == timedelta(0)


def test_when_each_list_could_first_be_online_comes_from_its_checks(data):
    for entry in (
        {"at": "2026-09-29T211657Z", "list": "prices-today", "result": "kept", "made": "2026-09-29T061238Z"},
        {"at": "2026-09-30T050000Z", "list": "prices-today", "result": "unchanged"},  # before it was made
        {"at": "2026-09-30T125500Z", "list": "prices-today", "result": "unchanged"},
        {"at": "2026-09-30T125800Z", "list": "other", "result": "unchanged"},
        {"at": "2026-09-30T130000Z", "list": "prices-today", "result": "failed", "why": "HTTP 503"},
        {"at": "2026-09-30T130500Z", "list": "prices-today", "result": "kept", "made": "2026-09-30T061200Z"},
        {"at": "2026-09-30T131000Z", "list": "prices-today", "result": "known", "made": "2026-09-30T061200Z"},
        {"at": "2026-10-01T061500Z", "list": "prices-today", "result": "kept", "made": "2026-10-01T061200Z"},
        {"at": "2026-10-01T061600Z", "list": "prices-today", "result": "kept", "made": "2026-10-01T061200Z"},
        {"at": "2026-10-01T062000Z", "list": "prices-today", "result": "kept", "made": "someday"},
        {"at": "not a time", "list": "prices-today", "result": "unchanged"},
        {"at": "2026-10-01T070000Z", "list": "prices-today"},  # no result: not a check
        {"at": "2026-09-29T200400Z", "list": "last-updated", "result": "same", "made": "2026-09-28T200554Z"},
        {"at": "2026-09-29T200800Z", "list": "last-updated", "result": "same", "made": "2026-09-28T200554Z"},
        {"at": "2026-09-29T201315Z", "list": "last-updated", "result": "new", "made": "2026-09-29T200557Z"},
    ):
        watching.log("mtgjson", entry)
    found = watching.entries("mtgjson")
    assert watching.online(found, "prices-today") == {
        datetime(2026, 9, 29, 6, 12, 38, tzinfo=UTC): datetime(
            2026, 9, 29, 6, 12, 38, tzinfo=UTC
        ),  # first check
        datetime(2026, 9, 30, 6, 12, tzinfo=UTC): datetime(2026, 9, 30, 12, 55, tzinfo=UTC),
        datetime(2026, 10, 1, 6, 12, tzinfo=UTC): datetime(2026, 10, 1, 6, 12, tzinfo=UTC),
    }
    assert watching.online(found, "last-updated") == {  # tcgcsv's
        datetime(2026, 9, 29, 20, 5, 57, tzinfo=UTC): datetime(2026, 9, 29, 20, 8, tzinfo=UTC)
    }
    assert watching.online(found, "none") == {}


def test_a_list_not_due_says_when_it_goes_online_once_that_s_after_its_expected_time():
    expected = datetime(2026, 10, 16, 6, 12, tzinfo=UTC)
    late = cadence.Plan(
        False,
        expected + timedelta(hours=4),
        expected,
        expected + timedelta(hours=6, minutes=48),
        timedelta(hours=6),
        16,
    )
    assert watching.waiting(late, "list") == (
        "next asked 2026-10-16 10:12 UTC; its next list expected 2026-10-16 06:12 UTC, "
        "online from about 2026-10-16 13:00 UTC"
    )
    early = replace(late, opens=expected - timedelta(minutes=2))
    assert (
        watching.waiting(early, "list")
        == "next asked 2026-10-16 10:12 UTC; its next list expected 2026-10-16 06:12 UTC"
    )


def test_etags_that_cant_be_read_are_none(data):
    assert watching.load_tags("cardmarket") == {}
    watching.save_tags("cardmarket", {"mtg": '"abc"'})
    assert json.loads((data / "cardmarket" / "watch-etags.json").read_text()) == {"mtg": '"abc"'}
    (data / "cardmarket" / "watch-etags.json").write_text('{"mtg": 5, "fab": "\\"x\\""}')
    assert watching.load_tags("cardmarket") == {"fab": '"x"'}


def test_the_days_kept_come_from_the_log_and_one_it_cant_read_is_left_out(data):
    for day in ("2026-09-28", "2026-09-27", "someday", "2026-09-28"):
        watching.log("goatbots", {"at": "2026-09-29T031600Z", "list": "prices", "result": "kept", "day": day})
    watching.log(
        "goatbots", {"at": "2026-09-29T031600Z", "list": "prices", "result": "failed", "day": "2026-09-26"}
    )
    watching.log(
        "goatbots", {"at": "2026-09-29T031600Z", "list": "other", "result": "kept", "day": "2026-09-25"}
    )
    assert [d.isoformat() for d in watching.days("goatbots", "prices")] == ["2026-09-27", "2026-09-28"]


def listed(name: str, ask: watching.Ask, folder) -> watching.Listed:
    return watching.Listed(name, f"Store {name}", folder, ask)


def kept(tags: dict[str, str], now: datetime, step) -> dict:
    tags["first"] = '"1"'
    step.ok("kept")
    return {"result": "kept"}


def test_one_list_s_bug_fails_its_own_step_and_the_lists_after_it_are_asked(data, tracker):
    def broken(tags: dict[str, str], now: datetime, step) -> dict:
        raise KeyError("createdAt")

    asked = []

    def after(tags: dict[str, str], now: datetime, step) -> dict:
        asked.append("after")
        step.ok("no new list")
        return {"result": "unchanged"}

    lists = [listed("first", kept, data), listed("broken", broken, data), listed("after", after, data)]
    res = watching.many("store", "Store", lists, tracker, lambda: NOW, always=True)
    why = f"KeyError: 'createdAt' (unexpected; details in {data / 'errors.log'}); asked again next run"
    assert res.failed == [("Store broken", why)] and asked == ["after"] and res.same == ["Store after"]
    assert tracker.outcomes()["Store broken"] == ("fail", why)
    assert watching.load_tags("store") == {"first": '"1"'}
    entry = next(e for e in watching.entries("store") if e["list"] == "broken")
    assert entry["result"] == "failed" and entry["why"] == why and "seconds" in entry
    assert "KeyError: 'createdAt'" in (data / "errors.log").read_text()


def test_the_etags_kept_are_saved_however_the_run_ends(data, tracker):
    def then(run: watching.Pass) -> None:
        raise RuntimeError("a bug after the lists")

    with pytest.raises(RuntimeError):
        watching.many("store", "Store", [listed("first", kept, data)], tracker, lambda: NOW, True, then)
    assert watching.load_tags("store") == {"first": '"1"'}


def test_a_noted_step_ends_its_ok_and_warn_notes_with_its_own(tracker):
    steps = [watching.Noted(tracker.step(str(n)), "learning") for n in range(5)]
    steps[0].update(1, 2)
    steps[0].ok("no new list")
    steps[1].ok()
    steps[2].warn("kept; a note")
    steps[3].fail("HTTP 503")
    steps[4].drop()
    assert tracker.steps[0].updates == [(1, 2)]
    assert tracker.outcomes() == {
        "0": ("ok", "no new list; learning"),
        "1": ("ok", "learning"),
        "2": ("warn", "kept; a note; learning"),
        "3": ("fail", "HTTP 503"),
        "4": ("drop",),
    }
