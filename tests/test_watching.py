"""What every store's watch shares: its log, read back for the checks it made and how long its
fetches took; one list's failure kept to its own step; and the ETags kept saved however a run
ends."""

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from riffle import cadence, lateness, runs, times, watching

NOW = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)


@pytest.fixture
def data(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    return tmp_path / "riffle"


def test_a_log_line_that_isnt_an_entry_is_left_out(data):
    assert watching.entries("tcgcsv") == []
    watching.log("tcgcsv", {"at": "2026-09-29T115500Z", "list": "last-updated", "result": "same"})
    with watching.log_path("tcgcsv", NOW).open("a") as f:
        f.write('not JSON\n["a list"]\n{"at": "2026-09-29T115')  # the last cut off mid-write
    kept = {"at": "2026-09-29T115500Z", "list": "last-updated", "result": "same"}
    assert watching.entries("tcgcsv") == [kept]


def test_each_check_goes_in_the_month_file_of_its_time(data, monkeypatch):
    monkeypatch.setattr(times, "now", lambda: datetime(2026, 11, 2, tzinfo=UTC))
    for entry in (
        {"at": "2026-10-01T000500Z", "list": "mtg", "result": "kept"},
        {"at": "2026-09-30T235500Z", "list": "mtg", "result": "unchanged"},
        {"list": "mtg", "result": "failed"},  # no time: now's month
    ):
        watching.log("cardmarket", entry)
    assert [path.name for path in watching.files("cardmarket")] == [
        "2026-09.jsonl",
        "2026-10.jsonl",
        "2026-11.jsonl",
    ]
    assert [entry["result"] for entry in watching.entries("cardmarket")] == ["unchanged", "kept", "failed"]
    assert [entry["result"] for entry in watching.back("cardmarket")] == ["failed", "kept", "unchanged"]


def old_log(data, store: str, text: str) -> None:
    (data / store).mkdir(parents=True, exist_ok=True)
    (data / store / "watch.jsonl").write_text(text)


OLD = (
    '{"at": "2026-09-28T120000Z", "list": "mtg", "result": "kept"}\n'
    '{"at": "2026-10-05T085135Z", "list": "mtg", "result": "unchanged"}\n'
    '{"at": "2026-10-05T0851'  # cut off mid-write
)


def test_the_log_kept_before_month_files_moves_to_its_last_entry_s_month_once_a_run_holds_the_lock(data):
    old_log(data, "cardmarket", OLD)
    inode = (data / "cardmarket" / "watch.jsonl").stat().st_ino
    assert [entry["result"] for entry in watching.read("cardmarket", NOW).entries] == ["kept", "unchanged"]
    with watching.held("cardmarket") as mine:
        assert mine
    dest = data / "cardmarket" / "watch" / "2026-10.jsonl"
    assert watching.files("cardmarket") == [dest]
    assert dest.read_text() == OLD and dest.stat().st_ino == inode  # moved, not copied
    watching.moved("cardmarket")  # nothing left to move
    assert dest.read_text() == OLD


def test_a_month_file_there_before_the_move_keeps_its_lines_after_the_old_ones(data):
    old_log(data, "cardmarket", OLD)
    newer = '{"at": "2026-10-05T090000Z", "list": "mtg", "result": "unchanged"}\n'
    dest = data / "cardmarket" / "watch" / "2026-10.jsonl"
    dest.parent.mkdir()
    dest.write_text(newer)
    (dest.parent / "2026-10.jsonl.part").write_text("a move cut off while writing")
    watching.moved("cardmarket")
    assert dest.read_text() == OLD + "\n" + newer and not (data / "cardmarket" / "watch.jsonl").exists()
    old_log(data, "cardmarket", OLD)  # cut off after the month file was written, before the old went
    watching.moved("cardmarket")
    assert dest.read_text() == OLD + "\n" + newer and watching.files("cardmarket") == [dest]


def test_an_old_log_with_no_time_it_can_read_moves_to_now_s_month(data, monkeypatch):
    monkeypatch.setattr(times, "now", lambda: NOW)
    old_log(data, "tcgcsv", "not JSON\n")
    watching.moved("tcgcsv")
    assert (data / "tcgcsv" / "watch" / "2026-09.jsonl").read_text() == "not JSON\n"


START = datetime(2026, 9, 1, tzinfo=UTC)


def a_year_of_checks(until: datetime) -> list[dict]:
    """Three lists' checks from START until a time: daily, made each day at 06:00 and a few
    minutes, checked every 3 hours, got at the first check after it's made but every fifth day a
    check later (served late), every 13th check failed, and the day before's kept again on each
    30th day (once across a month's end); rare, checked every 100 days; missing, answered on day 3
    that there's none, then failed every 40 days."""
    out: list[dict] = []
    n, before = 0, None
    for day in range((until - START).days + 1):
        base = START + timedelta(days=day)
        made = runs.name(base + timedelta(hours=6, minutes=day % 7))
        got = missed = False
        for k in range(8):
            at = base + timedelta(hours=3 * k, minutes=1)
            n += 1
            entry = {"at": runs.name(at), "list": "daily", "seconds": n % 17 + 0.5}
            if n % 13 == 0:
                entry |= {"result": "failed", "why": "HTTP 503"}
            elif day % 30 == 0 and k == 0 and before:
                entry |= {"result": "kept", "made": before}
            elif not got and runs.name(at) >= made and (day % 5 != 2 or missed):
                entry |= {"result": "kept", "made": made}
                got = True
            else:
                missed = missed or runs.name(at) >= made
                entry |= {"result": "unchanged"}
            out.append(entry)
        before = made
        at = runs.name(base + timedelta(hours=1))
        if day % 100 == 0:
            out.append({"at": at, "list": "rare", "result": "unchanged", "seconds": 1.0})
        if day == 3:
            out.append({"at": at, "list": "missing", "result": "missing"})
        elif day % 40 == 0:
            out.append({"at": at, "list": "missing", "result": "failed", "why": "HTTP 503"})
    return [entry for entry in out if runs.parse(entry["at"]) <= until]


@pytest.mark.parametrize(
    ("now", "months"),
    [
        (datetime(2027, 10, 10, 12, tzinfo=UTC), ["2027-09.jsonl", "2027-10.jsonl"]),  # 2026-09: over a year
        (datetime(2027, 3, 1, 12, tzinfo=UTC), ["2027-01.jsonl", "2027-02.jsonl", "2027-03.jsonl"]),
    ],
)
def test_the_schedule_from_month_files_is_the_schedule_from_one_file_holding_the_same_lines(
    data, monkeypatch, now, months
):
    every = a_year_of_checks(now)
    for entry in every:
        watching.log("mtgjson", entry)
    got = watching.read("mtgjson", now, keep=True)
    kept = [runs.parse(entry["made"]) for entry in every if "made" in entry]
    for name in ("daily", "rare", "missing"):
        found = watching.by_list(got.entries).get(name, [])
        online = watching.online(found, name, got.online.get(name))
        alone = watching.online(every, name)
        year = {m: b for m, b in alone.items() if now - m <= lateness.HISTORY}
        assert {m: b for m, b in online.items() if m in year} == year
        busy = watching.longest(found, name, now, lateness.WINDOW)
        assert busy == watching.longest(every, name, now, lateness.WINDOW)
        asked = watching.checks(every, name)
        assert max(watching.checks(found, name)) == max(asked)
        assert cadence.plan(kept, watching.checks(found, name), now, busy, online) == cadence.plan(
            kept, asked, now, busy, alone
        )
        answered = [
            entry["result"] for entry in every if entry["list"] == name and entry["result"] != "failed"
        ]
        assert [entry["result"] for entry in found if entry["result"] != "failed"][-1] == answered[-1]
    assert cadence.plan(kept, watching.checks(every, "daily"), now).gaps > lateness.LEAST_GAPS  # learned
    read: list[str] = []
    lines = watching._lines
    monkeypatch.setattr(watching, "_lines", lambda path: read.append(path.name) or lines(path))
    assert watching.read("mtgjson", now, keep=True) == got
    assert [name for name in read if name != "watch.jsonl"] == months


def test_a_month_s_summary_holds_what_a_run_needs_of_it(data):
    every = a_year_of_checks(datetime(2026, 10, 31, 23, 59, tzinfo=UTC))
    for entry in every:
        watching.log("mtgjson", entry)
    watching.read("mtgjson", datetime(2026, 12, 15, tzinfo=UTC), keep=True)
    september = json.loads((data / "mtgjson" / "watch" / "2026-09.summary.json").read_text())
    daily = september["lists"]["daily"]
    assert (daily["checks"], daily["failures"], daily["longest"]) == (240, 18, 16.5)
    alone = watching.online([entry for entry in every if entry["at"] < "2026-10"], "daily")
    assert daily["online"] == {runs.name(m): runs.name(b) for m, b in alone.items()}
    assert daily["online"]["2026-09-03T060200Z"] == "2026-09-03T090100Z"  # served late
    october = json.loads((data / "mtgjson" / "watch" / "2026-10.summary.json").read_text())
    again = datetime(2026, 9, 30, 6, 1, tzinfo=UTC)  # got again on 1 October: the first bound stands
    assert october["lists"]["daily"]["online"][runs.name(again)] == "2026-09-30T210100Z"
    read = watching.read("mtgjson", datetime(2026, 12, 15, tzinfo=UTC))
    assert read.online["daily"][again] == watching.online(every, "daily")[again] == again
    assert "rare" not in october["lists"] and october["carry"]["rare"]["last"]["at"] == "2026-09-01T010000Z"
    assert october["carry"]["missing"]["ok"]["result"] == "missing"
    assert october["carry"]["missing"]["last"]["result"] == "failed"
    assert october["carry"]["daily"]["last"]["at"] == "2026-10-31T210100Z"


def test_a_summary_missing_or_unreadable_is_made_again_and_written_only_when_the_run_holds_the_lock(data):
    now = datetime(2027, 9, 10, 12, tzinfo=UTC)
    for entry in a_year_of_checks(now):
        watching.log("mtgjson", entry)
    folder = data / "mtgjson" / "watch"
    first = watching.read("mtgjson", now)
    assert list(folder.glob("*.summary.json")) == []
    assert watching.read("mtgjson", now, keep=True) == first
    whole = (folder / "2027-06.summary.json").read_text()
    (folder / "2027-06.summary.json").unlink()
    (folder / "2027-07.summary.json").write_text('{"lists": {"daily": []}, "carry": {}}')
    (folder / "2027-05.summary.json").write_text("not JSON")
    assert watching.read("mtgjson", now, keep=True) == first
    assert (folder / "2027-06.summary.json").read_text() == whole
    assert json.loads((folder / "2027-07.summary.json").read_text())["lists"]["daily"]["checks"] == 248
    (folder / "2027-04.summary.json").write_text('{"lists": {"daily": {"online": {"x": "y"}}}, "carry": {}}')
    online = watching.read("mtgjson", now).online["daily"]
    assert online and not any(made.strftime("%Y-%m") == "2027-04" for made in online)  # a bound it can't read


def test_what_a_summary_carries_and_what_it_leaves_out():
    last, ok = (
        {"at": "2026-09-30T210000Z", "result": "failed"},
        {"at": "2026-09-30T180000Z", "result": "same"},
    )
    carry = {"a": {"last": last, "ok": ok}, "b": {"last": ok, "ok": ok}, "c": {"ok": "not an entry"}}
    assert watching._carried(carry) == [ok, ok, last]
    found = [
        {"at": "2026-10-01T000000Z", "result": "same"},
        {"list": 5},
        {"list": "d"},
        {"list": "d", "at": "x", "result": "same"},
    ]
    assert watching.by_list(found) == {"d": found[2:]}
    summary = watching.summarize(found, {})
    assert summary == {
        "lists": {"d": {"checks": 0, "failures": 0, "longest": 0.0, "online": {}}},
        "carry": {},
    }


def test_a_copy_set_aside_in_an_earlier_month_is_found(data, tmp_path):
    aside = {"result": "failed", "aside": "a.zip", "publish": "x"}
    watching.log("goatbots", {"at": "2026-08-30T100000Z", "list": "prices"} | aside)
    watching.log("goatbots", {"at": "2026-09-29T100000Z", "list": "other"} | aside | {"aside": "b.zip"})
    watching.log("goatbots", {"at": "2026-09-29T100500Z", "list": "prices", "result": "unchanged"})
    fresh = tmp_path / "fresh.zip"
    fresh.write_bytes(b"broken")
    e = watching.broken("goatbots", "prices", "x", fresh, "price.zip", NOW, "not a zip")
    assert str(e) == "not a zip; a copy of this publish is set aside already as a.zip, asked again next run"


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
        (datetime(2026, 8, 1, tzinfo=UTC), False),
        (datetime(2026, 9, 29, 10, tzinfo=UTC), False),
        (datetime(2026, 9, 29, 11, tzinfo=UTC), True),
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
