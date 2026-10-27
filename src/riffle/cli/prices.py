"""Handle price collection and historical price commands."""

from collections.abc import Callable
from functools import partial

import typer

from riffle import config, vault
from riffle.cli import _common as common
from riffle.progress import Tracker, contained, failure


def ingest_prices(
    delay: float = typer.Option(0.1, help="Seconds between tcgcsv requests"),
) -> None:
    """Keep today's prices: tcgcsv, Cardmarket, Scryfall, MTGJSON, GoatBots, Card Kingdom, Mana Pool."""
    with common._run("riffle ingest prices") as tracker:
        _snapshot_prices(tracker, online=True, delay=delay)


def _snapshot_prices(tracker: Tracker, online: bool, delay: float = 0.1) -> None:
    """Today's price snapshot. Problems are reported, never raised: a sync must finish without it."""
    from riffle.ingest.remote import cardmarket, goatbots, mtgjson, pricelists, scryfall, tcgcsv

    step = tracker.step("Scryfall prices")
    try:
        path, written = scryfall.snapshot_prices()
    except FileNotFoundError:
        step.fail("no bulk file yet — run: riffle ingest scryfall")
    except (ValueError, KeyError) as e:
        step.fail(str(e))
    except Exception as e:
        step.fail(failure(e))
    else:
        day = path.name.split(".")[0]
        if written:
            step.ok(f"kept {day}")
        elif online:
            step.ok(f"already have {day}")
        else:
            step.drop()  # offline resyncs (watch, ingest manabox) shouldn't repeat "already have"
    if not online:
        return
    # Each source in turn; whatever one raises fails only its own steps (see failure).
    sources: list[tuple[str, Callable[..., object]]] = [
        ("MTGJSON prices", partial(mtgjson.watch, always=True)),
        ("MTGJSON 90 days", mtgjson.snapshot),
        ("GoatBots prices", partial(goatbots.watch, always=True)),
        ("GoatBots years", goatbots.snapshot),
        ("Cardmarket prices", partial(cardmarket.watch, always=True)),
        ("Card Kingdom prices", partial(pricelists.watch, pricelists.CARD_KINGDOM)),
        ("Mana Pool prices", partial(pricelists.watch, pricelists.MANA_POOL)),
    ]
    for label, snapshot in sources:
        with contained(tracker, label, failure) as scope:
            snapshot(tracker=scope)
    with contained(tracker, "tcgcsv prices", failure) as scope:
        snap = tcgcsv.watch(delay=delay, tracker=scope, always=True).snap
        if snap is not None:
            common._echo(f"tcgcsv prices: {snap.day} · {snap.requests} requests")


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
