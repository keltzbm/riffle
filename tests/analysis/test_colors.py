"""WUBRG ordering is the invariant most likely to regress."""

from riffle.analysis.colors import wubrg_sort


def test_wubrg_sort_is_not_alphabetical():
    assert wubrg_sort(["G", "B", "U"]) == ["U", "B", "G"]
