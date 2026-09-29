"""Lists kept in runs: a base twice, later lists as differences against it, and how long a run
lasts decided by the lists' own sizes."""

import hashlib
import json
import random
from datetime import UTC, datetime, timedelta

import pytest

from riffle import runs

AT = datetime(2026, 9, 29, 6, 36, 3, 215000, tzinfo=UTC)


def listing(seed: int = 0, rows: int = 400, changed: int = 0) -> bytes:
    """A price list like a store's: rows that mostly stay, `changed` of them repriced."""
    rng = random.Random(1)
    data = [
        {"id": f"{rng.getrandbits(64):016x}", "price": rng.randint(10, 99999), "qty": rng.randint(0, 40)}
        for _ in range(rows)
    ]
    moved = random.Random(seed)
    for row in moved.sample(data, changed):
        row["price"] = moved.randint(10, 99999)
    return json.dumps({"meta": {"as_of": "x"}, "data": data}).encode()


def noise(size: int, seed: int) -> bytes:
    return random.Random(seed).randbytes(size)


def later(minutes: int) -> datetime:
    return AT + timedelta(minutes=minutes)


def test_a_stamp_is_the_utc_time_a_list_was_made():
    assert runs.name(AT) == "2026-09-29T063603.215Z"
    assert runs.name(AT.replace(microsecond=0)) == "2026-09-29T063603Z"
    assert runs.parse("2026-09-29T063603.215Z") == AT
    assert runs.parse("2026-09-29T063603Z") == AT.replace(microsecond=0)
    assert runs.parse("singles.new") is None


def test_the_first_list_is_a_run_s_base_kept_twice(tmp_path):
    data = listing()
    kept = runs.keep(tmp_path, AT, data)
    run = tmp_path / "2026-09-29T063603.215Z"
    first, copy = runs.copies(run)
    assert kept.kind == "base" and kept.path == first and kept.stamp == run.name
    assert first.read_bytes() == copy.read_bytes()
    assert kept.stored == 2 * first.stat().st_size and kept.size == len(data)
    assert kept.sha256 == hashlib.sha256(data).hexdigest()
    assert kept.file_sha256 == runs.file_sha256(first) and kept.notes == ()
    assert runs.rebuild(first) == runs.rebuild(copy) == data


def test_a_later_list_is_a_difference_against_the_base(tmp_path):
    runs.keep(tmp_path, AT, listing())
    new = listing(seed=1, changed=20)
    kept = runs.keep(tmp_path, later(30), new)
    assert kept.kind == "diff" and kept.path.name == "2026-09-29T070603.215Z.diff.zst"
    assert kept.path.parent.name == "2026-09-29T063603.215Z"
    assert kept.stored == kept.path.stat().st_size < len(new) / 10
    assert runs.rebuild(kept.path) == new
    assert list(runs.kept(tmp_path)) == ["2026-09-29T063603.215Z", "2026-09-29T070603.215Z"]


def test_each_difference_is_against_the_base_never_another_difference(tmp_path):
    base = listing()
    runs.keep(tmp_path, AT, base)
    lists = [listing(seed=n, changed=5 * n) for n in range(1, 5)]
    for n, data in enumerate(lists, 1):
        runs.keep(tmp_path, later(30 * n), data)
    files = runs.kept(tmp_path)
    for n, data in enumerate(lists, 1):
        path = files[runs.name(later(30 * n))]
        assert path.parent.name == runs.name(AT) and runs.rebuild(path) == data
    # losing one difference loses only its own list
    files[runs.name(later(60))].unlink()
    assert runs.rebuild(files[runs.name(later(90))]) == lists[2]


def test_a_list_whose_difference_costs_more_than_the_run_s_average_starts_a_new_run(tmp_path):
    runs.keep(tmp_path, AT, listing())
    for n in range(1, 4):
        assert runs.keep(tmp_path, later(n), listing(seed=n, changed=1)).kind == "diff"
    other = noise(20_000, seed=9)  # shares nothing with the base
    kept = runs.keep(tmp_path, later(10), other)
    assert kept.kind == "base" and [p.name for p in runs.runs(tmp_path)] == [
        runs.name(AT),
        runs.name(later(10)),
    ]
    assert runs.rebuild(kept.path) == other


def test_a_run_spans_at_most_30_days(tmp_path):
    runs.keep(tmp_path, AT, listing())
    assert runs.keep(tmp_path, AT + timedelta(days=29, hours=23), listing(seed=1, changed=1)).kind == "diff"
    assert runs.keep(tmp_path, AT + timedelta(days=30), listing(seed=2, changed=1)).kind == "base"


