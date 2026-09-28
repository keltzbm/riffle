import gzip
import json
from datetime import UTC, date, datetime, timedelta

import pytest
from typer.testing import CliRunner

from riffle import net, trickle
from riffle.analysis import metagame
from riffle.ingest import mtgo
from riffle.ingest.decklist import parse_text

INDEX = """
<a href="/decklist/modern-challenge-32-2026-09-1912850001">Modern Challenge 32</a>
<a href="https://www.mtgo.com/decklist/modern-league-2026-09-2012850002">Modern League</a>
<a href="/decklist/pioneer-league-2026-09-2012850003">Pioneer League</a>
<a href="/decklist/modern-showcase-challenge-2026-08-3112840000">last month</a>
<a href="/decklist/modern-league-2026-09-2012850002">duplicate link</a>
<a href="/decklists/2026/08">not an event</a>
"""


def _row(name, qty, side=False):
    return {"qty": str(qty), "sideboard": "true" if side else "false", "card_attributes": {"card_name": name}}


def _page(data):
    return f"<html><script>window.MTGO.decklists.data = {json.dumps(data)};\nother();</script></html>"


CHALLENGE = {
    "decklists": [
        {
            "player": "bob",
            "loginid": "2",
            "main_deck": [_row("Lightning Bolt", 3), _row("Lightning Bolt", 1), _row("Mountain", 56)],
            "sideboard_deck": [_row("Fire/Ice", 2, side=True)],
        },
        {
            "player": "alice",
            "loginid": "1",
            "main_deck": [_row("Thoughtseize", 4), _row("Swamp", 56)],
            "sideboard_deck": [_row("Lightning Bolt", 1, side=True)],
        },
    ],
    "standings": [
        {"loginid": "1", "login_name": "alice", "rank": 1},
        {"loginid": "2", "login_name": "bob", "rank": 7},
    ],
}
LEAGUE = {"decklists": [{"player": "carol", "main_deck": [_row("Thoughtseize", 2)], "sideboard_deck": []}]}


def test_slugs_and_classification():
    assert mtgo.parse_slug("modern-challenge-32-2026-04-1812839681") == (
        "modern-challenge-32",
        "2026-04-18",
        "12839681",
    )
    assert mtgo.parse_slug("decklists") is None
    assert mtgo.classify("modern-showcase-challenge") == "showcase"
    assert mtgo.classify("modern-league") == "league"
    assert mtgo.classify("modern-super-qualifier") == "qualifier"
    assert mtgo.event_format("modern-showcase-challenge") == "modern"
    assert mtgo.event_format("duel-commander-league") == "duel-commander"


def test_index_links_are_deduplicated():
    assert mtgo.event_slugs(INDEX) == [
        "modern-challenge-32-2026-09-1912850001",
        "modern-league-2026-09-2012850002",
        "pioneer-league-2026-09-2012850003",
        "modern-showcase-challenge-2026-08-3112840000",
    ]


def test_parse_event_merges_printings_and_ranks():
    e = mtgo.parse_event("modern-challenge-32-2026-09-1912850001", mtgo.extract_data(_page(CHALLENGE)))
    assert (e.format, e.kind, e.date, e.event_id) == ("modern", "challenge", "2026-09-19", "12850001")
    assert [d.player for d in e.decks] == ["alice", "bob"]  # sorted by rank
    bob = e.decks[1]
    assert bob.rank == 7 and bob.record is None
    assert [(c.name, c.qty) for c in bob.main] == [("Lightning Bolt", 4), ("Mountain", 56)]
    assert [(c.name, c.qty) for c in bob.side] == [("Fire/Ice", 2)]


def test_league_record_and_text_roundtrip():
    e = mtgo.parse_event("modern-league-2026-09-2012850002", LEAGUE)
    assert e.decks[0].record == "5-0"
    deck = parse_text(e.decks[0].to_text())
    assert [(x.name, x.quantity, x.board) for x in deck.entries] == [("Thoughtseize", 2, "main")]
    bob = mtgo.parse_event("modern-challenge-32-2026-09-1912850001", CHALLENGE).decks[1]
    boards = {(x.name, x.board) for x in parse_text(bob.to_text()).entries}
    assert ("Fire/Ice", "sideboard") in boards


