"""`riffle ingest prices` and the price step of sync: report problems, never stop the run."""

from datetime import date
from pathlib import Path

import pytest
from typer.testing import CliRunner

from riffle import net
from riffle.cli import app
from riffle.ingest import cardmarket, goatbots, mtgjson, pricelists, scryfall, scryfall_catalog, tcgcsv


def fake_snapshot(fetched: dict[str, int], failed: dict[str, str] | None = None):
    """A tcgcsv.snapshot that reports to the tracker as the real one does."""

    def snapshot(delay, tracker):
        snap = tcgcsv.Snapshot(day=date(2026, 9, 24), fetched=list(fetched), groups=dict(fetched))
        for game, n in fetched.items():
            tracker.step(f"tcgcsv {game}", unit="groups").ok(f"{n} groups")
        for game, why in (failed or {}).items():
            snap.failed.append((game, why))
            tracker.step(f"tcgcsv {game}", unit="groups").fail(why)
        snap.requests = 1 + sum(fetched.values())
        return snap

    return snapshot


def fake_source(label: str | None, outcome: tuple[str, str] = ("ok", "already have 2026-09-24")):
    """A stand-in for a price source's snapshot (mtgjson, goatbots, cardmarket, pricelists) that
    reports one step, labelled label or, for a store's lists (label None), by its first list; the
    real ones report one or more."""

    def snapshot(*lists, tracker):
        step = tracker.step(label or lists[0][0].label, unit="bytes")
        if outcome[0] == "ok":
            step.ok(outcome[1])
        else:
            step.fail(outcome[1])

    return snapshot


@pytest.fixture(autouse=True)
def quiet_sources(monkeypatch):
    """No test here reaches MTGJSON, GoatBots, Cardmarket, or a store; tests that care replace these."""
    monkeypatch.setattr(mtgjson, "snapshot", fake_source("MTGJSON prices"))
    monkeypatch.setattr(goatbots, "snapshot", fake_source("GoatBots prices"))
    monkeypatch.setattr(cardmarket, "snapshot", fake_source("Cardmarket mtg"))
    monkeypatch.setattr(pricelists, "snapshot", fake_source(None))


def test_ingest_prices_reports_every_source(monkeypatch):
    monkeypatch.setattr(
        scryfall, "snapshot_prices", lambda: (Path("/d/scryfall/daily/2026-09-24.jsonl.gz"), True)
    )
    monkeypatch.setattr(mtgjson, "snapshot", fake_source("MTGJSON prices", ("ok", "kept 2026-09-24, 5.2 MB")))
    monkeypatch.setattr(
        goatbots, "snapshot", fake_source("GoatBots prices", ("ok", "kept 2026-09-24, 76,070 prices"))
    )
    monkeypatch.setattr(
        cardmarket, "snapshot", fake_source("Cardmarket mtg", ("ok", "kept 2026-09-24, 98,512 products"))
    )
    monkeypatch.setattr(tcgcsv, "snapshot", fake_snapshot({"fab": 105, "op": 87}))
    result = CliRunner().invoke(app, ["ingest", "prices"])
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert lines[0].endswith("  riffle ingest prices")
    assert "  Scryfall prices: kept 2026-09-24 (" in lines[1]
    assert "  MTGJSON prices: kept 2026-09-24, 5.2 MB (" in lines[2]
    assert "  GoatBots prices: kept 2026-09-24, 76,070 prices (" in lines[3]
    assert "  Cardmarket mtg: kept 2026-09-24, 98,512 products (" in lines[4]
    assert "  Card Kingdom singles: already have 2026-09-24 (" in lines[5]
    assert "  Mana Pool singles: already have 2026-09-24 (" in lines[6]
    assert "  tcgcsv fab: 105 groups (" in lines[7] and "  tcgcsv op: 87 groups (" in lines[8]
    assert lines[9] == "tcgcsv prices: 2026-09-24 · 193 requests"


