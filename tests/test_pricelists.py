"""Store price lists: every list Card Kingdom and Mana Pool publish, each asked for once a run
and kept whole or as a difference (riffle.runs), with every check logged."""

import gzip
import json
import re
from datetime import UTC, datetime, timedelta

import pytest
from typer.testing import CliRunner

from riffle import locks, net, runs
from riffle.cli import _complete_store, app
from riffle.ingest import empties, pricelists

AT = datetime(2026, 9, 27, 20, 51, 50, tzinfo=UTC)  # 13:51 in Seattle
SINGLES, SEALED = pricelists.CARD_KINGDOM


def ck(rows: int = 40, made: str | None = "2026-09-27 13:08:38", price: str = "0.39") -> bytes:
    data = [
        {
            "id": n,
            "sku": f"4ED-{n}",
            "scryfall_id": "a363bc91-6c1b-4bd2-b4c6-3a1a7e1d6e5e",
            "is_foil": "false",
            "price_retail": price if n % 7 == 0 else "0.39",
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
    rows = [{"scryfall_id": f"s{n}", "price_cents": 180 + n, "price_cents_nm": 218} for n in range(40)]
    return json.dumps({"meta": {"as_of": as_of}, "data": rows}).encode()


class Store:
    """The stores as net.fetch_new: url -> body, None (404), or an exception to raise, and an
    ETag per url for a store that sends them. Records every (url, ETag sent)."""

    def __init__(self, answers: dict[str, bytes | None | Exception], tags: dict[str, str] | None = None):
        self.answers = answers
        self.tags = tags or {}
        self.asked: list[tuple[str, str | None]] = []

    def fetch(self, url, dest, known, etag=None, accept="*/*", progress=None):
        self.asked.append((url, etag))
        body = self.answers.get(url)
        if isinstance(body, Exception):
            raise body
        if body is None:
            return None
        tag = self.tags.get(url)
        if etag is not None and etag == tag:
            return net.Fetched("unchanged", b"", etag)
        head = body[: net.HEAD]
        if known(head):
            return net.Fetched("known", head, tag)
        dest.write_bytes(body)
        if progress:
            progress(len(body), None)
        return net.Fetched("new", head, tag, len(body))


def answers(**overrides) -> dict[str, bytes | None | Exception]:
    found: dict[str, bytes | None | Exception] = {SINGLES.url: ck(), SEALED.url: ck(5)}
    found.update(overrides)
    return found


def run(store: Store, tracker=None, at: datetime = AT, lists=pricelists.CARD_KINGDOM) -> pricelists.Watch:
    if tracker is None:
        return pricelists.watch(lists, fetch=store.fetch, clock=lambda: at)
    return pricelists.watch(lists, fetch=store.fetch, tracker=tracker, clock=lambda: at)


def kept(data_dir, store: str = "cardkingdom", name: str = "singles") -> dict:
    return runs.kept(data_dir / store / "lists" / name)


def logged(data_dir, store: str = "cardkingdom") -> list[dict]:
    return [json.loads(line) for line in (data_dir / store / "watch.jsonl").read_text().splitlines()]


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    return tmp_path / "riffle"


def test_the_first_list_is_kept_whole_as_its_run_s_base(data_dir, tracker):
    res = run(Store(answers()), tracker)
    assert res.kept == ["Card Kingdom singles", "Card Kingdom sealed"] and not res.failed
    (path,) = kept(data_dir).values()
    assert path.relative_to(data_dir).as_posix() == (
        "cardkingdom/lists/singles/2026-09-27T200838Z/2026-09-27T200838Z.json.zst"
    )
    assert runs.rebuild(path) == ck() and runs.rebuild(runs.copies(path.parent)[1]) == ck()
    size, whole = pricelists._size(path.stat().st_size), pricelists._size(len(ck()))
    # 13:08:38 Pacific is 20:08 UTC; the job log (not a terminal) shows UTC
    assert tracker.outcomes()["Card Kingdom singles"] == (
        "ok",
        f"kept the list made 2026-09-27 20:08 UTC, {whole}: a new run, {size} kept twice",
    )
    assert tracker.steps[0].unit == "bytes" and tracker.steps[0].updates == [(len(ck()), None)]


def test_a_list_made_later_is_kept_as_a_difference(data_dir, tracker):
    run(Store(answers()))
    later = ck(made="2026-09-27 16:08:38", price="0.41")
    res = run(Store(answers(**{SINGLES.url: later})), tracker, at=AT + timedelta(hours=3))
    assert res.kept == ["Card Kingdom singles"] and res.same == ["Card Kingdom sealed"]
    path = kept(data_dir)["2026-09-27T230838Z"]
    assert path.name.endswith(".diff.zst") and runs.rebuild(path) == later
    assert tracker.outcomes()["Card Kingdom singles"] == (
        "ok",
        f"kept the list made 2026-09-27 23:08 UTC, {pricelists._size(len(later))}: "
        f"a difference of {pricelists._size(path.stat().st_size)}",
    )


def test_a_list_already_kept_is_hung_up_on(data_dir, tracker):
    run(Store(answers()))
    res = run(Store(answers()), tracker, at=AT + timedelta(minutes=5))
    assert res.same == ["Card Kingdom singles", "Card Kingdom sealed"] and not res.kept
    assert tracker.outcomes()["Card Kingdom singles"] == ("ok", "have the list made 2026-09-27 20:08 UTC")
    assert len(kept(data_dir)) == 1
    assert logged(data_dir)[-2:] == [
        {"at": "2026-09-27T205650Z", "list": "singles", "result": "known", "made": "2026-09-27T200838Z"},
        {"at": "2026-09-27T205650Z", "list": "sealed", "result": "known", "made": "2026-09-27T200838Z"},
    ]


def test_every_list_kept_is_logged_with_its_hashes(data_dir):
    run(Store(answers()))
    entry = logged(data_dir)[0]
    path = kept(data_dir)["2026-09-27T200838Z"]
    assert entry == {
        "at": "2026-09-27T205150Z",
        "list": "singles",
        "result": "kept",
        "made": "2026-09-27T200838Z",
        "kind": "base",
        "file": path.relative_to(data_dir).as_posix(),
        "size": len(ck()),
        "stored": 2 * path.stat().st_size,
        "sha256": runs.hashlib.sha256(ck()).hexdigest(),
        "file_sha256": runs.file_sha256(path),
    }


def test_mana_pool_s_etag_is_sent_and_a_304_asks_nothing_more(data_dir, tracker):
    urls = {plist.url: mp() for plist in pricelists.MANA_POOL}
    tags = {url: f'"{n}"' for n, url in enumerate(urls)}
    first = Store(urls, tags)
    assert run(first, lists=pricelists.MANA_POOL).kept == [
        "Mana Pool singles",
        "Mana Pool variants",
        "Mana Pool sealed",
    ]
    assert first.asked == [(url, None) for url in urls]
    again = Store(urls, tags)
    res = run(again, tracker, lists=pricelists.MANA_POOL)
    assert again.asked == list(tags.items()) and len(res.same) == 3
    assert tracker.outcomes()["Mana Pool singles"] == ("ok", "no new list since the last one kept")
    assert logged(data_dir, "manapool")[-1] == {
        "at": "2026-09-27T205150Z",
        "list": "sealed",
        "result": "unchanged",
    }
    assert sorted(p.name for p in (data_dir / "manapool" / "lists").iterdir()) == [
        "sealed",
        "singles",
        "variants",
    ]


def test_an_etag_is_kept_only_once_its_list_is(data_dir):
    url = pricelists.MANA_POOL[0].url
    unstamped = json.dumps({"meta": {}, "data": [{"a": 1}]}).encode()
    run(Store({url: unstamped}, {url: '"1"'}), lists=pricelists.MANA_POOL[:1])
    again = Store({url: mp()}, {url: '"1"'})
    assert run(again, lists=pricelists.MANA_POOL[:1]).kept == ["Mana Pool singles"]
    assert again.asked == [(url, None)]  # the unstamped list's ETag wasn't kept, so it's asked whole


def test_an_unreadable_etag_file_is_no_etags(data_dir):
    (data_dir / "manapool").mkdir(parents=True)
    (data_dir / "manapool" / "watch-etags.json").write_text("{not json")
    url = pricelists.MANA_POOL[0].url
    assert run(Store({url: mp()}, {url: '"1"'}), lists=pricelists.MANA_POOL[:1]).kept == ["Mana Pool singles"]


@pytest.mark.parametrize(
    "body", [b'{"meta": {"created_at": "2026-09-27 13:08:38"}, "data": []}', b'{"data": null}']
)
def test_an_empty_list_keeps_nothing_and_is_asked_again(data_dir, tracker, body):
    res = run(Store(answers(**{SINGLES.url: body})), tracker)
    assert res.empty == ["Card Kingdom singles"] and res.kept == ["Card Kingdom sealed"]
    assert tracker.outcomes()["Card Kingdom singles"] == (
        "ok",
        "empty list, nothing kept; asked again next run",
    )
    assert kept(data_dir) == {}
    store = Store(answers(**{SINGLES.url: body}))
    run(store)
    assert [url for url, _ in store.asked] == [SINGLES.url, SEALED.url]
    assert logged(data_dir)[0]["result"] == "empty"


def test_a_list_empty_seven_runs_in_a_row_is_a_warning_and_still_asked(data_dir, tracker):
    empty = b'{"data": []}'
    for n in range(6):
        run(Store(answers(**{SINGLES.url: empty})), at=datetime(2026, 9, 21 + n, 20, 0, tzinfo=UTC))
    run(Store(answers(**{SINGLES.url: empty})), tracker, at=datetime(2026, 9, 27, 20, 0, tzinfo=UTC))
    assert tracker.outcomes()["Card Kingdom singles"] == (
        "warn",
        "empty list since 2026-09-21 (7 runs in a row); asked again every run",
    )
    run(Store(answers()))
    assert "cardkingdom/singles" not in json.loads(empties.path().read_text())  # rows again: forgotten


def test_a_list_with_no_stamp_is_set_aside_and_fails(data_dir, tracker):
    unstamped = ck(made=None)
    res = run(Store(answers(**{SINGLES.url: unstamped})), tracker)
    why = "pricelist: no created_at in it; kept aside as cardkingdom/aside/singles-2026-09-27T205150Z.json.gz"
    assert res.failed == [("Card Kingdom singles", why)] and res.kept == ["Card Kingdom sealed"]
    aside = data_dir / "cardkingdom" / "aside"
    assert gzip.decompress((aside / "singles-2026-09-27T205150Z.json.gz").read_bytes()) == unstamped
    run(Store(answers(**{SINGLES.url: ck(made="whenever")})))  # a stamp that isn't a time: the same
    assert sorted(p.name for p in aside.iterdir()) == [
        "singles-2026-09-27T205150Z-2.json.gz",
        "singles-2026-09-27T205150Z.json.gz",
    ]
    assert run(Store(answers())).kept == ["Card Kingdom singles"]
    assert logged(data_dir)[0] == {
        "at": "2026-09-27T205150Z",
        "list": "singles",
        "result": "failed",
        "why": why,
    }


def test_a_list_made_after_it_was_fetched_warns_that_the_zone_is_wrong(data_dir, tracker):
    run(Store(answers(**{SINGLES.url: ck(made="2026-09-27 21:00:00")})), tracker)
    outcome, said = tracker.outcomes()["Card Kingdom singles"]
    assert outcome == "warn" and said.endswith(
        "it says it was made after it was fetched, so its clock isn't Pacific time"
    )


def test_damage_found_in_a_base_is_a_warning_and_mended(data_dir, tracker):
    run(Store(answers()))
    first, copy = runs.copies(kept(data_dir)["2026-09-27T200838Z"].parent)
    copy.write_bytes(b"damaged")
    later = ck(made="2026-09-27 16:08:38", price="0.41")
    run(Store(answers(**{SINGLES.url: later})), tracker, at=AT + timedelta(hours=3))
    outcome, said = tracker.outcomes()["Card Kingdom singles"]
    assert outcome == "warn" and "2026-09-27T200838Z.copy.json.zst was damaged: set aside as" in said
    assert copy.read_bytes() == first.read_bytes()


def test_a_list_that_doesn_t_read_back_is_set_aside_and_fails(data_dir, tracker, monkeypatch):
    real = runs._write
    monkeypatch.setattr(runs, "_write", lambda path, data: real(path, data[:-4]))
    res = run(Store(answers()), tracker)
    (label, why), _ = res.failed
    assert label == "Card Kingdom singles" and why.endswith(
        "; the list is kept aside as cardkingdom/aside/singles-2026-09-27T205150Z.json.gz"
    )
    assert (
        gzip.decompress(
            (data_dir / "cardkingdom" / "aside" / "singles-2026-09-27T205150Z.json.gz").read_bytes()
        )
        == ck()
    )


def test_a_run_finding_the_store_s_lock_held_asks_nothing(data_dir, tracker):
    store = Store(answers())
    with locks.held(data_dir / "cardkingdom" / "watch.lock") as mine:
        assert mine
        res = run(store, tracker)
    assert res.busy and store.asked == [] and not (data_dir / "cardkingdom" / "watch.jsonl").exists()
    assert tracker.outcomes() == {"Card Kingdom lists": ("ok", "another run is asking for them")}


def test_a_list_wrapped_in_html_is_kept_wrapped(data_dir):
    wrapped = b"<html><head></head><body>" + ck() + b"</body></html>"
    assert "Card Kingdom singles" in run(Store(answers(**{SINGLES.url: wrapped}))).kept
    (path,) = kept(data_dir).values()
    assert runs.rebuild(path) == wrapped  # as returned; the loader unwraps it


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
    res = run(Store(answers(**{SINGLES.url: body})), tracker)
    assert res.failed == [("Card Kingdom singles", "pricelist: not the expected JSON")]
    assert res.kept == ["Card Kingdom sealed"] and kept(data_dir) == {}
    assert sorted(p.name for p in (data_dir / "cardkingdom" / "lists").iterdir()) == ["sealed"]


def test_a_list_ending_in_a_newline_is_kept(data_dir):
    assert "Card Kingdom singles" in run(Store(answers(**{SINGLES.url: ck() + b"\n"}))).kept


def test_a_big_list_is_checked_at_its_ends(data_dir):
    rows = ck(20_000)
    assert len(rows) > 8192
    assert "Card Kingdom singles" in run(Store(answers(**{SINGLES.url: rows}))).kept


def test_failures_are_asked_again_the_next_run(data_dir, tracker):
    run(Store(answers(**{SINGLES.url: net.FetchError("HTTP 503"), SEALED.url: None})), tracker)
    assert tracker.outcomes() == {
        "Card Kingdom singles": ("fail", "HTTP 503"),
        "Card Kingdom sealed": ("fail", "sealed_pricelist: HTTP 404"),
    }
    assert run(Store(answers())).kept == ["Card Kingdom singles", "Card Kingdom sealed"]


def test_no_answer_skips_the_store_s_other_lists(data_dir):
    store = Store(answers(**{SINGLES.url: net.NoAnswer("no answer after 3 tries (timed out)")}))
    res = run(store)
    assert store.asked == [(SINGLES.url, None)]
    assert res.failed == [
        ("Card Kingdom singles", "no answer after 3 tries (timed out)"),
        ("Card Kingdom sealed", "not asked: no answer to the list before"),
    ]
    assert [e["result"] for e in logged(data_dir)] == ["failed", "failed"]


def test_sizes_under_a_megabyte_are_in_kilobytes():
    assert pricelists._size(49_983) == "50 KB" and pricelists._size(51_698_006) == "51.7 MB"
    assert pricelists._size(312) == "312 bytes"


def test_every_store_list_has_its_own_folder():
    lists = [*pricelists.CARD_KINGDOM, *pricelists.MANA_POOL]
    assert len({(p.store, p.name) for p in lists}) == len({p.label for p in lists}) == len(lists) == 5
    assert pricelists.STORES == {"cardkingdom": pricelists.CARD_KINGDOM, "manapool": pricelists.MANA_POOL}


# ---- the lists kept a day, before 2026-09-29 ---------------------------------------------------


def test_a_list_kept_a_day_holds_when_it_was_made_and_fetched(tmp_path):
    path = tmp_path / "2026-09-27" / "singles.json.gz"
    path.parent.mkdir()
    path.write_bytes(gzip.compress(ck(), mtime=1_790_000_000))
    assert pricelists.kept_made(path, SINGLES) == datetime(2026, 9, 27, 20, 8, 38, tzinfo=UTC)
    assert pricelists.fetched(path) == datetime.fromtimestamp(1_790_000_000, UTC)
    (tmp_path / "broken.json.gz").write_bytes(b"not gzip")
    assert pricelists.kept_made(tmp_path / "broken.json.gz", SINGLES) is None


def test_a_file_with_no_gzip_time_has_no_fetch_time(tmp_path):
    (tmp_path / "zero.gz").write_bytes(gzip.compress(b"{}", mtime=0))
    (tmp_path / "plain.json").write_bytes(b"{}")
    assert pricelists.fetched(tmp_path / "zero.gz") is None
    assert pricelists.fetched(tmp_path / "plain.json") is None


# ---- riffle prices watch <store> ----------------------------------------------------------------------


@pytest.fixture
def cli(data_dir, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    return lambda *args: CliRunner().invoke(app, list(args))


def test_riffle_watch_keeps_a_store_s_lists(cli, monkeypatch):
    calls = []
    monkeypatch.setattr(pricelists, "watch", lambda lists, tracker: calls.append(lists) or pricelists.Watch())
    assert cli("prices", "watch", "manapool").exit_code == 0 and calls == [pricelists.MANA_POOL]


def test_riffle_watch_exits_1_when_a_list_fails(cli, monkeypatch):
    def failing(lists, tracker):
        tracker.step("Mana Pool singles").fail("HTTP 503")
        return pricelists.Watch()

    monkeypatch.setattr(pricelists, "watch", failing)
    result = cli("prices", "watch", "manapool")
    assert result.exit_code == 1 and "1 step failed: Mana Pool singles" in result.output


def test_riffle_watch_names_the_stores_when_given_another(cli):
    result = cli("prices", "watch", "tcgplayer")
    # CI's terminal gets colour codes, and the error box wraps at 80 columns: read the words alone
    said = " ".join(re.sub(r"\x1b\[[0-9;]*m", "", result.output).replace("│", " ").split())
    assert (
        result.exit_code == 2
        and "'tcgplayer' has no lists to watch; the stores: cardkingdom, manapool" in said
    )


def test_the_stores_tab_complete():
    assert _complete_store("man") == ["manapool"]


def test_a_check_is_logged_to_the_second(data_dir):
    run(Store(answers()), at=AT.replace(microsecond=926577))
    assert {e["at"] for e in logged(data_dir)} == {"2026-09-27T205150Z"}


def test_a_list_already_kept_under_a_new_etag_keeps_the_new_etag(data_dir):
    url = pricelists.MANA_POOL[0].url
    run(Store({url: mp()}, {url: '"1"'}), lists=pricelists.MANA_POOL[:1])
    moved = Store({url: mp()}, {url: '"2"'})  # the same list, served again under a new ETag
    assert run(moved, lists=pricelists.MANA_POOL[:1]).same == ["Mana Pool singles"]
    again = Store({url: mp()}, {url: '"2"'})
    run(again, lists=pricelists.MANA_POOL[:1])
    assert moved.asked == [(url, '"1"')] and again.asked == [(url, '"2"')]
