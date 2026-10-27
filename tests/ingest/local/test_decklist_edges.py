"""Decklist parsing against the messy text real exporters produce."""

import pytest

from riffle.ingest.local.decklist import load, parse_text


def _rows(text):
    return [(e.board, e.quantity, e.name) for e in parse_text(text).entries]


@pytest.mark.parametrize(
    "line, qty, name, set_code, num",
    [
        ("1 Sol Ring", 1, "Sol Ring", None, None),
        ("1x Sol Ring", 1, "Sol Ring", None, None),
        ("12 Forest", 12, "Forest", None, None),
        ("  4   Lightning Bolt  ", 4, "Lightning Bolt", None, None),
        ("1\tSol Ring", 1, "Sol Ring", None, None),
        ("1 Sol Ring (M3C) 283", 1, "Sol Ring", "M3C", "283"),
        ("1 Sol Ring (m3c) 283", 1, "Sol Ring", "M3C", "283"),
        ("1 Sol Ring (M3C) 283 *F*", 1, "Sol Ring", "M3C", "283"),
        ("1 Sol Ring (M3C) 283 *E*", 1, "Sol Ring", "M3C", "283"),
        ("1 Sol Ring (PLST) C21-263", 1, "Sol Ring", "PLST", "C21-263"),
        ("1 Sol Ring (SLD) 1512★", 1, "Sol Ring", "SLD", "1512★"),
        ("1 Sol Ring (M3C)", 1, "Sol Ring", "M3C", None),
        ("1 Fire // Ice", 1, "Fire // Ice", None, None),
        ("1 Yavimaya, Cradle of Growth", 1, "Yavimaya, Cradle of Growth", None, None),
        ("1 Y'shtola, Night's Blessed", 1, "Y'shtola, Night's Blessed", None, None),
        ("1 B.F.M. (Big Furry Monster)", 1, "B.F.M. (Big Furry Monster)", None, None),
        ("1 Borrowing 100,000 Arrows", 1, "Borrowing 100,000 Arrows", None, None),
    ],
)
def test_line_shapes(line, qty, name, set_code, num):
    (e,) = parse_text(line).entries
    assert (e.quantity, e.name, e.set_code, e.collector_number) == (qty, name, set_code, num)


@pytest.mark.parametrize(
    "header, board",
    [
        ("Commander", "commander"),
        ("COMMANDER", "commander"),
        ("Commanders:", "commander"),
        ("Deck", "main"),
        ("Mainboard", "main"),
        ("Main", "main"),
        ("Maindeck:", "main"),
        ("Sideboard", "sideboard"),
        ("SIDEBOARD:", "sideboard"),
        ("Companion", "companion"),
    ],
)
def test_headers(header, board):
    assert _rows(f"{header}\n1 Sol Ring\n") == [(board, 1, "Sol Ring")]


@pytest.mark.parametrize("section", ["Maybeboard", "MAYBEBOARD:", "Considering", "Tokens", "Maybe"])
def test_skipped_sections_never_count(section):
    text = f"Deck\n1 Sol Ring\n\n{section}\n1 Cyclonic Rift\n\nSideboard\n1 Forest\n"
    assert _rows(text) == [("main", 1, "Sol Ring"), ("sideboard", 1, "Forest")]


def test_byte_order_mark_does_not_eat_the_first_card():
    assert _rows("\ufeff1 Sol Ring\n2 Forest\n") == [("main", 1, "Sol Ring"), ("main", 2, "Forest")]


def test_load_reads_windows_files(tmp_path):
    p = tmp_path / "list.txt"
    p.write_bytes("\ufeff4 Lightning Bolt\r\n\r\n2 Duress\r\n".encode("utf-8"))
    d = load(p)
    assert d.slug == "list"
    assert [(e.board, e.quantity, e.name) for e in d.entries] == [
        ("main", 4, "Lightning Bolt"),
        ("sideboard", 2, "Duress"),
    ]


def test_zero_quantity_lines_are_ignored():
    assert _rows("0 Sol Ring\n1 Forest\n") == [("main", 1, "Forest")]


def test_comments_and_noise_are_ignored():
    text = "# my list\n// comment\nAbout\nName My Deck\n\nDeck\n4 Sol Ring\nnot a card line\n"
    assert _rows(text) == [("main", 4, "Sol Ring")]


def test_mtgo_style_first_blank_line_starts_sideboard_later_ones_dont_matter():
    text = "4 Bolt\n\n\n2 Duress\n\n1 Negate\n"
    assert _rows(text) == [("main", 4, "Bolt"), ("sideboard", 2, "Duress"), ("sideboard", 1, "Negate")]


def test_leading_blank_lines_do_not_start_a_sideboard():
    assert _rows("\n\n4 Bolt\n") == [("main", 4, "Bolt")]


def test_duplicate_lines_are_kept_separately():
    """Two printings of one card are two entries; counting sums them later."""
    d = parse_text("1 Sol Ring (M3C) 283\n1 Sol Ring (CMR) 472\n")
    assert [(e.set_code, e.quantity) for e in d.entries] == [("M3C", 1), ("CMR", 1)]
    assert d.count() == 2


def test_empty_input():
    assert parse_text("").entries == []
    assert parse_text("\n\n  \n").entries == []
