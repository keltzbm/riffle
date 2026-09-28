"""The Catalog every command reads: Magic's cards in Postgres, as the last Scryfall load left them.

open_catalog() opens one read-only, repeatable-read transaction for a command, so
every lookup sees the same snapshot even if a load commits meanwhile. It checks
that the schema is current and Magic's cards are loaded, and says what to run
when they aren't.

Names are resolved against every card, so names load in one query on first use
(about 40,000 rows). Prices, printings, and rules are fetched only for the cards
asked about, a whole collection or deck per query, and kept for the rest of the
command: a sync names a few thousand of the 118,000 printings.

Only live rows count: a card or printing Scryfall stopped listing is retired,
not deleted, and stays out of every answer here.
"""

from collections.abc import Collection, Iterator
from contextlib import contextmanager
from functools import cached_property
from typing import NamedTuple

from sqlalchemy import Connection, Engine, text
from sqlalchemy.exc import OperationalError

from riffle import db
from riffle.db import migrate
from riffle.models import CardRules, Prices, Printing

GAME = "mtg"
BASICS = {"plains", "island", "swamp", "mountain", "forest", "wastes"}
MYTHIC = {"mythic", "special", "bonus"}  # rarities Arena crafts with a mythic wildcard
STAND_INS = {"art_series", "token", "double_faced_token", "emblem"}  # never what a shared name means

# Every card's name and layout: what name resolution and display need, for every card.
CARDS = text("""
    SELECT c.card_id::text, c.name, m.layout
    FROM cards AS c
    JOIN mtg_cards AS m ON m.card_id = c.card_id
    WHERE c.game_id = :game AND c.retired_at IS NULL
""")

# The two queries below aggregate each card's printings in a LATERAL subquery, so each card's
# few printings are found through the card_id index. As a plain join, the planner scans and
# sorts all 118,000 printings instead, four times slower for a collection's worth of cards.

# How many live printings each of these cards has: where two real cards share a name, the one
# with more wins.
PRINTING_COUNTS = text("""
    SELECT w.card_id::text, a.n
    FROM unnest(CAST(:ids AS uuid[])) AS w (card_id)
    CROSS JOIN LATERAL (
        SELECT count(*) AS n FROM printings AS p WHERE p.card_id = w.card_id AND p.retired_at IS NULL
    ) AS a
""")

# The cheapest paper and MTGO price among these cards' printings. Digital printings have no
# paper price of their own, so they're left out of the paper minimum.
PRICES = text("""
    SELECT w.card_id::text, a.usd, a.tix
    FROM unnest(CAST(:ids AS uuid[])) AS w (card_id)
    CROSS JOIN LATERAL (
        SELECT (min(mp.usd) FILTER (WHERE NOT mp.digital))::float8 AS usd, min(mp.tix)::float8 AS tix
        FROM printings AS p
        JOIN mtg_printings AS mp ON mp.printing_id = p.printing_id
        WHERE p.card_id = w.card_id AND p.retired_at IS NULL
    ) AS a
""")

# The cards asked for are joined as a list (unnest), so each is found by its key.
RULES = text("""
    SELECT c.card_id::text, m.color_identity, c.type_line, c.rules_text
    FROM unnest(CAST(:ids AS uuid[])) AS w (card_id)
    JOIN cards AS c ON c.card_id = w.card_id
    JOIN mtg_cards AS m ON m.card_id = c.card_id
    WHERE c.game_id = :game AND c.retired_at IS NULL
""")

# Stored merged across a card's printings already: the strongest status wins. The key is
# (game_id, format, card_id), so each card is looked up once per format.
LEGALITIES = text("""
    SELECT l.card_id::text, l.format, l.status
    FROM unnest(CAST(:ids AS uuid[])) AS w (card_id)
    JOIN formats AS f ON f.game_id = :game
    JOIN legalities AS l ON l.game_id = f.game_id AND l.format = f.format AND l.card_id = w.card_id
""")

# The lowest rarity each card has among its Arena printings: the wildcard it costs.
ARENA = text("""
    SELECT DISTINCT ON (p.card_id) p.card_id::text, p.rarity
    FROM printings AS p
    WHERE p.game_id = :game AND p.retired_at IS NULL AND p.extra -> 'games' ? 'arena'
    ORDER BY p.card_id,
             CASE p.rarity WHEN 'common' THEN 0 WHEN 'uncommon' THEN 1 WHEN 'rare' THEN 2 ELSE 3 END
""")

