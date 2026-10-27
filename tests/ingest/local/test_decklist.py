from riffle.ingest.local.decklist import parse_text


def test_sections_quantities_and_pinned_printings():
    d = parse_text(
        "Commander\n1 Aesi, Tyrant of Gyre Strait\n\n"
        "Deck\n1x Sol Ring (M3C) 283 *F*\n2 Snow-Covered Forest\n// note\n"
    )
    assert [(e.board, e.quantity, e.name) for e in d.entries] == [
        ("commander", 1, "Aesi, Tyrant of Gyre Strait"),
        ("main", 1, "Sol Ring"),
        ("main", 2, "Snow-Covered Forest"),
    ]
    assert (d.entries[1].set_code, d.entries[1].collector_number) == ("M3C", "283")


def test_mtgo_blank_line_starts_sideboard_without_headers():
    d = parse_text("4 Sol Ring\n\n1 Cyclonic Rift\n")
    assert [e.board for e in d.entries] == ["main", "sideboard"]


def test_blank_lines_inside_headed_lists_do_not_switch_boards():
    d = parse_text("Deck\n1 Sol Ring\n\n1 Cyclonic Rift\n")
    assert {e.board for e in d.entries} == {"main"}


def test_names_with_commas_and_split_cards():
    d = parse_text("1 Fire // Ice\n1 Yavimaya, Cradle of Growth\n")
    assert [e.name for e in d.entries] == ["Fire // Ice", "Yavimaya, Cradle of Growth"]
