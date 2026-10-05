"""Card links and the card notes behind them (export/links.py, export/cards.py)."""

import re
from collections import Counter
from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest

from riffle import net
from riffle.export import cards
from riffle.export.links import card_link, safe_name, targets
from riffle.models import Holding, Prices

SRC = Path(__file__).parent.parent / "src" / "riffle"


# ---- the link form ----------------------------------------------------------------------
# The same cases as the vault's deckdata.link(), which writes the form by hand until the deck
# tools move into Riffle.


@pytest.mark.parametrize(
    ("name", "link", "in_table"),
    [
        ("Sol Ring", "[[Sol Ring]]", "[[Sol Ring]]"),
        ("Fire // Ice", "[[Fire|Fire // Ice]]", "[[Fire\\|Fire // Ice]]"),
        ("Summon: Bahamut", "[[Summon Bahamut|Summon: Bahamut]]", "[[Summon Bahamut\\|Summon: Bahamut]]"),
        ('Henzie "Toolbox" Torre', '[[Henzie Toolbox Torre|Henzie "Toolbox" Torre]]', None),
        ("Summon: Choco/Mog", "[[Summon ChocoMog|Summon: Choco/Mog]]", None),
        (
            "Delver of Secrets // Insectile Aberration",
            "[[Delver of Secrets|Delver of Secrets // Insectile Aberration]]",
            None,
        ),
    ],
)
def test_a_card_links_by_its_safe_name_under_its_own(name, link, in_table):
    assert card_link(name) == link
    assert card_link(name, table=True) == (in_table or link.replace("|", "\\|"))
    assert safe_name(name) == link[2:].split("|")[0].removesuffix("]]")


def test_targets_are_read_in_every_form_a_note_writes():
    text = "[[Sol Ring]], [[Fire\\|Fire // Ice]] | [[Doomsday#Combo|it]] ![[aesi-lands-data]] [[ Bolt ]]"
    assert targets(text) == ["Sol Ring", "Fire", "Doomsday", "aesi-lands-data", "Bolt"]


def test_no_other_code_writes_a_link():
    """The link form lives in links.py alone: anything else that writes one goes through it."""
    found = [
        f"{p.relative_to(SRC)}:{n}"
        for p in SRC.rglob("*.py")
        if p.name != "links.py"
        for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
        if re.search(r"\[\[\{", line)
    ]
    assert found == []


# ---- which cards get notes --------------------------------------------------------------


def test_gather_counts_the_notes_that_link_each_target(tmp_path):
    (tmp_path / "a.md").write_text("[[Sol Ring]] and [[Sol Ring]], [[Fire|Fire // Ice]]")
    (tmp_path / "b.md").write_text("[[Sol Ring]]")
    (tmp_path / "bad.md").write_bytes(b"\xff[[Mana Crypt]]")  # not UTF-8: the sync reports it
    found = cards.gather(sorted(tmp_path.iterdir()))
    assert found == Counter({"Sol Ring": 2, "Fire": 1})


def test_sort_names_each_card_s_note_and_what_can_t_open_it(cat):
    found = Counter(
        {
            "Sol Ring": 2,
            "sol ring": 1,  # Obsidian matches without regard to case
            "Summon: Bahamut": 3,  # a colon: no note can have the name
            "Summon Bahamut": 1,
            "Lightning Bolt": 1,  # a note in the vault has the name: the link opens it, not a card
            "Fire // Ice": 1,  # a note has Fire // Ice's note's name
            "aesi-lands-data": 1,  # not a card
        }
    )
    names = {"lightning bolt": Path("decks/Lightning Bolt.md"), "fire": Path("Fire.md")}
    # Riffle's own: a note for the card, but nothing to report, since no one edits a version log
    own = ["Sol Ring", "Murderous Rider // Swift End", "Fire // Ice"]
    links = cards.sort(found, cat, names, own)
    assert links.cards == {"o-sol": "Sol Ring", "o-summon": "Summon Bahamut", "o-rider": "Murderous Rider"}
    assert links.misses == {"Summon: Bahamut": ("[[Summon Bahamut|Summon: Bahamut]]", 3)}
    assert links.taken == {"Fire": Path("Fire.md")}
    assert links.clashes == {}
    assert cards.sort(Counter(), cat, names, own).taken == {}