def test_an_older_list_found_late_goes_in_the_latest_run(tmp_path):
    runs.keep(tmp_path, AT, listing())
    kept = runs.keep(tmp_path, AT - timedelta(minutes=30), listing(seed=1, changed=3))
    assert kept.kind == "diff" and kept.path.parent.name == runs.name(AT)


def test_a_base_and_list_past_zstd_s_reach_start_a_new_run(tmp_path, monkeypatch):
    runs.keep(tmp_path, AT, listing())
    monkeypatch.setattr(runs, "REACH", 1000)
    assert runs.keep(tmp_path, later(30), listing(seed=1, changed=1)).kind == "base"


def test_a_base_too_small_to_difference_against_starts_a_new_run(tmp_path):
    runs.keep(tmp_path, AT, b"{}")
    assert runs.keep(tmp_path, later(30), b"{}\n").kind == "base"


def test_a_damaged_copy_of_the_base_is_set_aside_and_written_again(tmp_path):
    runs.keep(tmp_path, AT, listing())
    first, copy = runs.copies(tmp_path / runs.name(AT))
    good = first.read_bytes()
    copy.write_bytes(good[:-9] + bytes(9))
    kept = runs.keep(tmp_path, later(30), listing(seed=1, changed=2))
    assert kept.kind == "diff" and copy.read_bytes() == good
    (aside,) = copy.parent.glob("*.damaged-*")
    assert aside.name.startswith(copy.name + ".damaged-")
    assert kept.notes == (
        f"{copy.name} was damaged: set aside as {aside.name}, and written again from the other copy",
    )
    assert runs.name(AT) in runs.kept(tmp_path)  # a copy set aside isn't a list


def test_a_missing_first_copy_is_written_again_from_the_second(tmp_path):
    runs.keep(tmp_path, AT, listing())
    first, copy = runs.copies(tmp_path / runs.name(AT))
    first.unlink()
    kept = runs.keep(tmp_path, later(30), listing(seed=1, changed=2))
    assert first.read_bytes() == copy.read_bytes()
    assert kept.notes == (f"{first.name} was missing, and written again from the other copy",)


def test_a_base_damaged_in_both_copies_leaves_the_new_list_a_run_of_its_own(tmp_path):
    runs.keep(tmp_path, AT, listing())
    run = tmp_path / runs.name(AT)
    for path in runs.copies(run):
        path.write_bytes(b"not zstd")
    data = listing(seed=1, changed=2)
    kept = runs.keep(tmp_path, later(30), data)
    assert kept.kind == "base" and runs.rebuild(kept.path) == data
    assert kept.notes == (
        f"{run.name}: neither copy of its base reads whole: its lists can't be rebuilt until one is "
        "restored; this list starts a new run",
    )
    with pytest.raises(runs.Damaged, match="neither copy"):
        runs.read_base(run)


def test_a_difference_is_rebuilt_from_the_base_s_second_copy_when_the_first_is_gone(tmp_path):
    runs.keep(tmp_path, AT, listing())
    data = listing(seed=1, changed=2)
    kept = runs.keep(tmp_path, later(30), data)
    runs.copies(kept.path.parent)[0].unlink()
    assert runs.rebuild(kept.path) == data


def test_a_file_that_doesn_t_read_back_as_its_list_is_set_aside(tmp_path, monkeypatch):
    real = runs._write
    monkeypatch.setattr(runs, "_write", lambda path, data: real(path, data[:-4]))  # a disk that drops bytes
    with pytest.raises(runs.Unverified, match="didn't read back as the list written"):
        runs.keep(tmp_path, AT, listing())
    run = tmp_path / runs.name(AT)
    assert runs.kept(tmp_path) == {} and len(list(run.glob("*.damaged-*"))) == 1


def test_only_lists_count_as_kept(tmp_path):
    runs.keep(tmp_path, AT, listing())
    run = tmp_path / runs.name(AT)
    (run / "2026-09-29T070603Z.diff.zst.part").write_bytes(b"")
    (tmp_path / "notes").mkdir()
    assert list(runs.kept(tmp_path)) == [runs.name(AT)]
    assert runs.runs(tmp_path) == [run] and runs.runs(tmp_path / "none") == []
    assert runs.stamp_of(run / "x.txt") is None