def test_ingest_prices_reports_every_source_failing_then_exits_1(monkeypatch):
    def no_bulk():
        raise FileNotFoundError("no Scryfall bulk file yet")

    def down(delay, tracker):
        raise net.FetchError("no answer after 3 tries")

    monkeypatch.setattr(scryfall, "snapshot_prices", no_bulk)
    monkeypatch.setattr(mtgjson, "snapshot", fake_source("MTGJSON prices", ("fail", "Meta.json: HTTP 404")))
    monkeypatch.setattr(goatbots, "snapshot", fake_source("GoatBots prices", ("fail", "HTTP 403")))
    monkeypatch.setattr(cardmarket, "snapshot", fake_source("Cardmarket mtg", ("fail", "HTTP 503")))
    monkeypatch.setattr(pricelists, "snapshot", fake_source(None, ("fail", "HTTP 502")))
    monkeypatch.setattr(tcgcsv, "snapshot", down)
    result = CliRunner().invoke(app, ["ingest", "prices"])
    assert result.exit_code == 1, result.output
    assert "! Scryfall prices: no bulk file yet — run: riffle ingest scryfall" in result.output
    assert "! MTGJSON prices: Meta.json: HTTP 404" in result.output
    assert "! GoatBots prices: HTTP 403" in result.output
    assert "! Cardmarket mtg: HTTP 503" in result.output
    assert "! Card Kingdom singles: HTTP 502" in result.output
    assert "! Mana Pool singles: HTTP 502" in result.output
    assert "! tcgcsv prices: no answer after 3 tries" in result.output
    assert result.output.rstrip().endswith(
        "7 steps failed: Scryfall prices, MTGJSON prices, GoatBots prices, Cardmarket mtg, "
        "Card Kingdom singles, Mana Pool singles, tcgcsv prices"
    )


def test_a_disk_error_in_one_source_is_reported_and_the_rest_still_run(monkeypatch):
    def full(*lists, tracker):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(scryfall, "snapshot_prices", lambda: (Path("/d/2026-09-24.jsonl.gz"), False))
    monkeypatch.setattr(mtgjson, "snapshot", full)
    monkeypatch.setattr(goatbots, "snapshot", full)
    monkeypatch.setattr(cardmarket, "snapshot", full)
    monkeypatch.setattr(pricelists, "snapshot", full)
    monkeypatch.setattr(tcgcsv, "snapshot", fake_snapshot({"mtg": 456}))
    result = CliRunner().invoke(app, ["ingest", "prices"])
    assert result.exit_code == 1, result.output
    assert "! MTGJSON prices: [Errno 28] No space left on device" in result.output
    assert "! GoatBots prices: [Errno 28] No space left on device" in result.output
    assert "! Cardmarket prices: [Errno 28] No space left on device" in result.output
    assert "! Card Kingdom prices: [Errno 28] No space left on device" in result.output
    assert "! Mana Pool prices: [Errno 28] No space left on device" in result.output
    assert "  tcgcsv mtg: 456 groups (" in result.output


def test_a_game_that_failed_is_named(monkeypatch):
    monkeypatch.setattr(scryfall, "snapshot_prices", lambda: (Path("/d/2026-09-24.jsonl.gz"), False))
    monkeypatch.setattr(tcgcsv, "snapshot", fake_snapshot({"mtg": 456}, failed={"fab": "HTTP 503"}))
    result = CliRunner().invoke(app, ["ingest", "prices"])
    assert "  Scryfall prices: already have 2026-09-24 (" in result.output
    assert "! tcgcsv fab: HTTP 503" in result.output


def test_ingest_scryfall_loads_the_postgres_catalog_after_the_download(monkeypatch):
    calls = []
    monkeypatch.setattr(scryfall, "refresh", lambda force, tracker: calls.append(("refresh", force)))
    monkeypatch.setattr(scryfall_catalog, "update", lambda tracker, force: calls.append(("postgres", force)))
    monkeypatch.setattr(scryfall, "snapshot_prices", lambda: (Path("/d/2026-09-24.jsonl.gz"), False))
    result = CliRunner().invoke(app, ["ingest", "scryfall", "--force", "--no-sync"])
    assert result.exit_code == 0, result.output
    assert calls == [("refresh", True), ("postgres", True)]


def test_an_offline_resync_mentions_prices_only_when_it_kept_some(monkeypatch):
    kept = {"now": False}
    monkeypatch.setattr(
        scryfall, "refresh", lambda force, tracker: tracker.step("Scryfall bulk data").ok("current")
    )
    monkeypatch.setattr(scryfall_catalog, "update", lambda tracker, force: None)
    monkeypatch.setattr(scryfall, "snapshot_prices", lambda: (Path("/d/2026-09-24.jsonl.gz"), kept["now"]))
    quiet = CliRunner().invoke(app, ["ingest", "scryfall", "--no-sync"])
    assert quiet.exit_code == 0, quiet.output
    assert "Scryfall bulk data: current" in quiet.output and "Scryfall prices" not in quiet.output
    kept["now"] = True
    assert (
        "Scryfall prices: kept 2026-09-24"
        in CliRunner().invoke(app, ["ingest", "scryfall", "--no-sync"]).output
    )
