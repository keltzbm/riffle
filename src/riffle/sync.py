"""The work behind `riffle sync`, kept free of the CLI and the database so it's testable."""

from collections import Counter
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path

from riffle import vault
from riffle.analysis import ownership, pricing
from riffle.analysis.resolve import counts, resolve_deck, resolve_holdings
from riffle.export import formats, obsidian
from riffle.ingest import arena, manabox
from riffle.models import Deck, Holding
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
class SyncResult:
    decks: list[str] = field(default_factory=list)
    changed_notes: int = 0
    versions: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)  # (step, why), for the CLI to report


def run(mtg_dir: Path, inv: Inventory, catalog: Catalog, today: str | None = None) -> SyncResult:
    """Rebuild the generated notes, writing those that changed, and append the version logs,
    dated today in UTC. The state the logs diff against is saved once, after the last deck,
    or when the run is cut off before it."""
    today = today or obsidian.today()
    gen, log = mtg_dir / "_generated", mtg_dir / "_log"
    res = SyncResult()
    pins = formats.owned_printings(inv.holdings)
    keep = {"collection-summary.md"}
    unreadable: vault.Unreadable = []
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
