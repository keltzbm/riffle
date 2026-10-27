"""WUBRG ordering is the invariant most likely to regress."""

from riffle.analysis.colors import archetype_name, wubrg_sort


def test_wubrg_sort_is_not_alphabetical():
    assert wubrg_sort(["G", "B", "U"]) == ["U", "B", "G"]


def test_archetype_name_handles_unsorted_input():
    assert archetype_name(["G", "U"]) == "Simic"
    assert archetype_name(["B", "W", "U"]) == "Esper"


def test_four_color_names_survived_the_port():
    assert archetype_name(["W", "U", "B", "G"]) == "Witch"
