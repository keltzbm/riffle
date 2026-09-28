from datetime import date

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


def test_price_log_is_append_only_once_per_scryfall_day(tmp_path, cat, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    mtg = _vault(tmp_path)
    inv = sync.Inventory([])
    res = sync.run(mtg, inv, cat, today="2026-09-21")
    assert (res.prices_logged, res.prices_day) == (1, date(2026, 9, 21))
    assert sync.run(mtg, inv, cat, today="2026-09-21").prices_logged == 0
    cat.day = date(2026, 9, 22)
    assert sync.run(mtg, inv, cat, today="2026-09-22").prices_logged == 1
    log = (mtg / "_log" / "prices.md").read_text()
    assert "2026-09-21 | Cyclonic Rift | $30.00 | 2.00" in log
    assert "2026-09-22 | Cyclonic Rift" in log


def test_a_run_after_midnight_with_yesterday_s_catalog_logs_nothing(tmp_path, cat, monkeypatch):
    """C10: the log was dated by the Mac's day, so an offline run after midnight logged the old
    catalog under the new day, and that day's real sync then logged nothing."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    mtg = _vault(tmp_path)
    inv = sync.Inventory([])
    sync.run(mtg, inv, cat, today="2026-09-21")
    assert sync.run(mtg, inv, cat, today="2026-09-22").prices_logged == 0  # still the 21st's prices
    cat.day = date(2026, 9, 22)
    assert sync.run(mtg, inv, cat, today="2026-09-22").prices_logged == 1  # the 22nd's, once loaded
    log = (mtg / "_log" / "prices.md").read_text()
    assert log.count("| Cyclonic Rift |") == 2


def test_a_catalog_with_no_prices_logs_nothing_and_says_so(tmp_path, cat, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    cat.day = None
    res = sync.run(_vault(tmp_path), sync.Inventory([]), cat, today="2026-09-21")
    assert (res.prices_logged, res.prices_day) == (0, None)
    assert "price log: the catalog holds no Scryfall prices yet, so nothing was logged" in res.warnings


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
    assert "| 🟥 1 | [[Cyclonic Rift]] |" in data


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
    (mtg.parent / "shopping.md").write_bytes(b"- [ ] **[[Sol Ring]]** #mtg/buy caf\xe9\n")
    res = sync.run(mtg, sync.Inventory([]), cat, today="2026-09-22")
    assert res.decks == [] and res.removed == []  # aesi-lands' last generated note stays
    assert (mtg / "_generated" / "aesi-lands-data.md").exists()
    assert res.failed == [
        (
            "vault notes",
            "skipped, can't read mtg/commander/aesi-copy.md (not UTF-8), "
            "mtg/commander/aesi-lands.md (not UTF-8), shopping.md (not UTF-8)",
        )
    ]
    assert res.prices_logged == 0  # no buy list could be read; a later run today can still log it
    assert "2026-09-22" not in (mtg / "_log" / "prices.md").read_text()


def test_a_buy_list_in_a_readable_note_is_still_logged_when_another_isnt(tmp_path, cat, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    mtg = _vault(tmp_path)
    (mtg.parent / "notes.md").write_bytes(b"caf\xe9\n")
    res = sync.run(mtg, sync.Inventory([]), cat, today="2026-09-21")
    assert res.decks == ["aesi-lands"] and res.prices_logged == 1
    assert res.failed == [("vault notes", "skipped, can't read notes.md (not UTF-8)")]


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
