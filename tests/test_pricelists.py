"""Store price lists: each day's kept once, under the day the store's own stamp says,
as returned but gzipped."""

import gzip
import json
from datetime import UTC, date, datetime

import pytest

from riffle import net
from riffle.ingest import empties, pricelists

DAY = date(2026, 9, 27)
AT = datetime(2026, 9, 27, 20, 51, 50, tzinfo=UTC)  # 13:51 in Seattle
SINGLES, SEALED = pricelists.CARD_KINGDOM


def ck(rows: int = 2, made: str | None = "2026-09-27 13:08:38") -> bytes:
    data = [
        {
            "id": n,
            "sku": f"4ED-{n}",
            "scryfall_id": "a363bc91-6c1b-4bd2-b4c6-3a1a7e1d6e5e",
            "is_foil": "false",
            "price_retail": "0.39",
            "qty_retail": 12,
            "condition_values": {"nm_price": "0.39", "nm_qty": 2, "ex_price": "0.31", "ex_qty": 8},
        }
        for n in range(rows)
    ]
    meta = {"base_url": "https://www.cardkingdom.com/"}
    if made is not None:
        meta = {"created_at": made, **meta}
    return json.dumps({"meta": meta, "data": data}).encode()


def mp(as_of: str = "2026-09-27T20:23:47.529Z") -> bytes:
    rows = [{"scryfall_id": "a", "price_cents": 180, "price_cents_nm": 218}]
    return json.dumps({"meta": {"as_of": as_of}, "data": rows}).encode()


class Source:
    """The stores as a download: url -> body, None (404), or an exception to raise. Records every
    url asked for."""

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


def answers(**overrides) -> dict[str, bytes | None | Exception]:
    found: dict[str, bytes | None | Exception] = {SINGLES.url: ck(), SEALED.url: ck(1)}
    found.update(overrides)
    return found


def run(
    source: Source, tracker=None, at: datetime = AT, lists=pricelists.CARD_KINGDOM
) -> pricelists.Snapshot:
    if tracker is None:
        return pricelists.snapshot(lists, download=source.download, clock=lambda: at)
    return pricelists.snapshot(lists, download=source.download, tracker=tracker, clock=lambda: at)


def put(path, body: bytes) -> None:
    """A list kept by an earlier run, as it would have been."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(gzip.compress(body, mtime=1_790_000_000))


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    return tmp_path / "riffle"


def daily(data_dir, store="cardkingdom") -> dict[str, list[str]]:
    root = data_dir / store / "daily"
    return {d.name: sorted(p.name for p in d.iterdir()) for d in sorted(root.iterdir()) if d.is_dir()}


def test_each_list_is_kept_as_returned_under_its_own_day(data_dir, tracker):
    snap = run(Source(answers()), tracker)
    assert snap.kept == ["Card Kingdom singles", "Card Kingdom sealed"] and not snap.failed
    day = data_dir / "cardkingdom" / "daily" / "2026-09-27"
    assert gzip.decompress((day / "singles.json.gz").read_bytes()) == ck()
    assert gzip.decompress((day / "sealed.json.gz").read_bytes()) == ck(1)
    size = (day / "singles.json.gz").stat().st_size / 1e6
    # 13:08:38 Pacific is 20:08 UTC; the job log (not a terminal) shows UTC
    assert tracker.outcomes()["Card Kingdom singles"] == (
        "ok",
        f"kept 2026-09-27, made 2026-09-27 20:08 UTC, {size:,.1f} MB",
    )
    assert tracker.steps[0].unit == "bytes" and tracker.steps[0].updates == [(len(ck()), len(ck()))]
    assert daily(data_dir) == {"2026-09-27": ["sealed.json.gz", "singles.json.gz"]}


def test_a_kept_list_holds_when_it_was_fetched(data_dir):
    run(Source(answers()))
    got = pricelists.fetched(pricelists.target(SINGLES, DAY))
    assert got is not None and abs((got - datetime.now(UTC)).total_seconds()) < 60


def test_a_file_with_no_gzip_time_has_no_fetch_time(tmp_path):
    (tmp_path / "zero.gz").write_bytes(gzip.compress(b"{}", mtime=0))
    (tmp_path / "plain.json").write_bytes(b"{}")
    assert pricelists.fetched(tmp_path / "zero.gz") is None
    assert pricelists.fetched(tmp_path / "plain.json") is None


def test_a_list_kept_for_the_store_s_today_isnt_asked_for_again(data_dir, tracker):
    run(Source(answers()))
    source = Source(answers())
    # 03:00 UTC on the 28th is still the 27th in Seattle
    snap = run(source, tracker, at=datetime(2026, 9, 28, 3, 0, tzinfo=UTC))
    assert source.asked == [] and snap.skipped == ["Card Kingdom singles", "Card Kingdom sealed"]
    assert tracker.outcomes() == {
        "Card Kingdom singles": ("ok", "already have 2026-09-27"),
        "Card Kingdom sealed": ("ok", "already have 2026-09-27"),
    }


def test_yesterday_s_list_fetched_today_isnt_kept_as_today_s(data_dir, tracker):
    """C3: a list was kept under the Mac's day, so one fetched before the store made the day's
    list was filed as that day, and the day's real list was never asked for."""
    run(Source(answers()))
    source = Source(answers())
    snap = run(source, tracker, at=datetime(2026, 9, 28, 13, 0, tzinfo=UTC))  # 06:00 in Seattle
    assert source.asked == [SINGLES.url, SEALED.url] and snap.skipped == [
        "Card Kingdom singles",
        "Card Kingdom sealed",
    ]
    assert tracker.outcomes()["Card Kingdom singles"] == (
        "ok",
        "already have 2026-09-27, made 2026-09-27 20:08 UTC; the 2026-09-28 list isn't out yet",
    )
    assert daily(data_dir) == {"2026-09-27": ["sealed.json.gz", "singles.json.gz"]}
    later = answers(
        **{SINGLES.url: ck(made="2026-09-28 13:08:38"), SEALED.url: ck(1, made="2026-09-28 13:08:45")}
    )
    snap = run(Source(later), at=datetime(2026, 9, 28, 22, 0, tzinfo=UTC))
    assert snap.kept == ["Card Kingdom singles", "Card Kingdom sealed"]
    assert list(daily(data_dir)) == ["2026-09-27", "2026-09-28"]


