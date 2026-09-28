"""A sync that meets trouble finishes what it can, then says what failed and exits 1."""

import fcntl
import json
import os
import socket
import threading
from datetime import date
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from riffle import cli, net
from riffle.cli import app
from riffle.ingest import cardmarket, goatbots, mtgjson, pricelists, scryfall, scryfall_catalog, tcgcsv
from riffle.progress import Watched

REAL_UPDATE = scryfall_catalog.update
NOTE = "---\ngame: mtg\nformat: modern\n---\n\n## Moxfield import\n\n```\n4 Lightning Bolt\n```\n"


class Clock:
    """time.monotonic and time.sleep for net.wait_online, with no real waiting."""

    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def network(monkeypatch, up_after: float | None) -> list[tuple[str, int]]:
    """The network comes up after up_after seconds (None: never). Returns every address tried."""
    clock, tried = Clock(), []

    def create_connection(address, timeout):
        tried.append(address)
        if up_after is None or clock.now < up_after:
            raise OSError("Network is unreachable")
        return socket.socket()

    monkeypatch.setattr(net.socket, "create_connection", create_connection)
    monkeypatch.setattr(net.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(net.time, "sleep", clock.sleep)
    return tried


# ---- waiting for the network ---------------------------------------------------------------


def test_an_online_host_answers_at_once(monkeypatch):
    tried = network(monkeypatch, up_after=0)
    assert net.wait_online("api.scryfall.com") == 0
    assert tried == [("api.scryfall.com", 443)]


def test_the_wait_ends_when_the_network_comes_up(monkeypatch):
    tried = network(monkeypatch, up_after=12)
    assert net.wait_online("api.scryfall.com", pause=5) == 15
    assert len(tried) == 4  # at 0, 5, 10, and 15 seconds


def test_the_wait_gives_up_at_its_timeout(monkeypatch):
    tried = network(monkeypatch, up_after=None)
    assert net.wait_online("api.scryfall.com", timeout=20, pause=5) is None
    assert len(tried) == 5  # at 0, 5, 10, 15, and 20 seconds; the next would be past the timeout


# ---- failed steps --------------------------------------------------------------------------


def test_watched_passes_steps_on_and_remembers_failures(tracker):
    watched = Watched(tracker)
    step = watched.step("card catalog", total=3, unit="printings")
    step.update(1, 3)
    step.ok("done")
    watched.step("Scryfall set list").fail("HTTP 503")
    watched.step("network").drop()
    assert watched.failed == ["Scryfall set list"]
    assert tracker.outcomes() == {
        "card catalog": ("ok", "done"),
        "Scryfall set list": ("fail", "HTTP 503"),
        "network": ("drop",),
    }
    assert (tracker.steps[0].total, tracker.steps[0].unit, tracker.steps[0].updates) == (
        3,
        "printings",
        [(1, 3)],
    )


@pytest.fixture
def vault(tmp_path, monkeypatch, opened):
    """A one-deck vault under a fake HOME, the in-memory catalog, and a price snapshot kept."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))  # the default vault, under HOME
    mtg = tmp_path / "atelier" / "library" / "tcg" / "mtg"
    (mtg / "modern").mkdir(parents=True)
    (mtg / "modern" / "burn.md").write_text(NOTE)
    monkeypatch.setattr(scryfall, "snapshot_prices", lambda: (Path("/d/2026-09-26.jsonl.gz"), False))
    return mtg


# What an online sync does, in order: the Scryfall download and catalog load, then each price source.
ONLINE = ["refresh", "catalog", "mtgjson", "goatbots", "cardmarket", "cardkingdom", "manapool", "tcgcsv"]


def online_steps(monkeypatch, calls, refresh_fails=False):
    """The online steps, recorded in calls, each reporting a step as the real one does."""

    def refresh(force, tracker):
        calls.append("refresh")
        step = tracker.step("Scryfall bulk data")
        if refresh_fails:
            step.fail("HTTP 503")
        else:
            step.ok("current")

    def snapshot(delay, tracker):
        calls.append("tcgcsv")
        tracker.step("tcgcsv mtg").ok("456 groups")
        return tcgcsv.Snapshot(day=date(2026, 9, 26), fetched=["mtg"], groups={"mtg": 456})

    monkeypatch.setattr(scryfall, "refresh", refresh)
    monkeypatch.setattr(scryfall_catalog, "update", lambda tracker, force: calls.append("catalog"))
    monkeypatch.setattr(mtgjson, "snapshot", lambda tracker: calls.append("mtgjson"))
    monkeypatch.setattr(goatbots, "snapshot", lambda tracker: calls.append("goatbots"))
    monkeypatch.setattr(cardmarket, "snapshot", lambda tracker: calls.append("cardmarket"))
    monkeypatch.setattr(pricelists, "snapshot", lambda lists, tracker: calls.append(lists[0].store))
    monkeypatch.setattr(tcgcsv, "snapshot", snapshot)


def test_without_a_network_the_sync_goes_offline_and_exits_1(vault, monkeypatch):
    network(monkeypatch, up_after=None)
    calls: list[str] = []
    online_steps(monkeypatch, calls)
    result = CliRunner().invoke(app, ["sync"])
    assert result.exit_code == 1, result.output
    assert "! network: no connection after 2m 00s; syncing offline" in result.output
    assert calls == []  # no download, no catalog load, no price downloads
    assert (vault / "_generated" / "burn-data.md").exists()
    assert result.output.rstrip().endswith("1 step failed: network")


def test_a_network_that_comes_up_late_is_waited_for(vault, monkeypatch):
    network(monkeypatch, up_after=20)
    calls: list[str] = []
    online_steps(monkeypatch, calls)
    result = CliRunner().invoke(app, ["sync"])
    assert result.exit_code == 0, result.output
    assert "network: up after 20.0s" in result.output
    assert calls == ONLINE


def test_a_network_that_is_up_goes_unmentioned(vault, monkeypatch):
    network(monkeypatch, up_after=0)
    online_steps(monkeypatch, [])
    result = CliRunner().invoke(app, ["sync"])
    assert result.exit_code == 0, result.output
    assert "network" not in result.output


def test_a_failed_download_doesnt_stop_the_sync(vault, monkeypatch):
    network(monkeypatch, up_after=0)
    calls: list[str] = []
    online_steps(monkeypatch, calls, refresh_fails=True)
    result = CliRunner().invoke(app, ["sync"])
    assert result.exit_code == 1, result.output
    assert calls == ONLINE
    assert "1 decks · 2 notes updated" in result.output
    assert result.output.rstrip().endswith("1 step failed: Scryfall bulk data")


def test_every_failed_step_is_named(vault, monkeypatch):
    def no_bulk():
        raise FileNotFoundError("no Scryfall bulk file yet")

    monkeypatch.setattr(scryfall, "snapshot_prices", no_bulk)
    network(monkeypatch, up_after=None)
    online_steps(monkeypatch, [])
    result = CliRunner().invoke(app, ["sync"])
    assert result.output.rstrip().endswith("2 steps failed: network, Scryfall prices")


def test_watch_keeps_watching_after_a_failed_resync(monkeypatch, capsys):
    def resync():
        raise typer.Exit(1)

    monkeypatch.setattr(cli, "_resync", resync)
    cli._resync_and_keep_watching()
    assert "resync failed; still watching" in capsys.readouterr().err


# ---- one source down, one bug: only its own steps fail ---------------------------------------


def test_scryfall_alone_down_fails_only_its_own_steps(vault, monkeypatch):
    monkeypatch.setattr(net.time, "sleep", lambda s: None)

    def create_connection(address, timeout):
        if address[0] == "api.scryfall.com":
            raise socket.gaierror(8, "nodename nor servname provided, or not known")
        return socket.socket()

    monkeypatch.setattr(net.socket, "create_connection", create_connection)
    calls: list[str] = []
    online_steps(monkeypatch, calls, refresh_fails=True)
    result = CliRunner().invoke(app, ["sync"])
    assert calls == ONLINE  # every price source still asked
    assert "network" not in result.output
    assert result.output.rstrip().endswith("1 step failed: Scryfall bulk data")


def test_the_network_wait_tries_every_source_scryfall_first():
    hosts = cli._hosts()
    assert hosts[0] == "api.scryfall.com" and len(hosts) == len(set(hosts))
    others = {"mtgjson.com", "www.goatbots.com", "tcgcsv.com", "api.cardkingdom.com", "manapool.com"}
    assert others <= set(hosts)


def test_a_bug_in_one_source_fails_its_steps_and_the_rest_still_run(vault, monkeypatch):
    network(monkeypatch, up_after=0)
    calls: list[str] = []
    online_steps(monkeypatch, calls)

    def mtgjson_snapshot(tracker):
        calls.append("mtgjson")
        tracker.step("MTGJSON prices", unit="bytes")
        raise ZeroDivisionError("division by zero")

    monkeypatch.setattr(mtgjson, "snapshot", mtgjson_snapshot)
    result = CliRunner().invoke(app, ["sync"])
    assert calls == ONLINE
    log = cli.config.data_dir() / "errors.log"
    why = f"ZeroDivisionError: division by zero (unexpected; details in {log})"
    assert f"MTGJSON prices: {why}" in result.output
    assert "Traceback" not in result.output and "Traceback" in log.read_text()
    assert result.output.rstrip().endswith("1 step failed: MTGJSON prices")


def test_a_bug_in_the_scryfall_price_step_is_reported_the_same_way(vault, monkeypatch):
    def snapshot_prices():
        raise AttributeError("'list' object has no attribute 'get'")

    monkeypatch.setattr(scryfall, "snapshot_prices", snapshot_prices)
    result = CliRunner().invoke(app, ["sync", "--offline"])
    why = "AttributeError: 'list' object has no attribute 'get' (unexpected;"
    assert f"Scryfall prices: {why}" in result.output


def test_a_database_url_that_cant_be_used_still_keeps_the_prices(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    (tmp_path / "config" / "riffle").mkdir(parents=True)
    (tmp_path / "config" / "riffle" / "config.toml").write_text('database_url = "not a url"\n')
    (tmp_path / "atelier" / "library" / "tcg" / "mtg").mkdir(parents=True)
    monkeypatch.setattr(scryfall, "snapshot_prices", lambda: (Path("/d/2026-09-26.jsonl.gz"), False))
    network(monkeypatch, up_after=0)
    calls: list[str] = []
    online_steps(monkeypatch, calls)
    monkeypatch.setattr(scryfall_catalog, "update", REAL_UPDATE)
    result = CliRunner().invoke(app, ["sync"])
    assert result.exit_code == 1
    assert calls == [c for c in ONLINE if c != "catalog"]  # every price source, after the failed load
    assert "card catalog: database_url in " in result.output
    assert result.output.count("can't be used") == 2  # the catalog load, then the vault half


# ---- the vault half ---------------------------------------------------------------------------


def test_a_missing_vault_is_reported_and_nothing_is_built_there(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(scryfall, "snapshot_prices", lambda: (Path("/d/2026-09-26.jsonl.gz"), False))
    mtg = tmp_path / "atelier" / "library" / "tcg" / "mtg"
    result = CliRunner().invoke(app, ["sync", "--offline"])
    assert result.exit_code == 1
    assert f"vault: no deck folder at {mtg}; set vault in {tmp_path / 'config'}" in result.output
    assert not (tmp_path / "atelier").exists()
    assert "0 decks" not in result.output


def test_a_note_that_isnt_utf8_fails_a_step_naming_it(vault, monkeypatch):
    (vault / "modern" / "bogles.md").write_bytes(NOTE.encode() + b"caf\xe9\n")
    result = CliRunner().invoke(app, ["sync", "--offline"])
    assert result.exit_code == 1
    assert "vault notes: skipped, can't read mtg/modern/bogles.md (not UTF-8)" in result.output
    assert "1 decks · " in result.output  # burn still synced
    assert (vault / "_generated" / "burn-data.md").exists()
    assert result.output.rstrip().endswith("1 step failed: vault notes")


def test_watch_keeps_watching_after_an_unexpected_error(monkeypatch, capsys):
    def resync():
        raise UnicodeDecodeError("utf-8", b"\xe9", 0, 1, "invalid continuation byte")

    monkeypatch.setattr(cli, "_resync", resync)
    cli._resync_and_keep_watching()
    err = capsys.readouterr().err
    assert "resync failed: UnicodeDecodeError: " in err and err.rstrip().endswith("still watching")


# ---- one run at a time -------------------------------------------------------------------------


def test_a_second_run_waits_for_the_first_and_says_whose_turn_it_is(tracker, capsys):
    path = cli._lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    held = path.open("a+")
    fcntl.flock(held, fcntl.LOCK_EX)
    path.write_text(json.dumps({"command": "riffle sync", "pid": 123, "since": "2026-09-28T13:02:00+00:00"}))
    threading.Timer(0.2, held.close).start()  # the first run ends
    with cli._one_at_a_time(tracker, "riffle ingest prices"):
        assert json.loads(path.read_text())["command"] == "riffle ingest prices"
    assert "waiting for riffle sync (pid 123), running since " in capsys.readouterr().out
    assert tracker.outcomes()["waiting for its turn"][0] == "ok"


def test_a_run_with_the_lock_free_goes_straight_on_and_records_itself(tracker):
    with cli._one_at_a_time(tracker, "riffle sync"):
        held = json.loads(cli._lock_path().read_text())
    assert held["command"] == "riffle sync" and held["pid"] == os.getpid()
    assert tracker.outcomes() == {}


def test_an_unreadable_lock_file_names_another_run(tmp_path):
    (tmp_path / "sync.lock").write_text("")
    assert cli._holder(tmp_path / "sync.lock") == "another riffle run"
    assert cli._holder(tmp_path / "missing") == "another riffle run"


def test_a_bug_is_still_reported_when_errors_log_cant_be_written(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    (tmp_path / "riffle").write_text("a file where the data folder should be")
    assert cli._failure(ValueError("bad")) == "ValueError: bad (unexpected)"
    assert cli._failure(OSError()) == "OSError"


def test_a_manabox_export_is_copied_whole_then_the_vault_resynced(vault, tmp_path, monkeypatch):
    export = tmp_path / "ManaBox_Collection.csv"
    export.write_text("Name,Set code,Collector number,Foil,Quantity\nLightning Bolt,m10,146,normal,4\n")
    result = CliRunner().invoke(app, ["ingest", "manabox", str(export)])
    assert result.exit_code == 0, result.output
    stored = cli.config.load().collection_csv
    assert stored.read_bytes() == export.read_bytes() and stored.stat().st_mtime == export.stat().st_mtime
    assert not stored.with_name(stored.name + ".part").exists()
    assert "1 decks · " in result.output


def test_watch_says_when_something_changed_in_the_mac_s_time(monkeypatch, denver):
    from datetime import UTC, datetime

    from riffle import times

    seen = iter([{"a.md": 1.0}, {"a.md": 1.0}, {"a.md": 2.0}, {"a.md": 2.0}])  # unchanged, then changed
    naps = iter([None, None, KeyboardInterrupt])

    def nap(seconds):
        if (e := next(naps)) is not None:
            raise e

    resyncs = []
    monkeypatch.setattr(cli, "_watched", lambda cfg: next(seen))
    monkeypatch.setattr(cli, "_resync_and_keep_watching", lambda: resyncs.append(1))
    monkeypatch.setattr("time.sleep", nap)
    monkeypatch.setattr(times, "now", lambda: datetime(2026, 9, 28, 9, 41, 7, tzinfo=UTC))
    output = CliRunner().invoke(app, ["watch"]).output
    assert "\n03:41:07 MDT changed: a.md\n" in output and output.rstrip().endswith("stopped")
    assert len(resyncs) == 2  # once at the start, once for the change
