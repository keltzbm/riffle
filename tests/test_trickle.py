"""riffle.trickle: the request log, the pace and the owed list, without a network."""

import json
from datetime import UTC, datetime, timedelta

from riffle import trickle

NOW = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)


def _req(minutes_ago, verdict="whole"):
    return trickle.Request(
        trickle.stamp(NOW - timedelta(minutes=minutes_ago)), "https://x.test", 200, 10, 5, verdict
    )


def test_the_log_reads_back_only_as_far_as_it_needs():
    log = trickle.RequestLog("test")
    for minutes in range(3000, 20, -1):  # about 390 KB: past the first 64 KB read
        log.add(_req(minutes))
    log.add(_req(10, "empty"))
    log.add(_req(1))
    with log.path.open("a") as f:
        f.write("not json\n")
    assert [r.verdict for r in log.since(NOW - timedelta(minutes=15))] == ["empty", "whole"]
    assert log.count(NOW - timedelta(days=30)) == 2982
    assert trickle.RequestLog("nothing").since(NOW) == []


def test_the_budget_is_the_level_cut_to_the_ceiling():
    log = trickle.RequestLog("test")
    pace = trickle.Pace(level=3)
    assert pace.budget(log, NOW) == 3
    for minutes in (1, 5, 14, 16):  # the last is outside the window
        log.add(_req(minutes))
    assert pace.budget(log, NOW) == 2
    log.add(_req(0))
    log.add(_req(0))
    assert pace.budget(log, NOW) == 0


def test_throttles_close_together_pause_longer_and_slow_the_pace():
    pace = trickle.Pace(level=3)
    assert pace.throttled(NOW) == NOW + timedelta(hours=3) and pace.level == 2
    assert pace.paused(NOW + timedelta(hours=2)) == NOW + timedelta(hours=3)
    assert pace.paused(NOW + timedelta(hours=3)) is None
    again = NOW + timedelta(hours=4)
    assert pace.throttled(again) == again + timedelta(hours=6) and pace.level == 1
    third = again + timedelta(hours=7)
    assert pace.throttled(third) == third + timedelta(hours=12) and pace.level == 1  # the slowest level
    assert pace.throttled(third + timedelta(hours=13)) == third + timedelta(hours=25)  # 12 hours at most
    much_later = third + timedelta(days=3)
    assert pace.throttled(much_later) == much_later + timedelta(hours=3)


def test_whole_answers_raise_the_pace_a_level_at_a_time():
    pace = trickle.Pace(level=1)
    assert not any(pace.whole() for _ in range(trickle.SPEED_UP_AFTER - 1))
    assert pace.whole() and (pace.level, pace.whole_streak) == (2, 0)
    top = trickle.Pace(level=3)
    assert not any(top.whole() for _ in range(trickle.SPEED_UP_AFTER * 2)) and top.level == 3


def test_the_pace_survives_a_save_and_a_bad_file():
    pace = trickle.Pace(level=2)
    pace.throttled(NOW)
    trickle.save_pace("test", pace)
    assert trickle.load_pace("test") == pace
    path = trickle.pace_path("test")
    path.write_text(json.dumps({"level": 9}))
    assert trickle.load_pace("test").level == 3
    path.write_text(json.dumps({"surprise": 1}))
    assert trickle.load_pace("test") == trickle.Pace()
    path.write_text("not json")
    assert trickle.load_pace("test") == trickle.Pace()


def test_owed_items_are_due_every_run_while_fresh_then_weekly():
    fresh = trickle.Owed(day="2026-09-01", found="x")
    assert fresh.due(NOW)
    fresh.tried(NOW, "empty", miss=True)
    assert fresh.due(NOW + timedelta(minutes=10)) and fresh.tries == 1
    old = trickle.Owed(day="2026-08-01", found="x")
    old.tried(NOW, "not published yet", miss=False)
    assert (old.tries, old.last) == (0, "not published yet")
    assert not old.due(NOW + timedelta(days=6)) and old.due(NOW + timedelta(days=7))


def test_the_owed_list_survives_a_save_and_skips_bad_entries():
    owed = {"a": trickle.Owed(day="2026-09-01", found="x", tries=2)}
    trickle.save_owed("test", owed)
    assert trickle.load_owed("test") == owed
    trickle.owed_path("test").write_text(
        json.dumps({"a": {"day": "2026-09-01", "found": "x"}, "b": {"oops": 1}})
    )
    assert list(trickle.load_owed("test")) == ["a"]
    assert trickle.now().tzinfo is UTC