def test_a_day_not_kept_is_kept_even_when_its_list_comes_the_next_day(data_dir):
    snap = run(Source(answers()), at=datetime(2026, 9, 28, 13, 0, tzinfo=UTC))
    assert snap.kept == ["Card Kingdom singles", "Card Kingdom sealed"]
    assert list(daily(data_dir)) == ["2026-09-27"]


def test_mana_pool_s_day_is_its_utc_date(data_dir, tracker):
    """as_of is UTC: a list made at 01:00 UTC is the next day's, though it's still the day
    before in Denver."""
    source = Source({plist.url: mp("2026-09-28T01:00:00.000Z") for plist in pricelists.MANA_POOL})
    snap = run(source, tracker, at=datetime(2026, 9, 28, 1, 30, tzinfo=UTC), lists=pricelists.MANA_POOL)
    assert snap.kept == ["Mana Pool singles", "Mana Pool variants", "Mana Pool sealed"]
    assert daily(data_dir, "manapool") == {
        "2026-09-28": ["sealed.json.gz", "singles.json.gz", "variants.json.gz"]
    }
    assert tracker.outcomes()["Mana Pool singles"][1].startswith(
        "kept 2026-09-28, made 2026-09-28 01:00 UTC, "
    )


def test_the_time_shown_is_local_on_a_terminal(data_dir, tracker, on_a_terminal):
    run(Source(answers()), tracker)
    assert tracker.outcomes()["Card Kingdom singles"][1].startswith(
        "kept 2026-09-27, made 2026-09-27 14:08 MDT, "
    )


def test_a_list_made_after_it_was_fetched_warns_that_the_zone_is_wrong(data_dir, tracker):
    """Read as Pacific time, 13:08 is 20:08 UTC: a list fetched at 13:10 UTC that says so was
    made on a clock that isn't Pacific. Its day is its date all the same, so it's kept."""
    snap = run(Source(answers()), tracker, at=datetime(2026, 9, 27, 13, 10, tzinfo=UTC))
    assert snap.kept == ["Card Kingdom singles", "Card Kingdom sealed"]
    kind, note = tracker.outcomes()["Card Kingdom singles"]
    assert kind == "warn" and note.startswith("kept 2026-09-27, made 2026-09-27 20:08 UTC, ")
    assert note.endswith("; it says it was made after it was fetched, so its clock isn't Pacific time")


