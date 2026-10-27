"""Register command groups for the riffle executable."""

import typer

from riffle.cli import check, db, decks, ingest, mtgo, prices, schedule, sync

app = typer.Typer(help="Collection, decks, prices, and the Obsidian vault.", no_args_is_help=True)

ingest_app = typer.Typer(help="Load outside data.", no_args_is_help=True)

app.add_typer(ingest_app, name="ingest")

meta_app = typer.Typer(help="MTGO metagame: league 5-0s, challenges, showcases.", no_args_is_help=True)

app.add_typer(meta_app, name="meta")

mtgo_app = typer.Typer(help="MTGO decklists from mtgo.com, a few pages at a time.", no_args_is_help=True)

app.add_typer(mtgo_app, name="mtgo")

prices_app = typer.Typer(help="Card prices from the days the store keeps.", no_args_is_help=True)

app.add_typer(prices_app, name="prices")

app.command()(ingest.init)
ingest_app.command("scryfall")(ingest.ingest_scryfall)
ingest_app.command("manabox")(ingest.ingest_manabox)
ingest_app.command("arena")(ingest.ingest_arena)
ingest_app.command("prices")(prices.ingest_prices)
app.command("watch")(ingest.watch_cmd)
mtgo_app.command("trickle")(mtgo.mtgo_trickle)
mtgo_app.command("status")(mtgo.mtgo_status)
mtgo_app.command("forget")(mtgo.mtgo_forget)
meta_app.command("cards")(mtgo.meta_cards)
meta_app.command("decks")(mtgo.meta_decks)
meta_app.command("show")(mtgo.meta_show)
app.command()(decks.legal)
app.command()(decks.decks)
app.command()(decks.own)
app.command()(decks.price)
app.command()(decks.compare)
app.command()(decks.similar)
prices_app.command("log")(prices.prices_log)
app.command("export")(decks.export_deck)
app.command("sync")(sync.sync_cmd)
app.command("status")(check.status_cmd)
app.command("check")(check.check_cmd)
app.command("card")(decks.card_cmd)
schedule_app = typer.Typer(
    help="The scheduled jobs, launchd's on macOS and systemd's on Linux: `riffle sync` daily, the MTGO"
    " trickle, and each store's watch."
)

app.add_typer(schedule_app, name="schedule")

schedule_app.callback(invoke_without_command=True)(schedule.schedule_main)
schedule_app.command("show")(schedule.schedule_show)
schedule_app.command("set")(schedule.schedule_set)
schedule_app.command("remove")(schedule.schedule_remove)
schedule_app.command("trickle")(schedule.schedule_trickle)
schedule_app.command("watch")(schedule.schedule_watch)
db_app = typer.Typer(help="The Postgres database: start it, migrate it, check it.", no_args_is_help=True)

app.add_typer(db_app, name="db")

db_app.command("up")(db.db_up)
db_app.command("upgrade")(db.db_upgrade)
db_app.command("status")(db.db_status)
db_app.command("backup")(db.db_backup)
db_app.command("restore")(db.db_restore)
