"""Store price lists: one copy a day of each, kept as returned but gzipped."""

import gzip
import json
from datetime import date

import pytest

from riffle import net
from riffle.ingest import pricelists

DAY = date(2026, 9, 27)
SINGLES, SEALED = pricelists.CARD_KINGDOM


def ck(rows: int = 2) -> bytes:
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
    meta = {"created_at": "2026-09-27 05:05:57", "base_url": "https://www.cardkingdom.com/"}
    return json.dumps({"meta": meta, "data": data}).encode()


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


def run(source: Source, tracker=None, today: date = DAY) -> pricelists.Snapshot:
    if tracker is None:
        return pricelists.snapshot(pricelists.CARD_KINGDOM, download=source.download, today=today)
    return pricelists.snapshot(
        pricelists.CARD_KINGDOM, download=source.download, tracker=tracker, today=today
    )


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    return tmp_path / "riffle"


def test_each_list_is_kept_as_returned_for_the_day(data_dir, tracker):
    snap = run(Source(answers()), tracker)
    assert snap.kept == ["Card Kingdom singles", "Card Kingdom sealed"] and not snap.failed
    day = data_dir / "cardkingdom" / "daily" / "2026-09-27"
    assert gzip.decompress((day / "singles.json.gz").read_bytes()) == ck()
    assert gzip.decompress((day / "sealed.json.gz").read_bytes()) == ck(1)
    note = tracker.outcomes()["Card Kingdom singles"]
    assert note == ("ok", f"kept 2026-09-27, {(day / 'singles.json.gz').stat().st_size / 1e6:,.1f} MB")
    assert tracker.steps[0].unit == "bytes" and tracker.steps[0].updates == [(len(ck()), len(ck()))]
    assert sorted(p.name for p in day.iterdir()) == ["sealed.json.gz", "singles.json.gz"]


def test_a_list_kept_today_isnt_asked_for_again(data_dir, tracker):
    run(Source(answers()))
    source = Source(answers())
    snap = run(source, tracker)
    assert source.asked == [] and snap.skipped == ["Card Kingdom singles", "Card Kingdom sealed"]
    assert tracker.outcomes() == {
        "Card Kingdom singles": ("ok", "already have 2026-09-27"),
        "Card Kingdom sealed": ("ok", "already have 2026-09-27"),
    }


def test_the_next_day_gets_its_own_copy(data_dir):
    run(Source(answers()))
    snap = run(Source(answers()), today=date(2026, 9, 28))
    assert len(snap.kept) == 2
    assert (data_dir / "cardkingdom" / "daily" / "2026-09-28" / "singles.json.gz").exists()


def test_a_list_wrapped_in_html_is_kept_wrapped(data_dir):
    wrapped = b"<html><head></head><body>" + ck() + b"</body></html>"
    snap = run(Source(answers(**{SINGLES.url: wrapped})))
    assert "Card Kingdom singles" in snap.kept
    path = pricelists.target(SINGLES, DAY)
    assert gzip.decompress(path.read_bytes()) == wrapped  # as returned; the loader unwraps it


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
    body = json.dumps({"data": [{"scryfall_id": "a", "price_cents": 180, "price_cents_nm": 218}]}).encode()
    source = Source({plist.url: body for plist in pricelists.MANA_POOL})
    snap = pricelists.snapshot(pricelists.MANA_POOL, download=source.download, tracker=tracker, today=DAY)
    assert snap.kept == ["Mana Pool singles", "Mana Pool variants", "Mana Pool sealed"]
    day = data_dir / "manapool" / "daily" / "2026-09-27"
    assert sorted(p.name for p in day.iterdir()) == ["sealed.json.gz", "singles.json.gz", "variants.json.gz"]
    assert gzip.decompress((day / "variants.json.gz").read_bytes()) == body


def test_every_store_list_has_its_own_file():
    lists = [*pricelists.CARD_KINGDOM, *pricelists.MANA_POOL]
    assert len({(p.store, p.name) for p in lists}) == len({p.label for p in lists}) == len(lists) == 5


def test_a_big_list_is_checked_at_its_ends(data_dir):
    rows = ck(20_000)
    assert len(rows) > 8192
    snap = run(Source(answers(**{SINGLES.url: rows})))
    assert "Card Kingdom singles" in snap.kept