# A printing as PostgresCatalog returns it, found by one LATERAL subquery per key asked
# about, so every step is an index lookup. As plain joins, the planner scans all of
# external_ids or printings instead, whatever the number of keys.
_PRINTING = """
    SELECT e.external_id, p.card_id::text, c.name, s.code, p.collector_number,
           coalesce(mp.frame, '') AS frame, coalesce(mp.border_color, '') AS border_color,
           mp.usd::float8 AS usd
    FROM printings AS p
    JOIN external_ids AS e ON e.printing_id = p.printing_id AND e.source = 'scryfall'
    JOIN sets AS s ON s.set_id = p.set_id
    JOIN cards AS c ON c.card_id = p.card_id
    JOIN mtg_printings AS mp ON mp.printing_id = p.printing_id
"""

# By Scryfall ID, the ID ManaBox exports: the registry's primary key. An ID names one printing;
# LIMIT 1 says so, and keeps the planner from flattening the subquery back into a join.
PRINTINGS = text(f"""
    SELECT f.*
    FROM unnest(CAST(:ids AS text[])) AS w (scryfall_id)
    CROSS JOIN LATERAL (
        {_PRINTING}
        WHERE p.printing_id = (
            SELECT printing_id FROM external_ids WHERE source = 'scryfall' AND external_id = w.scryfall_id
        ) AND p.retired_at IS NULL
        LIMIT 1
    ) AS f
""")

# By set and collector number. Where those name printings in two languages, English answers.
# The game and retirement conditions are written out as the partial index on (set_id,
# collector_number, lang) states them, so the planner can use it: a bound parameter can't
# match an index predicate.
PRINTINGS_AT = text(f"""
    SELECT w.code, w.number, f.*
    FROM unnest(CAST(:codes AS text[]), CAST(:numbers AS text[])) AS w (code, number)
    CROSS JOIN LATERAL (
        {_PRINTING}
        WHERE p.set_id = (SELECT set_id FROM sets WHERE game_id = '{GAME}' AND code = w.code)
          AND p.collector_number = w.number AND p.game_id = '{GAME}' AND p.retired_at IS NULL
        ORDER BY p.lang <> 'en', e.external_id
        LIMIT 1
    ) AS f
""")


class Unavailable(Exception):
    """The catalog can't be read: Postgres is unreachable, its schema is behind, or it holds no cards."""


class Card(NamedTuple):
    name: str
    layout: str