def test_missing_data_is_an_error():
    try:
        mtgo.extract_data("<html>no script</html>")
    except ValueError as e:
        assert "layout may have changed" in str(e)
    else:
        raise AssertionError("expected ValueError")


def test_card_stats_and_find():
    e1 = mtgo.parse_event("modern-challenge-32-2026-09-1912850001", CHALLENGE)
    e2 = mtgo.parse_event("modern-league-2026-09-2012850002", LEAGUE)
    stats = {s.name: s for s in metagame.card_stats([e1, e2])}
    bolt = stats["Lightning Bolt"]
    assert (bolt.decks, bolt.main_decks, bolt.side_decks, bolt.copies) == (2, 1, 1, 5)
    assert stats["Thoughtseize"].decks == 2 and stats["Thoughtseize"].avg == 3.0
    assert "Fire/Ice" not in {s.name for s in metagame.card_stats([e1], board="main")}
    assert [d.player for _, d in metagame.find_decks([e1, e2], card="thoughtseize")] == ["alice", "carol"]
    assert [d.player for _, d in metagame.find_decks([e1, e2], player="BOB")] == ["bob"]


EMPTY = {"decklists": []}


def test_old_empty_files_are_refetched_and_hidden(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    slug = "modern-league-2026-09-2012850002"
    mtgo.save(mtgo.parse_event(slug, EMPTY))  # what the first version wrote
    assert not mtgo.is_stored(slug)
    assert mtgo.load("modern") == []


def test_fingerprint_groups_identical_lists_only():
    e = mtgo.parse_event("modern-challenge-32-2026-09-1912850001", CHALLENGE)
    alice, bob = e.decks
    same = mtgo.MtgoDeck("zed", list(reversed(bob.main)), bob.side)
    assert same.fingerprint == bob.fingerprint  # order and pilot don't matter
    assert alice.fingerprint != bob.fingerprint
    moved = mtgo.MtgoDeck("zed", bob.main, [mtgo.Card("Fire/Ice", 1)])
    assert moved.fingerprint != bob.fingerprint  # sideboard counts


# ---- more parsing edge cases -------------------------------------------------------


@pytest.mark.parametrize(
    "name, fmt, kind",
    [
        ("modern-league", "modern", "league"),
        ("pauper-challenge-32", "pauper", "challenge"),
        ("modern-showcase-qualifier", "modern", "showcase"),
        ("legacy-super-qualifier", "legacy", "qualifier"),
        ("pioneer-preliminary", "pioneer", "preliminary"),
        ("duel-commander-league", "duel-commander", "league"),
        ("vintage-cube-draft", "vintage", "other"),
    ],
)
def test_format_and_kind_table(name, fmt, kind):
    assert (mtgo.event_format(name), mtgo.classify(name)) == (fmt, kind)


@pytest.mark.parametrize(
    "slug",
    [
        "premodern-league-2026-03-3110365",  # league ids are short and look like series ids
        "modern-challenge-64-2026-03-3112837908",
    ],
)
def test_real_slug_shapes_parse(slug):
    name, day, eid = mtgo.parse_slug(slug)
    assert day == "2026-03-31" and eid.isdigit() and not name.endswith("-")


def test_main_deck_rows_flagged_sideboard_move_to_side():
    data = {
        "decklists": [
            {
                "player": "x",
                "main_deck": [_row("Bolt", 4), _row("Duress", 2, side=True)],
                "sideboard_deck": [_row("Duress", 1, side=True)],
            }
        ]
    }
    d = mtgo.parse_event("modern-league-2026-09-2012850002", data).decks[0]
    assert [(c.name, c.qty) for c in d.main] == [("Bolt", 4)]
    assert [(c.name, c.qty) for c in d.side] == [("Duress", 3)]


def test_ranks_by_login_name_and_unranked_last():
    data = {
        "decklists": [
            {"player": "Zed", "main_deck": []},
            {"player": "Amy", "main_deck": []},
            {"player": "Bo", "main_deck": []},
        ],
        "standings": [{"login_name": "amy", "rank": 2}, {"login_name": "BO", "rank": "1"}],
    }
    decks = mtgo.parse_event("modern-challenge-32-2026-09-1912850001", data).decks
    assert [(d.player, d.rank) for d in decks] == [("Bo", 1), ("Amy", 2), ("Zed", None)]


def test_rows_without_names_are_skipped_and_quantity_key_accepted():
    data = {"decklists": [{"player": "x", "main_deck": [{"quantity": 3, "card_name": "Bolt"}, {"qty": "2"}]}]}
    d = mtgo.parse_event("modern-league-2026-09-2012850002", data).decks[0]
    assert [(c.name, c.qty) for c in d.main] == [("Bolt", 3)]


def test_explicit_wins_record():
    data = {"decklists": [{"player": "x", "main_deck": [], "wins": {"wins": "4", "losses": "1"}}]}
    assert mtgo.parse_event("modern-challenge-32-2026-09-1912850001", data).decks[0].record == "4-1"


def test_bad_slug_raises():
    with pytest.raises(ValueError, match="not an event slug"):
        mtgo.parse_event("decklists", CHALLENGE)


def test_event_roundtrips_through_storage(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    e = mtgo.parse_event("modern-challenge-32-2026-09-1912850001", CHALLENGE)
    mtgo.save(e)
    [back] = mtgo.load()
    assert back == e


def test_load_filters(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    mtgo.save(mtgo.parse_event("modern-challenge-32-2026-09-1912850001", CHALLENGE))
    mtgo.save(mtgo.parse_event("modern-league-2026-09-2012850002", LEAGUE))
    mtgo.save(mtgo.parse_event("pioneer-league-2026-09-2012850003", LEAGUE))
    assert [e.date for e in mtgo.load("modern")] == ["2026-09-20", "2026-09-19"]  # newest first
    assert [e.kind for e in mtgo.load("modern", kinds=["league"])] == ["league"]
    assert mtgo.load("modern", since=date(2026, 9, 20))[0].kind == "league"
    assert len(mtgo.load(["modern", "pioneer"])) == 3
    assert mtgo.load(folder=tmp_path / "nowhere") == []


# ---- the trickle ------------------------------------------------------------------

NOW = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
SEP = "https://www.mtgo.com/decklists/2026/09"
AUG = "https://www.mtgo.com/decklists/2026/08"
OLD = "modern-challenge-32-2026-08-0312840001"  # past PENDING_DAYS, and past FRESH_DAYS
OLDER = "modern-league-2026-08-0212840000"
YOUNG = "modern-league-2026-09-2012850002"
STORED = "modern-challenge-32-2026-09-1812849999"  # asked for again to check an empty answer


def _url(slug):
    return f"https://www.mtgo.com/decklist/{slug}"


class Site:
    """mtgo.com as a dict: text answers 200, an int is a bare status, an exception is
    raised, and anything else is a 404. moved maps a URL to where it redirects."""

    def __init__(self, pages=None, moved=None):
        self.pages, self.moved, self.asked = dict(pages or {}), dict(moved or {}), []

    def __call__(self, url):
        self.asked.append(url)
        page = self.pages.get(url, 404)
        if isinstance(page, Exception):
            raise page
        if isinstance(page, int):
            return net.Answer(page, url, b"")
        return net.Answer(200, self.moved.get(url, url), page.encode())


class Clock:
    def __init__(self, at=NOW):
        self.at = at

    def __call__(self):
        return self.at


def _trickle(site, clock=None, **kw):
    return mtgo.run_trickle(get=site, clock=clock or Clock(), sleep=lambda s: None, **kw)


def _owe(*slugs):
    owed = trickle.load_owed(mtgo.SOURCE)
    for slug in slugs:
        owed[slug] = trickle.Owed(day=mtgo.parse_slug(slug)[1], found="2026-09-01T00:00:00+00:00")
    trickle.save_owed(mtgo.SOURCE, owed)


def _store_canary(page=CHALLENGE):
    mtgo.save(mtgo.parse_event(STORED, CHALLENGE))
    return {_url(STORED): _page(page)}


@pytest.fixture
def no_index(monkeypatch):
    """Every index already read: a run goes straight to owed events."""
    monkeypatch.setattr(mtgo, "next_index", lambda months, at: None)


def test_a_first_run_reads_this_months_index_then_the_newest_owed_events():
    challenge, league, pioneer = mtgo.event_slugs(INDEX)[:3]
    site = Site({SEP: INDEX, **{_url(s): _page(CHALLENGE) for s in (challenge, league, pioneer)}})
    res = _trickle(site)
    assert site.asked == [SEP, _url(pioneer), _url(league)]  # 3 pages at the top pace, newest first
    assert [e.slug for e in res.fetched] == [pioneer, league]
    assert (res.listed, res.newly_owed, res.owed, res.due) == (4, 4, 2, 2)
    raw = json.loads(gzip.decompress(mtgo.raw_path(pioneer).read_bytes()))
    assert (raw["url"], raw["fetched"], raw["data"]) == (
        _url(pioneer),
        "2026-09-21T12:00:00+00:00",
        CHALLENGE,
    )
    assert [e.slug for e in mtgo.load("pioneer")] == [pioneer]  # the raw folder isn't read as events
    log = trickle.RequestLog(mtgo.SOURCE).since(NOW)
    assert [(r.url, r.verdict) for r in log] == [
        (SEP, "whole"),
        (_url(pioneer), "whole"),
        (_url(league), "whole"),
    ]


def _read(minutes_ago, events=5, at=NOW):
    return {"read": trickle.stamp(at - timedelta(minutes=minutes_ago)), "events": events}


def test_which_index_a_run_reads():
    assert mtgo.next_index({}, NOW) == (2026, 9)
    fresh = {"2026-09": _read(10)}
    assert mtgo.next_index(fresh, NOW) == (2026, 8)  # the sweep back begins
    assert mtgo.next_index({"2026-09": _read(61)}, NOW) == (2026, 9)  # the current month, hourly
    assert mtgo.next_index({**fresh, "2026-08": _read(9000), "2026-07": _read(9000)}, NOW) == (2026, 6)
    oct3 = datetime(2026, 10, 3, tzinfo=UTC)
    just_ended = {"2026-10": _read(10, at=oct3), "2026-09": _read(61, at=oct3)}
    assert mtgo.next_index(just_ended, oct3) == (2026, 9)  # hourly too, for a week
    ended = {**fresh, "2026-08": _read(1, 0), "2026-07": _read(1, 0), "2026-06": _read(1, 0)}
    assert mtgo.next_index(ended, NOW) is None and mtgo.sweep_next(ended, NOW) is None
    assert mtgo.next_index({**ended, "2026-07": _read(1)}, NOW) == (
        2026,
        5,
    )  # a month with events resets the count


def test_the_ceiling_leaves_only_what_the_last_15_minutes_allow():
    log = trickle.RequestLog(mtgo.SOURCE)
    for minutes in (1, 2, 3, 20):
        log.add(trickle.Request(trickle.stamp(NOW - timedelta(minutes=minutes)), "x", 200, 1, 1, "whole"))
    site = Site({SEP: INDEX})
    res = _trickle(site)
    assert res.budget == 2 and site.asked == [SEP, _url(mtgo.event_slugs(INDEX)[2])]


def test_a_month_just_begun_may_list_no_events():
    october = "https://www.mtgo.com/decklists/2026/10"
    site = Site({october: "<html></html>"})
    res = _trickle(site, Clock(datetime(2026, 10, 1, 6, tzinfo=UTC)))
    assert site.asked == [october] and res.throttled is None and res.stopped is None


def test_an_empty_page_for_a_young_event_is_not_published_yet(no_index):
    _owe(YOUNG)
    site = Site({_url(YOUNG): "<html>not rendered yet</html>"})
    res = _trickle(site)
    assert res.pending == [YOUNG] and site.asked == [_url(YOUNG)]  # nothing checked, nothing counted
    owed = trickle.load_owed(mtgo.SOURCE)[YOUNG]
    assert (owed.tries, owed.last) == (0, "not published yet")


def test_an_old_empty_page_is_a_miss_when_a_stored_event_comes_back_whole(no_index):
    _owe(OLD)
    site = Site({_url(OLD): _page(EMPTY), **_store_canary()})
    res = _trickle(site)
    assert site.asked == [_url(OLD), _url(STORED)]
    assert res.missed == [(OLD, "empty")] and res.throttled is None
    owed = trickle.load_owed(mtgo.SOURCE)[OLD]
    assert (owed.tries, owed.last, owed.last_try) == (1, "empty", "2026-09-21T12:00:00+00:00")
    assert trickle.RequestLog(mtgo.SOURCE).since(NOW)[-1].verdict == "canary whole"


def test_an_old_empty_page_and_a_stripped_stored_event_pause_the_trickle(no_index):
    _owe(OLD, OLDER)
    site = Site({_url(OLD): _page(EMPTY), **_store_canary(EMPTY)})
    res = _trickle(site)
    assert site.asked == [_url(OLD), _url(STORED)]  # the run stops there
    assert "came back empty" in res.throttled and res.resume == NOW + timedelta(hours=3)
    assert (res.level, trickle.load_owed(mtgo.SOURCE)[OLD].tries) == (2, 0)  # nothing counted against it
    later = Site()
    res = _trickle(later, Clock(NOW + timedelta(hours=1)))
    assert res.paused_until == NOW + timedelta(hours=3) and later.asked == []


def test_an_old_months_empty_index_is_checked_against_a_stored_event(monkeypatch):
    monkeypatch.setattr(mtgo, "next_index", lambda months, at: (2026, 8))
    site = Site({AUG: "<html>no links</html>", **_store_canary()})
    res = _trickle(site)
    assert site.asked == [AUG, _url(STORED)] and res.throttled is None
    assert json.loads(mtgo.months_path().read_text())["2026-08"]["events"] == 0
    site.pages[_url(STORED)] = _page(EMPTY)
    res = _trickle(site, Clock(NOW + timedelta(minutes=20)))
    assert "index listed no events" in res.throttled


def test_nothing_stored_to_check_against_ends_the_run_counting_nothing(no_index):
    _owe(OLD)
    res = _trickle(Site({_url(OLD): _page(EMPTY)}))
    assert "nothing stored" in res.stopped and trickle.load_owed(mtgo.SOURCE)[OLD].tries == 0


def test_a_404_or_a_redirect_is_an_ordinary_miss(no_index):
    _owe(OLD, OLDER)
    site = Site({_url(OLDER): _page(CHALLENGE)}, moved={_url(OLDER): "https://www.mtgo.com/decklists"})
    res = _trickle(site)
    assert sorted(res.missed) == [(OLD, "missing"), (OLDER, "redirect")]
    owed = trickle.load_owed(mtgo.SOURCE)
    assert owed[OLD].tries == owed[OLDER].tries == 1


def test_a_429_throttles_without_asking_for_a_stored_event(no_index):
    _owe(OLD)
    site = Site({_url(OLD): 429})
    res = _trickle(site)
    assert site.asked == [_url(OLD)] and "429" in res.throttled and res.resume
    assert trickle.RequestLog(mtgo.SOURCE).since(NOW)[-1].verdict == "throttled"


@pytest.mark.parametrize("page, why", [(net.FetchError("timed out"), "timed out"), (503, "HTTP 503")])
def test_no_answer_ends_the_run_and_counts_nothing(no_index, page, why):
    _owe(OLD, OLDER)
    site = Site({_url(OLD): page})
    res = _trickle(site)
    assert site.asked == [_url(OLD)] and why in res.stopped and res.throttled is None
    assert trickle.load_owed(mtgo.SOURCE)[OLD].last_try is None
    assert trickle.load_pace(mtgo.SOURCE).paused_until is None


def test_lists_that_wont_parse_fail_the_step_and_stay_owed(no_index, tracker):
    _owe(OLD)
    site = Site({_url(OLD): _page({"decklists": [{"player": "x", "main_deck": "not rows"}]})})
    res = _trickle(site, tracker=tracker)
    assert [slug for slug, _ in res.broken] == [OLD] and OLD in trickle.load_owed(mtgo.SOURCE)
    assert tracker.outcomes()["mtgo events"][0] == "fail"


def test_whole_answers_raise_the_pace(no_index):
    trickle.save_pace(mtgo.SOURCE, trickle.Pace(level=2, whole_streak=trickle.SPEED_UP_AFTER - 1))
    _owe(OLD)
    res = _trickle(Site({_url(OLD): _page(CHALLENGE)}))
    assert res.raised and res.level == 3


def test_the_old_misses_file_moves_into_the_owed_list():
    _store_canary()
    mtgo.misses_path().write_text(json.dumps({OLD: 2, STORED: 3, "not-a-slug": 1}))
    trickle.save_pace(mtgo.SOURCE, trickle.Pace(paused_until=trickle.stamp(NOW + timedelta(hours=1))))
    res = _trickle(Site())
    owed = trickle.load_owed(mtgo.SOURCE)
    assert res.carried == 1 and list(owed) == [OLD] and owed[OLD].tries == 2
    assert not mtgo.misses_path().exists()


def test_a_run_already_going_keeps_another_from_asking():
    site = Site({SEP: INDEX})
    with mtgo._lock() as held:
        assert held
        assert _trickle(site).busy and site.asked == []
        with pytest.raises(RuntimeError, match="in progress"):
            mtgo.forget(OLD)


def test_forget_and_status(no_index):
    _owe(OLD, YOUNG)
    assert mtgo.forget(YOUNG) and not mtgo.forget(YOUNG)
    log = trickle.RequestLog(mtgo.SOURCE)
    log.add(trickle.Request(trickle.stamp(NOW - timedelta(minutes=5)), "x", 200, 1, 1, "whole"))
    log.add(trickle.Request(trickle.stamp(NOW - timedelta(hours=2)), "x", 200, 1, 1, "empty"))
    st = mtgo.status(Clock())
    assert list(st.owed) == [OLD] and st.due == 1 and (len(st.window), len(st.day)) == (1, 2)
    assert not st.sweep_done and st.paused_until is None


# ---- the commands -----------------------------------------------------------------


def _cli(*args):
    from riffle.cli import app

    return CliRunner().invoke(app, list(args))


def test_the_trickle_command_reports_the_run(monkeypatch):
    def run(tracker):
        tracker.step("mtgo events").fail("1 unreadable")
        return mtgo.TrickleResult(
            level=2,
            budget=2,
            owed=7,
            due=3,
            carried=4,
            pending=["young"],
            missed=[("gone", "missing")],
            throttled="x came back empty",
            resume=NOW + timedelta(hours=3),
            broken=[("bad", "KeyError")],
        )

    monkeypatch.setattr(mtgo, "run_trickle", run)
    result = _cli("mtgo", "trickle")
    assert result.exit_code == 1
    for line in (
        "4 events from mtgo-misses.json moved to the owed list",
        "0 new events · 7 owed, 3 due · 2 pages a run",
        "1 not published yet: young",
        "gone: missing, still owed",
        "throttled: x came back empty; paused until 2026-09-21 15:00 UTC",
        "! bad: its lists wouldn't parse: KeyError",
    ):
        assert line in result.output


@pytest.mark.parametrize(
    "res, line",
    [
        (mtgo.TrickleResult(busy=True), "another trickle run is in progress"),
        (mtgo.TrickleResult(paused_until=NOW), "paused until 2026-09-21 12:00 UTC"),
        (mtgo.TrickleResult(level=3), "as many requests as allowed"),
        (mtgo.TrickleResult(level=3, budget=3, raised=True, stopped="no answer"), "up to 3 pages a run"),
    ],
)
def test_the_trickle_command_says_why_it_asked_little(monkeypatch, res, line):
    monkeypatch.setattr(mtgo, "run_trickle", lambda tracker: res)
    result = _cli("mtgo", "trickle")
    assert result.exit_code == 0 and line in result.output


def test_the_status_command():
    _owe(OLD, YOUNG)
    trickle.save_pace(mtgo.SOURCE, trickle.Pace(level=2, whole_streak=4))
    mtgo._save_months({"2026-09": _read(10, at=trickle.now())})
    result = _cli("mtgo", "status")
    assert result.exit_code == 0
    for line in (
        "pace       2 pages a run, up a level after 140 more whole answers",
        "owed       2 events, 2 due now",
        "2026-09  1",
        "2026-08  1",
        "indexes    read back to 2026-09, still going back",
    ):
        assert line in result.output


def test_the_forget_command():
    _owe(OLD)
    assert _cli("mtgo", "forget", OLD).output == f"forgot {OLD}\n"
    assert _cli("mtgo", "forget", OLD).exit_code == 1
