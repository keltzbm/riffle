"""Locks between runs of the same job: a launchd job and a run by hand never overlap."""

import fcntl
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def held(path: Path) -> Iterator[bool]:
    """Whether this run holds path's lock, until the block ends. Never waits: a run that finds
    it held has another run doing its work."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        yield True
