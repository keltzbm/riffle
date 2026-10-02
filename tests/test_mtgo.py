import gzip
import json
from dataclasses import asdict
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from riffle import net, times, trickle
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


# ---- storage, by month ------------------------------------------------------------

STORED_SLUG = "modern-challenge-32-2026-09-1812849999"


def test_an_event_is_kept_under_its_year_and_month():
    path = mtgo.save(mtgo.parse_event(STORED_SLUG, CHALLENGE))
    assert path == mtgo.store_dir() / "2026" / "09" / f"{STORED_SLUG}.json"
    assert mtgo.stored_path(STORED_SLUG) == path and mtgo.is_stored(STORED_SLUG)
    assert mtgo.event_path("not-an-event") == mtgo.store_dir() / "not-an-event.json"


def test_a_run_moves_events_earlier_builds_kept_in_the_top_folder(monkeypatch):
    monkeypatch.setattr(mtgo, "next_index", lambda months, at: None)
    event = mtgo.parse_event(STORED_SLUG, CHALLENGE)
    top = mtgo.store_dir() / f"{STORED_SLUG}.json"
    top.parent.mkdir(parents=True)
    top.write_text(json.dumps(asdict(event)))
    undated = mtgo.store_dir() / "premodern-league-2026-09-3111007.json"
    undated.write_text("{}")
    assert mtgo.stored_path(STORED_SLUG) == top and mtgo.is_stored(STORED_SLUG)  # before the move
    res = _trickle(Site())
    assert res.moved == 1 and not top.exists() and undated.exists()  # a name that isn't an event's stays
    assert mtgo.event_path(STORED_SLUG).exists() and mtgo.stored_path(STORED_SLUG) == mtgo.event_path(
        STORED_SLUG
    )
    assert _trickle(Site(), Clock(NOW + timedelta(minutes=10))).moved == 0


def test_load_reads_the_top_folder_and_only_the_months_it_needs():
    mtgo.save(mtgo.parse_event("modern-challenge-32-2026-09-1912850001", CHALLENGE))
    top = mtgo.parse_event("modern-league-2026-08-2012850002", LEAGUE)
    (mtgo.store_dir() / f"{top.slug}.json").write_text(json.dumps(asdict(top)))  # not moved yet
    july = mtgo.store_dir() / "2026" / "07"
    july.mkdir(parents=True)
    (july / "modern-league-2026-07-0112840000.json").write_text("{cut off")  # would raise if read
    assert [e.date for e in mtgo.load(since=date(2026, 8, 15))] == ["2026-09-19", "2026-08-20"]


def test_an_event_cut_off_while_written_leaves_no_file(monkeypatch):
    def cut_off(self, target):
        raise KeyboardInterrupt

    with monkeypatch.context() as m:
        m.setattr(Path, "replace", cut_off)
        with pytest.raises(KeyboardInterrupt):
            mtgo.save(mtgo.parse_event(STORED_SLUG, CHALLENGE))
    assert not mtgo.event_path(STORED_SLUG).exists() and not mtgo.is_stored(STORED_SLUG)
    assert mtgo.load() == []  # the part written isn't read as an event


def test_meta_show_finds_an_event_under_its_month_or_where_earlier_builds_kept_it():
    mtgo.save(mtgo.parse_event(STORED_SLUG, CHALLENGE))
    assert _cli("meta", "show", STORED_SLUG, "alice").output.startswith("4 Thoughtseize\n")
    top = mtgo.parse_event(OLD, LEAGUE)
    (mtgo.store_dir() / f"{OLD}.json").write_text(json.dumps(asdict(top)))
    assert _cli("meta", "show", OLD, "carol").output == "2 Thoughtseize\n"
    assert "no stored event" in _cli("meta", "show", OLDER, "carol").output


# ---- the trickle ------------------------------------------------------------------

NOW = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
SEP = "https://www.mtgo.com/decklists/2026/09"
AUG = "https://www.mtgo.com/decklists/2026/08"
OLD = "modern-challenge-32-2026-08-0312840001"  # past PENDING_DAYS, and past FRESH_DAYS
OLDER = "modern-league-2026-08-0212840000"
YOUNG = "modern-league-2026-09-2012850002"
STORED = STORED_SLUG  # fetched whole a few minutes ago: the canary


def _url(slug):
    return f"https://www.mtgo.com/decklist/{slug}"


class Site:
    """mtgo.com as a dict: text answers 200, an int is a bare status, an Answer is given
    as it is, an exception is raised, and anything else is a 404. moved maps a URL to
    where it redirects."""

    def __init__(self, pages=None, moved=None):
        self.pages, self.moved, self.asked = dict(pages or {}), dict(moved or {}), []

    def __call__(self, url):
        self.asked.append(url)
        page = self.pages.get(url, 404)
        if isinstance(page, Exception):
            raise page
        if isinstance(page, net.Answer):
            return page
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


def _tried(slug, asks, last="empty", at=NOW - timedelta(minutes=10), again=0):
    """An owed event asked asks times, the last at at, with again retries left in its round."""
    owed = trickle.load_owed(mtgo.SOURCE)
    owed[slug].asks, owed[slug].last_try, owed[slug].last = asks, trickle.stamp(at), last
    owed[slug].again, owed[slug].rounds = again, asks - 1 if again else asks
    trickle.save_owed(mtgo.SOURCE, owed)


def _level(n):
    trickle.save_pace(mtgo.SOURCE, trickle.Pace(level=n))


def _log(url, minutes_ago, verdict="whole", at=NOW):
    trickle.RequestLog(mtgo.SOURCE).add(
        trickle.Request(trickle.stamp(at - timedelta(minutes=minutes_ago)), url, 200, 1, 1, verdict)
    )


def _canary(minutes_ago=10, page=CHALLENGE):
    """STORED, fetched whole minutes_ago: what a run asks for again to check an empty answer."""
    mtgo.save(mtgo.parse_event(STORED, CHALLENGE))
    _log(_url(STORED), minutes_ago)
    return {_url(STORED): _page(page)}