def test_a_note_belongs_to_the_card_its_name_resolves_to(cat, monkeypatch):
    """Bind // Liberate's note would be Bind, and Bind is a card of its own: it gets the note."""
    real = cat.resolve
    monkeypatch.setattr(cat, "resolve", lambda name: "o-bolt" if name == "Fire" else real(name))
    links = cards.sort(Counter({"Fire // Ice": 1, "Fire": 1}), cat, {})
    assert links.cards == {"o-bolt": "Lightning Bolt"}
    assert links.clashes == {"Fire // Ice": "Lightning Bolt"}
    monkeypatch.setattr(cat, "resolve", lambda name: None if name == "Fire" else real(name))
    assert cards.sort(Counter({"Fire // Ice": 1}), cat, {}).clashes == {"Fire // Ice": "Fire"}
    assert cards.sort(Counter(), cat, {}, own=["Fire // Ice"]).clashes == {}  # Riffle's own link


# ---- what a note holds ------------------------------------------------------------------


def links_to(*card_ids: str) -> cards.Links:
    from tests.conftest import CARDS

    return cards.Links(cards={c: safe_name(CARDS[c][0]) for c in card_ids})


def test_a_note_for_each_linked_card_written_once(tmp_path, cat):
    links = links_to("o-sol", "o-fire", "o-delver")
    res = cards.write(tmp_path, links, cat, [], ["commander", "modern", "cube"], pictures="link")
    assert (res.cards, res.written, res.removed) == (3, 3, 0)
    folder = tmp_path / "cards"
    assert sorted(p.name for p in folder.iterdir()) == ["Delver of Secrets.md", "Fire.md", "Sol Ring.md"]
    sol = (folder / "Sol Ring.md").read_text()
    assert sol.startswith(
        "---\ntype: card\ngame: mtg\n---\n\n![Sol Ring](https://cards.scryfall.io/normal/front/s-o-sol.jpg)\n"
    )
    assert "aliases" not in sol
    assert (
        "[Scryfall](https://scryfall.com/card/tst/1) · [EDHREC](https://edhrec.com/route/?cc=Sol+Ring)\n"
        in sol
    )
    assert "**Sol Ring**  \nArtifact  \n{T}: Add {C}{C}.\n" in sol
    assert (
        "Price on 2026-09-21: $1.00 paper, 0.05 tix "
        "(Scryfall's, at the cheapest printing that can be played).\n" in sol
    )
    assert "Legal in commander. Not legal in modern.\n" in sol  # cube isn't a format Scryfall knows
    assert "Not owned.\n" in sol and sol.endswith("edits here are overwritten\n")

    again = cards.write(tmp_path, links, cat, [], ["commander", "modern"], pictures="link")
    assert (again.written, again.removed) == (0, 0)


def test_a_card_of_two_faces_shows_both_under_its_full_name(tmp_path, cat):
    cards.write(tmp_path, links_to("o-fire", "o-delver"), cat, [], [], pictures="link")
    delver = (tmp_path / "cards" / "Delver of Secrets.md").read_text()
    assert 'aliases: ["Delver of Secrets // Insectile Aberration"]' in delver
    assert "![Delver of Secrets](https://cards.scryfall.io/normal/front/s-delver.jpg)\n" in delver
    assert "![Insectile Aberration](https://cards.scryfall.io/normal/back/s-delver.jpg)\n" in delver
    assert "**Delver of Secrets** {U}  \nCreature — Human Wizard · 1/1  \nTransform it." in delver
    assert "**Insectile Aberration**  \nCreature — Human Insect · 3/2  \nFlying" in delver
    fire = (tmp_path / "cards" / "Fire.md").read_text()
    assert "[Gatherer](https://gatherer.wizards.com/Pages/Card/Details.aspx?multiverseid=599089)" in fire
    assert "**Ice** {1}{U}  \nInstant  \nTap.  \nDraw." in fire
    assert "Legal in" not in fire  # no lists, so no formats to say


def test_a_card_printed_only_in_foil_has_no_picture(tmp_path, cat):
    cards.write(tmp_path, links_to("o-crypt"), cat, [], [], pictures="link")
    crypt = (tmp_path / "cards" / "Mana Crypt.md").read_text()
    assert "![" not in crypt and "https://scryfall.com/search?q=%21%22Mana+Crypt%22" in crypt


