"""Run the sync workflow directly, with isolated files and controlled sources."""

from contextlib import contextmanager
from datetime import date
from pathlib import Path

import pytest

from riffle import config
from riffle.ingest.local import manabox
from riffle.ingest.remote import scryfall, tcgcsv
from riffle.ops import sync
from riffle.store import postgres

NOTE = "---\ngame: mtg\nformat: modern\n---\n\n```\n4 Lightning Bolt\n```\n"


class Output:
    def __init__(self):
        self.events = []

    def prices(self, snapshot):
        self.events.append(("prices", snapshot))

    def no_collection(self):
        self.events.append(("no collection", None))

    def synced(self, result):
        self.events.append(("synced", result))


@pytest.fixture
def deck(opened, monkeypatch):
    cfg = config.load()
    mtg = cfg.mtg_dir
    (mtg / "modern").mkdir(parents=True)
    (mtg / "modern" / "burn.md").write_text(NOTE)
    monkeypatch.setattr(scryfall, "snapshot_prices", lambda: (Path("2026-10-27.jsonl.gz"), False))
    return mtg


def unexpected(*args, **kwargs):
    pytest.fail("an offline operation attempted a download, disk check, or backup")


def test_offline_pickup_and_notes_run_without_terminal_output(deck, opened, tracker, monkeypatch, capsys):
    cfg = config.load()
    cfg.downloads.mkdir()
    export = cfg.downloads / "ManaBox_Collection.csv"
    export.write_text("Name,Quantity\nLightning Bolt,4\n")
    for module, name in (
        (sync.net, "wait_online"),
        (sync.ingest, "refresh"),
        (sync.disk, "check"),
        (sync.database, "backup"),
    ):
        monkeypatch.setattr(module, name, unexpected)
    output = Output()

    sync.run(tracker, output)

    assert cfg.collection_csv.read_bytes() == export.read_bytes()
    assert cfg.collection_csv.stat().st_mtime_ns == export.stat().st_mtime_ns
    assert len(manabox.kept(cfg.manabox_exports)) == 1
    assert (deck / "_generated" / "burn-data.md").exists()
    assert [kind for kind, _v in output.events] == ["synced"]
    result = output.events[0][1]
    assert result.decks == ["burn"] and result.changed_notes == 2
    assert result.failed == []
    assert opened == ["open", "close"]
    assert capsys.readouterr() == ("", "")


def test_a_failed_refresh_step_still_reaches_notes_and_the_final_backup(deck, tracker, monkeypatch):
    output = Output()
    snapshot = tcgcsv.Snapshot(date(2026, 10, 27), requests=3)
    monkeypatch.setattr(sync.net, "wait_online", lambda *args, **kwargs: 0)

    def refresh(tracker):
        output.events.append(("refresh", None))
        tracker.step("Scryfall bulk data").fail("HTTP 503")

    def prices(tracker, online):
        assert online
        return snapshot

    def backup(tracker, url, warn_elsewhere):
        output.events.append(("backup", (url, warn_elsewhere)))
        assert (deck / "_generated" / "burn-data.md").exists()

    monkeypatch.setattr(sync.ingest, "refresh", refresh)
    monkeypatch.setattr(sync.ingest, "snapshot_prices", prices)
    monkeypatch.setattr(sync.disk, "check", lambda tracker: output.events.append(("disk", None)))
    monkeypatch.setattr(sync.database, "backup", backup)

    sync.run(tracker, output, offline=False)

    assert [kind for kind, _v in output.events] == [
        "refresh",
        "prices",
        "disk",
        "no collection",
        "synced",
        "backup",
    ]
    assert output.events[1][1] is snapshot
    assert output.events[-1][1] == (config.load().database_url, True)
    assert tracker.outcomes()["Scryfall bulk data"] == ("fail", "HTTP 503")


@pytest.mark.parametrize("missing_vault", [False, True])
def test_network_fallback_keeps_the_online_invocations_backup(deck, tracker, monkeypatch, missing_vault):
    if missing_vault:
        config.config_path().write_text('vault = ""\n')
    output = Output()
    monkeypatch.setattr(sync.net, "wait_online", lambda *args, **kwargs: None)
    monkeypatch.setattr(sync.ingest, "refresh", unexpected)
    monkeypatch.setattr(sync.disk, "check", unexpected)
    monkeypatch.setattr(
        sync.database,
        "backup",
        lambda tracker, url, warn_elsewhere: output.events.append(("backup", warn_elsewhere)),
    )

    sync.run(tracker, output, offline=False)

    assert tracker.outcomes()["network"] == ("fail", "no connection after 2m 00s; syncing offline")
    assert output.events[-1] == ("backup", True)
    if missing_vault:
        assert tracker.outcomes()["vault"] == ("fail", config.no_vault())
        assert not (deck / "_generated").exists()
    else:
        assert (deck / "_generated" / "burn-data.md").exists()
        assert [kind for kind, _v in output.events] == ["no collection", "synced", "backup"]


@pytest.mark.parametrize("failure", ["catalog", "inventory"])
def test_an_unavailable_catalog_or_inventory_closes_the_catalog_and_skips_backup(
    deck, tracker, monkeypatch, failure
):
    closed = []

    @contextmanager
    def catalog():
        try:
            if failure == "catalog":
                raise postgres.Unavailable("catalog not ready")
            yield object()
        finally:
            closed.append(True)

    def inventory(*args):
        raise manabox.NotExport("collection changed while opening the catalog")

    monkeypatch.setattr(sync.net, "wait_online", lambda *args, **kwargs: 0)
    monkeypatch.setattr(sync.ingest, "refresh", lambda tracker: None)
    monkeypatch.setattr(sync.ingest, "snapshot_prices", lambda tracker, online: None)
    monkeypatch.setattr(sync.disk, "check", lambda tracker: None)
    monkeypatch.setattr(postgres, "open_catalog", catalog)
    monkeypatch.setattr(sync.syncmod, "inventory", inventory)
    monkeypatch.setattr(sync.database, "backup", unexpected)
    output = Output()

    with pytest.raises(sync.Unavailable, match="catalog not ready|collection changed"):
        sync.run(tracker, output, offline=False)

    assert closed == [True]
    assert [kind for kind, _v in output.events] == ["no collection"]
    assert not (deck / "_generated").exists()