class PostgresCatalog:
    """A Catalog over one connection. Card IDs are Riffle's card_id, as text."""

    def __init__(self, conn: Connection):
        self.conn = conn
        self._printings: dict[str, Printing | None] = {}  # by Scryfall ID; None: not in the catalog
        self._rules: dict[str, CardRules | None] = {}
        self._prices: dict[str, Prices] = {}

    @cached_property
    def _cards(self) -> dict[str, Card]:
        rows = self.conn.execute(CARDS, {"game": GAME})
        return {card_id: Card(*rest) for card_id, *rest in rows}

    @cached_property
    def _names(self) -> tuple[dict[str, str], dict[str, str]]:
        """(full names, front faces), lowercased -> card_id. Where cards share a name, a real
        card beats a token, emblem, or Art Series card, then the one with more printings wins."""
        claims: dict[tuple[bool, str], list[str]] = {}  # (full name?, name) -> the cards it names
        for card_id, card in self._cards.items():
            full = card.name.lower()
            claims.setdefault((True, full), []).append(card_id)
            claims.setdefault((False, full.split(" // ")[0]), []).append(card_id)
        shared = {key: self._real_first(ids) for key, ids in claims.items() if len(ids) > 1}
        tied = {c for ids in shared.values() if len(ids) > 1 for c in ids}
        counts: dict[str, int] = {}
        if tied:
            counts = {c: n for c, n in self.conn.execute(PRINTING_COUNTS, {"ids": list(tied)})}
        exact: dict[str, str] = {}
        front: dict[str, str] = {}
        for (is_full, name), ids in claims.items():
            finalists = shared.get((is_full, name), ids)
            winner = finalists[0] if len(finalists) == 1 else min(finalists, key=lambda c: (-counts[c], c))
            (exact if is_full else front)[name] = winner
        return exact, front

    def _real_first(self, card_ids: list[str]) -> list[str]:
        """The real cards among these, or all of them if none is."""
        real = [c for c in card_ids if self._cards[c].layout not in STAND_INS]
        return real or card_ids

    @cached_property
    def _arena(self) -> dict[str, str]:
        return {card_id: rarity for card_id, rarity in self.conn.execute(ARENA, {"game": GAME})}

    def resolve(self, name: str) -> str | None:
        key = name.strip().lower()
        exact, front = self._names
        if key.startswith("a-"):  # Arena rebalanced cards, "A-Name"
            key = key[2:]
        if "/" in key and " // " not in key:  # MTGO writes split cards as "Fire/Ice"
            key = key.replace("/", " // ")
        return exact.get(key) or front.get(key) or front.get(key.split(" // ")[0])

    def name(self, card_id: str) -> str:
        card = self._cards.get(card_id)
        return card.name if card else card_id

    def prices(self, card_ids: Collection[str]) -> dict[str, Prices]:
        wanted = [c for c in set(card_ids) if c not in self._prices and c in self._cards]
        if wanted:
            self._prices.update(dict.fromkeys(wanted, Prices()))
            for card_id, usd, tix in self.conn.execute(PRICES, {"ids": wanted}):
                self._prices[card_id] = Prices(usd=usd, tix=tix)
        return {c: p for c in card_ids if (p := self._prices.get(c)) is not None}

    def mtgo_name(self, card_id: str) -> str:
        card = self._cards.get(card_id)
        if card is None or " // " not in card.name:
            return self.name(card_id)
        if card.layout in {"split", "aftermath"}:
            return card.name.replace(" // ", "/")
        return card.name.split(" // ")[0]

    def is_basic(self, card_id: str) -> bool:
        return self.name(card_id).lower() in BASICS

    def arena_rarity(self, card_id: str) -> str | None:
        rarity = self._arena.get(card_id)
        return "mythic" if rarity in MYTHIC else rarity

    def rules(self, card_ids: Collection[str]) -> dict[str, CardRules]:
        wanted = [c for c in set(card_ids) if c not in self._rules and c in self._cards]
        if wanted:
            found = {c: CardRules(legalities={}) for c in wanted}
            params = {"game": GAME, "ids": wanted}
            for card_id, identity, type_line, rules_text in self.conn.execute(RULES, params):
                found[card_id] = CardRules({}, tuple(identity), type_line or "", rules_text or "")
            for card_id, fmt, status in self.conn.execute(LEGALITIES, params):
                found[card_id].legalities[fmt] = status
            self._rules.update(found)
        return {c: r for c in card_ids if (r := self._rules.get(c)) is not None}

    def printings(self, scryfall_ids: Collection[str]) -> dict[str, Printing]:
        wanted = [s for s in set(scryfall_ids) if s not in self._printings]
        if wanted:
            self._printings.update(dict.fromkeys(wanted))
            for row in self.conn.execute(PRINTINGS, {"ids": wanted}):
                self._printings[row[0]] = Printing(*row)
        return {s: p for s in scryfall_ids if (p := self._printings.get(s)) is not None}

    def printings_at(self, places: Collection[tuple[str, str]]) -> dict[tuple[str, str], Printing]:
        """Scryfall set codes are lowercase; ManaBox writes them uppercase."""
        if not places:
            return {}
        asked = {(code.lower(), number): (code, number) for code, number in places}
        codes, numbers = zip(*asked, strict=True)
        found = {}
        for code, number, *row in self.conn.execute(
            PRINTINGS_AT, {"codes": list(codes), "numbers": list(numbers)}
        ):
            printing = Printing(*row)
            self._printings.setdefault(printing.scryfall_id, printing)
            found[asked[(code, number)]] = printing
        return found


def check(conn: Connection) -> None:
    """Raise Unavailable, saying what to run, unless the schema is current and Magic's cards are loaded."""
    why = migrate.behind(conn)
    if why:
        raise Unavailable(why)
    loaded = text("SELECT EXISTS (SELECT FROM cards WHERE game_id = :game AND retired_at IS NULL)")
    if not conn.execute(loaded, {"game": GAME}).scalar_one():
        raise Unavailable("no Magic cards in Postgres yet — run: riffle ingest scryfall")


@contextmanager
def open_catalog(engine: Engine | None = None) -> Iterator[PostgresCatalog]:
    """The catalog, read in one read-only, repeatable-read transaction that ends with the block."""
    try:
        eng = engine or db.engine()
    except db.BadURL as e:
        raise Unavailable(str(e)) from e
    try:
        conn = eng.connect()
    except OperationalError as e:
        url = db.display(eng.url.render_as_string(hide_password=False))
        why = f"can't reach Postgres at {url}: {db.reason(e)} — start it with: riffle db up"
        raise Unavailable(why) from e
    with conn:
        snapshot = conn.execution_options(isolation_level="REPEATABLE READ", postgresql_readonly=True)
        with snapshot.begin():
            # Every query here is a short lookup; compiling one with JIT takes longer than running it.
            snapshot.execute(text("SET LOCAL jit = off"))
            check(snapshot)
            yield PostgresCatalog(snapshot)
