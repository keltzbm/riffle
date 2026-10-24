"""`riffle db up`: start the local Postgres with Docker Compose and wait until it's healthy;
and the start of a command run inside its container, for `riffle db backup` and `restore`.

compose.yaml sits at the repo root, and Riffle runs as an editable install from
that checkout, so the file is found from here. Compose reads the .env file beside
it: the password when it first creates the database, and at every `up` the address
Postgres publishes on and where its files live (RIFFLE_DB_ADDRESS, RIFFLE_DB_DATA).
"""

import shutil
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]  # src/riffle/db/compose.py -> the checkout
# Where Docker Desktop, OrbStack and Homebrew put docker on a Mac, which a launchd job's PATH
# (/usr/bin:/bin:/usr/sbin:/sbin) leaves out.
PLACES = ("/usr/local/bin/docker", "/opt/homebrew/bin/docker")
NO_DOCKER = (
    "docker isn't on PATH, in /usr/local/bin, or in /opt/homebrew/bin: install Docker Engine (Linux)"
    " or OrbStack or Docker Desktop (macOS), and start it"
)

Runner = Callable[[list[str]], subprocess.CompletedProcess]
Which = Callable[[str], str | None]


def _run(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(args)  # output goes straight to the terminal


def docker(which: Which = shutil.which, places: Sequence[str] = PLACES) -> str | None:
    """How to run docker: by its name when it's on PATH, else by its path in the first of the
    places where it is."""
    if which("docker") is not None:
        return "docker"
    return next((place for place in places if Path(place).is_file()), None)


def command(compose_file: Path, program: str = "docker") -> list[str]:
    return [program, "compose", "--file", str(compose_file), "up", "--detach", "--wait"]


def _found(repo: Path, which: Which, places: Sequence[str]) -> tuple[Path, str]:
    """compose.yaml and docker, or FileNotFoundError saying which is missing."""
    compose_file = repo / "compose.yaml"
    if not compose_file.exists():
        raise FileNotFoundError(f"no compose.yaml in {repo}: run Riffle from its git checkout")
    program = docker(which, places)
    if program is None:
        raise FileNotFoundError(NO_DOCKER)
    return compose_file, program


def up(
    repo: Path = REPO, run: Runner = _run, which: Which = shutil.which, places: Sequence[str] = PLACES
) -> int:
    """Start the database; the exit code is Compose's."""
    compose_file, program = _found(repo, which, places)
    return run(command(compose_file, program)).returncode


def in_db(repo: Path = REPO, which: Which = shutil.which, places: Sequence[str] = PLACES) -> list[str]:
    """The start of a command run inside the database's container, its input and output passed
    through as they are: -T gives it no terminal, which would mangle a binary dump."""
    compose_file, program = _found(repo, which, places)
    return [program, "compose", "--file", str(compose_file), "exec", "-T", "db"]
