"""compose.yaml's two settings, RIFFLE_DB_ADDRESS and RIFFLE_DB_DATA: the file itself, how Compose
reads it with them unset and set, and the README that documents them."""

import json
import re
import shutil
import subprocess
from itertools import takewhile

import pytest

from riffle.db import compose
from tests.conftest import docker_env

COMPOSE_FILE = compose.REPO / "compose.yaml"  # the file riffle db up runs
README = compose.REPO / "README.md"
SETTINGS = ("RIFFLE_DB_ADDRESS", "RIFFLE_DB_DATA")
# the keys every Compose version renders for a port and a volume; a version adds others
PORT_KEYS = ("host_ip", "published", "target", "protocol")
VOLUME_KEYS = ("type", "source", "target")
# an address in Tailscale's range, 100.64.0.0/10
TAILSCALE = re.compile(r"\b100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d{1,3}\.\d{1,3}\b")


def test_compose_file_takes_the_address_and_data_folder_from_the_environment():
    """The published port and the data volume read the two settings, with 127.0.0.1 and the named
    volume as their defaults, and the volume is still declared under the same project name, so a
    machine with neither set keeps its data in tcg_pgdata."""
    lines = COMPOSE_FILE.read_text(encoding="utf-8").splitlines()
    assert "name: tcg" in lines
    assert '    ports: ["${RIFFLE_DB_ADDRESS:-127.0.0.1}:5432:5432"]' in lines
    assert '    volumes: ["${RIFFLE_DB_DATA:-pgdata}:/var/lib/postgresql"]' in lines
    assert "volumes:" in lines, "no top-level volumes block"
    declared = takewhile(lambda line: line.startswith(" "), lines[lines.index("volumes:") + 1 :])
    assert "pgdata:" in [line.split("#")[0].strip() for line in declared]


# ---- Compose's own reading ------------------------------------------------------------


@pytest.fixture
def render(tmp_path, monkeypatch):
    """Renders compose.yaml as Compose reads it, with the settings given written to an env file.
    The project directory is an empty temporary one and the env file its own, so the repository's
    .env, which holds a real password, is never read; the shell's values of the three variables
    are cleared first, since they would outrank the file's. Skips where Docker Compose isn't
    installed, as on CI's macOS runners."""
    for name in ("POSTGRES_PASSWORD", *SETTINGS):
        monkeypatch.delenv(name, raising=False)
    env = docker_env()
    if shutil.which("docker") is None:
        pytest.skip("docker isn't on PATH")
    version = subprocess.run(["docker", "compose", "version"], capture_output=True, text=True, env=env)
    if version.returncode != 0:
        pytest.skip(f"docker compose version failed: {(version.stderr or version.stdout).strip()}")

    def run(**settings: str) -> dict:
        env_file = tmp_path / "env"
        values = {"POSTGRES_PASSWORD": "dummy", **settings}
        env_file.write_text("".join(f"{name}={value}\n" for name, value in values.items()), encoding="utf-8")
        args = ["docker", "compose", "--file", str(COMPOSE_FILE), "--project-directory", str(tmp_path)]
        args += ["--env-file", str(env_file), "config", "--format", "json"]
        result = subprocess.run(args, capture_output=True, text=True, cwd=tmp_path, env=env)
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)

    return run


def _db(config: dict) -> tuple[dict, dict]:
    """The db service's one port and one volume, on the keys compared."""
    [port] = config["services"]["db"]["ports"]
    [volume] = config["services"]["db"]["volumes"]
    return {k: port[k] for k in PORT_KEYS}, {k: volume[k] for k in VOLUME_KEYS}


def _published(host_ip: str) -> dict:
    return {"host_ip": host_ip, "published": "5432", "target": 5432, "protocol": "tcp"}


def _mount(kind: str, source: str) -> dict:
    return {"type": kind, "source": source, "target": "/var/lib/postgresql"}


def test_compose_publishes_on_localhost_and_keeps_the_named_volume_with_nothing_set(render):
    """Unset, Compose publishes Postgres on 127.0.0.1 and keeps the data in the named volume, as it
    did before the settings existed: a Mac's `riffle db up` changes nothing."""
    config = render()
    port, volume = _db(config)
    assert port == _published("127.0.0.1")
    assert volume == _mount("volume", "pgdata")
    assert "pgdata" in config["volumes"]


def test_compose_publishes_on_the_address_and_mounts_the_folder_when_set(render, tmp_path):
    """Set, Compose publishes Postgres on that address alone and bind-mounts the folder in place of
    the named volume, so a server answers only where it's told and keeps the files on its own disk."""
    data = tmp_path / "data"
    port, volume = _db(render(RIFFLE_DB_ADDRESS="100.64.0.1", RIFFLE_DB_DATA=str(data)))
    assert port == _published("100.64.0.1")
    assert volume == _mount("bind", str(data))


# ---- the README ------------------------------------------------------------------------


def _database_section() -> str:
    """The README's Database section, its heading to the next."""
    match = re.search(r"^## Database\b.*?(?=^## |\Z)", README.read_text(encoding="utf-8"), re.M | re.S)
    assert match, "the README has no Database section"
    return match.group()


def _a_paragraph_names(section: str, *words: str) -> bool:
    return any(all(word in paragraph for word in words) for paragraph in section.split("\n\n"))


def test_the_readme_documents_both_settings_beside_their_defaults():
    """A reader of the Database section finds each setting with, in the same paragraph, what it is
    when unset."""
    section = _database_section()
    assert _a_paragraph_names(section, "RIFFLE_DB_ADDRESS", "127.0.0.1"), "RIFFLE_DB_ADDRESS and its default"
    assert _a_paragraph_names(section, "RIFFLE_DB_DATA", "pgdata"), "RIFFLE_DB_DATA and its default"


def test_the_readme_s_server_example_is_a_placeholder_not_a_machine():
    """The server example gives an address and a folder a reader replaces with their own, and no
    other Tailscale address is in the README, so it names no real machine."""
    section = _database_section()
    assert "RIFFLE_DB_ADDRESS=100.64.0.1" in section
    assert "RIFFLE_DB_DATA=/srv/postgres/riffle" in section
    assert set(TAILSCALE.findall(README.read_text(encoding="utf-8"))) == {"100.64.0.1"}
