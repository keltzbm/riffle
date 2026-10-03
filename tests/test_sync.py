import pytest

from riffle import sync

NOTE = """---
game: mtg
format: commander
---

## Moxfield import

```
Commander
1 Aesi, Tyrant of Gyre Strait

Deck
1 Sol Ring
1 Cyclonic Rift
```

- [ ] 🟢 **[[Cyclonic Rift]]** · ~$30 #mtg/buy
"""


def _vault(tmp_path):
    mtg = tmp_path / "library" / "tcg" / "mtg"
    (mtg / "commander").mkdir(parents=True)
    (mtg / "commander" / "aesi-lands.md").write_text(NOTE)
    return mtg


def test_sync_writes_only_machine_zones(tmp_path, cat, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    mtg = _vault(tmp_path)
    before = (mtg / "commander" / "aesi-lands.md").read_text()
    inv = sync.Inventory([])
    res = sync.run(mtg, inv, cat, today="2026-09-21")

    assert res.decks == ["aesi-lands"]
    assert (mtg / "commander" / "aesi-lands.md").read_text() == before
    written = {p.relative_to(mtg).parts[0] for p in mtg.rglob("*") if p.is_file()}
    assert written == {"commander", "_generated", "_log"}
    data = (mtg / "_generated" / "aesi-lands-data.md").read_text()
    assert "3 cards" in data and "[[Cyclonic Rift]]" in data


def test_notes_carry_no_date_so_a_day_later_with_the_same_prices_writes_nothing(tmp_path, cat, monkeypatch):
    """S1: each note said `generated: <today>`, so every sync of a new day rewrote all of them."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    mtg = _vault(tmp_path)
    inv = sync.Inventory([])
    assert sync.run(mtg, inv, cat, today="2026-09-21").changed_notes == 2
    gen = mtg / "_generated"
    before = {p.name: p.stat().st_mtime_ns for p in gen.iterdir()}
    assert sync.run(mtg, inv, cat, today="2026-09-22").changed_notes == 0
    assert {p.name: p.stat().st_mtime_ns for p in gen.iterdir()} == before
    assert "generated:" not in (gen / "aesi-lands-data.md").read_text()
    assert "generated:" not in (gen / "collection-summary.md").read_text()


def test_the_old_price_log_is_closed_once_and_never_appended_to(tmp_path, cat, monkeypatch):
    """S2: the price log grew by a line a buy card a day; `riffle prices log` reads the store instead."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    mtg = _vault(tmp_path)
    log = mtg / "_log" / "prices.md"
    log.parent.mkdir(parents=True)
    log.write_text("2026-09-20 | Cyclonic Rift | $29.00 | 1.90")  # no newline at the end
    sync.run(mtg, sync.Inventory([]), cat, today="2026-09-21")
    closed = log.read_text()
    assert closed == (
        "2026-09-20 | Cyclonic Rift | $29.00 | 1.90\n"
        "\nNot appended to since 2026-09-21: `riffle prices log` prints every buy card's price for "
        "each Scryfall day the store keeps.\n"
    )
    sync.run(mtg, sync.Inventory([]), cat, today="2026-09-22")
    assert log.read_text() == closed


def test_a_vault_without_a_price_log_gets_none(tmp_path, cat, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    mtg = _vault(tmp_path)
    sync.run(mtg, sync.Inventory([]), cat, today="2026-09-21")
    assert not (mtg / "_log" / "prices.md").exists()


def test_the_state_is_saved_once_however_many_decks_changed(tmp_path, cat, monkeypatch):
    """S3: it was saved after each deck whose list changed, up to 3.6 MB each time."""
    from riffle.export import obsidian

    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    mtg = _vault(tmp_path)
    for n in range(50):
        (mtg / "commander" / f"aesi-{n}.md").write_text(NOTE)
    saved, save = [], obsidian.save_state
    monkeypatch.setattr(obsidian, "save_state", lambda state: (saved.append(len(state)), save(state)))
    assert len(sync.run(mtg, sync.Inventory([]), cat, today="2026-09-21").versions) == 51
    assert saved == [51]
    saved.clear()
    sync.run(mtg, sync.Inventory([]), cat, today="2026-09-21")
    assert saved == []  # nothing changed, nothing saved


def test_a_run_cut_off_partway_saves_the_logs_it_appended(tmp_path, cat, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    mtg = _vault(tmp_path)
    (mtg / "commander" / "zz-last.md").write_text(NOTE)
    real = sync.analyse

    def stops_at_the_last(deck, inv, catalog):
        if deck.slug == "zz-last":
            raise KeyboardInterrupt
        return real(deck, inv, catalog)

    monkeypatch.setattr(sync, "analyse", stops_at_the_last)
    with pytest.raises(KeyboardInterrupt):
        sync.run(mtg, sync.Inventory([]), cat, today="2026-09-21")
    from riffle.export import obsidian

    assert set(obsidian.load_state()) == {"aesi-lands"}  # its log was appended, and its state kept
    monkeypatch.setattr(sync, "analyse", real)
    assert sync.run(mtg, sync.Inventory([]), cat, today="2026-09-21").versions == ["zz-last"]


def test_conflict_copies_are_named_and_never_read(tmp_path, cat, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    mtg = _vault(tmp_path)
    (mtg / "commander" / "aesi-lands [conflicted].md").write_text(NOTE)
    (mtg / "_log").mkdir()
    (mtg / "_log" / "aesi-lands-versions.sync-conflict-20261002-061151-ABCDEFG.md").write_text("x")
    (mtg / "_generated").mkdir()
    (mtg / "_generated" / "aesi-lands-data [conflicted 2].md").write_text("x")
    res = sync.run(mtg, sync.Inventory([]), cat, today="2026-09-21")
    assert res.decks == ["aesi-lands"]
    assert res.removed == ["aesi-lands-data [conflicted 2].md"]  # rebuilt anyway
    assert res.warnings == [
        "sync-conflict copies, not read; merge each by hand: "
        "mtg/_log/aesi-lands-versions.sync-conflict-20261002-061151-ABCDEFG.md, "
        "mtg/commander/aesi-lands [conflicted].md"
    ]


def test_notes_are_dated_today_in_utc(monkeypatch):
    from datetime import UTC, datetime

    from riffle import times
    from riffle.export import obsidian

    monkeypatch.setattr(times, "now", lambda: datetime(2026, 9, 29, 0, 30, tzinfo=UTC))  # 18:30 in Denver
    assert obsidian.today() == "2026-09-29"


def test_versions_log_records_changes_only(tmp_path, cat, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    mtg = _vault(tmp_path)
    inv = sync.Inventory([])
    assert sync.run(mtg, inv, cat, today="2026-09-21").versions == ["aesi-lands"]
    assert sync.run(mtg, inv, cat, today="2026-09-21").versions == []
    note = mtg / "commander" / "aesi-lands.md"
    note.write_text(note.read_text().replace("1 Sol Ring\n", "1 Fire // Ice\n"))
    assert sync.run(mtg, inv, cat, today="2026-09-22").versions == ["aesi-lands"]
    log = (mtg / "_log" / "aesi-lands-versions.md").read_text()
    assert "\\+ 1 [[Fire // Ice]]" in log and "\\- 1 [[Sol Ring]]" in log


def test_generated_note_not_rewritten_when_unchanged(tmp_path, cat, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    mtg = _vault(tmp_path)
    inv = sync.Inventory([])
    first = sync.run(mtg, inv, cat, today="2026-09-21").changed_notes
    assert first == 2
    assert sync.run(mtg, inv, cat, today="2026-09-21").changed_notes == 0


def test_generated_note_carries_import_blocks_and_stale_notes_are_pruned(tmp_path, cat, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    mtg = _vault(tmp_path)
    gen = mtg / "_generated"
    gen.mkdir(parents=True)
    (gen / "aesi-lands.data.md").write_text("old name")
    res = sync.run(mtg, sync.Inventory([]), cat, today="2026-09-21")
    assert res.removed == ["aesi-lands.data.md"]
    data = (gen / "aesi-lands-data.md").read_text()
    assert "## Moxfield import" in data and "## MTGO import" in data
    assert "| ○ 1 | [[Cyclonic Rift]] |" in data
    assert "✓ 0 own · ○ 3 buy" in data


def test_old_dotted_versions_log_is_renamed_not_lost(tmp_path, cat, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    mtg = _vault(tmp_path)
    (mtg / "_log").mkdir(parents=True)
    (mtg / "_log" / "aesi-lands.versions.md").write_text("history\n")
    sync.run(mtg, sync.Inventory([]), cat, today="2026-09-21")
    assert (mtg / "_log" / "aesi-lands-versions.md").read_text().startswith("history")
    assert not (mtg / "_log" / "aesi-lands.versions.md").exists()


def test_rename_happens_even_when_the_list_is_unchanged(tmp_path, cat, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    mtg = _vault(tmp_path)
    sync.run(mtg, sync.Inventory([]), cat, today="2026-09-21")
    new = mtg / "_log" / "aesi-lands-versions.md"
    new.rename(mtg / "_log" / "aesi-lands.versions.md")
    sync.run(mtg, sync.Inventory([]), cat, today="2026-09-21")
    assert new.exists()


def test_a_note_that_isnt_utf8_is_skipped_and_named_and_the_rest_carries_on(tmp_path, cat, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    mtg = _vault(tmp_path)
    sync.run(mtg, sync.Inventory([]), cat, today="2026-09-21")
    (mtg / "commander" / "aesi-copy.md").write_bytes(NOTE.encode("utf-16"))
    (mtg / "commander" / "aesi-lands.md").write_bytes(NOTE.encode() + b"caf\xe9\n")  # saved as Latin-1
    (mtg.parent / "shopping.md").write_bytes(b"- [ ] **[[Sol Ring]]** #mtg/buy caf\xe9\n")  # not read
    res = sync.run(mtg, sync.Inventory([]), cat, today="2026-09-22")
    assert res.decks == [] and res.removed == []  # aesi-lands' last generated note stays
    assert (mtg / "_generated" / "aesi-lands-data.md").exists()
    assert res.failed == [
        (
            "vault notes",
            "skipped, can't read mtg/commander/aesi-copy.md (not UTF-8), "
            "mtg/commander/aesi-lands.md (not UTF-8)",
        )
    ]


@pytest.mark.parametrize("state", ['{"aesi-lands": {"hash"', "[]"])
def test_an_unreadable_sync_state_is_set_aside_and_the_logs_start_again(tmp_path, cat, monkeypatch, state):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    mtg = _vault(tmp_path)
    sync.run(mtg, sync.Inventory([]), cat, today="2026-09-21")
    path = tmp_path / "data" / "riffle" / "sync-state.json"
    path.write_text(state)
    res = sync.run(mtg, sync.Inventory([]), cat, today="2026-09-22")
    [(label, why)] = res.failed
    assert label == "version logs" and f"{path} " in why and "set aside as sync-state." in why
    [bad] = path.parent.glob("sync-state.*.bad.json")
    assert bad.read_text() == state
    assert res.versions == ["aesi-lands"]
    assert (mtg / "_log" / "aesi-lands-versions.md").read_text().count("Baseline: 3 cards.") == 2
    assert sync.run(mtg, sync.Inventory([]), cat, today="2026-09-22").failed == []  # once only


def test_a_note_is_written_whole_or_not_at_all(tmp_path):
    """A sync service reading the note mid-write must see the old one, never half of the new."""
    from riffle.export import obsidian

    note = tmp_path / "x-data.md"
    assert obsidian._write(note, "old\n")
    with pytest.raises(UnicodeEncodeError):
        obsidian._write(note, "new \ud800\n")  # fails partway through writing
    assert note.read_text() == "old\n"
    assert [p.name for p in tmp_path.iterdir()] == ["x-data.md"]  # no part file left behind


def test_a_note_that_went_missing_is_written_again_though_nothing_changed(tmp_path, cat, monkeypatch):
    """C52: a sync service can turn a note into a conflict copy and drop the original. The next
    sync removes the copy and writes the note again, whether or not its deck changed."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    mtg = _vault(tmp_path)
    sync.run(mtg, sync.Inventory([]), cat, today="2026-09-21")
    note = mtg / "_generated" / "aesi-lands-data.md"
    text = note.read_text()
    note.rename(note.with_name("aesi-lands-data [conflicted 3].md"))
    res = sync.run(mtg, sync.Inventory([]), cat, today="2026-09-21")
    assert (res.changed_notes, res.versions) == (1, [])
    assert res.removed == ["aesi-lands-data [conflicted 3].md"]
    assert note.read_text() == text
