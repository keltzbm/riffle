"""A sync that meets trouble finishes what it can, then says what failed and exits 1."""

import socket
from datetime import date
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from riffle import cli, net
from riffle.cli import app
from riffle.ingest import cardmarket, goatbots, mtgjson, pricelists, scryfall, scryfall_catalog, tcgcsv
from riffle.progress import Watched

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
