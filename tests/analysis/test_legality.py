import pytest

from riffle.analysis import legality
from riffle.ingest.local.decklist import parse_text


def _deck(text, fmt):
    d = parse_text(text, slug="t")
    d.meta["format"] = fmt
    return d


def _modern(extra=""):
    return _deck(f"Deck\n4 Lightning Bolt\n56 Forest\n{extra}", "modern")


def _messages(rep):
    return [(i.severity, i.card, i.message) for i in rep.issues]


def test_card_status_and_copy_limits(cat):
    assert legality.is_legal("o-bolt", "modern", cat)
    assert not legality.is_legal("o-bolt", "pioneer", cat)
    assert legality.is_legal("o-sol", "vintage", cat)  # restricted still counts as legal
    assert legality.copy_limit("o-sol", "vintage", cat) == 1
    assert legality.copy_limit("o-bolt", "modern", cat) == 4
    assert legality.copy_limit("o-bolt", "commander", cat) == 1
    assert legality.copy_limit("o-snowf", "commander", cat) is None  # snow basics are basics
    assert legality.copy_limit("o-rats", "commander", cat) is None  # "any number of cards named"


def test_playset_eligibility(cat):
    assert legality.playset_eligible("o-bolt", cat)
    assert not legality.playset_eligible("o-crypt", cat)  # vintage isn't a built format


def test_format_aliases():
    assert legality.normalize_format("EDH") == "commander"
    assert legality.normalize_format("Pauper Commander") == "paupercommander"
    assert legality.normalize_format("duel-commander") == "duel"


def test_legal_modern_deck(cat):
    rep = legality.check_deck(_modern("\nSideboard\n3 Relentless Rats"), cat)
    assert rep.legal, _messages(rep)


def test_modern_errors(cat):
    rep = legality.check_deck(_deck("Deck\n5 Lightning Bolt\n1 Sol Ring\n20 Forest\n", "modern"), cat)
    msgs = _messages(rep)
    assert ("error", "Lightning Bolt", "5 copies — max 4") in msgs
    assert ("error", "Sol Ring", "not legal in modern") in msgs
    assert any("needs at least 60" in m for _, _, m in msgs)


def test_sideboard_copies_count_toward_limit(cat):
    rep = legality.check_deck(_modern("\nSideboard\n1 Lightning Bolt"), cat)
    assert ("error", "Lightning Bolt", "5 copies — max 4") in _messages(rep)


def test_restricted(cat):
    rep = legality.check_deck(_deck("Deck\n2 Mana Crypt\n58 Forest\n", "vintage"), cat)
    assert ("error", "Mana Crypt", "2 copies — restricted") in _messages(rep)


def _commander(body, total_forests):
    return _deck(
        f"Commander\n1 Aesi, Tyrant of Gyre Strait\n\nDeck\n{body}{total_forests} Forest\n", "commander"
    )


def test_legal_commander_deck(cat):
    rep = legality.check_deck(_commander("1 Cyclonic Rift\n1 Sol Ring\n", 97), cat)
    assert rep.legal, _messages(rep)


def test_commander_errors(cat):
    rep = legality.check_deck(
        _commander("1 Lightning Bolt\n2 Sol Ring\n1 Mana Crypt\n30 Relentless Rats\n", 50), cat
    )
    msgs = _messages(rep)
    assert ("error", "Lightning Bolt", "outside color identity (R)") in msgs
    assert ("error", "Relentless Rats", "outside color identity (B)") in msgs
    assert ("error", "Sol Ring", "2 copies — max 1") in msgs
    assert ("error", "Mana Crypt", "banned in commander") in msgs
    assert ("error", None, "85 cards — commander decks are exactly 100") in msgs
    assert not any(c == "Relentless Rats" and "copies" in m for _, c, m in msgs)


def test_missing_commander_and_unmatched(cat):
    rep = legality.check_deck(_deck("Deck\n1 Not A Card\n99 Forest\n", "commander"), cat)
    msgs = _messages(rep)
    assert ("error", "Not A Card", "not matched to a card") in msgs
    assert ("error", None, "no commander — add a Commander section") in msgs


def test_unknown_format(cat):
    rep = legality.check_deck(_modern(), cat, fmt="jumpstart-ish")
    assert not rep.legal and "unknown format" in rep.issues[0].message


