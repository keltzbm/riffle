"""`riffle db up`: start the local Postgres with Docker Compose and wait until it's healthy.

compose.yaml sits at the repo root, and Riffle runs as an editable install from
that checkout, so the file is found from here. Compose reads the .env file beside
it: the password when it first creates the database, and at every `up` the address
Postgres publishes on and where its files live (RIFFLE_DB_ADDRESS, RIFFLE_DB_DATA).
"""

import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]  # src/riffle/db/compose.py -> the checkout

Runner = Callable[[list[str]], subprocess.CompletedProcess]


def _run(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(args)  # output goes straight to the terminal


def command(compose_file: Path) -> list[str]:
    return ["docker", "compose", "--file", str(compose_file), "up", "--detach", "--wait"]


def up(repo: Path = REPO, run: Runner = _run, which: Callable[[str], str | None] = shutil.which) -> int:
    """Start the database; the exit code is Compose's."""
    compose_file = repo / "compose.yaml"
    if not compose_file.exists():
        raise FileNotFoundError(f"no compose.yaml in {repo}: run Riffle from its git checkout")
    if which("docker") is None:
        raise FileNotFoundError(
            "docker isn't on PATH: install Docker Engine (Linux) or OrbStack or Docker Desktop (macOS),"
            " and start it"
        )
    return run(command(compose_file)).returncode
