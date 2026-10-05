"""The work behind `riffle sync`, kept free of the CLI and the database so it's testable."""

from collections import Counter
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path

from riffle import vault
from riffle.analysis import ownership, pricing
from riffle.analysis.resolve import counts, resolve_deck, resolve_holdings
from riffle.export import cards, formats, obsidian
from riffle.ingest import arena, manabox
from riffle.models import Deck, Holding
from riffle.progress import failure
from riffle.store import Catalog


@dataclass
class Inventory:
    holdings: list[Holding]
    unresolved: list[str] = field(default_factory=list)

    @cached_property
    def owned(self) -> Counter:
        """Copies owned per card_id. Computed once: holdings are resolved before
        an Inventory is built and never change after."""
        return counts(self.holdings)


def inventory(collection_csv: Path, catalog: Catalog) -> Inventory:
    holdings = manabox.load(collection_csv) if collection_csv.exists() else []
    return Inventory(holdings, resolve_holdings(holdings, catalog))


def arena_inventory(arena_list: Path, catalog: Catalog) -> Inventory:
    holdings = arena.load(arena_list) if arena_list.exists() else []
    return Inventory(holdings, resolve_holdings(holdings, catalog))


@dataclass
class DeckReport:
    deck: Deck
    rows: list[ownership.Row]
    price: pricing.DeckPrice
    unresolved: list[str]


def analyse(deck: Deck, inv: Inventory, catalog: Catalog) -> DeckReport:
    missing = resolve_deck(deck, catalog)
    rows = ownership.diff(deck, inv.owned, catalog)
    return DeckReport(deck, rows, pricing.price(rows, catalog), missing)


@dataclass
class CardNotes:
    """How the sync writes the card notes (export/cards.py)."""

    vault: Path  # the whole vault: a card name another note has gets no card note
    notes: Path  # the folder whose links get card notes
    pictures: str = "cache"  # card_images
    fetch: cards.Fetch | None = None  # None: no picture is fetched, as offline
    progress: cards.Progress | None = None


@dataclass
class SyncResult:
    decks: list[str] = field(default_factory=list)
    changed_notes: int = 0
    versions: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)  # (step, why), for the CLI to report
    card_notes: cards.Result | None = None  # None when card notes are off
    cards_removed: int = 0  # with card notes off, the notes removed


def run(
    mtg_dir: Path,
    inv: Inventory,
    catalog: Catalog,
    today: str | None = None,
    card_notes: CardNotes | None = None,
) -> SyncResult:
    """Rebuild the generated notes, writing those that changed, and append the version logs,
    dated today in UTC. The state the logs diff against is saved once, after the last deck,
    or when the run is cut off before it. Then a note for every card a note links, unless
    card_notes is None (off), which removes them."""
    today = today or obsidian.today()
    gen, log = mtg_dir / "_generated", mtg_dir / "_log"
    res = SyncResult()
    pins = formats.owned_printings(inv.holdings)
    keep = {"collection-summary.md"}
    unreadable: vault.Unreadable = []
    played: set[str] = set()  # the formats the lists are in
    try:
        state = obsidian.load_state()
    except obsidian.Unreadable as e:
        bad = obsidian.set_aside_state()
        why = f"{e}; set aside as {bad.name}, so each deck's log starts again from a baseline"
        res.failed.append(("version logs", why))
        state = {}
    try:
        for deck in vault.decks(mtg_dir, unreadable):
            rep = analyse(deck, inv, catalog)
            res.decks.append(deck.slug)
            imports = {
                "Moxfield import — owned printings pinned": formats.moxfield(deck, catalog, pins),
                "ManaBox import — owned printings pinned": formats.manabox(deck, catalog, pins),
                "MTGO import": formats.mtgo(deck, catalog),
            }
            text = obsidian.deck_data(deck, rep.rows, rep.price, rep.unresolved, imports)
            res.changed_notes += obsidian.write_deck(gen, deck, text)
            played.add(deck.format.lower())
            keep.add(f"{deck.slug}-data.md")
            if obsidian.append_version(log, deck, catalog, today, state):
                res.versions.append(deck.slug)
            if rep.unresolved:
                res.warnings.append(f"{deck.slug}: unmatched {', '.join(rep.unresolved)}")
    finally:
        if res.versions:  # the logs appended so far, even when a deck stopped the run
            obsidian.save_state(state)
    res.changed_notes += obsidian.write_summary(gen, obsidian.collection_summary(inv.holdings, catalog))
    keep.update(f"{p.stem}-data.md" for p, _ in unreadable)  # kept as it was until the note reads again
    res.removed = obsidian.prune(gen, keep)
    obsidian.close_price_log(log / "prices.md", today)
    if card_notes is None:
        res.cards_removed = cards.remove(gen)
    else:
        _card_notes(res, mtg_dir, card_notes, catalog, inv, played)
    copies = [p.relative_to(mtg_dir.parent).as_posix() for p in vault.conflicts(mtg_dir)]
    if copies:
        res.warnings.append(f"sync-conflict copies, not read; merge each by hand: {', '.join(copies)}")
    if unreadable:
        whys = {p.relative_to(mtg_dir.parent).as_posix(): why for p, why in unreadable}
        notes = ", ".join(f"{name} ({why})" for name, why in whys.items())
        res.failed.append(("vault notes", f"skipped, can't read {notes}"))
    if inv.unresolved:
        res.warnings.append(f"collection: {len(inv.unresolved)} rows unmatched, e.g. {inv.unresolved[:3]}")
    return res


def _card_notes(
    res: SyncResult,
    mtg_dir: Path,
    how: CardNotes,
    catalog: Catalog,
    inv: Inventory,
    played: set[str],
) -> None:
    """Write the card notes into res, after the rest of the vault, so they follow the tables
    just written. A folder that can't be written fails this step alone."""
    gen = mtg_dir / "_generated"
    try:
        found = cards.gather(vault.linking(how.notes))
        own = cards.gather(vault.riffle_notes(mtg_dir))
        links = cards.sort(found, catalog, vault.names(how.vault), own)
        res.card_notes = cards.write(
            gen, links, catalog, inv.holdings, played, how.pictures, how.fetch, how.progress
        )
    except OSError as e:
        res.failed.append(("card notes", f"can't write {gen / cards.FOLDER} ({e.strerror or e})"))
        return
    except Exception as e:  # a bug here fails this step alone: the decks' notes are written
        res.failed.append(("card notes", failure(e)))
        return
    c = res.card_notes
    if c.misses:
        n = len(c.misses)
        res.warnings.append(
            f"{n} card link{'s' * (n != 1)} can't open the card's note as written; "
            "riffle check vault lists them"
        )
    if c.taken or c.clashes:
        n = len(c.taken) + len(c.clashes)
        res.warnings.append(
            f"{n} linked card{'s have' if n != 1 else ' has'} no note, its name being another note's or "
            "card's; riffle check vault lists them"
        )
    if c.unfetched and c.why:  # tried and failed; an offline sync tries none
        why = f" ({c.why})"
        res.warnings.append(
            f"card pictures: {c.unfetched:,} not fetched yet{why}; "
            "linked from Scryfall until the next sync fetches them"
        )