def test_the_copies_owned_are_listed_by_printing(tmp_path, cat):
    held = [
        Holding("Sol Ring", 2, set_code="cmm", collector_number="410", card_id="o-sol"),
        Holding("Sol Ring", 1, set_code="2xm", collector_number="270", foil=True, card_id="o-sol"),
        Holding("Sol Ring", 1, card_id="o-sol"),
        Holding("Not A Card", 1),
    ]
    cards.write(tmp_path, links_to("o-sol"), cat, held, [], pictures="off")
    assert (
        "Owned: 4 (1 2XM 270 foil, 2 CMM 410, 1 printing unknown).\n"
        in (tmp_path / "cards" / "Sol Ring.md").read_text()
    )


def test_the_price_keeps_its_day_until_it_moves_by_a_cent(tmp_path, cat, monkeypatch):
    note = tmp_path / "cards" / "Sol Ring.md"
    cards.write(tmp_path, links_to("o-sol"), cat, [], [], pictures="off")
    cat.day = date(2026, 9, 22)
    assert cards.write(tmp_path, links_to("o-sol"), cat, [], [], pictures="off").written == 0
    assert "Price on 2026-09-21: $1.00 paper" in note.read_text()
    monkeypatch.setattr(cat, "prices", lambda ids: {"o-sol": Prices(1.01, 0.05)})
    assert cards.write(tmp_path, links_to("o-sol"), cat, [], [], pictures="off").written == 1
    assert "Price on 2026-09-22: $1.01 paper, 0.05 tix" in note.read_text()


@pytest.mark.parametrize(
    ("prices", "said"),
    [
        (Prices(1.5, None), "$1.50 paper, not on MTGO"),
        (Prices(None, 0.2), "no paper price, 0.20 tix"),
        (None, "no paper price, not on MTGO"),
    ],
)
def test_a_price_says_what_is_missing(prices, said):
    assert cards.price_line(prices, "2026-09-21").startswith(f"Price on 2026-09-21: {said} (")


def test_no_price_line_before_the_catalog_holds_a_day(tmp_path, cat):
    cat.day = None
    cards.write(tmp_path, links_to("o-sol"), cat, [], [], pictures="off")
    assert "Price" not in (tmp_path / "cards" / "Sol Ring.md").read_text()


def test_a_card_no_longer_linked_loses_its_note_and_a_name_in_another_case_is_taken_over(tmp_path, cat):
    folder = tmp_path / "cards"
    folder.mkdir()
    (folder / "sol ring.md").write_text("written by the vault's script")
    (folder / "Cyclonic Rift.md").write_text("old")
    (folder / "README.txt").write_text("not a note")
    res = cards.write(tmp_path, links_to("o-sol"), cat, [], [], pictures="off")
    assert (res.written, res.removed) == (1, 1)
    assert sorted(p.name for p in folder.iterdir()) == ["README.txt", "Sol Ring.md"]
    assert "type: card" in (folder / "Sol Ring.md").read_text()


def test_off_removes_the_folder(tmp_path, cat):
    assert cards.remove(tmp_path) == 0
    cards.write(tmp_path, links_to("o-sol", "o-rift"), cat, [], [], pictures="link")
    assert cards.remove(tmp_path) == 2
    assert not (tmp_path / "cards").exists()


# ---- pictures ---------------------------------------------------------------------------


def fetcher(log: list[str], fail: set[str] = frozenset()):
    def fetch(url, dest):
        log.append(url)
        if any(f in url for f in fail):
            raise net.FetchError("no answer")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"jpg")
        return 3

    return fetch


def test_cache_keeps_each_picture_once_and_links_the_copy(tmp_path, cat):
    fetched: list[str] = []
    progress: list[tuple[int, int | None]] = []
    links = links_to("o-sol", "o-delver")
    res = cards.write(
        tmp_path, links, cat, [], [], "cache", fetcher(fetched), lambda d, t: progress.append((d, t))
    )
    assert (res.fetched, res.unfetched) == (3, 0)
    assert sorted(fetched) == [
        "https://cards.scryfall.io/normal/back/s-delver.jpg",
        "https://cards.scryfall.io/normal/front/s-delver.jpg",
        "https://cards.scryfall.io/normal/front/s-o-sol.jpg",
    ]
    assert sorted(progress) == [(1, 3), (2, 3), (3, 3)]
    img = tmp_path / "cards" / "img"
    assert sorted(p.name for p in img.iterdir()) == ["s-delver-2.jpg", "s-delver.jpg", "s-o-sol.jpg"]
    delver = (tmp_path / "cards" / "Delver of Secrets.md").read_text()
    assert "![Delver of Secrets](img/s-delver.jpg)\n![Insectile Aberration](img/s-delver-2.jpg)\n" in delver

    again = cards.write(tmp_path, links_to("o-sol"), cat, [], [], "cache", fetcher(fetched))
    assert (again.fetched, again.written, again.removed) == (0, 0, 1) and len(fetched) == 3
    assert [p.name for p in img.iterdir()] == ["s-o-sol.jpg"]  # Delver's pictures went with its note

    cards.write(tmp_path, links_to("o-sol"), cat, [], [], "link")
    assert not img.exists()  # linked from Scryfall now: no copies kept


