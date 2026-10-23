"""`riffle init`: the vault asked for on a terminal or given with --vault, and written into the
config; in a config that exists, nothing but its vault line changes."""

import io
import sys
import tomllib
from pathlib import Path

import pytest
from typer.testing import CliRunner

from riffle import cli, config
from riffle.cli import app

QUESTION = "Where is your Obsidian vault?"
EXISTING = (
    "# riffle configuration\n"
    'vault = ""\n'
    "# the folder in the vault that holds a folder per game\n"
    'notes = "games/tcg"\n'
    "\n"
    'database_url = "postgresql+psycopg://tcg@127.0.0.1:1/unreachable"\n'
    'card_images = "link"  # Scryfall\'s pictures\n'
)


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    """A scratch HOME with no config yet; returns where the config goes."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / ".local" / "share"))
    return tmp_path / ".config" / "riffle" / "config.toml"


@pytest.fixture
def folder(tmp_path) -> Path:
    """A folder that exists, for the vault."""
    path = tmp_path / "notes"
    path.mkdir()
    return path


def init(*args: str, **kw):
    return CliRunner().invoke(app, ["init", *args], **kw)


def vault_in(path: Path) -> Path | None:
    """The vault the config names, expanded; None for an empty one."""
    value = tomllib.loads(path.read_text(encoding="utf-8"))["vault"]
    return Path(value).expanduser() if value else None


def shown(output: str, key: str) -> Path:
    """The path init printed on its `key` line, expanded."""
    line = next(line for line in output.splitlines() if line.split()[:1] == [key])
    return Path(line.split(maxsplit=1)[1].strip()).expanduser()


def squashed(said: str) -> str:
    """A message without its spaces: an error box folds a long path over its lines."""
    return said.replace(" ", "")


def test_without_a_terminal_or_vault_the_config_is_written_empty_and_says_what_to_add(home, plain):
    result = init()
    assert result.exit_code in (0, 2), result.output  # a crash would be 1
    assert QUESTION not in result.output
    assert vault_in(home) is None
    assert set(tomllib.loads(config.DEFAULT_CONFIG)) <= set(tomllib.loads(home.read_text(encoding="utf-8")))
    said = plain(result.output)
    assert "no vault set:" in said and "riffle init" in said, said
    assert config.tilde(home) in squashed(said) or str(home) in squashed(said), said


def test_with_vault_the_config_names_the_folder_and_init_shows_it(home, folder):
    result = init("--vault", str(folder))
    assert result.exit_code == 0, result.output
    assert QUESTION not in result.output and "no vault set" not in result.output
    assert vault_in(home) == folder
    assert shown(result.output, "vault") == folder
    assert shown(result.output, "notes") == folder / "games" / "tcg"


def test_on_a_terminal_init_asks_for_the_vault_and_writes_the_answer(home, folder, monkeypatch):
    monkeypatch.setattr(cli, "_interactive", lambda: True)
    result = init(input=f"{folder}\n")
    assert result.exit_code == 0, result.output
    assert QUESTION in result.output
    assert vault_in(home) == folder
    assert shown(result.output, "vault") == folder


def test_init_asks_nothing_when_the_vault_is_set_or_given(home, folder, tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "_interactive", lambda: True)
    home.parent.mkdir(parents=True)
    home.write_text(f'vault = "{folder}"\nnotes = "games/tcg"\n', encoding="utf-8")
    before = home.read_text(encoding="utf-8")
    result = init()  # no input: a question would end in EOF and an abort
    assert result.exit_code == 0, result.output
    assert QUESTION not in result.output and home.read_text(encoding="utf-8") == before
    other = tmp_path / "other"
    other.mkdir()
    result = init("--vault", str(other))
    assert result.exit_code == 0, result.output
    assert QUESTION not in result.output and vault_in(home) == other


@pytest.mark.parametrize("line", ['vault = ""', 'vault = "~/elsewhere"'])
def test_an_existing_config_keeps_every_other_line_where_it_was(home, folder, line):
    home.parent.mkdir(parents=True)
    text = EXISTING.replace('vault = ""', line)
    home.write_text(text, encoding="utf-8")
    result = init("--vault", str(folder))
    assert result.exit_code == 0, result.output
    before, after = text.splitlines(), home.read_text(encoding="utf-8").splitlines()
    assert len(after) == len(before)
    assert [line for n, line in enumerate(after) if n != 1] == [
        line for n, line in enumerate(before) if n != 1
    ]
    assert Path(tomllib.loads(after[1])["vault"]).expanduser() == folder


def test_a_config_without_a_vault_line_gets_one_and_keeps_the_rest(home, folder):
    home.parent.mkdir(parents=True)
    text = '# my settings\nnotes = "games/tcg"\n# the database\ndatabase_url = "postgresql+psycopg://tcg@h/tcg"\n'
    home.write_text(text, encoding="utf-8")
    result = init("--vault", str(folder))
    assert result.exit_code == 0, result.output
    assert vault_in(home) == folder
    rest = iter(home.read_text(encoding="utf-8").splitlines())
    assert all(line in rest for line in text.splitlines())  # the old lines, in their order


@pytest.mark.parametrize("what", ["missing", "a file"])
def test_a_path_that_isn_t_a_folder_is_refused(home, tmp_path, plain, what):
    path = tmp_path / "nowhere"
    if what == "a file":
        path.write_text("", encoding="utf-8")
    result = init("--vault", str(path))
    assert result.exit_code == 2, result.output
    assert squashed(f"no folder at {path}") in squashed(plain(result.output)), result.output
    assert not home.exists() or "nowhere" not in home.read_text(encoding="utf-8")


def test_an_empty_vault_is_refused_not_read_as_this_folder(home, plain):
    result = init("--vault", "")
    assert result.exit_code == 2, result.output
    assert "no folder given" in plain(result.output)
    assert not home.exists()


def test_a_vault_key_in_a_table_isn_t_the_config_s_own(home, folder):
    home.parent.mkdir(parents=True)
    home.write_text('notes = "games/tcg"\n[backup]\nvault = "x"\n', encoding="utf-8")
    result = init("--vault", str(folder))
    assert result.exit_code == 0, result.output
    assert vault_in(home) == folder
    assert tomllib.loads(home.read_text(encoding="utf-8"))["backup"] == {"vault": "x"}


def test_a_quoted_vault_key_is_rewritten_where_it_is(home, folder):
    home.parent.mkdir(parents=True)
    home.write_text('"vault" = ""\nnotes = "games/tcg"\n', encoding="utf-8")
    result = init("--vault", str(folder))
    assert result.exit_code == 0, result.output
    after = home.read_text(encoding="utf-8").splitlines()
    assert len(after) == 2 and after[1] == 'notes = "games/tcg"'
    assert vault_in(home) == folder


def test_a_config_that_can_t_take_a_vault_line_is_left_alone(home, folder, plain):
    home.parent.mkdir(parents=True)
    home.write_text('vault.where = "x"\n', encoding="utf-8")  # vault is a table here
    result = init("--vault", str(folder))
    assert result.exit_code == 2, result.output
    assert "can't take a vault line as it is" in plain(result.output)
    assert home.read_text(encoding="utf-8") == 'vault.where = "x"\n'


def test_each_line_keeps_its_own_ending(home, folder):
    home.parent.mkdir(parents=True)
    home.write_bytes(b'# c\r\nvault = ""\r\nnotes = "games/tcg"\r\n')
    result = init("--vault", str(folder))
    assert result.exit_code == 0, result.output
    assert home.read_bytes() == b'# c\r\nvault = "~/notes"\r\nnotes = "games/tcg"\r\n'


def test_a_home_no_one_has_here_is_refused(home, plain):
    result = init("--vault", "~nosuchuser-riffle/notes")
    assert result.exit_code == 2, result.output
    assert "no folder at ~nosuchuser-riffle/notes" in plain(result.output)


class _Terminal(io.StringIO):
    def isatty(self) -> bool:
        return True


def test_interactive_means_stdin_is_a_terminal(monkeypatch):
    monkeypatch.setattr(sys, "stdin", _Terminal())
    assert cli._interactive() is True
    monkeypatch.setattr(sys, "stdin", io.StringIO())
    assert cli._interactive() is False
