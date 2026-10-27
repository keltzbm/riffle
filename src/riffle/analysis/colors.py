"""Order Magic colors by WUBRG."""

from riffle import WUBRG


def wubrg_sort(colors: list[str]) -> list[str]:
    """Canonical order. Never use sorted()."""
    return [c for c in WUBRG if c in colors]