def test_a_picture_not_fetched_yet_is_linked_from_scryfall(tmp_path, cat):
    res = cards.write(tmp_path, links_to("o-sol"), cat, [], [], "cache", fetch=None)  # offline
    assert (res.fetched, res.unfetched) == (0, 1)
    assert (
        "](https://cards.scryfall.io/normal/front/s-o-sol.jpg)"
        in (tmp_path / "cards" / "Sol Ring.md").read_text()
    )
    res = cards.write(tmp_path, links_to("o-sol"), cat, [], [], "cache", fetcher([]))
    assert (res.fetched, res.written) == (1, 1)
    assert "](img/s-o-sol.jpg)" in (tmp_path / "cards" / "Sol Ring.md").read_text()


def test_fetching_stops_after_a_few_failures_and_the_rest_wait(tmp_path, cat, monkeypatch):
    monkeypatch.setattr(cards, "WORKERS", 1)  # one at a time, so the count is exact
    fetched: list[str] = []
    every = links_to(*[c for c in ("o-sol", "o-rift", "o-forest", "o-snowf", "o-aesi", "o-bolt", "o-rats")])
    res = cards.write(tmp_path, every, cat, [], [], "cache", fetcher(fetched, fail={"cards.scryfall.io"}))
    assert len(fetched) == cards.GIVE_UP
    assert (res.fetched, res.unfetched, res.why) == (0, 7, "no answer")


def test_a_picture_that_isnt_there_counts_as_not_fetched(tmp_path, cat):
    res = cards.write(tmp_path, links_to("o-sol"), cat, [], [], "cache", lambda url, dest: None)
    assert (res.fetched, res.unfetched) == (0, 1)
    assert res.why == "https://cards.scryfall.io/normal/front/s-o-sol.jpg isn't there"


def test_pictures_off_writes_none(tmp_path, cat):
    cards.write(tmp_path, links_to("o-sol"), cat, [], [], "off")
    assert "![" not in (tmp_path / "cards" / "Sol Ring.md").read_text()


def test_a_card_without_a_view_still_gets_its_links(cat):
    view = cat.card_views(["o-sol"])["o-sol"]
    assert cards.pages("Sol Ring", None).startswith("[Scryfall](https://scryfall.com/search?q=")
    assert cards.picture_files(None) == []
    assert cards.note("Sol Ring", "Sol Ring", replace(view, faces=()), [], []).count("**") == 0


def test_a_note_named_with_decomposed_accents_is_taken_over_not_deleted(tmp_path, cat):
    """macOS opens "Andúril" whether its ú is one character or u and an accent; Python's strings
    differ. The old script's notes had the second form: the sync wrote each, then pruned it."""
    import unicodedata

    folder = tmp_path / "cards"
    folder.mkdir()
    (folder / unicodedata.normalize("NFD", "Andúril, Flame of the West.md")).write_text(
        "old", encoding="utf-8"
    )
    res = cards.write(tmp_path, links_to("o-anduril"), cat, [], [], pictures="off")
    assert (res.written, res.removed) == (1, 0)
    assert [unicodedata.normalize("NFC", p.name) for p in folder.iterdir()] == [
        "Andúril, Flame of the West.md"
    ]
    assert "type: card" in (folder / "Andúril, Flame of the West.md").read_text(encoding="utf-8")


def test_targets_and_note_names_compare_accents_composed(tmp_path):
    import unicodedata

    from riffle import vault
    from riffle.export.links import note_key

    nfd = unicodedata.normalize("NFD", "Andúril")
    assert targets(f"[[{nfd}]]") == ["Andúril"]
    assert note_key(nfd) == note_key("ANDÚRIL") == "andúril"
    (tmp_path / f"{nfd}.md").write_text("", encoding="utf-8")
    assert list(vault.names(tmp_path)) == ["andúril"]
