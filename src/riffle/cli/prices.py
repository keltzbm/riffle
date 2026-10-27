"""Handle price collection and historical price commands."""

import typer

from riffle import config, vault
from riffle.cli import _common as common
from riffle.ingest.remote import tcgcsv
from riffle.ops import ingest as workflow


def ingest_prices(
    delay: float = typer.Option(0.1, help="Seconds between tcgcsv requests"),
) -> None:
    """Keep today's prices: tcgcsv, Cardmarket, Scryfall, MTGJSON, GoatBots, Card Kingdom, Mana Pool."""
    with common._run("riffle ingest prices") as tracker:
        snap = workflow.snapshot_prices(tracker, online=True, delay=delay)
        if snap is not None:
            _summary(snap)


def _summary(snapshot: tcgcsv.Snapshot) -> None:
    common._echo(f"tcgcsv prices: {snapshot.day} · {snapshot.requests} requests")


def prices_log(
    cards: list[str] = typer.Argument(None, help="Card names [default: every unticked #mtg/buy line's]"),
) -> None:
    """Each card's price on every Scryfall day the store keeps, a line a card a day:
    `date | card | paper | tix`, by the rule deck prices use. What _log/prices.md held."""
    from riffle.analysis import pricing
    from riffle.export import obsidian
    from riffle.ingest.remote import scryfall

    days = scryfall.price_days()
    if not days:
        common._echo("no Scryfall price days kept yet; riffle sync keeps one a day", err=True)
        raise typer.Exit(1)
    failed = False
    if cards:
        names = cards
    else:
        unreadable: vault.Unreadable = []
        names = vault.buy_cards(common._vault(config.load())[1], unreadable)
        for path, why in unreadable:
            common._echo(f"  ! can't read {path} ({why}); its buy lines are left out", err=True)
    with common._catalog() as cat:
        wanted: dict[str, str] = {}  # card_id -> name, as the catalog has it
        for name in names:
            card_id = cat.resolve(name)
            if card_id is None:
                common._echo(
                    f"no card named {name!r}" if cards else f"  ! buy list: unmatched {name}", err=True
                )
                failed = failed or bool(cards)
            else:
                wanted[card_id] = cat.name(card_id)
        printings = cat.price_printings(wanted)
    order = sorted(wanted, key=lambda card_id: wanted[card_id])
    for day, path in days:
        try:
            prices = pricing.on_day(printings, scryfall.day_prices(path, pricing.scryfall_ids(printings)))
        except scryfall.Unreadable as e:
            common._echo(f"  ! {day}: {e}; that day is left out", err=True)
            failed = True
            continue
        for card_id in order:
            typer.echo(obsidian.price_line(day.isoformat(), wanted[card_id], prices[card_id]))
    if failed:
        raise typer.Exit(1)