def test_a_list_made_just_after_its_fetch_by_the_mac_s_clock_is_fine(data_dir, tracker):
    run(Source(answers()), tracker, at=datetime(2026, 9, 27, 20, 5, tzinfo=UTC))  # 3 minutes early
    assert tracker.outcomes()["Card Kingdom singles"][0] == "ok"


@pytest.mark.parametrize(
    "body", [b'{"meta": {"created_at": "2026-09-27 13:08:38"}, "data": []}', b'{"data": null}']
)
def test_an_empty_list_keeps_nothing_and_is_asked_again(data_dir, tracker, body):
    snap = run(Source(answers(**{SINGLES.url: body})), tracker)
    assert snap.empty == ["Card Kingdom singles"] and snap.kept == ["Card Kingdom sealed"]
    assert tracker.outcomes()["Card Kingdom singles"] == (
        "ok",
        "empty list, nothing kept; asked again next run",
    )
    assert daily(data_dir) == {"2026-09-27": ["sealed.json.gz"]}
    source = Source(answers(**{SINGLES.url: body}))
    run(source)
    assert source.asked == [SINGLES.url]  # asked again; sealed is kept


def test_a_list_empty_seven_runs_in_a_row_is_a_warning_and_still_asked(data_dir, tracker):
    empty = b'{"data": []}'
    for n in range(6):
        run(Source(answers(**{SINGLES.url: empty})), at=datetime(2026, 9, 21 + n, 20, 0, tzinfo=UTC))
    source = Source(answers(**{SINGLES.url: empty}))
    run(source, tracker, at=datetime(2026, 9, 27, 20, 0, tzinfo=UTC))
    assert source.asked == [SINGLES.url]
    assert tracker.outcomes()["Card Kingdom singles"] == (
        "warn",
        "empty list since 2026-09-21 (7 runs in a row); asked again every run",
    )
    run(Source(answers()))
    assert "cardkingdom/singles" not in json.loads(empties.path().read_text())  # rows again: forgotten


def test_a_list_with_no_stamp_is_set_aside_not_kept_as_a_day(data_dir, tracker):
    unstamped = ck(made=None)
    snap = run(Source(answers(**{SINGLES.url: unstamped})), tracker)
    why = (
        "pricelist: no created_at in it; kept aside as cardkingdom/aside/singles-2026-09-27T205150Z.json.gz,"
        " not as a day"
    )
    assert snap.failed == [("Card Kingdom singles", why)] and snap.kept == ["Card Kingdom sealed"]
    assert daily(data_dir) == {"2026-09-27": ["sealed.json.gz"]}  # the day's spot stays free
    aside = data_dir / "cardkingdom" / "aside"
    assert gzip.decompress((aside / "singles-2026-09-27T205150Z.json.gz").read_bytes()) == unstamped
    run(Source(answers(**{SINGLES.url: ck(made="whenever")})))  # a stamp that isn't a time: the same
    assert sorted(p.name for p in aside.iterdir()) == [
        "singles-2026-09-27T205150Z-2.json.gz",
        "singles-2026-09-27T205150Z.json.gz",
    ]
    snap = run(Source(answers()))
    assert snap.kept == ["Card Kingdom singles"]  # the day's list still gets its spot


def test_a_list_kept_under_the_wrong_day_moves_to_its_own(data_dir, tracker):
    """Before, a list was filed under the Mac's day: the 27th's list fetched on the morning of
    the 28th went under the 28th."""
    put(pricelists.target(SINGLES, date(2026, 9, 28)), ck())
    later = answers(
        **{SINGLES.url: ck(made="2026-09-28 13:08:38"), SEALED.url: ck(1, made="2026-09-28 13:08:45")}
    )
    snap = run(Source(later), tracker, at=datetime(2026, 9, 28, 22, 0, tzinfo=UTC))
    assert snap.kept == ["Card Kingdom singles", "Card Kingdom sealed"]
    moved = [s.outcome for s in tracker.steps if s.label == "Card Kingdom singles"]
    assert moved[0] == ("ok", "moved the list kept under 2026-09-28 to 2026-09-27, the day it was made")
    assert daily(data_dir) == {
        "2026-09-27": ["singles.json.gz"],
        "2026-09-28": ["sealed.json.gz", "singles.json.gz"],
    }
    assert gzip.decompress(pricelists.target(SINGLES, DAY).read_bytes()) == ck()


