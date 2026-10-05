"""Shared fixtures: an in-memory Catalog (no database, no download) and a test Postgres."""

import os
import re
import time
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date
from types import SimpleNamespace

import pytest

from riffle import disk
from riffle.export.links import safe_name
from riffle.models import CardRules, CardView, Face, PricedPrinting, Prices, Printing

CARDS = {
    # card_id: (name, layout, usd, tix)
    "o-sol": ("Sol Ring", "normal", 1.0, 0.05),
    "o-rift": ("Cyclonic Rift", "normal", 30.0, 2.0),
    "o-forest": ("Forest", "normal", 0.1, 0.01),
    "o-snowf": ("Snow-Covered Forest", "normal", 0.5, 0.02),
    "o-aesi": ("Aesi, Tyrant of Gyre Strait", "normal", 5.0, 0.3),
    "o-fire": ("Fire // Ice", "split", 1.0, 0.1),
    "o-rider": ("Murderous Rider // Swift End", "adventure", 2.0, None),
    "o-yshtola": ("Y'shtola, Night's Blessed", "normal", 3.0, None),
    "o-bolt": ("Lightning Bolt", "normal", 1.0, 0.02),
    "o-rats": ("Relentless Rats", "normal", 0.2, 0.01),
    "o-crypt": ("Mana Crypt", "normal", 150.0, 20.0),
    "o-thrasios": ("Thrasios, Triton Hero", "normal", 10.0, 1.0),
    "o-tymna": ("Tymna the Weaver", "normal", 12.0, 1.0),
    "o-dwarves": ("Seven Dwarves", "normal", 0.25, 0.01),
    "o-teferi": ("Teferi, Hero of Dominaria", "normal", 15.0, 3.0),
    "o-lurrus": ("Lurrus of the Dream-Den", "normal", 3.0, 0.4),
    "o-delver": ("Delver of Secrets // Insectile Aberration", "transform", 0.25, 0.02),
    "o-summon": ("Summon: Bahamut", "normal", 4.0, None),
    "o-anduril": ("Andúril, Flame of the West", "normal", 2.0, None),
}

_CMDR = {"commander": "legal", "duel": "legal"}
RULES = {
    # card_id: (legalities, color_identity, type_line, oracle_text)
    "o-sol": (
        {**_CMDR, "modern": "not_legal", "legacy": "banned", "vintage": "restricted"},
        (),
        "Artifact",
        "{T}: Add {C}{C}.",
    ),
    "o-rift": ({**_CMDR, "modern": "not_legal", "legacy": "legal"}, ("U",), "Instant", "Overload {6}{U}"),
    "o-forest": ({**_CMDR, "modern": "legal"}, ("G",), "Basic Land — Forest", "({T}: Add {G}.)"),
    "o-snowf": ({**_CMDR, "modern": "legal"}, ("G",), "Basic Snow Land — Forest", "({T}: Add {G}.)"),
    "o-aesi": (
        {**_CMDR, "modern": "legal"},
        ("U", "G"),
        "Legendary Creature — Serpent",
        "You may play an additional land on each of your turns.",
    ),
    "o-fire": ({**_CMDR, "modern": "legal"}, ("U", "R"), "Instant // Instant", "Fire\nIce"),
    "o-bolt": (
        {**_CMDR, "modern": "legal", "pioneer": "not_legal"},
        ("R",),
        "Instant",
        "Lightning Bolt deals 3 damage to any target.",
    ),
    "o-rats": (
        {**_CMDR, "modern": "legal"},
        ("B",),
        "Creature — Rat",
        "A deck can have any number of cards named Relentless Rats.",
    ),
    "o-crypt": (
        {"commander": "banned", "duel": "banned", "modern": "not_legal", "vintage": "restricted"},
        (),
        "Artifact",
        "At the beginning of your upkeep, flip a coin.",
    ),
    "o-thrasios": (
        {**_CMDR, "modern": "legal"},
        ("U", "G"),
        "Legendary Creature — Merfolk Wizard",
        "{4}: Scry 1. Partner",
    ),
    "o-tymna": (
        {**_CMDR, "modern": "legal"},
        ("W", "B"),
        "Legendary Creature — Human Cleric",
        "Lifelink. Partner",
    ),
    "o-dwarves": (
        {**_CMDR, "modern": "legal"},
        ("R",),
        "Creature — Dwarf",
        "A deck can have up to seven cards named Seven Dwarves.",
    ),
    "o-teferi": (
        {**_CMDR, "modern": "legal"},
        ("W", "U"),
        "Legendary Planeswalker — Teferi",
        "+1: Draw a card.",
    ),
    "o-lurrus": (
        {**_CMDR, "modern": "legal"},
        ("W", "B"),
        "Legendary Creature — Cat Nightmare",
        "Companion — Each permanent card in your starting deck has mana value 2 or less.",
    ),
}
FRONT = "https://cards.scryfall.io/normal/front"
VIEWS = {
    # card_id: what its note shows; any other card shows its rules and is pictured by "s-<id>"
    "o-fire": CardView(
        (
            Face("Fire", "{1}{R}", "Instant", "Fire deals 2 damage."),
            Face("Ice", "{1}{U}", "Instant", "Tap.\nDraw."),
        ),
        (("Fire // Ice", f"{FRONT}/s-fire.jpg?1662"),),
        "s-fire",
        "dmr",
        "215",
        599089,
    ),
    "o-delver": CardView(
        (
            Face("Delver of Secrets", "{U}", "Creature — Human Wizard", "Transform it.", "1/1"),
            Face("Insectile Aberration", "", "Creature — Human Insect", "Flying", "3/2"),
        ),
        (
            ("Delver of Secrets", f"{FRONT}/s-delver.jpg?1"),
            ("Insectile Aberration", "https://cards.scryfall.io/normal/back/s-delver.jpg?1"),
        ),
        "s-delver",
        "inr",
        "60",
    ),
    "o-crypt": CardView((Face("Mana Crypt", "{0}", "Artifact"),)),  # printed only in foil: no picture
}
PRINTINGS = {
    "s-sol-m3c": Printing("s-sol-m3c", "o-sol", "Sol Ring", "m3c", "283"),
    "s-rift-2x2": Printing("s-rift-2x2", "o-rift", "Cyclonic Rift", "2x2", "45"),
}