@pytest.fixture
def no_index(monkeypatch):
    """Every index already read and the sweep done: a run goes straight to owed events,
    newest first."""
    monkeypatch.setattr(mtgo, "next_index", lambda months, at: None)
    monkeypatch.setattr(mtgo, "_sweep", lambda months, at: (None, True))


def test_a_first_run_reads_this_months_index_then_events_newest_and_oldest_in_turn():
    _level(3)
    challenge, league, pioneer, showcase = mtgo.event_slugs(INDEX)
    site = Site({SEP: INDEX, **{_url(s): _page(CHALLENGE) for s in (challenge, league, pioneer, showcase)}})
    res = _trickle(site)
    # the sweep back through the indexes has begun: the newest, then the oldest
    assert site.asked == [SEP, _url(pioneer), _url(showcase)]
    assert [e.slug for e in res.fetched] == [pioneer, showcase]
    assert (res.listed, res.newly_owed, res.owed, res.due) == (4, 4, 2, 2)
    assert res.index_outcome == "4 events, 4 newly owed"
    assert res.raised and res.level == 4  # every answer whole
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
        (_url(showcase), "whole"),
    ]
    # the league, whole at 36 hours old, says when leagues are ready; the showcase, from the
    # backlog, says nothing
    assert json.loads(mtgo.ages_path().read_text()) == {"league": 36.0}
    assert trickle.load_pace(mtgo.SOURCE).oldest_first  # the next run starts from the oldest


def test_while_the_sweep_goes_on_each_run_starts_from_the_other_end(monkeypatch):
    monkeypatch.setattr(mtgo, "next_index", lambda months, at: None)
    _owe(YOUNG, OLD, OLDER)
    site = Site({_url(s): _page(LEAGUE) for s in (YOUNG, OLD, OLDER)})
    _trickle(site)
    _trickle(site, Clock(NOW + timedelta(minutes=10)))  # two pages now: every answer was whole
    assert site.asked == [_url(YOUNG), _url(OLDER), _url(OLD)]
    owed = {s: trickle.Owed(day=mtgo.parse_slug(s)[1], found="x") for s in (YOUNG, OLD, OLDER)}
    assert mtgo.due(owed, NOW, oldest_first=True) == [OLDER, YOUNG, OLD]
    assert mtgo.due(owed, NOW, oldest_first=False) == [YOUNG, OLDER, OLD]
    assert mtgo.due(owed, NOW) == [YOUNG, OLD, OLDER]


def test_never_asked_events_taken_in_turn():
    assert mtgo._in_turn(["a", "b", "c", "d", "e"], oldest_first=True) == ["e", "a", "d", "b", "c"]
    assert mtgo._in_turn(["a", "b", "c"], oldest_first=False) == ["a", "c", "b"]
    assert mtgo._in_turn([], oldest_first=False) == []


def _read(minutes_ago, events=5, at=NOW):
    return {"read": trickle.stamp(at - timedelta(minutes=minutes_ago)), "events": events}