def test_a_misfiled_copy_of_a_day_already_kept_is_set_aside(data_dir, tracker):
    put(pricelists.target(SINGLES, DAY), ck())
    put(pricelists.target(SINGLES, date(2026, 9, 28)), ck())
    run(Source(answers()), tracker, at=datetime(2026, 9, 28, 22, 0, tzinfo=UTC))
    note = tracker.steps[0].outcome
    assert note == (
        "ok",
        "set the list kept under 2026-09-28 aside as cardkingdom/aside/singles-2026-09-21T141320Z.json.gz:"
        " 2026-09-27 has its list",
    )
    assert (data_dir / "cardkingdom" / "aside" / "singles-2026-09-21T141320Z.json.gz").exists()


def test_misfiled_lists_in_a_row_each_reach_their_own_day(data_dir):
    """Three mornings of the old filing: the 28th holds the 27th's list, the 29th the 28th's."""
    put(pricelists.target(SINGLES, DAY), ck())
    put(pricelists.target(SINGLES, date(2026, 9, 28)), ck())
    put(pricelists.target(SINGLES, date(2026, 9, 29)), ck(made="2026-09-28 13:08:38"))
    source = Source(answers(**{SINGLES.url: ck(made="2026-09-29 13:08:38")}))
    run(source, at=datetime(2026, 9, 29, 22, 0, tzinfo=UTC), lists=(SINGLES,))
    for day, stamp in [(27, "2026-09-27"), (28, "2026-09-28"), (29, "2026-09-29")]:
        kept = pricelists.kept_made(pricelists.target(SINGLES, date(2026, 9, day)), SINGLES)
        assert kept is not None and kept.date().isoformat() == stamp
    assert len(list((data_dir / "cardkingdom" / "aside").iterdir())) == 1


def test_two_lists_filed_under_each_other_s_days_both_find_a_place(data_dir):
    put(pricelists.target(SINGLES, DAY), ck(made="2026-09-28 13:08:38"))
    put(pricelists.target(SINGLES, date(2026, 9, 28)), ck())
    snap = run(Source(answers()), at=datetime(2026, 9, 28, 22, 0, tzinfo=UTC), lists=(SINGLES,))
    assert snap.skipped == ["Card Kingdom singles"]  # the 27th's list, now under the 27th
    assert pricelists.filed_right(pricelists.target(SINGLES, DAY), SINGLES)
    assert len(list((data_dir / "cardkingdom" / "aside").iterdir())) == 1  # the 28th's, set aside


def test_an_unreadable_kept_list_is_set_aside(data_dir, tracker):
    path = pricelists.target(SINGLES, DAY)
    path.parent.mkdir(parents=True)
    path.write_bytes(b"not gzip")
    snap = run(Source(answers()), tracker, lists=(SINGLES,))
    assert snap.kept == ["Card Kingdom singles"]
    assert tracker.steps[0].outcome[1].endswith(": it has no readable created_at")
    assert gzip.decompress(path.read_bytes()) == ck()


def test_a_misfiled_list_in_the_way_of_a_fetched_one_moves_first(data_dir, tracker):
    put(pricelists.target(SINGLES, DAY), ck(made="2026-09-26 13:08:38"))
    snap = run(Source(answers()), tracker, at=datetime(2026, 9, 28, 13, 0, tzinfo=UTC), lists=(SINGLES,))
    assert snap.kept == ["Card Kingdom singles"]
    assert tracker.steps[1].outcome == (  # its own line, while the fetched list's step runs
        "ok",
        "moved the list kept under 2026-09-27 to 2026-09-26, the day it was made",
    )
    assert daily(data_dir) == {"2026-09-26": ["singles.json.gz"], "2026-09-27": ["singles.json.gz"]}


def test_refiling_a_list_that_is_its_own_day_s_changes_nothing(data_dir):
    put(pricelists.target(SINGLES, DAY), ck())
    assert (
        pricelists.refile(pricelists.target(SINGLES, DAY), SINGLES) == "the list under 2026-09-27 is its own"
    )


def test_a_list_wrapped_in_html_is_kept_wrapped(data_dir):
    wrapped = b"<html><head></head><body>" + ck() + b"</body></html>"
    snap = run(Source(answers(**{SINGLES.url: wrapped})))
    assert "Card Kingdom singles" in snap.kept
    path = pricelists.target(SINGLES, DAY)
    assert gzip.decompress(path.read_bytes()) == wrapped  # as returned; the loader unwraps it
    assert pricelists.filed_right(path, SINGLES)