class FakeCatalog:
    day: date | None = date(2026, 9, 21)  # the Scryfall prices it holds

    def resolve(self, name):
        key = name.strip().lower()
        for oid, (n, *_) in CARDS.items():
            if n.lower() == key or n.lower().split(" // ")[0] == key.split(" // ")[0]:
                return oid
        for oid, (n, *_) in CARDS.items():
            if safe_name(n).lower() == " ".join(key.split()):
                return oid
        return None

    def name(self, oid):
        return CARDS[oid][0]

    def prices(self, card_ids):
        return {c: Prices(*CARDS[c][2:4]) for c in card_ids if c in CARDS}

    def prices_day(self):
        return self.day

    def price_printings(self, card_ids):
        """One printing a card, playable, its Scryfall ID "s-" and the card's."""
        return {c: [PricedPrinting(f"s-{c}", True, False)] for c in card_ids if c in CARDS}

    def printings(self, sids):
        return {s: PRINTINGS[s] for s in sids if s in PRINTINGS}

    def printings_at(self, places):
        at = {(p.set_code, p.collector_number): p for p in PRINTINGS.values()}
        return {(code, n): at[(code.lower(), n)] for code, n in places if (code.lower(), n) in at}

    def mtgo_name(self, oid):
        name, layout = CARDS[oid][:2]
        if " // " in name:
            return name.replace(" // ", "/") if layout == "split" else name.split(" // ")[0]
        return name

    def arena_rarity(self, oid):
        return {"o-sol": "uncommon", "o-rift": "mythic", "o-aesi": "rare"}.get(oid)

    def is_basic(self, oid):
        return CARDS[oid][0] in {"Forest", "Island", "Plains", "Swamp", "Mountain", "Wastes"}

    def rules(self, card_ids):
        return {c: CardRules(*RULES[c]) for c in card_ids if c in RULES}

    def card_views(self, card_ids):
        views = {}
        for c in card_ids:
            if c in VIEWS:
                views[c] = VIEWS[c]
            elif c in CARDS:
                name, (_, _, kind, text) = CARDS[c][0], RULES.get(c, ({}, (), "", ""))
                views[c] = CardView(
                    (Face(name, "", kind, text),), ((name, f"{FRONT}/s-{c}.jpg?7"),), f"s-{c}", "tst", "1"
                )
        return views


@pytest.fixture
def cat():
    return FakeCatalog()


@pytest.fixture
def opened(cat, monkeypatch):
    """open_catalog yields the in-memory catalog; the list records each time it's opened and closed."""
    from riffle.store import postgres

    events = []

    @contextmanager
    def open_catalog():
        events.append("open")
        yield cat
        events.append("close")

    monkeypatch.setattr(postgres, "open_catalog", open_catalog)
    return events


