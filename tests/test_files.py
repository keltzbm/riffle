"""Read cache objects and keep replacements from exposing partial files."""

from pathlib import Path

import pytest

from riffle import files, runs
from riffle.export import obsidian


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        (None, {}),
        (b"", {}),
        (b'{"cut":', {}),
        (b"\xff", {}),
        (b"[]", {}),
        (b"null", {}),
        (b"false", {}),
        ('{"name": "é", "nested": {"count": 2}}'.encode(), {"name": "é", "nested": {"count": 2}}),
    ],
    ids=["missing", "empty", "truncated", "invalid UTF-8", "array", "null", "boolean", "object"],
)
def test_read_json_object_returns_only_readable_objects(tmp_path, content, expected):
    path = tmp_path / "cache.json"
    if content is not None:
        path.write_bytes(content)
    assert files.read_json_object(path) == expected


def test_a_cache_that_cannot_be_opened_returns_a_fresh_empty_object(tmp_path, monkeypatch):
    def unreadable(path, **kwargs):
        raise PermissionError("not readable")

    monkeypatch.setattr(Path, "read_text", unreadable)
    first = files.read_json_object(tmp_path / "first.json")
    first["changed"] = True
    assert files.read_json_object(tmp_path / "second.json") == {}


@pytest.mark.parametrize("before", [None, b"old"])
def test_replacement_becomes_visible_only_after_the_block_finishes(tmp_path, before):
    path = tmp_path / "nested" / "cache.json"
    if before is not None:
        path.parent.mkdir()
        path.write_bytes(before)
    with files.replacing(path) as part:
        assert part == path.with_name("cache.json.part")
        part.write_bytes(b"new")
        assert (path.read_bytes() if path.exists() else None) == before
    assert path.read_bytes() == b"new"
    assert not part.exists()


@pytest.mark.parametrize("cleanup", [False, True], ids=["keep partial", "discard partial"])
@pytest.mark.parametrize("error", [ValueError, KeyboardInterrupt], ids=["failure", "interrupted"])
def test_failed_replacements_preserve_the_target_and_the_callers_partial_policy(tmp_path, cleanup, error):
    path = tmp_path / "cache.json"
    path.write_bytes(b"old")
    with pytest.raises(error), files.replacing(path, cleanup=cleanup) as part:
        part.write_bytes(b"partial")
        raise error("stopped")
    assert path.read_bytes() == b"old"
    assert part.exists() is not cleanup
    if not cleanup:
        assert part.read_bytes() == b"partial"


@pytest.mark.parametrize("cleanup", [False, True])
def test_a_failed_rename_does_not_replace_the_target(tmp_path, monkeypatch, cleanup):
    def refused(part, destination):
        raise OSError("rename refused")

    path = tmp_path / "cache.json"
    path.write_bytes(b"old")
    monkeypatch.setattr(Path, "replace", refused)
    with pytest.raises(OSError, match="rename refused"), files.replacing(path, cleanup=cleanup) as part:
        part.write_bytes(b"new")
    assert path.read_bytes() == b"old"
    assert part.exists() is not cleanup


def test_notes_keep_the_hidden_temporary_name_and_skip_unchanged_content(tmp_path, monkeypatch):
    path = tmp_path / "note.md"
    replaced = []
    replace = Path.replace

    def record(part, destination):
        replaced.append(part.name)
        return replace(part, destination)

    monkeypatch.setattr(Path, "replace", record)
    assert obsidian.write_whole(path, "é\n")
    changed_at = path.stat().st_mtime_ns
    assert not obsidian.write_whole(path, "é\n")
    assert replaced == [".note.md.part"]
    assert path.stat().st_mtime_ns == changed_at
    assert path.read_bytes() == "é\n".encode()


def test_a_durability_failure_leaves_the_old_file_and_cleans_up(tmp_path, monkeypatch):
    def failed(fd):
        raise OSError("sync failed")

    path = tmp_path / "backup.dump"
    path.write_bytes(b"old")
    monkeypatch.setattr(runs.os, "fsync", failed)
    with pytest.raises(OSError, match="sync failed"):
        runs.write(path, b"new")
    assert path.read_bytes() == b"old"
    assert [p.name for p in tmp_path.iterdir()] == ["backup.dump"]
