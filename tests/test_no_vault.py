"""With no vault set, each command that reads the vault says what to add and exits 2 before it
opens the database; the sync still collects, and fails only its vault step."""

import tomllib
from pathlib import Path

import pytest
from typer.testing import CliRunner

from riffle import cli, config
from riffle.cli import app
from riffle.ingest import scryfall
from tests.test_sync_failures import ONLINE, before_closing, network, online_steps

pytestmark = pytest.mark.usefixtures("backup_made")

UNREACHABLE = 'database_url = "postgresql+psycopg://tcg@127.0.0.1:1/unreachable"\n'
# the commands that read the vault, each deck named by its slug, so the vault is needed
COMMANDS = [
    ["decks"],
    ["own", "burn"],
    ["legal", "burn"],
    ["price", "burn"],
    ["compare", "burn", "twin"],
    ["similar", "burn"],
    ["export", "burn"],
    ["prices", "log"],
    ["card", "Sol Ring"],
    ["check", "vault"],
    ["check", "collection"],
]


@pytest.fixture(params=["", 'vault = ""\n'], ids=["no vault line", "an empty vault"])
def unset(request, tmp_path, monkeypatch) -> Path:
    """A config under a scratch HOME that names no vault, written either way; a price day and
    a collection kept, so prices log and check collection get as far as the vault. Returns
    the config's path."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    path = tmp_path / "config" / "riffle" / "config.toml"
    path.parent.mkdir(parents=True)
    path.write_text(f'{request.param}notes = "games/tcg"\ncard_images = "link"\n{UNREACHABLE}')
    scryfall.prices_dir().mkdir(parents=True)
    (scryfall.prices_dir() / "2026-09-26.jsonl.gz").write_bytes(b"")
    (config.data_dir() / "collection.csv").write_text(
        "Name,Set code,Collector number,Foil,Quantity\nLightning Bolt,m10,146,normal,4\n"
    )
    return path


def names_the_config(said: str, path: Path) -> bool:
    """Whether a message names the config file, by its ~ form or in full. Spaces are dropped
    first: an error box folds a long path over its lines, and plain() makes each fold a space."""
    squashed = said.replace(" ", "")
    return config.tilde(path) in squashed or str(path) in squashed


@pytest.mark.parametrize("command", COMMANDS, ids=" ".join)
def test_a_vault_command_says_what_to_add_and_exits_2_before_the_database(unset, plain, command):
    result = CliRunner().invoke(app, command)
    assert result.exit_code == 2, result.output
    said = plain(result.output)
    assert "no vault set:" in said and "riffle init" in said, said
    assert said.index("no vault set:") < said.index("riffle init")
    assert names_the_config(said, unset), said
    assert "Postgres" not in said  # the command stopped before it asked the database


def test_sync_watch_says_what_to_add_and_never_starts_watching(unset, plain, monkeypatch):
    resyncs: list[int] = []
    monkeypatch.setattr(cli, "_resync_and_keep_watching", lambda: resyncs.append(1))

    def stop(seconds):  # a watch that got past the check ends at its first nap
        raise KeyboardInterrupt

    monkeypatch.setattr("time.sleep", stop)
    result = CliRunner().invoke(app, ["sync", "--watch"])
    assert result.exit_code == 2, result.output
    said = plain(result.output)
    assert "no vault set:" in said and "riffle init" in said, said
    assert resyncs == []


def test_the_sync_runs_its_other_steps_and_fails_only_the_vault_step(unset, monkeypatch):
    monkeypatch.setattr(scryfall, "snapshot_prices", lambda: (Path("/d/2026-09-26.jsonl.gz"), False))
    network(monkeypatch, up_after=0)
    calls: list[str] = []
    online_steps(monkeypatch, calls)
    result = CliRunner().invoke(app, ["sync"])
    assert result.exit_code == 1, result.output
    assert calls == ONLINE  # the refresh, the catalog load and every price source still ran
    assert "! vault: no vault set:" in result.output and "riffle init" in result.output
    assert names_the_config(result.output, unset), result.output
    assert before_closing(result.output) == "1 step failed: vault"


def test_deck_completion_stays_quiet_without_a_vault(unset):
    assert cli._deck_names() == [] and cli._complete_deck("") == []


def test_the_default_config_names_no_vault():
    assert tomllib.loads(config.DEFAULT_CONFIG)["vault"] == ""


def test_a_config_without_a_vault_reads_as_none(unset):
    cfg = config.load()
    assert (cfg.vault, cfg.notes, cfg.mtg_dir) == (None, None, None)


def test_no_config_at_all_reads_as_no_vault(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "none"))
    assert config.load().vault is None


def test_a_vault_set_in_the_config_still_reads(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    (tmp_path / "config" / "riffle").mkdir(parents=True)
    (tmp_path / "config" / "riffle" / "config.toml").write_text('vault = "~/notes"\nnotes = "games/tcg"\n')
    cfg = config.load()
    assert cfg.vault == tmp_path / "notes"
    assert cfg.notes == tmp_path / "notes" / "games" / "tcg" and cfg.mtg_dir == cfg.notes / "mtg"


@pytest.mark.parametrize(
    "command",
    [
        ["legal", "{list}"],
        ["own", "{list}"],
        ["price", "{list}"],
        ["export", "{list}"],
        ["compare", "{list}", "{list}"],
    ],
    ids=lambda command: command[0],
)
def test_a_deck_given_by_its_file_needs_no_vault(unset, tmp_path, plain, command):
    listed = tmp_path / "list.txt"
    listed.write_text("4 Lightning Bolt\n", encoding="utf-8")
    result = CliRunner().invoke(app, [arg.format(list=listed) for arg in command])
    said = plain(result.output)
    assert "no vault set" not in said
    assert result.exit_code == 1 and "Postgres" in said, said  # it got as far as the database


def test_similar_needs_the_vault_whatever_the_deck(unset, tmp_path, plain):
    listed = tmp_path / "list.txt"
    listed.write_text("4 Lightning Bolt\n", encoding="utf-8")
    result = CliRunner().invoke(app, ["similar", str(listed)])
    assert result.exit_code == 2 and "no vault set:" in plain(result.output)


@pytest.mark.parametrize(
    ("value", "why"),
    [('"   "', None), ('"~nosuchuser-riffle/notes"', "'~nosuchuser-riffle/notes', a home no one has here")],
    ids=["blank", "no one's home"],
)
def test_a_vault_that_names_no_folder_reads_as_none(tmp_path, monkeypatch, value, why):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    config.write_default().write_text(f"vault = {value}\n", encoding="utf-8")
    cfg = config.load()
    assert (cfg.vault, cfg.notes) == (None, None)
    named = f"vault in {config.tilde(config.config_path())} is {why}; no vault is used"
    assert cfg.unread == ([named] if why else [])