@pytest.fixture(autouse=True)
def isolated(tmp_path_factory, monkeypatch):
    """Every test gets its own config and data directories, and a configured database no
    one can reach, so nothing a test runs touches real files or the real database. Tests
    that need their own set XDG_* again; database tests use the pg fixtures below."""
    home = tmp_path_factory.mktemp("xdg")
    (home / "config" / "riffle").mkdir(parents=True)
    (home / "config" / "riffle" / "config.toml").write_text(
        'notes = "games/tcg"\ndatabase_url = "postgresql+psycopg://tcg@127.0.0.1:1/unreachable"\n'
        'card_images = "link"\n'  # no test fetches a card's picture unless it says so
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(home / "data"))
    monkeypatch.setattr(disk, "WARN_BELOW", 0)  # the machine's own free space warns nowhere


# ---- times ----------------------------------------------------------------------------


@pytest.fixture
def denver():
    """The Mac's clock set to Denver's time (MDT in summer), for what the terminal shows."""
    with pytest.MonkeyPatch.context() as m:
        m.setenv("TZ", "America/Denver")
        time.tzset()
        yield
    time.tzset()


@pytest.fixture
def on_a_terminal(denver, monkeypatch):
    """riffle.times.shown thinks it prints to a terminal, in Denver."""
    from riffle import times

    monkeypatch.setattr(times, "sys", SimpleNamespace(stdout=SimpleNamespace(isatty=lambda: True)))


# ---- progress -----------------------------------------------------------------------


@dataclass
class RecordedStep:
    label: str
    total: int | None
    unit: str
    updates: list[tuple[int, int | None]] = field(default_factory=list)
    outcome: tuple[str, ...] | None = None  # ("ok", note), ("warn", note), ("fail", why), ("drop",)

    def update(self, done: int, total: int | None = None) -> None:
        self.updates.append((done, total))

    def ok(self, note: str = "") -> None:
        self.outcome = ("ok", note)

    def warn(self, note: str) -> None:
        self.outcome = ("warn", note)

    def fail(self, why: str) -> None:
        self.outcome = ("fail", why)

    def drop(self) -> None:
        self.outcome = ("drop",)


class Recorder:
    """A progress Tracker that keeps every step it was given, in order."""

    def __init__(self) -> None:
        self.steps: list[RecordedStep] = []

    def step(self, label: str, total: int | None = None, unit: str = "") -> RecordedStep:
        self.steps.append(RecordedStep(label, total, unit))
        return self.steps[-1]

    def outcomes(self) -> dict[str, tuple[str, ...] | None]:
        return {s.label: s.outcome for s in self.steps}


@pytest.fixture
def tracker() -> Recorder:
    return Recorder()


@pytest.fixture
def plain() -> Callable[[str], str]:
    """A command's output as its words alone: CI's terminal gets colour codes, and an error box
    wraps its text between │ borders at 80 columns, where a long path moves the breaks."""
    return lambda output: " ".join(re.sub(r"\x1b\[[0-9;]*m", "", output).replace("│", " ").split())


# ---- Postgres ------------------------------------------------------------------
# Database tests use their own database, tcg_test, dropped and recreated once per
# session and migrated to head. Each test then runs in a transaction that's rolled
# back, so tests never see each other's rows. No server reachable: the tests skip,
# unless RIFFLE_REQUIRE_POSTGRES is set (Linux CI), where they fail instead.

PG_TEST_URL = os.environ.get("RIFFLE_TEST_DATABASE_URL", "postgresql+psycopg://tcg@localhost:5432/tcg_test")


@pytest.fixture(scope="session")
def pg_engine():
    from sqlalchemy import create_engine, make_url, text
    from sqlalchemy.exc import OperationalError

    from riffle.db import migrate

    url = make_url(PG_TEST_URL)
    if not (url.database or "").endswith("_test"):
        pytest.fail(f"refusing to drop {url.database!r}: the test database's name must end in _test")
    admin = create_engine(
        url.set(database="postgres"), isolation_level="AUTOCOMMIT", connect_args={"connect_timeout": 3}
    )
    try:
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{url.database}" WITH (FORCE)'))
            conn.execute(text(f'CREATE DATABASE "{url.database}"'))
    except OperationalError:
        if os.environ.get("RIFFLE_REQUIRE_POSTGRES"):
            raise
        pytest.skip(f"no Postgres at {url.render_as_string(hide_password=True)} (start it: riffle db up)")
    finally:
        admin.dispose()
    engine = create_engine(url)
    migrate.upgrade(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def pg(pg_engine):
    """A connection inside a transaction that's rolled back after the test."""
    with pg_engine.connect() as conn:
        trans = conn.begin()
        try:
            yield conn
        finally:
            trans.rollback()