@pytest.mark.parametrize(
    "body",
    [
        b"<html><body>Service Unavailable</body></html>",
        b'{"error": "rate limited"}',
        ck()[:-40],  # cut off
        b"[]",
        b"",
    ],
)
def test_a_body_that_isnt_a_whole_list_keeps_nothing(data_dir, tracker, body):
    snap = run(Source(answers(**{SINGLES.url: body})), tracker)
    assert snap.failed == [("Card Kingdom singles", "pricelist: not the expected JSON")]
    assert snap.kept == ["Card Kingdom sealed"]  # the other list carries on
    assert sorted(p.name for p in pricelists.day_dir("cardkingdom", DAY).iterdir()) == ["sealed.json.gz"]
    assert sorted(p.name for p in (data_dir / "cardkingdom" / "daily").iterdir()) == ["2026-09-27"]


def test_a_wrapped_list_cut_off_keeps_nothing(data_dir):
    wrapped = b"<html><head></head><body>" + ck()[:-40]
    snap = run(Source(answers(**{SINGLES.url: wrapped, SEALED.url: None})))
    assert [label for label, _ in snap.failed] == ["Card Kingdom singles", "Card Kingdom sealed"]
    assert list((data_dir / "cardkingdom" / "daily").iterdir()) == []  # no debris, no empty day


def test_a_list_ending_in_a_newline_is_kept(data_dir):
    snap = run(Source(answers(**{SINGLES.url: ck() + b"\n"})))
    assert "Card Kingdom singles" in snap.kept


def test_a_full_disk_while_gzipping_fails_the_step_and_leaves_nothing(data_dir, tracker, monkeypatch):
    def full(src, dst, *args):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(pricelists.shutil, "copyfileobj", full)
    snap = run(Source(answers()), tracker)
    assert tracker.outcomes()["Card Kingdom singles"] == ("fail", "[Errno 28] No space left on device")
    assert len(snap.failed) == 2 and not snap.kept
    assert list((data_dir / "cardkingdom" / "daily").iterdir()) == []


def test_failures_are_retried_the_next_run(data_dir, tracker):
    run(Source(answers(**{SINGLES.url: net.FetchError("HTTP 503"), SEALED.url: None})), tracker)
    assert tracker.outcomes() == {
        "Card Kingdom singles": ("fail", "HTTP 503"),
        "Card Kingdom sealed": ("fail", "sealed_pricelist: HTTP 404"),
    }
    snap = run(Source(answers()))
    assert snap.kept == ["Card Kingdom singles", "Card Kingdom sealed"]


def test_no_answer_skips_the_store_s_other_lists(data_dir):
    source = Source(answers(**{SINGLES.url: net.NoAnswer("no answer after 3 tries (timed out)")}))
    snap = run(source)
    assert source.asked == [SINGLES.url]
    assert snap.failed == [
        ("Card Kingdom singles", "no answer after 3 tries (timed out)"),
        ("Card Kingdom sealed", "not asked: no answer to the list before"),
    ]


def test_mana_pool_is_three_lists_in_its_own_folder(data_dir, tracker):
    source = Source({plist.url: mp() for plist in pricelists.MANA_POOL})
    snap = run(source, tracker, lists=pricelists.MANA_POOL)
    assert snap.kept == ["Mana Pool singles", "Mana Pool variants", "Mana Pool sealed"]
    day = data_dir / "manapool" / "daily" / "2026-09-27"
    assert sorted(p.name for p in day.iterdir()) == ["sealed.json.gz", "singles.json.gz", "variants.json.gz"]
    assert gzip.decompress((day / "variants.json.gz").read_bytes()) == mp()


def test_every_store_list_has_its_own_file():
    lists = [*pricelists.CARD_KINGDOM, *pricelists.MANA_POOL]
    assert len({(p.store, p.name) for p in lists}) == len({p.label for p in lists}) == len(lists) == 5


def test_a_big_list_is_checked_at_its_ends(data_dir):
    rows = ck(20_000)
    assert len(rows) > 8192
    snap = run(Source(answers(**{SINGLES.url: rows})))
    assert "Card Kingdom singles" in snap.kept