def _failed(asks, minutes_ago=10, days=(), last="empty"):
    """A month whose index failed slowly asks times, the last minutes_ago, believed empty on
    days: in its round until it has failed trickle.ROUND times."""
    again = trickle.ROUND - asks if asks < trickle.ROUND else 0
    miss = trickle.Owed(day="2026-07-31", found="x", asks=asks, last=last, again=again, rounds=asks // 3)
    miss.last_try = trickle.stamp(NOW - timedelta(minutes=minutes_ago))
    return {"miss": asdict(miss), "empty_days": list(days)}


THREE_DAYS = ("2026-09-01", "2026-09-08", "2026-09-15")
WEEK = 7 * 24 * 60


def test_which_index_a_run_reads():
    assert mtgo.next_index({}, NOW) == (2026, 9)
    fresh = {"2026-09": _read(10)}
    assert mtgo.next_index(fresh, NOW) == (2026, 8)  # the sweep back begins
    assert mtgo.next_index({"2026-09": _read(61)}, NOW) == (2026, 9)  # the current month, hourly
    assert mtgo.next_index({**fresh, "2026-08": _read(9000), "2026-07": _read(9000)}, NOW) == (2026, 6)
    oct3 = datetime(2026, 10, 3, tzinfo=UTC)
    just_ended = {"2026-10": _read(10, at=oct3), "2026-09": _read(61, at=oct3)}
    assert mtgo.next_index(just_ended, oct3) == (2026, 9)  # hourly too, for a week
    # the current month failed a run ago: asked again now, its hour not up
    assert mtgo.next_index({"2026-09": {**_read(10), **_failed(1)}}, NOW) == (2026, 9)
    assert mtgo.next_index({"2026-09": {**_read(70), **_failed(3)}}, NOW) == (2026, 8)  # its round over


def test_a_month_whose_index_failed_is_asked_again_before_the_sweep_goes_on():
    fresh = {"2026-09": _read(10), "2026-08": _read(9000)}
    assert mtgo.next_index({**fresh, "2026-07": _failed(1)}, NOW) == (2026, 7)  # a run ago
    assert mtgo.next_index({**fresh, "2026-07": _failed(3)}, NOW) == (2026, 6)  # its round over: waits
    in_round_first = {**fresh, "2026-07": _failed(3, minutes_ago=WEEK + 1), "2026-06": _failed(1)}
    assert mtgo.next_index(in_round_first, NOW) == (2026, 6)
    assert mtgo.next_index({**fresh, "2026-07": _failed(3, minutes_ago=WEEK + 1)}, NOW) == (2026, 7)
    odd = {**fresh, "2026-07": {"miss": {"day": "2026-07-32", "found": "x", "last_try": "x"}}}
    assert mtgo._month_retry(odd, NOW, set()) is None  # one that can't say when isn't asked
    assert mtgo._miss({"miss": {"surprise": 1}}) is None


def test_the_sweep_ends_on_three_months_in_a_row_believed_empty():
    fresh = {"2026-09": _read(10)}
    waiting = {**fresh, "2026-08": _failed(3), "2026-07": _failed(3), "2026-06": _failed(3)}
    assert mtgo._sweep(waiting, NOW) == (None, False)  # until each is believed empty
    assert mtgo.next_index(waiting, NOW) is None
    ended = {**fresh, **{m: _failed(3, days=THREE_DAYS) for m in ("2026-08", "2026-07", "2026-06")}}
    assert mtgo._sweep(ended, NOW) == ((2026, 5), True)  # the next older month, never asked
    assert mtgo.next_index(ended, NOW) == (2026, 5)
    asked = {**ended, "2026-05": _failed(3, minutes_ago=2 * 24 * 60, days=THREE_DAYS)}
    assert mtgo._sweep(asked, NOW) == (None, True)  # asked two days ago
    assert mtgo.next_index(asked, NOW) is None  # believed empty: not retried either
    asked["2026-05"] = _failed(3, minutes_ago=WEEK + 1, days=THREE_DAYS)
    assert mtgo._sweep(asked, NOW) == ((2026, 5), True)  # once a week
    assert mtgo._sweep({**ended, "2026-05": _read(9000)}, NOW) == ((2026, 4), False)  # it lists events
    assert mtgo.sweep_next(ended, NOW) == (2026, 5)


def test_the_ceiling_leaves_only_what_the_last_15_minutes_allow():
    _level(3)
    for minutes in (1, 2, 3, 20):
        _log("x", minutes)
    site = Site({SEP: INDEX})
    res = _trickle(site)
    assert res.budget == 2 and site.asked == [SEP, _url(mtgo.event_slugs(INDEX)[2])]


def test_a_canary_takes_its_request_from_the_ceiling(no_index):
    _level(5)
    _owe(OLD, OLDER)
    _tried(OLD, asks=1, again=2)
    pages = {_url(OLD): _page(EMPTY), **_canary(minutes_ago=10)}
    for minutes in (1, 2):
        _log("x", minutes)
    res = _trickle(Site(pages))  # 3 in the window: room for 2
    assert res.budget == 2 and "as many requests as allowed" in res.stopped
    assert trickle.load_owed(mtgo.SOURCE)[OLD].tries == 1 and OLDER in trickle.load_owed(mtgo.SOURCE)


def test_a_month_just_begun_may_list_no_events():
    october = "https://www.mtgo.com/decklists/2026/10"
    site = Site({october: "<html></html>"})
    res = _trickle(site, Clock(datetime(2026, 10, 1, 6, tzinfo=UTC)))
    assert site.asked == [october] and res.throttled is None and res.stopped is None
    assert mtgo._read_months()["2026-10"] == {"read": "2026-10-01T06:00:00+00:00", "events": 0}
    assert res.index_outcome == "no events yet"


def test_an_empty_page_for_a_young_event_is_not_published_yet(no_index):
    _owe(YOUNG)
    site = Site({_url(YOUNG): "<html>not rendered yet</html>"})
    res = _trickle(site)
    assert res.pending == [YOUNG] and site.asked == [_url(YOUNG)]  # nothing checked, nothing counted
    owed = trickle.load_owed(mtgo.SOURCE)[YOUNG]
    assert (owed.tries, owed.last, owed.warm()) == (0, "not published yet", True)
    assert owed.retry_at() == NOW  # asked again at the next run


def test_an_empty_page_asked_for_the_first_time_is_a_failed_try_and_nothing_more(no_index):
    _owe(OLD)
    site = Site({_url(OLD): _page(EMPTY), **_canary()})
    res = _trickle(site)
    assert site.asked == [_url(OLD)] and res.missed == [(OLD, "empty", None)]  # asked again next run
    owed = trickle.load_owed(mtgo.SOURCE)[OLD]
    assert (owed.tries, owed.asks, owed.last, owed.again) == (0, 1, "empty", 2)
    assert trickle.load_pace(mtgo.SOURCE).paused_until is None


def test_an_empty_page_on_a_retry_is_believed_when_the_canary_comes_back_whole(no_index):
    _owe(OLD)
    _tried(OLD, asks=1, again=2)
    site = Site({_url(OLD): _page(EMPTY), **_canary()})
    res = _trickle(site)
    assert site.asked == [_url(OLD), _url(STORED)]
    assert res.missed == [(OLD, "empty", None)] and res.throttled is None
    owed = trickle.load_owed(mtgo.SOURCE)[OLD]
    assert (owed.tries, owed.asks, owed.last_try) == (1, 2, "2026-09-21T12:00:00+00:00")
    assert trickle.RequestLog(mtgo.SOURCE).since(NOW)[-1].verdict == "canary whole"


def test_the_canary_is_a_page_fetched_whole_in_the_last_half_hour_not_todays_or_the_page_itself(no_index):
    _owe(OLD)
    _tried(OLD, asks=1, again=2)
    today = "modern-league-2026-09-2112850100"
    pages = {_url(OLD): _page(EMPTY), **_canary(minutes_ago=20)}
    _log(_url(OLD), 15)  # the page itself, whole before: not its own check
    _log(_url(today), 5)  # today's event: mtgo.com may still be adding lists
    _log(_url(STORED), 2, verdict="empty")
    site = Site(pages)
    _trickle(site)
    assert site.asked == [_url(OLD), _url(STORED)]


def test_with_no_canary_an_empty_page_is_not_believed_and_the_run_goes_on(no_index):
    _level(2)
    _owe(OLD, OLDER)
    _tried(OLD, asks=1, again=2)
    mtgo.save(mtgo.parse_event(STORED, CHALLENGE))
    _log(_url(STORED), 31)  # whole, but too long ago
    site = Site({_url(OLD): _page(EMPTY), _url(OLDER): _page(LEAGUE)})
    res = _trickle(site)
    assert site.asked == [_url(OLD), _url(OLDER)] and res.stopped is None
    assert trickle.load_owed(mtgo.SOURCE)[OLD].tries == 0 and [e.slug for e in res.fetched] == [OLDER]


@pytest.mark.parametrize(
    "page, moved, verdict, why",
    [
        (_page(EMPTY), None, "canary stripped", "came back without its lists"),
        (_page(CHALLENGE), "https://www.mtgo.com/decklists", "canary redirect", "was redirected"),
    ],
)
def test_a_canary_that_fails_ends_the_run_counting_nothing_and_pausing_nothing(
    no_index, page, moved, verdict, why
):
    _level(2)
    _owe(OLD, OLDER)
    _tried(OLD, asks=1, again=2)
    pages = {_url(OLD): _page(EMPTY), **_canary(page=page)}
    site = Site(pages, moved={_url(STORED): moved} if moved else None)
    res = _trickle(site)
    assert site.asked == [_url(OLD), _url(STORED)]  # the run stops there
    assert why in res.stopped and "nothing counted" in res.stopped and res.throttled is None
    assert trickle.RequestLog(mtgo.SOURCE).since(NOW)[-1].verdict == verdict
    owed = trickle.load_owed(mtgo.SOURCE)[OLD]
    assert (owed.tries, owed.asks) == (0, 2)
    pace = trickle.load_pace(mtgo.SOURCE)
    assert pace.paused_until is None and pace.level == 2
    later = Site()
    _trickle(later, Clock(NOW + timedelta(minutes=10)))
    assert later.asked[0] == _url(OLD)  # its round goes on, first in line


def test_an_old_months_empty_index_is_a_failed_try_believed_only_on_a_retry(monkeypatch):
    monkeypatch.setattr(mtgo, "next_index", lambda months, at: (2026, 8))
    site = Site({AUG: "<html>no links</html>", **_canary()})
    res = _trickle(site)
    assert site.asked == [AUG] and res.throttled is None and res.stopped is None
    month = mtgo._read_months()["2026-08"]
    assert "events" not in month and month["miss"]["asks"] == 1 and "empty_days" not in month
    assert res.index_outcome == "empty; asked again at the next run"
    res = _trickle(site, Clock(NOW + timedelta(minutes=10)))
    assert site.asked == [AUG, AUG, _url(STORED)]
    month = mtgo._read_months()["2026-08"]
    assert (month["miss"]["asks"], month["miss"]["tries"], month["empty_days"]) == (2, 1, ["2026-09-21"])
    _log(_url(STORED), 0, at=NOW + timedelta(minutes=20))
    res = _trickle(site, Clock(NOW + timedelta(minutes=20)))
    month = mtgo._read_months()["2026-08"]
    assert month["empty_days"] == ["2026-09-21"]  # one day, however many tries
    assert res.index_outcome == "empty; next try in an hour"  # its round over


def test_a_404_for_an_index_is_believed_at_once(monkeypatch):
    monkeypatch.setattr(mtgo, "next_index", lambda months, at: (2026, 8))
    _trickle(Site())
    month = mtgo._read_months()["2026-08"]
    assert (month["miss"]["last"], month["miss"]["tries"], month["empty_days"]) == (
        "missing",
        1,
        ["2026-09-21"],
    )


def test_an_index_answered_from_another_page_is_a_failed_try_not_that_months_list(monkeypatch):
    monkeypatch.setattr(mtgo, "next_index", lambda months, at: (2026, 8))
    site = Site({AUG: INDEX}, moved={AUG: "https://www.mtgo.com/decklists"})
    res = _trickle(site)
    month = mtgo._read_months()["2026-08"]
    assert "events" not in month and month["miss"]["last"] == "redirect" and res.newly_owed == 0
    assert trickle.load_owed(mtgo.SOURCE) == {}
    assert trickle.RequestLog(mtgo.SOURCE).since(NOW)[-1].note == "https://www.mtgo.com/decklists"


def test_an_index_that_doesnt_answer_is_asked_again_at_the_next_run(monkeypatch):
    monkeypatch.setattr(mtgo, "next_index", lambda months, at: (2026, 8))
    res = _trickle(Site({AUG: net.FetchError("timed out")}))
    assert "timed out" in res.stopped and mtgo._read_months()["2026-08"]["miss"]["last"] == "no answer"


def test_a_month_read_whole_and_then_failing_keeps_what_it_listed(monkeypatch):
    mtgo._save_months({"2026-09": _read(61)})
    res = _trickle(Site({SEP: "<html>no links</html>"}))
    month = mtgo._read_months()["2026-09"]
    assert month["events"] == 5 and month["miss"]["asks"] == 1 and res.index == "2026-09"


def test_months_saved_as_listing_no_events_are_forgotten_once_and_read_again(no_index):
    mtgo._save_months(
        {
            "2025-02": {"read": "2026-09-28T10:00:00+00:00", "events": 0},
            "2023-09": {"read": "2026-09-28T11:00:00+00:00", "events": 0},
            "2025-03": {"read": "2026-09-28T10:10:00+00:00", "events": 424},
            "2026-09": {"read": "2026-09-01T05:00:00+00:00", "events": 0},  # a new month: an answer
            "2024-01": {"surprise": 1},
        }
    )
    res = _trickle(Site())
    assert res.forgotten == ["2025-02", "2023-09"]
    assert sorted(mtgo._read_months()) == ["2024-01", "2025-03", "2026-09"]
    assert _trickle(Site(), Clock(NOW + timedelta(minutes=10))).forgotten == []


def test_a_404_or_a_redirect_is_an_ordinary_miss(no_index):
    _level(2)
    _owe(OLD, OLDER)
    site = Site({_url(OLDER): _page(CHALLENGE)}, moved={_url(OLDER): "https://www.mtgo.com/decklists"})
    res = _trickle(site)
    assert sorted((slug, outcome) for slug, outcome, _ in res.missed) == [
        (OLD, "missing"),
        (OLDER, "redirect"),
    ]
    owed = trickle.load_owed(mtgo.SOURCE)
    assert owed[OLD].tries == owed[OLDER].tries == 1


def test_a_failed_page_is_asked_at_each_of_the_next_two_runs_then_waits(no_index):
    _owe(YOUNG, OLD, OLDER)
    pages = {_url(s): _page(CHALLENGE) for s in (YOUNG, OLD, OLDER)}
    site, clock = Site(pages, moved={_url(YOUNG): "https://www.mtgo.com/decklists"}), Clock()
    runs = []
    for step in [timedelta(0)] + [timedelta(minutes=10)] * 4 + [timedelta(hours=1)]:
        clock.at += step
        runs.append(_trickle(site, clock))
    # the newest, three runs in a row; while it waits its hour, the others; then it again
    assert site.asked == [_url(s) for s in (YOUNG, YOUNG, YOUNG, OLD, OLDER, YOUNG)]
    assert runs[1].missed == [(YOUNG, "redirect", None)]
    assert runs[2].missed == [(YOUNG, "redirect", NOW + timedelta(minutes=20, hours=1))]
    assert [r.level for r in runs] == [1, 1, 1, 2, 3, 3]  # a run with every answer whole adds a page
    owed = trickle.load_owed(mtgo.SOURCE)
    assert list(owed) == [YOUNG] and (owed[YOUNG].tries, owed[YOUNG].asks) == (4, 4)
    assert owed[YOUNG].retry_at() == clock.at  # a new round


def test_a_round_whose_next_run_came_late_starts_cold(no_index):
    _level(2)
    _owe(OLD, OLDER)
    _tried(OLD, asks=1, at=NOW - timedelta(hours=2), again=2)  # its page is cold again
    owed = trickle.load_owed(mtgo.SOURCE)
    assert mtgo.due(owed, NOW) == [OLDER, OLD]  # after every event never asked for
    site = Site({_url(OLDER): _page(LEAGUE), _url(OLD): _page(EMPTY), **_canary()})
    res = _trickle(site)
    assert site.asked == [_url(OLDER), _url(OLD)]  # its empty answer isn't checked: it may be the build
    assert trickle.load_owed(mtgo.SOURCE)[OLD].tries == 0 and res.retried == 0


def test_a_whole_answer_on_a_retry_is_counted(no_index):
    _owe(OLD)
    _tried(OLD, asks=1, again=2)
    res = _trickle(Site({_url(OLD): _page(CHALLENGE)}))
    assert res.retried == 1 and [e.slug for e in res.fetched] == [OLD]


def test_every_page_gets_a_minute_to_answer(monkeypatch):
    asked = []
    monkeypatch.setattr(net, "get_once", lambda url, accept, timeout: asked.append((url, timeout)))
    mtgo._get(SEP)
    mtgo._get(_url(YOUNG))
    assert asked == [(SEP, 60.0), (_url(YOUNG), 60.0)]


@pytest.mark.parametrize(
    "status, retry_after, resume, why",
    [
        (429, None, NOW + timedelta(hours=3), "mtgo.com answered 429"),
        (403, None, NOW + timedelta(hours=3), "mtgo.com answered 403"),
        (503, "600", NOW + timedelta(minutes=10), "mtgo.com answered 503, Retry-After 600"),
        (
            200,
            "Mon, 21 Sep 2026 14:00:00 GMT",
            NOW + timedelta(hours=2),
            "mtgo.com answered 200, Retry-After Mon, 21 Sep 2026 14:00:00 GMT",
        ),
    ],
)
def test_a_pause_only_when_mtgo_com_asks_for_one(no_index, status, retry_after, resume, why):
    _level(2)
    _owe(OLD, OLDER)
    site = Site({_url(OLD): net.Answer(status, _url(OLD), b"", retry_after)})
    res = _trickle(site)
    assert site.asked == [_url(OLD)] and res.throttled == why and res.resume == resume
    pace = trickle.load_pace(mtgo.SOURCE)
    assert (pace.paused_until, pace.why, pace.level) == (trickle.stamp(resume), why, 1)
    assert trickle.RequestLog(mtgo.SOURCE).since(NOW)[-1].verdict == "throttled"
    assert trickle.load_owed(mtgo.SOURCE)[OLD].last_try is None  # the page isn't to blame
    later = _trickle(Site(), Clock(resume - timedelta(minutes=1)))
    assert later.paused_until == resume and later.why == why


def test_no_answer_ends_the_run_and_the_page_is_asked_again_at_the_next(no_index):
    _level(2)
    _owe(OLD, OLDER)
    site = Site({_url(OLD): net.FetchError("timed out")})
    res = _trickle(site)
    assert site.asked == [_url(OLD)] and "timed out" in res.stopped and res.throttled is None
    owed = trickle.load_owed(mtgo.SOURCE)[OLD]
    assert (owed.last, owed.asks, owed.tries, owed.warm()) == ("no answer", 1, 0, True)
    assert trickle.load_pace(mtgo.SOURCE).paused_until is None


def test_a_5xx_ends_the_run_and_counts_nothing(no_index):
    _owe(OLD, OLDER)
    site = Site({_url(OLD): 503})
    res = _trickle(site)
    assert site.asked == [_url(OLD)] and "HTTP 503" in res.stopped and res.throttled is None
    assert trickle.load_owed(mtgo.SOURCE)[OLD].last_try is None
    assert trickle.load_pace(mtgo.SOURCE).paused_until is None


def test_lists_that_wont_parse_fail_the_step_and_stay_owed(no_index, tracker):
    _owe(OLD)
    site = Site({_url(OLD): _page({"decklists": [{"player": "x", "main_deck": "not rows"}]})})
    res = _trickle(site, tracker=tracker)
    assert [slug for slug, _ in res.broken] == [OLD] and OLD in trickle.load_owed(mtgo.SOURCE)
    assert tracker.outcomes()["mtgo events"][0] == "fail"


def test_only_a_run_whose_every_answer_is_whole_raises_the_pace(no_index):
    _level(2)
    _owe(OLD)
    res = _trickle(Site({_url(OLD): _page(CHALLENGE)}))
    assert res.raised and res.level == 3
    _owe(OLDER)
    res = _trickle(Site({_url(OLDER): _page(EMPTY)}), Clock(NOW + timedelta(minutes=10)))
    assert not res.raised and res.level == 3
    res = _trickle(Site(), Clock(NOW + timedelta(days=1)))  # nothing due: nothing asked
    assert not res.raised and res.level == 3


def test_an_event_younger_than_its_kind_has_ever_come_back_whole_waits(no_index):
    _log(_url("pioneer-league-2026-09-1512850100"), 0, at=datetime(2026, 9, 15, 6, tzinfo=UTC))  # at 6 hours
    _log(_url("pioneer-league-2026-07-1512830100"), 0, at=datetime(2026, 9, 15, 6, tzinfo=UTC))  # the backlog
    _log(
        _url("modern-challenge-32-2026-09-1512850101"),
        0,
        verdict="empty",
        at=datetime(2026, 9, 15, 2, tzinfo=UTC),
    )
    _log(SEP, 0, at=datetime(2026, 9, 15, 2, tzinfo=UTC))
    _log(_url(UNDATED), 0, at=datetime(2026, 9, 15, 2, tzinfo=UTC))  # a name with no real date
    league, challenge = "modern-league-2026-09-2112850200", "modern-challenge-32-2026-09-2112850201"
    _owe(league, challenge)
    _level(2)
    early = Clock(datetime(2026, 9, 21, 4, tzinfo=UTC))
    site = Site()
    res = _trickle(site, early)
    assert site.asked == [_url(challenge)]  # no challenge has come back whole yet: asked at any age
    assert (res.waiting, res.due) == (1, 1)  # the challenge, missing, is asked again at the next run
    assert json.loads(mtgo.ages_path().read_text()) == {"league": 6.0}
    assert mtgo.waiting(trickle.load_owed(mtgo.SOURCE), {"league": 6.0}, early()) == 1
    _trickle(site, Clock(datetime(2026, 9, 21, 6, 10, tzinfo=UTC)))
    assert site.asked[1:] == [_url(league), _url(challenge)]  # the league, old enough now; the challenge's
    # page, asked two hours ago, is cold again: its retry goes after events never asked for


def test_learned_ages_are_read_back_and_relearned_from_a_bad_file():
    log = trickle.RequestLog(mtgo.SOURCE)
    _log(_url("pioneer-league-2026-09-1512850100"), 0, at=datetime(2026, 9, 15, 6, tzinfo=UTC))
    mtgo.ages_path().parent.mkdir(parents=True, exist_ok=True)
    mtgo.ages_path().write_text('{"league": 2.5}')
    assert mtgo._read_ages(log) == {"league": 2.5}
    mtgo.ages_path().write_text('["not", "ages"]')
    assert mtgo._read_ages(log) == {"league": 6.0}
    assert mtgo.waiting({"odd": trickle.Owed(day="2026-09-31", found="x")}, {"other": 1.0}, NOW) == 0
    bad_day = {"modern-league-2026-09-2012850002": trickle.Owed(day="2026-09-31", found="x")}
    assert mtgo.waiting(bad_day, {"league": 1.0}, NOW) == 0


def test_the_old_misses_file_moves_into_the_owed_list():
    mtgo.save(mtgo.parse_event(STORED, CHALLENGE))
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


UNDATED = "premodern-league-2026-09-3111007"  # as mtgo.com listed it on 2026-10-01: 31 September


@pytest.mark.parametrize("day", ["2026-09-31", "2026-02-30", "2026-13-01", "2026-00-10"])
def test_a_name_whose_date_is_not_a_day_is_not_an_event(day):
    assert mtgo.parse_slug(f"premodern-league-{day}11007") is None


def test_a_leap_day_is_a_day():
    assert mtgo.parse_slug("modern-league-2028-02-2911007") == ("modern-league", "2028-02-29", "11007")


def test_an_index_listing_an_undated_name_twice_owes_nothing_for_it():
    link = f'<a href="/decklist/{UNDATED}">Premodern League</a>'
    index = INDEX + link + link
    assert UNDATED not in mtgo.event_slugs(index) and mtgo.undated_slugs(index) == [UNDATED]
    site = Site({SEP: index})
    res = _trickle(site)
    assert res.undated == [UNDATED] and UNDATED not in trickle.load_owed(mtgo.SOURCE)
    assert mtgo._read_months()["2026-09"]["undated"] == [UNDATED]
    assert _url(UNDATED) not in site.asked


def test_an_owed_page_with_no_real_date_is_set_aside_and_the_rest_are_fetched(no_index, tracker):
    _owe(OLD)
    owed = trickle.load_owed(mtgo.SOURCE)
    owed[UNDATED] = trickle.Owed(day="2026-09-31", found="2026-10-01T20:42:27+00:00")
    owed[UNDATED].tried(NOW - timedelta(hours=1), "redirect", miss=True)
    trickle.save_owed(mtgo.SOURCE, owed)
    site = Site({_url(OLD): _page(CHALLENGE)})
    res = _trickle(site, tracker=tracker)
    assert res.set_aside == [UNDATED] and [e.slug for e in res.fetched] == [OLD] and not res.undue
    assert UNDATED not in trickle.load_owed(mtgo.SOURCE) and _url(UNDATED) not in site.asked
    kept = [json.loads(line) for line in trickle.set_aside_path(mtgo.SOURCE).read_text().splitlines()]
    assert kept == [
        {
            "key": UNDATED,
            "why": "its name holds no real date",
            "at": trickle.stamp(NOW),
            "day": "2026-09-31",
            "found": "2026-10-01T20:42:27+00:00",
            "tries": 1,
            "last_try": trickle.stamp(NOW - timedelta(hours=1)),
            "last": "redirect",
            "asks": 1,
            "again": 2,
            "rounds": 0,
        }
    ]
    assert "mtgo owed list" not in tracker.outcomes()
    assert _trickle(Site()).set_aside == []  # set aside once


def test_an_owed_event_that_cant_say_when_its_due_fails_alone(no_index, tracker):
    _owe(OLD, OLDER)
    owed = trickle.load_owed(mtgo.SOURCE)
    owed[OLDER].day = "2026-09-31"  # a real name over a day that isn't one
    owed[OLDER].tried(NOW - timedelta(hours=1), "redirect", miss=True)
    trickle.save_owed(mtgo.SOURCE, owed)
    assert mtgo.due(owed, NOW) == [OLD]  # left out, and nothing raised
    site = Site({_url(OLD): _page(CHALLENGE)})
    res = _trickle(site, tracker=tracker)
    assert [e.slug for e in res.fetched] == [OLD]
    assert [(slug, type(e)) for slug, e in res.undue] == [(OLDER, ValueError)]
    assert OLDER in trickle.load_owed(mtgo.SOURCE)  # still owed: nothing is dropped
    assert tracker.outcomes()["mtgo owed list"][0] == "fail"


def test_a_paused_run_still_says_which_owed_events_cant_say_when_theyre_due(no_index, tracker):
    _owe(OLDER)
    owed = trickle.load_owed(mtgo.SOURCE)
    owed[OLDER].day = "2026-09-31"
    owed[OLDER].tried(NOW - timedelta(hours=1), "redirect", miss=True)
    trickle.save_owed(mtgo.SOURCE, owed)
    pace = trickle.load_pace(mtgo.SOURCE)
    pace.paused_until = trickle.stamp(NOW + timedelta(hours=1))
    trickle.save_pace(mtgo.SOURCE, pace)
    res = _trickle(Site(), tracker=tracker)
    assert res.paused_until is not None and [slug for slug, _ in res.undue] == [OLDER]


def test_forget_and_status(no_index):
    _owe(OLD, YOUNG)
    assert mtgo.forget(YOUNG) and not mtgo.forget(YOUNG)
    _log("x", 5)
    _log("x", 120, verdict="empty")
    st = mtgo.status(Clock())
    assert list(st.owed) == [OLD] and st.at == NOW and (len(st.window), len(st.day)) == (1, 2)
    assert not st.sweep_done and st.paused_until is None and (st.ages, st.waiting) == ({}, 0)


def test_when_a_page_is_asked_again():
    assert mtgo.next_try(NOW, None) == mtgo.next_try(NOW, NOW) == "asked again at the next run"
    assert mtgo.next_try(NOW, NOW + timedelta(minutes=50)) == "next try in an hour"
    assert mtgo.next_try(NOW, NOW + timedelta(hours=16)) == "next try in 16 hours"
    assert mtgo.next_try(NOW, NOW + timedelta(days=7)) == "next try in 7 days"


def test_months_not_read_whole_yet_newest_first():
    months = {
        "2026-09": _read(10),
        "2026-08": {**_read(9000), **_failed(1)},  # read whole once: not listed
        "2025-02": _failed(1, days=["2026-09-21"]),
        "2023-09": _failed(3, days=THREE_DAYS),
        "2023-08": _failed(3, minutes_ago=60),
        "2023-04": {"miss": {"day": "2023-04-31", "found": "x", "last_try": trickle.stamp(NOW), "asks": 3}},
    }
    rows = [(key, miss.asks, days, when) for key, miss, days, when in mtgo.unread_months(months, NOW)]
    assert rows == [
        ("2025-02", 1, 1, "asked again at the next run"),
        ("2023-09", 3, 3, "not asked again"),
        ("2023-08", 3, 0, "next try in 7 days"),
        ("2023-04", 3, 0, "can't say when it's due"),
    ]


# ---- the commands -----------------------------------------------------------------


def _cli(*args):
    from riffle.cli import app

    return CliRunner().invoke(app, list(args))


def test_the_trickle_command_reports_the_run(monkeypatch):
    soon = trickle.now() + timedelta(hours=1, minutes=1)

    def run(tracker):
        tracker.step("mtgo events").fail("1 unreadable")
        return mtgo.TrickleResult(
            level=2,
            budget=2,
            owed=7,
            due=3,
            waiting=2,
            carried=4,
            moved=1841,
            forgotten=["2025-02", "2023-09"],
            retried=1,
            pending=["young"],
            missed=[("gone", "missing", soon), ("again", "empty", None)],
            throttled="mtgo.com answered 429",
            resume=NOW + timedelta(hours=3),
            broken=[("bad", "KeyError")],
            index="2026-09",
            set_aside=[UNDATED],
            undated=[UNDATED],
            undue=[("odd", ValueError("day 31 must be in range 1..30"))],
        )

    monkeypatch.setattr(mtgo, "run_trickle", run)
    result = _cli("mtgo", "trickle")
    assert result.exit_code == 1
    for line in (
        f"set aside 1 owed page whose name holds no real date: {UNDATED} (kept in mtgo-owed-set-aside.jsonl)",
        f"the 2026-09 index lists 1 name with no real date: {UNDATED}",
        "! odd: can't say when it's due: ValueError: day 31 must be in range 1..30 (unexpected; details in",
        "4 events from mtgo-misses.json moved to the owed list",
        "moved 1841 stored events into mtgo/<year>/<month>/",
        "forgot 2 months saved as listing no events: 2025-02, 2023-09; each is read again",
        "0 new events (1 on a retry) · 2 missed · 7 owed, 3 due, 2 too new to ask · 2 pages a run",
        "1 not published yet: young",
        "gone: missing, still owed; next try in an hour",
        "again: empty, still owed; asked again at the next run",
        "paused until 2026-09-21 15:00 UTC: mtgo.com answered 429",
        "! bad: its lists wouldn't parse: KeyError",
    ):
        assert line in result.output


@pytest.mark.parametrize(
    "res, line",
    [
        (mtgo.TrickleResult(busy=True), "another trickle run is in progress"),
        (
            mtgo.TrickleResult(paused_until=NOW, why="mtgo.com answered 403"),
            "paused until 2026-09-21 12:00 UTC: mtgo.com answered 403; nothing asked",
        ),
        (mtgo.TrickleResult(level=3), "as many requests as allowed"),
        (mtgo.TrickleResult(forgotten=["2025-02"]), "forgot 1 month saved as listing no events: 2025-02"),
        (
            mtgo.TrickleResult(level=3, budget=3, raised=True, stopped="no answer"),
            "every answer whole: up to 3",
        ),
        (mtgo.TrickleResult(level=1, budget=1), "0 new events · 0 missed · 0 owed, 0 due · 1 pages a run"),
    ],
)
def test_the_trickle_command_says_why_it_asked_little(monkeypatch, res, line):
    monkeypatch.setattr(mtgo, "run_trickle", lambda tracker: res)
    result = _cli("mtgo", "trickle")
    assert result.exit_code == 0 and line in result.output


def test_a_pause_is_shown_in_the_mac_s_time_on_a_terminal(monkeypatch, on_a_terminal):
    monkeypatch.setattr(mtgo, "run_trickle", lambda tracker: mtgo.TrickleResult(paused_until=NOW))
    assert (
        "paused until 2026-09-21 06:00 MDT: mtgo.com asked; nothing asked" in _cli("mtgo", "trickle").output
    )


def test_the_status_command():
    _owe(OLD, YOUNG)
    trickle.save_pace(mtgo.SOURCE, trickle.Pace(level=2))
    at = trickle.now()
    mtgo._save_months({"2026-09": _read(10, at=at), "2025-02": _failed(1, days=["2026-09-21"])})
    mtgo.ages_path().write_text(json.dumps({"league": 100000.0, "challenge": 9.62}))
    result = _cli("mtgo", "status")
    assert result.exit_code == 0
    for line in (
        "pace       2 pages a run, one more after each run whose every answer is whole",
        "paused     no",
        "owed       2 events: 2 never asked, 0 to retry",
        "too new    1, each kind asked from the age it first came back whole: challenge 9.6 h, league",
        "2026-09  1",
        "2026-08  1",
        "indexes    read back to 2025-02, still going back",
        "2025-02  not read yet: empty, asked once, believed empty on 1 day; asked again at the next run",
    ):
        assert line in result.output


def test_the_status_command_says_why_its_paused_and_at_the_top_pace():
    trickle.save_pace(
        mtgo.SOURCE,
        trickle.Pace(
            level=5, paused_until=trickle.stamp(trickle.now() + timedelta(hours=1)), why="x said so"
        ),
    )
    mtgo._save_months({"2026-09": _failed(2, days=THREE_DAYS[:2])})
    output = _cli("mtgo", "status").output
    assert "pace       5 pages a run, as many as the ceiling allows" in output
    assert ": x said so" in output and "believed empty on 2 days" in output and "asked 2 times" in output


def test_the_status_command_lists_the_retries_newest_first():
    at = trickle.now()
    owed = trickle.load_owed(mtgo.SOURCE)
    owed["never-asked"] = trickle.Owed(day=at.date().isoformat(), found="x")
    for n in range(12):  # each a day older than the last, each after a round
        slug = f"retry-{n:02}"
        owed[slug] = trickle.Owed(day=(at - timedelta(days=n)).date().isoformat(), found="x")
        owed[slug].tried(at - timedelta(minutes=30), "redirect", miss=True)
        owed[slug].asks, owed[slug].again, owed[slug].rounds = trickle.ROUND, 0, 1
    owed["retry-01"].last, owed["retry-01"].asks, owed["retry-01"].again = "not published yet", 2, 1
    trickle.save_owed(mtgo.SOURCE, owed)
    lines = _cli("mtgo", "status").output.splitlines()
    assert "owed       13 events: 1 never asked, 12 to retry" in lines
    first = lines.index(next(line for line in lines if line.startswith("retries")))
    soon = times.utc(owed["retry-00"].retry_at())
    assert lines[first : first + 3] == [
        f"retries    retry-00  redirect, asked 3 times, next try {soon}",
        "           retry-01  not published yet, asked 2 times, due now",
        f"           retry-02  redirect, asked 3 times, next try {times.utc(owed['retry-02'].retry_at())}",
    ]
    assert lines[first + 9].startswith("           retry-09") and lines[first + 10] == "           and 2 more"


def test_the_status_command_survives_an_owed_event_with_no_real_date():
    owed = trickle.load_owed(mtgo.SOURCE)
    owed[UNDATED] = trickle.Owed(day="2026-09-31", found="x")
    owed[UNDATED].tried(trickle.now() - timedelta(minutes=30), "redirect", miss=True)
    trickle.save_owed(mtgo.SOURCE, owed)
    mtgo._save_months({"2026-09": {**_read(10, at=trickle.now()), "undated": [UNDATED]}})
    result = _cli("mtgo", "status")
    assert result.exit_code == 0
    line = f"{UNDATED}  redirect, asked once, its date isn't a day; the next run sets it aside"
    assert line in result.output
    assert f"1 listed with no real date, not taken as events: {UNDATED}" in result.output


def test_the_forget_command():
    _owe(OLD)
    assert _cli("mtgo", "forget", OLD).output == f"forgot {OLD}\n"
    assert _cli("mtgo", "forget", OLD).exit_code == 1


def test_an_event_file_that_isn_t_json_counts_as_not_stored():
    path = mtgo.store_dir() / "modern-league-2026-09-2812345.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{cut off")
    assert not mtgo.is_stored("modern-league-2026-09-2812345")
