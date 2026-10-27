"""Read JSON objects and replace files without exposing partial writes."""

import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


def read_json_object(path: Path) -> dict:
    """Read a JSON object, or return an empty dictionary when it cannot be read."""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


@contextmanager
def replacing(path: Path, *, temporary: Path | None = None, cleanup: bool = False) -> Iterator[Path]:
    """Replace a file only after the caller finishes writing its temporary file.

    Leave partial files by default. Callers that discard failed writes request
    cleanup; callers that need durability flush and sync before leaving the block.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    part = temporary if temporary is not None else path.with_name(path.name + ".part")
    try:
        yield part
        part.replace(path)
    finally:
        if cleanup:
            part.unlink(missing_ok=True)