def test_a_card_without_rules_is_flagged_not_judged(cat, monkeypatch):
    monkeypatch.setattr(type(cat), "rules", lambda self, card_ids: {})
    rep = legality.check_deck(_modern(), cat)
    assert rep.legal and {i.message for i in rep.warnings} == {"no legality data"}


def test_one_odd_printing_cannot_hide_a_legal_card():
    from riffle.models import merge_legalities

    normal = {"commander": "legal", "vintage": "restricted", "modern": "not_legal"}
    gold_border = {"commander": "not_legal", "vintage": "not_legal", "modern": "not_legal"}
    assert merge_legalities([gold_border, normal]) == normal
    assert merge_legalities([normal, gold_border]) == normal  # order doesn't matter
    banned = {"commander": "banned"}
    assert merge_legalities([normal, banned])["commander"] == "banned"
    assert merge_legalities([]) == {}


# ---- more single-card rules ------------------------------------------------------


@pytest.mark.parametrize(
    "oid, fmt, limit",
    [
        ("o-dwarves", "modern", 7),
        ("o-dwarves", "commander", 7),  # "up to seven" beats the format's 4 or 1
        ("o-forest", "modern", None),
        ("o-forest", "commander", None),
        ("o-rats", "modern", None),
        ("o-crypt", "vintage", 1),
        ("o-sol", "vintage", 1),  # restricted
        ("o-bolt", "legacy", 4),  # no legacy key -> format default
        ("o-bolt", "made-up", 4),
    ],
)
def test_copy_limit_table(cat, oid, fmt, limit):
    assert legality.copy_limit(oid, fmt, cat) == limit


@pytest.mark.parametrize("card_id, limit", [("o-forest", None), ("o-bolt", 4)])
def test_copy_limits_fall_back_to_basic_status_without_rules(cat, monkeypatch, card_id, limit):
    monkeypatch.setattr(cat, "rules", lambda ids: {})
    assert legality.copy_limit(card_id, "modern", cat) == limit


@pytest.mark.parametrize(
    "oid, fmt, expected",
    [
        ("o-bolt", "modern", "legal"),
        ("o-bolt", "pioneer", "not_legal"),
        ("o-crypt", "commander", "banned"),
        ("o-sol", "vintage", "restricted"),
        ("o-bolt", "legacy", "unknown"),
        ("o-nothing", "modern", "unknown"),
        ("o-bolt", "EDH", "legal"),
        ("o-bolt", " Modern ", "legal"),  # aliases and whitespace
    ],
)
def test_status_table(cat, oid, fmt, expected):
    assert legality.status(oid, fmt, cat) == expected


@pytest.mark.parametrize(
    "raw, key",
    [
        ("Commander", "commander"),
        ("cEDH", "commander"),
        ("Duel Commander", "duel"),
        ("PDH", "paupercommander"),
        ("Historic Brawl", "brawl"),
        ("standard_brawl", "standardbrawl"),
        ("Penny Dreadful", "penny"),
        ("Old-School", "oldschool"),
    ],
)
def test_normalize_format_table(raw, key):
    assert legality.normalize_format(raw) == key


# ---- decks: constructed ------------------------------------------------------------


def test_basics_have_no_copy_limit_in_constructed(cat):
    assert legality.check_deck(_deck("Deck\n60 Forest\n", "modern"), cat).legal


def test_seven_dwarves(cat):
    ok = legality.check_deck(_deck("Deck\n7 Seven Dwarves\n53 Forest\n", "modern"), cat)
    assert ok.legal, _messages(ok)
    bad = legality.check_deck(_deck("Deck\n8 Seven Dwarves\n52 Forest\n", "modern"), cat)
    assert ("error", "Seven Dwarves", "8 copies — max 7") in _messages(bad)


def test_sideboard_limit_counts_companion(cat):
    extra = "\nSideboard\n15 Relentless Rats\n\nCompanion\n1 Lurrus of the Dream-Den"
    rep = legality.check_deck(_modern(extra), cat)
    assert ("error", None, "16 sideboard cards — max 15") in _messages(rep)


def test_commander_section_in_constructed_is_a_warning(cat):
    d = _deck("Commander\n1 Aesi, Tyrant of Gyre Strait\n\nDeck\n60 Forest\n", "modern")
    rep = legality.check_deck(d, cat)
    assert rep.legal
    assert any("no command zone" in i.message for i in rep.warnings)


def test_same_card_across_boards_reported_once(cat):
    rep = legality.check_deck(_deck("Deck\n3 Sol Ring\n57 Forest\n\nSideboard\n2 Sol Ring\n", "modern"), cat)
    sol = [m for s, c, m in _messages(rep) if c == "Sol Ring"]
    assert sol == ["not legal in modern", "5 copies — max 4"]


def test_every_listed_format_has_rules():
    for fmt in ("standard", "pioneer", "modern", "legacy", "vintage", "pauper", "commander", "oathbreaker"):
        assert fmt in legality.FORMAT_RULES


# ---- decks: commander ----------------------------------------------------------------


def test_partner_commanders_share_identity(cat):
    d = _deck(
        "Commander\n1 Tymna the Weaver\n1 Thrasios, Triton Hero\n\nDeck\n"
        "1 Cyclonic Rift\n1 Relentless Rats\n96 Forest\n",
        "commander",
    )
    rep = legality.check_deck(d, cat)
    assert rep.legal and not rep.warnings, _messages(rep)  # WUBG covers U and B cards


def test_two_commanders_without_partner_warns(cat):
    d = _deck(
        "Commander\n1 Aesi, Tyrant of Gyre Strait\n1 Tymna the Weaver\n\nDeck\n98 Forest\n", "commander"
    )
    rep = legality.check_deck(d, cat)
    assert any("partner" in i.message for i in rep.warnings)


def test_three_commanders_is_an_error(cat):
    d = _deck(
        "Commander\n1 Aesi, Tyrant of Gyre Strait\n1 Tymna the Weaver\n1 Thrasios, Triton Hero\n\n"
        "Deck\n97 Forest\n",
        "commander",
    )
    assert ("error", None, "3 commanders — at most 2") in _messages(legality.check_deck(d, cat))


def test_non_legendary_commander_warns(cat):
    d = _deck("Commander\n1 Sol Ring\n\nDeck\n99 Forest\n", "commander")
    rep = legality.check_deck(d, cat)
    assert any(c == "Sol Ring" and "legendary" in m for s, c, m in _messages(rep))


def test_commander_sideboard_is_ignored_with_warning(cat):
    rep = legality.check_deck(
        _deck(
            "Commander\n1 Aesi, Tyrant of Gyre Strait\n\nDeck\n99 Forest\n\nSideboard\n1 Lightning Bolt\n",
            "commander",
        ),
        cat,
    )
    assert rep.legal  # the off-color Bolt doesn't count
    assert any("sideboard cards ignored" in i.message for i in rep.warnings)


def test_companion_checked_for_identity_but_not_size(cat):
    d = _deck(
        "Commander\n1 Aesi, Tyrant of Gyre Strait\n\nDeck\n99 Forest\n\n"
        "Companion\n1 Lurrus of the Dream-Den\n",
        "commander",
    )
    msgs = _messages(legality.check_deck(d, cat))
    assert ("error", "Lurrus of the Dream-Den", "outside color identity (WB)") in msgs
    assert not any("cards — commander decks" in m for _, _, m in msgs)


def test_snow_basics_unlimited_in_commander(cat):
    rep = legality.check_deck(_commander("30 Snow-Covered Forest\n", 69), cat)
    assert rep.legal, _messages(rep)


def test_standard_brawl_is_sixty(cat):
    rep = legality.check_deck(
        _deck("Commander\n1 Aesi, Tyrant of Gyre Strait\n\nDeck\n99 Forest\n", "standardbrawl"), cat
    )
    assert ("error", None, "100 cards — standardbrawl decks are exactly 60") in _messages(rep)


def test_format_override_beats_note(cat):
    d = _commander("1 Cyclonic Rift\n", 98)
    assert legality.check_deck(d, cat).legal
    assert not legality.check_deck(d, cat, fmt="modern").legal  # Rift isn't modern-legal


def test_errors_and_warnings_split(cat):
    rep = legality.LegalityReport("x", "modern")
    rep.error("e")
    rep.warn("w")
    assert [i.message for i in rep.errors] == ["e"] and [i.message for i in rep.warnings] == ["w"]
    assert not rep.legal
