"""The only user-facing surface. Everything here is a thin wrapper."""

import shutil
import sys
from collections.abc import Callable, Collection, Iterable, Iterator, Sequence
from contextlib import ExitStack, contextmanager
from datetime import UTC, date, datetime, timedelta
from functools import partial
from pathlib import Path
from typing import Annotated

import typer

from riffle import config, disk, net, times, vault
from riffle import sync as syncmod
from riffle.export import formats
from riffle.models import Deck
from riffle.progress import Step, Tracker, Watched, contained, elapsed, failure, open_tracker
from riffle.store import Catalog

app = typer.Typer(help="Collection, decks, prices, and the Obsidian vault.", no_args_is_help=True)
ingest_app = typer.Typer(help="Load outside data.", no_args_is_help=True)
app.add_typer(ingest_app, name="ingest")
meta_app = typer.Typer(help="MTGO metagame: league 5-0s, challenges, showcases.", no_args_is_help=True)
app.add_typer(meta_app, name="meta")
mtgo_app = typer.Typer(help="MTGO decklists from mtgo.com, a few pages at a time.", no_args_is_help=True)
app.add_typer(mtgo_app, name="mtgo")
prices_app = typer.Typer(help="Card prices from the days the store keeps.", no_args_is_help=True)
app.add_typer(prices_app, name="prices")

# ---- tab completion --------------------------------------------------------------------
# The shell runs `riffle` itself on every Tab press and offers what these return. When
# nothing matches, Typer's zsh script falls back to file names, so a path still completes.


def _offer(values: Iterable[str], incomplete: str) -> list[str]:
    return [v for v in values if v.startswith(incomplete)]


def _deck_names() -> list[str]:
    """What `riffle decks` lists. Nothing when the vault can't be read, rather than a
    traceback in the middle of the prompt."""
    try:
        return [d.slug for d in vault.decks(config.load().mtg_dir)]
    except (OSError, ValueError):
        return []


def _complete_deck(incomplete: str) -> list[str]:
    return _offer(_deck_names(), incomplete)


def _complete_deck_or_all(incomplete: str) -> list[str]:
    return _offer([*_deck_names(), "all"], incomplete)


def _complete_legal_format(incomplete: str) -> list[str]:
    from riffle.analysis.legality import FORMAT_RULES

    return _offer(FORMAT_RULES, incomplete)


def _complete_mtgo_format(incomplete: str) -> list[str]:
    from riffle.ingest import mtgo

    return _offer([*mtgo.FORMATS, "all"], incomplete)


def _complete_store(incomplete: str) -> list[str]:
    from riffle import watching

    return _offer(list(watching.STORES), incomplete)


def _complete_kind(incomplete: str) -> list[str]:
    from riffle.ingest import mtgo

    return _offer(mtgo.KINDS, incomplete)


def _choices(*values: str) -> Callable[[str], list[str]]:
    """Completion from a fixed list."""

    def complete(incomplete: str) -> list[str]:
        return _offer(values, incomplete)

    return complete


DeckRef = Annotated[
    str,
    typer.Argument(help="Deck note slug (aesi-lands) or a path to .md/.txt", autocompletion=_complete_deck),
]
DecksRef = Annotated[
    str,
    typer.Argument(
        help="Deck note slug (aesi-lands), a path to .md/.txt, or all", autocompletion=_complete_deck_or_all
    ),
]


def _echo(message: str = "", err: bool = False) -> None:
    """typer.echo through whatever sys.stdout or sys.stderr is at the moment. While a command's
    steps are showing, those are Rich's stand-ins, which print a line above the running steps;
    typer.echo on its own finds the terminal underneath them and writes onto the display's last
    line, leaving a stale progress line behind. So anything printed inside _tracked goes
    through here."""
    typer.echo(message, file=sys.stderr if err else sys.stdout)


@contextmanager
def _catalog() -> Iterator[Catalog]:
    """The card catalog for the rest of the block, or exit 1 saying what to run."""
    from riffle.store import postgres

    with ExitStack() as stack:
        try:
            cat = stack.enter_context(postgres.open_catalog())
        except postgres.Unavailable as e:
            _echo(str(e), err=True)
            raise typer.Exit(1) from e
        yield cat


@contextmanager
def _tracked(title: str) -> Iterator[Tracker]:
    """A command's steps. A failed step doesn't stop the command: it finishes its work, then
    names what failed and exits 1, so the scheduled job's last exit shows it. Inside the block,
    print with _echo, not typer.echo."""
    with open_tracker(title) as tracker:
        watched = Watched(tracker)
        yield watched
    if watched.failed:
        n = len(watched.failed)
        typer.echo(f"{n} step{'s' * (n != 1)} failed: {', '.join(watched.failed)}", err=True)
        raise typer.Exit(1)


def _lock_path() -> Path:
    return config.data_dir() / "sync.lock"


def _holder(path: Path) -> str:
    """Who holds the run lock, from what they wrote in it, for the waiting message."""
    import json

    try:
        held = json.loads(path.read_text(encoding="utf-8"))
        since = datetime.fromisoformat(held["since"]).astimezone()
        return f"{held['command']} (pid {held['pid']}), running since {since:%H:%M %Z}"
    except (OSError, ValueError, KeyError, TypeError):
        return "another riffle run"


@contextmanager
def _one_at_a_time(tracker: Tracker, command: str) -> Iterator[None]:
    """One run at a time writes prices, the card catalog and the vault. Runs share download
    names (.part, .new), the price log and sync-state.json, so a second run waits for the
    first to finish, saying whose turn it is. The trickle has its own lock."""
    import fcntl
    import json
    import os
    import time

    path = _lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            _echo(f"waiting for {_holder(path)} to finish")
            step, start = tracker.step("waiting for its turn"), time.monotonic()
            fcntl.flock(f, fcntl.LOCK_EX)
            step.ok(f"after {elapsed(time.monotonic() - start)}")
        f.seek(0)
        f.truncate()
        f.write(json.dumps({"command": command, "pid": os.getpid(), "since": datetime.now(UTC).isoformat()}))
        f.flush()
        yield


@contextmanager
def _run(title: str) -> Iterator[Tracker]:
    """_tracked, holding the run lock (see _one_at_a_time) for the whole command."""
    with _tracked(title) as tracker, _one_at_a_time(tracker, title):
        yield tracker


@contextmanager
def _setup() -> Iterator[tuple[config.Config, Catalog, syncmod.Inventory]]:
    cfg = config.load()
    with _catalog() as cat:
        yield cfg, cat, syncmod.inventory(cfg.collection_csv, cat)


@app.command()
def init() -> None:
    """Write a default config and show where everything lives."""
    path = config.write_default()
    cfg = config.load()
    typer.echo(f"config     {path}")
    typer.echo(f"vault      {cfg.vault}")
    typer.echo(f"notes      {cfg.notes}")
    typer.echo(f"data       {config.data_dir()}")
    typer.echo(f"database   {cfg.database_url}")
    for key, why in (cfg.obsolete or {}).items():
        typer.echo(f"  ! `{key}` in config is ignored: {why}", err=True)
    if cfg.old_notes:
        typer.echo(f"  ! {cfg.old_notes}", err=True)


def _refresh(tracker: Tracker, force: bool = False) -> None:
    """Scryfall's bulk files and set list, downloaded, and default cards loaded into the card catalog."""
    from riffle.ingest import scryfall, scryfall_catalog

    scryfall.refresh(force=force, tracker=tracker)
    scryfall_catalog.update(tracker=tracker, force=force)


@ingest_app.command("scryfall")
def ingest_scryfall(
    force: bool = typer.Option(False, help="Download and load default cards even if already kept"),
    no_sync: bool = typer.Option(False, "--no-sync", help="Don't resync the vault afterwards"),
) -> None:
    """Download every Scryfall bulk file not kept yet and the set list, and load the cards."""
    with _run("riffle ingest scryfall") as tracker:
        _refresh(tracker, force=force)
        if no_sync:
            _snapshot_prices(tracker, online=False)
        else:
            _run_sync(tracker, offline=True)


def _copy_manabox(src: Path) -> Path:
    """Whole or not at all, keeping the export's own time: a sync reading the collection
    meanwhile sees the old file or the new one."""
    dest = config.load().collection_csv
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    shutil.copy2(src.expanduser(), tmp)
    tmp.replace(dest)
    return dest


@ingest_app.command("manabox")
def ingest_manabox(
    csv_path: Path = typer.Argument(None, help="Default: newest ManaBox*.csv in ~/Downloads"),
    no_sync: bool = typer.Option(False, "--no-sync", help="Don't resync the vault afterwards"),
) -> None:
    """Copy a ManaBox collection export into the data folder."""
    from riffle.ingest import manabox

    src = csv_path or manabox.newest_export(config.load().downloads)
    if src is None:
        raise typer.BadParameter("no ManaBox*.csv in ~/Downloads — pass a path")
    typer.echo(f"{src.name} -> {_copy_manabox(src)}")
    if not no_sync:
        _resync()


@ingest_app.command("arena")
def ingest_arena(path: Path) -> None:
    """Load an Arena collection export (text list or CSV with name + count)."""
    from riffle.ingest import arena

    holdings = arena.load(path.expanduser())
    dest = config.load().arena_list
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text("".join(f"{h.quantity} {h.name}\n" for h in holdings), encoding="utf-8")
    typer.echo(f"{len(holdings)} Arena cards -> {dest}")


KindOpt = typer.Option(
    None,
    "--kind",
    "-k",
    help="league | challenge | showcase | qualifier | preliminary; repeatable",
    autocompletion=_complete_kind,
)


def _kinds(kind: list[str] | None) -> list[str] | None:
    from riffle.ingest import mtgo

    bad = [k for k in kind or [] if k not in mtgo.KINDS]
    if bad:
        raise typer.BadParameter(f"--kind must be one of {', '.join(mtgo.KINDS)}")
    return kind or None


FormatsOpt = typer.Option(
    ["modern"],
    "--format",
    "-f",
    help="modern, pioneer, pauper, ... or all; repeatable",
    autocompletion=_complete_mtgo_format,
)


def _formats(fmt: list[str]) -> list[str] | None:
    """None means every format."""
    fmts = [f.lower() for f in fmt]
    return None if "all" in fmts else fmts


@ingest_app.command("prices")
def ingest_prices(
    delay: float = typer.Option(0.1, help="Seconds between tcgcsv requests"),
) -> None:
    """Keep today's prices: tcgcsv, Cardmarket, Scryfall, MTGJSON, GoatBots, Card Kingdom, Mana Pool."""
    with _run("riffle ingest prices") as tracker:
        _snapshot_prices(tracker, online=True, delay=delay)


def _snapshot_prices(tracker: Tracker, online: bool, delay: float = 0.1) -> None:
    """Today's price snapshot. Problems are reported, never raised: a sync must finish without it."""
    from riffle.ingest import cardmarket, goatbots, mtgjson, pricelists, scryfall, tcgcsv

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
            _echo(f"tcgcsv prices: {snap.day} · {snap.requests} requests")


def _events(fmt: list[str], days: int, kind: list[str] | None):
    from riffle.ingest import mtgo

    events = mtgo.load(_formats(fmt), date.today() - timedelta(days=days), _kinds(kind))
    if not events:
        names = "/".join(fmt)
        typer.echo(
            f"no stored {names} events in the last {days} days — see what's owed: riffle mtgo status",
            err=True,
        )
        raise typer.Exit(1)
    return events


@app.command("watch")
def watch_cmd(
    store: str = typer.Argument(..., help="The store whose lists to keep", autocompletion=_complete_store),
) -> None:
    """Keep every new list a store publishes: each of its lists asked for once, a new one kept
    whole or as a difference against its run's first (Scryfall's files as served). The jobs
    (riffle schedule watch) run this every 5 minutes; tcgcsv, Cardmarket, MTGJSON, GoatBots
    and Scryfall ask only when their next list is due, as learned from their own lists and
    checks."""
    from riffle import watching

    if store not in watching.STORES:
        stores = ", ".join(watching.STORES)
        raise typer.BadParameter(f"'{store}' has no lists to watch; the stores: {stores}")
    with _tracked(f"riffle watch {store}") as tracker:  # a failed step exits 1 on leaving
        _watcher(store)(tracker=tracker)


def _watcher(store: str) -> Callable[..., object]:
    """A store's watch, one of watching.STORES."""
    from riffle.ingest import cardmarket, goatbots, mtgjson, pricelists, scryfall, tcgcsv

    watchers: dict[str, Callable[..., object]] = {
        "cardkingdom": partial(pricelists.watch, pricelists.CARD_KINGDOM),
        "manapool": partial(pricelists.watch, pricelists.MANA_POOL),
        "cardmarket": cardmarket.watch,
        "tcgcsv": tcgcsv.watch,
        "mtgjson": mtgjson.watch,
        "goatbots": goatbots.watch,
        "scryfall": scryfall.watch,
    }
    return watchers[store]


@mtgo_app.command("trickle")
def mtgo_trickle() -> None:
    """One run of the trickle: at most one index page, then owed events (a page that failed
    at one of the last two runs first, then those never asked for, then the other
    retries), as many as the pace allows. The job (riffle schedule trickle) runs this
    every 10 minutes."""
    from riffle import trickle
    from riffle.ingest import mtgo

    with _tracked("riffle mtgo trickle") as tracker:  # report inside: a failed step exits 1 on leaving
        res = mtgo.run_trickle(tracker=tracker)
        if res.busy:
            _echo("another trickle run is in progress; nothing asked")
            return
        if res.set_aside:
            pages = "page" if len(res.set_aside) == 1 else "pages"
            kept = trickle.set_aside_path(mtgo.SOURCE).name
            _echo(
                f"set aside {len(res.set_aside)} owed {pages} whose name holds no real date: "
                f"{', '.join(res.set_aside)} (kept in {kept})"
            )
        if res.carried:
            _echo(f"{res.carried} events from {mtgo.misses_path().name} moved to the owed list")
        if res.moved:
            _echo(f"moved {res.moved} stored events into mtgo/<year>/<month>/")
        if res.forgotten:
            months = "month" if len(res.forgotten) == 1 else "months"
            listed, n = ", ".join(res.forgotten), len(res.forgotten)
            _echo(f"forgot {n} {months} saved as listing no events: {listed}; each is read again")
        if res.undated:
            names = "name" if len(res.undated) == 1 else "names"
            listed = ", ".join(res.undated)
            _echo(f"the {res.index} index lists {len(res.undated)} {names} with no real date: {listed}")
        if res.paused_until:
            _echo(
                f"paused until {times.shown(res.paused_until)}: {res.why or 'mtgo.com asked'}; nothing asked"
            )
        elif not res.budget:
            _echo("the last 15 minutes hold as many requests as allowed; nothing asked")
        retried = f" ({res.retried} on a retry)" if res.retried else ""
        young = f", {res.waiting} too new to ask" if res.waiting else ""
        _echo(
            f"{len(res.fetched)} new events{retried} · {len(res.missed)} missed · "
            f"{res.owed} owed, {res.due} due{young} · {res.level} pages a run"
        )
        if res.pending:
            _echo(f"{len(res.pending)} not published yet: {', '.join(res.pending)}")
        now = trickle.now()
        for slug, outcome, when in res.missed:
            _echo(f"  {slug}: {outcome}, still owed; {mtgo.next_try(now, when)}")
        if res.throttled and res.resume:
            _echo(f"paused until {times.shown(res.resume)}: {res.throttled}")
        if res.raised:
            _echo(f"every answer whole: up to {res.level} pages a run")
        if res.stopped:
            _echo(f"stopped early: {res.stopped}")
        for slug, err in res.broken:
            _echo(f"  ! {slug}: its lists wouldn't parse: {err}", err=True)
        for slug, e in res.undue:
            _echo(f"  ! {slug}: can't say when it's due: {failure(e)}; skipped", err=True)


@mtgo_app.command("status")
def mtgo_status() -> None:
    """Pace, pause, recent requests, what's owed by month, and how far back the indexes are read."""
    from collections import Counter

    from riffle import trickle
    from riffle.ingest import mtgo

    st = mtgo.status()
    pace = f"pace       {st.pace.level} pages a run"
    if st.pace.level < trickle.LEVELS[-1]:
        pace += ", one more after each run whose every answer is whole"
    else:
        pace += ", as many as the ceiling allows"
    typer.echo(pace)
    if st.paused_until:
        typer.echo(f"paused     until {times.shown(st.paused_until)}: {st.pace.why or 'mtgo.com asked'}")
    else:
        typer.echo("paused     no")
    typer.echo(f"requests   {len(st.window)} in the last 15 minutes (at most {trickle.CEILING})")
    verdicts = ", ".join(f"{n} {v}" for v, n in Counter(r.verdict for r in st.day).most_common())
    typer.echo(f"           {len(st.day)} in the last 24 hours{': ' + verdicts if verdicts else ''}")
    retries = sorted(
        (s for s, o in st.owed.items() if o.last_try is not None),
        key=lambda s: (st.owed[s].day, s),
        reverse=True,
    )
    never = len(st.owed) - len(retries)
    typer.echo(f"owed       {len(st.owed)} events: {never} never asked, {len(retries)} to retry")
    if st.ages:
        ages = ", ".join(f"{kind} {hours:.1f} h" for kind, hours in sorted(st.ages.items()))
        typer.echo(f"too new    {st.waiting}, each kind asked from the age it first came back whole: {ages}")
    months = sorted(Counter(o.day[:7] for o in st.owed.values()).items(), reverse=True)
    for month, n in months[:12]:
        typer.echo(f"           {month}  {n}")
    if len(months) > 12:
        typer.echo(f"           older    {sum(n for _, n in months[12:])}")
    width = max((len(s) for s in retries[:10]), default=0)
    for n, slug in enumerate(retries[:10]):
        o = st.owed[slug]
        asked = "once" if o.asks == 1 else f"{o.asks} times"
        try:
            at = o.retry_at()
            when = f"next try {times.shown(at)}" if at and at > st.at else "due now"
        except ValueError:
            when = "its date isn't a day; the next run sets it aside"
        typer.echo(f"{'retries' if n == 0 else '':<11}{slug:<{width}}  {o.last}, asked {asked}, {when}")
    if len(retries) > 10:
        typer.echo(f"           and {len(retries) - 10} more")
    read = sorted(st.months)
    if read:
        sweep = "the sweep is done" if st.sweep_done else "still going back"
        typer.echo(f"indexes    read back to {read[0]}, {sweep}")
        for key, miss, days, when in mtgo.unread_months(st.months, st.at):
            asked = "once" if miss.asks == 1 else f"{miss.asks} times"
            empty = f", believed empty on {days} {'day' if days == 1 else 'days'}" if days else ""
            typer.echo(f"           {key}  not read yet: {miss.last}, asked {asked}{empty}; {when}")
        undated = [s for m in st.months.values() for s in m.get("undated", ())]
        if undated:
            listed = ", ".join(undated)
            typer.echo(f"           {len(undated)} listed with no real date, not taken as events: {listed}")
    else:
        typer.echo("indexes    none read yet")
    typer.echo(f"log        {trickle.RequestLog(mtgo.SOURCE).path}")


@mtgo_app.command("forget")
def mtgo_forget(
    slug: str = typer.Argument(..., help="An owed event's slug, as mtgo-owed.json lists it"),
) -> None:
    """Drop an event from the owed list, so the trickle stops asking for it."""
    from riffle.ingest import mtgo

    try:
        dropped = mtgo.forget(slug)
    except RuntimeError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1) from e
    if not dropped:
        typer.echo(f"{slug} isn't owed", err=True)
        raise typer.Exit(1)
    typer.echo(f"forgot {slug}")


@meta_app.command("cards")
def meta_cards(
    fmt: list[str] = FormatsOpt,
    days: int = typer.Option(14),
    kind: list[str] = KindOpt,
    board: str = typer.Option(
        "all", help="all | main | side", autocompletion=_choices("all", "main", "side")
    ),
    top: int = typer.Option(40, help="Rows to show; 0 for all"),
) -> None:
    """Most-played cards: share of decks, average copies, main vs side."""
    from riffle.analysis import metagame

    events = _events(fmt, days, kind)
    n = sum(len(e.decks) for e in events)
    distinct = len({d.fingerprint for e in events for d in e.decks})
    stats = metagame.card_stats(events, board)
    typer.echo(f"{n} decks ({distinct} distinct lists) from {len(events)} events\n")
    typer.echo(f"{'decks':>6} {'share':>6} {'avg':>4}  {'main':>4} {'side':>4}  card")
    for s in stats[: top or None]:
        typer.echo(
            f"{s.decks:>6} {s.share(n):>6.0%} {s.avg:>4.1f}  {s.main_decks:>4} {s.side_decks:>4}  {s.name}"
        )


@meta_app.command("decks")
def meta_decks(
    fmt: list[str] = FormatsOpt,
    days: int = typer.Option(14),
    kind: list[str] = KindOpt,
    card: str = typer.Option(None, "--card", "-c", help="Only decks playing this card"),
    player: str = typer.Option(None, "--player", "-p"),
) -> None:
    """List stored decks; `riffle meta show <event> <player>` prints one.
    The 6-character column is the list's fingerprint: equal values are the same 75."""
    from riffle.analysis import metagame

    for e, d in metagame.find_decks(_events(fmt, days, kind), card, player):
        place = d.record or (f"#{d.rank}" if d.rank else "")
        typer.echo(f"{e.date}  {e.kind:<10} {place:>5}  {d.fingerprint[:6]}  {d.player:<20} {e.slug}")


@meta_app.command("show")
def meta_show(
    event: str = typer.Argument(..., help="Event slug, as `riffle meta decks` lists it"),
    player: str = typer.Argument(...),
    out: Path = typer.Option(None, "-o", "--out", help="Write an MTGO .txt that `riffle own` can read"),
) -> None:
    """Print one stored decklist in MTGO .txt form."""
    import json

    from riffle.ingest import mtgo

    path = mtgo.stored_path(event)
    if not path.exists():
        raise typer.BadParameter(f"no stored event {event} — see what's still owed: riffle mtgo status")
    ev = mtgo.Event.from_dict(json.loads(path.read_text(encoding="utf-8")))
    deck = next((d for d in ev.decks if d.player.lower() == player.lower()), None)
    if deck is None:
        raise typer.BadParameter(f"{player} has no list in {event}")
    if out:
        out.expanduser().write_text(deck.to_text(), encoding="utf-8")
        typer.echo(f"wrote {out}")
    else:
        typer.echo(deck.to_text(), nl=False)


@app.command()
def legal(
    deck: DecksRef,
    fmt: str = typer.Option(
        None,
        "--format",
        "-f",
        help="Check against another format; default: the note's",
        autocompletion=_complete_legal_format,
    ),
) -> None:
    """Is a deck legal? Size, copies, bans, sideboard, commander color identity. 'all' checks every deck."""
    from riffle.analysis import legality

    cfg = config.load()
    targets = vault.decks(cfg.mtg_dir) if deck == "all" else [vault.find(cfg.mtg_dir, deck)]
    with _catalog() as cat:
        reports = [legality.check_deck(d, cat, fmt) for d in targets]
    failed = False
    for d, rep in zip(targets, reports, strict=True):
        head = f"{d.slug} · {rep.format}"
        if rep.legal and not rep.warnings:
            typer.echo(f"✓ {head}")
            continue
        verdict = "✓" if rep.legal else f"✗ {len(rep.errors)} error{'s' * (len(rep.errors) != 1)}"
        typer.secho(f"{verdict} {head}", bold=True)
        for i in rep.errors + rep.warnings:
            mark = "✗" if i.severity == "error" else "!"
            typer.echo(f"  {mark} {i.card + ' — ' if i.card else ''}{i.message}")
        failed = failed or not rep.legal
    if failed:
        raise typer.Exit(1)


def _text(value: object) -> str:
    """A frontmatter value as text: a list's items joined, nothing when it's missing."""
    if isinstance(value, list):
        return ", ".join(map(str, value))
    return "" if value is None else str(value)


def _columns(rows: Sequence[Sequence[str]], right: Collection[int] = ()) -> list[str]:
    """Rows as lines of columns two spaces apart, each as wide as its widest cell. Widths
    count terminal cells, so accented and wide characters line up; columns in `right`
    align right."""
    from rich.cells import cell_len

    widths = [max(map(cell_len, column)) for column in zip(*rows, strict=True)]
    lines = []
    for row in rows:
        cells = []
        for i, (cell, width) in enumerate(zip(row, widths, strict=True)):
            pad = " " * (width - cell_len(cell))
            cells.append(pad + cell if i in right else cell + pad)
        lines.append("  ".join(cells).rstrip())
    return lines


def _colors(deck: Deck) -> str:
    """The note's colors as letters, "WU"; "C" for colorless, nothing when the note doesn't say."""
    value = deck.meta.get("colors")
    if value is None:
        return ""
    letters = "".join(value if isinstance(value, list) else [str(value)]).upper()
    return letters or "C"


def _listed(value: object) -> list[object]:
    """A frontmatter value as a list: a list's items, a value alone, nothing when missing."""
    return [v for v in (value if isinstance(value, list) else [value]) if v is not None]


def _matches(value: object, wanted: str) -> bool:
    """A frontmatter value is wanted, in any case; a list is when any of its items is."""
    return any(str(v).lower() == wanted.lower() for v in _listed(value))


def _known(field: str, wanted: str | None, found: list[Deck]) -> None:
    """A value no deck note has is a typo or a missing note: say which values there are."""
    if wanted is None or not found or any(_matches(d.meta.get(field), wanted) for d in found):
        return
    values = sorted({str(v) for d in found for v in _listed(d.meta.get(field))})
    if len(values) <= 12:
        hint = f"; the vault has {', '.join(values)}" if values else ""
    else:
        import difflib

        close = difflib.get_close_matches(wanted, values, n=3)
        hint = f"; close: {', '.join(close)}" if close else ""
    raise typer.BadParameter(f"no deck has {field} {wanted}{hint}", param_hint=f"'--{field}'")


SORTS = ("name", "format", "strategy", "colors", "cards", "checked", "to-buy")


@app.command()
def decks(
    fmt: str = typer.Option(None, "--format", help="Only this format: commander, modern, …"),
    strategy: str = typer.Option(None, "--strategy", help="Only this strategy: aggro, control, …"),
    colors: str = typer.Option(None, "--colors", help="Exactly these colors, any order: wu; c for colorless"),
    archetype: str = typer.Option(None, "--archetype", help="Only this archetype: tribal, ramp, …"),
    to_buy: int = typer.Option(None, "--to-buy", min=0, help="At most N cards left to buy"),
    sort: str = typer.Option("name", "--sort", help=" | ".join(SORTS), autocompletion=_choices(*SORTS)),
) -> None:
    """List the deck notes: format, strategy, colors, cards, when the list was last checked.
    Filters narrow it; --to-buy reads the collection and adds a column of cards still to buy."""
    if sort not in SORTS:
        raise typer.BadParameter(f"one of {', '.join(SORTS)}", param_hint="'--sort'")
    wanted_colors = None
    if colors is not None:
        if not colors or set(colors.upper()) - set("WUBRGC"):
            raise typer.BadParameter("letters from wubrg, or c for colorless", param_hint="'--colors'")
        wanted_colors = set(colors.upper()) - {"C"}
    cfg = config.load()
    found = vault.decks(cfg.mtg_dir)
    for field, wanted in (("format", fmt), ("strategy", strategy), ("archetype", archetype)):
        _known(field, wanted, found)
    kept = [
        d
        for d in found
        if all(
            wanted is None or _matches(d.meta.get(field), wanted)
            for field, wanted in (("format", fmt), ("strategy", strategy), ("archetype", archetype))
        )
        and (wanted_colors is None or set(_colors(d)) - {"C"} == wanted_colors)
    ]
    short: dict[str, int] = {}
    if to_buy is not None or sort == "to-buy":
        from riffle.analysis.ownership import BUY, summary

        with _setup() as (_, cat, inv):
            short = {d.slug: summary(syncmod.analyse(d, inv, cat).rows)[BUY] for d in kept}
        if to_buy is not None:
            kept = [d for d in kept if short[d.slug] <= to_buy]
    if not kept:
        if found:
            _echo("no decks match", err=True)
            raise typer.Exit(1)
        return
    keys = {
        "name": lambda d: (d.slug,),
        "format": lambda d: (_text(d.meta.get("format")), d.slug),
        "strategy": lambda d: (_text(d.meta.get("strategy")), d.slug),
        "colors": lambda d: (_colors(d), d.slug),
        "cards": lambda d: (d.count(), d.slug),
        "checked": lambda d: (_text(d.meta.get("checked")), d.slug),
        "to-buy": lambda d: (short[d.slug], d.slug),
    }
    rows = []
    for d in sorted(kept, key=keys[sort]):
        checked = _text(d.meta.get("checked"))
        row = [
            d.slug,
            _text(d.meta.get("format")),
            _text(d.meta.get("strategy")),
            _colors(d),
            f"{d.count()} cards",
            f"checked {checked}" if checked else "not checked",
        ]
        if short:
            row.append(f"{short[d.slug]} to buy")
        rows.append(row)
    for line in _columns(rows, right={4} | ({6} if short else set())):
        typer.echo(line)


SHOW = ("buy", "own", "all")


@app.command()
def own(
    deck: DeckRef,
    show: str = typer.Option("buy", "--show", "-s", help="buy | own | all", autocompletion=_choices(*SHOW)),
    all_cards: bool = typer.Option(False, "--all", "-a", help="Same as --show all"),
    on_arena: bool = typer.Option(
        False, "--arena", help="Check against your Arena collection instead of paper"
    ),
) -> None:
    """What to buy for a deck. Numbers are copies in the deck. -a adds what you own."""
    from riffle.analysis.ownership import BUY, MARK, OWN, summary, wildcards

    show = "all" if all_cards else show
    if show not in SHOW:
        raise typer.BadParameter(f"--show must be one of {', '.join(SHOW)}")
    cfg = config.load()
    if on_arena and not cfg.arena_list.exists():
        raise typer.BadParameter("no Arena collection yet — run: riffle ingest arena <file>")
    with _catalog() as cat:
        if on_arena:
            inv = syncmod.arena_inventory(cfg.arena_list, cat)
        else:
            inv = syncmod.inventory(cfg.collection_csv, cat)
        rep = syncmod.analyse(vault.find(cfg.mtg_dir, deck), inv, cat)
        wc = wildcards(rep.rows, cat) if on_arena else {}
    buy_word = "Craft" if on_arena else "Buy"

    wanted = {"buy": [BUY], "own": [OWN], "all": [BUY, OWN]}[show]
    for status in wanted:
        rows = [r for r in rep.rows if r.status == status]
        if not rows:
            continue
        if len(wanted) > 1:
            typer.secho(f"\n{buy_word if status == BUY else 'Own'} ({len(rows)})", bold=True)
        for r in rows:
            n = r.shortfall if status == BUY else r.needed
            extra = f"  (own {r.owned} of {r.needed})" if status == BUY and r.partial else ""
            typer.echo(f"{MARK[status]} {n:>2}  {r.name}{extra}")

    s = summary(rep.rows)
    typer.echo(f"\n{MARK[OWN]} {s[OWN]} own · {MARK[BUY]} {s[BUY]} {buy_word.lower()}")
    if on_arena:
        typer.echo("wildcards: " + " · ".join(f"{n} {k}" for k, n in wc.items() if n))
    elif show == "buy" and s[OWN]:
        typer.echo("(-a to list what you own too)")
    if rep.unresolved:
        typer.echo(f"unmatched: {', '.join(rep.unresolved)}", err=True)


@app.command()
def price(
    deck: DeckRef, budget_tix: float = typer.Option(None, help="Compare the MTGO total to a tix budget")
) -> None:
    """Paper cost to finish the deck, and MTGO cost for the whole list."""
    with _setup() as (cfg, cat, inv):
        rep = syncmod.analyse(vault.find(cfg.mtg_dir, deck), inv, cat)
    dp = rep.price
    typer.echo(f"paper, whole deck    ${dp.usd_total:,.2f}")
    typer.echo(f"paper, still to buy  ${dp.usd_to_buy:,.2f}")
    typer.echo(f"MTGO, whole deck     {dp.tix_total:,.2f} tix")
    if budget_tix is not None:
        verdict = "fits" if dp.tix_total <= budget_tix else "over by"
        extra = "" if dp.tix_total <= budget_tix else f" {dp.tix_total - budget_tix:,.2f}"
        typer.echo(f"  {verdict}{extra} a {budget_tix:,.0f}-tix budget")
    if dp.missing_on_mtgo:
        typer.echo(f"not on MTGO: {', '.join(dp.missing_on_mtgo)}")


@prices_app.command("log")
def prices_log(
    cards: list[str] = typer.Argument(None, help="Card names [default: every unticked #mtg/buy line's]"),
) -> None:
    """Each card's price on every Scryfall day the store keeps, a line a card a day:
    `date | card | paper | tix`, by the rule deck prices use. What _log/prices.md held."""
    from riffle.analysis import pricing
    from riffle.export import obsidian
    from riffle.ingest import scryfall

    days = scryfall.price_days()
    if not days:
        _echo("no Scryfall price days kept yet; riffle sync keeps one a day", err=True)
        raise typer.Exit(1)
    failed = False
    if cards:
        names = cards
    else:
        unreadable: vault.Unreadable = []
        names = vault.buy_cards(config.load().notes, unreadable)
        for path, why in unreadable:
            _echo(f"  ! can't read {path} ({why}); its buy lines are left out", err=True)
    with _catalog() as cat:
        wanted: dict[str, str] = {}  # card_id -> name, as the catalog has it
        for name in names:
            card_id = cat.resolve(name)
            if card_id is None:
                _echo(f"no card named {name!r}" if cards else f"  ! buy list: unmatched {name}", err=True)
                failed = failed or bool(cards)
            else:
                wanted[card_id] = cat.name(card_id)
        printings = cat.price_printings(wanted)
    order = sorted(wanted, key=lambda card_id: wanted[card_id])
    for day, path in days:
        try:
            prices = pricing.on_day(printings, scryfall.day_prices(path, pricing.scryfall_ids(printings)))
        except scryfall.Unreadable as e:
            _echo(f"  ! {day}: {e}; that day is left out", err=True)
            failed = True
            continue
        for card_id in order:
            typer.echo(obsidian.price_line(day.isoformat(), wanted[card_id], prices[card_id]))
    if failed:
        raise typer.Exit(1)


@app.command("export")
def export_deck(
    deck: DecksRef,
    to: str = typer.Option(
        "moxfield",
        help="moxfield | manabox | mtgo | arena | tcgplayer",
        autocompletion=_choices(*formats.FORMATS),
    ),
    pin: str = typer.Option(
        "owned", help="owned | none — pin printings you own", autocompletion=_choices("owned", "none")
    ),
    out: Path = typer.Option(None, "-o", "--out", help="File, or a folder when the deck is 'all'"),
) -> None:
    """Write a deck — or every deck, with 'all' — in a format another app imports."""
    if to not in formats.FORMATS:
        raise typer.BadParameter(f"--to must be one of {', '.join(formats.FORMATS)}")
    if deck == "all" and out is None:
        raise typer.BadParameter("exporting all decks needs --out <folder>")
    with _setup() as (cfg, cat, inv):
        pins = formats.owned_printings(inv.holdings) if pin == "owned" else {}
        targets = vault.decks(cfg.mtg_dir) if deck == "all" else [vault.find(cfg.mtg_dir, deck)]
        rendered = []
        for d in targets:
            rep = syncmod.analyse(d, inv, cat)
            rendered.append((d, rep.unresolved, formats.render(to, rep.deck, rep.rows, cat, pins)))
    for d, unresolved, text in rendered:
        if deck == "all":
            folder = out.expanduser()
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / f"{d.slug}-{to}.txt"
            path.write_text(text, encoding="utf-8")
            typer.echo(f"wrote {path}")
        elif out:
            out.expanduser().write_text(text, encoding="utf-8")
            typer.echo(f"wrote {out}")
        else:
            typer.echo(text, nl=False)
        if unresolved:
            typer.echo(f"{d.slug}: unmatched (left as written): {', '.join(unresolved)}", err=True)


def _run_sync(tracker: Tracker, offline: bool = True) -> None:
    """Everything the data affects: collection pickup, prices, generated notes, logs."""
    from riffle.ingest import manabox

    cfg0 = config.load()
    if not offline:
        offline = not _online(tracker)
    if not offline:
        _refresh(tracker)
    _snapshot_prices(tracker, online=not offline)
    if not offline:
        disk.check(tracker)
    newest = manabox.newest_export(cfg0.downloads)
    stored = cfg0.collection_csv
    if newest and (not stored.exists() or newest.stat().st_mtime > stored.stat().st_mtime):
        _copy_manabox(newest)
        _echo(f"picked up {newest.name} from Downloads")
    if not cfg0.collection_csv.exists():
        _echo(
            "no collection yet — export from ManaBox to ~/Downloads, or: riffle ingest manabox <csv>",
            err=True,
        )
    if cfg0.old_notes:
        tracker.step("vault setting").warn(cfg0.old_notes)
    for why in cfg0.unread:
        tracker.step("setting").warn(why)
    if not cfg0.notes.is_dir():  # a missing or mistyped vault: nothing is written there
        tracker.step("vault").fail(
            f"no notes folder at {cfg0.notes}; set vault and notes in {config.config_path()}"
        )
        return
    if not cfg0.mtg_dir.is_dir():  # a user of other games only: not a fault
        tracker.step("vault").ok(f"no MTG decks in {cfg0.notes}; Riffle reads MTG decks only for now")
        return
    pictures: list[Step] = []

    def progress(done: int, total: int | None) -> None:
        if not pictures:
            pictures.append(tracker.step("card pictures", total, "pictures"))
        pictures[0].update(done, total)

    with _setup() as (cfg, cat, inv):
        card_notes = (
            syncmod.CardNotes(cfg.vault, cfg.notes, cfg.card_images, None if offline else _picture, progress)
            if cfg.card_notes
            else None
        )
        res = syncmod.run(cfg.mtg_dir, inv, cat, card_notes=card_notes)
    if pictures and res.cards:
        pictures[0].ok(f"{res.cards.fetched:,} fetched")
    for label, why in res.failed:
        tracker.step(label).fail(why)
    _echo(
        f"{len(res.decks)} decks · {res.changed_notes} notes updated · "
        f"versions changed: {', '.join(res.versions) or 'none'}"
    )
    if res.cards:
        c = res.cards
        _echo(f"card notes: {c.cards:,} ({c.written:,} written, {c.removed:,} removed)")
    elif res.cards_removed:
        _echo(f"card notes: off ({res.cards_removed:,} removed)")
    if res.removed:
        _echo(f"removed stale generated notes: {', '.join(res.removed)}")
    for w in res.warnings:
        _echo(f"  ! {w}", err=True)


def _picture(url: str, dest: Path) -> int | None:
    """A card's picture from Scryfall's image server, written whole to dest."""
    return net.download(url, dest, accept="image/*", timeout=30)


NETWORK_WAIT = 120.0  # seconds a sync waits for the network before carrying on offline


def _hosts() -> list[str]:
    """Every host an online sync asks, Scryfall's first."""
    from urllib.parse import urlparse

    from riffle.ingest import cardmarket, goatbots, mtgjson, pricelists, scryfall, tcgcsv

    urls = [scryfall.BULK_INDEX, mtgjson.BASE, goatbots.BASES[0], cardmarket.BASE, tcgcsv.BASE]
    urls += [plist.url for plist in pricelists.CARD_KINGDOM + pricelists.MANA_POOL]
    return list(dict.fromkeys(host for url in urls if (host := urlparse(url).hostname)))


def _online(tracker: Tracker) -> bool:
    """Whether any source can be reached, after waiting up to NETWORK_WAIT for one: the
    scheduled job runs as the Mac wakes, before the network is back. Without any the sync goes
    offline. One source that's down (Scryfall's name not resolving, say) fails its own steps
    and costs the others nothing."""
    step = tracker.step("network")
    waited = net.wait_online(*_hosts(), timeout=NETWORK_WAIT)
    if waited is None:
        step.fail(f"no connection after {elapsed(NETWORK_WAIT)}; syncing offline")
        return False
    if waited < 1:
        step.drop()
    else:
        step.ok(f"up after {elapsed(waited)}")
    return True


def _resync() -> None:
    """An offline sync, for commands that change local data."""
    with _run("riffle sync") as tracker:
        _run_sync(tracker, offline=True)


@app.command("sync")
def sync_cmd(
    offline: bool = typer.Option(False, help="Skip the Scryfall refresh and price downloads"),
    watch: bool = typer.Option(
        False, "--watch", help="Fetch nothing: resync whenever a deck note is saved or a ManaBox export lands"
    ),
    interval: float = typer.Option(5.0, help="Seconds between checks, with --watch"),
) -> None:
    """Refresh card data, keep today's prices, pick up a ManaBox export, rewrite _generated/, append _log/.
    With --watch, resync from what's kept at every change instead, until Ctrl-C."""
    if watch:
        _watch_vault(interval)
        return
    with _run("riffle sync") as tracker:
        _run_sync(tracker, offline=offline)


def _watched(cfg: config.Config) -> dict[str, float]:
    """Everything whose change should trigger a resync, with its mtime."""
    from riffle.ingest import manabox

    paths = [p for p in vault.deck_notes(cfg.mtg_dir)]
    paths += [cfg.collection_csv, cfg.arena_list, config.config_path()]
    newest = manabox.newest_export(cfg.downloads)
    if newest:
        paths.append(newest)
    return {str(p): p.stat().st_mtime for p in paths if p.exists()}


def _resync_and_keep_watching() -> None:
    """A failed resync is reported, and watching goes on: the next save may fix it."""
    try:
        _resync()
    except typer.Exit:
        typer.echo("resync failed; still watching", err=True)
    except Exception as e:
        typer.echo(f"resync failed: {failure(e)}; still watching", err=True)


@app.command("status")
def status_cmd(
    days: int = typer.Option(None, "--days", "-d", min=1, max=90, help="Days each strip shows [default: 14]"),
) -> None:
    """What Riffle has kept, source by source and day by day, with gaps marked; then the MTGO
    backlog, the scheduled jobs, the store's size and free disk, and the backup. Reads only;
    exits 1 if a source's files can't be read."""
    from riffle import status

    lines, readable = status.report(times.now(), days or status.DAYS, config.data_dir())
    for line in lines:
        typer.echo(line)
    if not readable:
        raise typer.Exit(1)


CHECKS = ("prices", "vault")


@app.command("check")
def check_cmd(
    what: str = typer.Argument("prices", help=" | ".join(CHECKS), autocompletion=_choices(*CHECKS)),
) -> None:
    """Check every kept price file: filed under the day its own stamp says, and made before it
    was fetched. With vault, check every card link in the notes opens the card's note. Reads
    only; exits 1 if anything is wrong."""
    from riffle.ingest import checks

    if what not in CHECKS:
        raise typer.BadParameter(f"what to check: {' or '.join(CHECKS)}, not {what!r}")
    if what == "vault":
        _check_vault()
        return
    wrong = 0
    for rep in checks.run():
        typer.echo(f"{rep.source:<14}{rep.summary()}")
        for note in rep.notes:
            typer.echo(f"{'':<14}{note}")
        for problem in rep.problems:
            typer.echo(f"  ! {problem}", err=True)
        wrong += len(rep.problems)
    if wrong:
        typer.echo(f"{wrong} price file{'s' * (wrong != 1)} dated wrong", err=True)
        raise typer.Exit(1)


def _check_vault() -> None:
    """Each card link that can't open the card's note as written, with the link that would, and
    each card name another note has."""
    from riffle.export import cards
    from riffle.export.links import safe_name, written

    cfg = config.load()
    with _catalog() as cat:
        found = cards.gather(vault.linking(cfg.notes))
        own = cards.gather(vault.riffle_notes(cfg.mtg_dir))
        links = cards.sort(found, cat, vault.names(cfg.vault), own)
    n = len(links.cards)
    typer.echo(f"card links: {n:,} card{'s' * (n != 1)} linked in {config.tilde(cfg.notes)}")
    for target, (fix, notes) in links.misses.items():
        typer.echo(f"  ! {written(target)} in {notes} note{'s' * (notes != 1)}: write {fix}", err=True)
    for name, path in links.taken.items():
        typer.echo(f"  ! {name}: {config.tilde(path)} has that name, so the card has no note", err=True)
    for name, other in links.clashes.items():
        typer.echo(f"  ! {name}: its note would be {safe_name(name)}, which names {other}; no note", err=True)
    misses, without = len(links.misses), len(links.taken) + len(links.clashes)
    if misses or without:
        typer.echo(
            f"{misses} link{'s' * (misses != 1)} can't open the card's note; "
            f"{without} linked card{'s' * (without != 1)} without a note",
            err=True,
        )
        raise typer.Exit(1)
    typer.echo("every card link opens the card's note")


@app.command("card")
def card_cmd(
    name: str = typer.Argument(..., help="The card's name, full, front face, or as a link writes it"),
    open_page: bool = typer.Option(False, "--open", help="Open the card's page on Scryfall"),
) -> None:
    """What a card's note holds: each face's text, the price, legality in the formats your lists
    are in, and the copies you own. Reads only."""
    import webbrowser

    from riffle.export import cards

    with _setup() as (cfg, cat, inv):
        card_id = cat.resolve(name)
        if card_id is None:
            typer.echo(f"no card named {name!r}", err=True)
            raise typer.Exit(1)
        view = cat.card_views([card_id]).get(card_id)
        day = cat.prices_day()
        facts = [
            cards.price_line(cat.prices([card_id]).get(card_id), day.isoformat() if day else None),
            cards.legality(cat.rules([card_id]).get(card_id), vault.formats(cfg.mtg_dir)),
            cards.owned([h for h in inv.holdings if h.card_id == card_id]),
        ]
        full = cat.name(card_id)
    faces = view.faces if view else ()
    if len(faces) != 1:
        typer.echo(full)
    for face in faces:
        head, *rest = cards.face_lines(face)
        typer.echo(head)
        for line in rest:
            typer.echo(f"  {line}")
    for fact in facts:
        if fact:
            typer.echo(fact)
    url = cards.scryfall_url(full, view)
    typer.echo(url)
    if open_page:
        webbrowser.open(url)


def _watch_vault(interval: float) -> None:
    """Resync whenever a deck note is saved or a new ManaBox export lands. Ctrl-C to stop."""
    import time

    cfg = config.load()
    typer.echo(f"watching {cfg.mtg_dir} and ~/Downloads — Ctrl-C to stop")
    _resync_and_keep_watching()
    seen = _watched(cfg)
    try:
        while True:
            time.sleep(interval)
            now = _watched(cfg)
            if now != seen:
                changed = sorted(
                    Path(p).name for p in set(now) ^ set(seen) | {p for p in now if seen.get(p) != now[p]}
                )
                typer.echo(f"\n{times.local(times.now(), '%H:%M:%S')} changed: {', '.join(changed)}")
                _resync_and_keep_watching()
                seen = _watched(cfg)
    except KeyboardInterrupt:
        typer.echo("\nstopped")


schedule_app = typer.Typer(
    help="The launchd jobs (macOS): `riffle sync` daily, the MTGO trickle, and each store's watch."
)
app.add_typer(schedule_app, name="schedule")


def _show_schedule() -> None:
    from riffle import schedule as sched

    _show_sync()
    typer.echo()
    _show_every(sched.TRICKLE, "trickle", "riffle schedule trickle")
    for job in _watch_jobs():
        typer.echo()
        _show_every(job, f"{job.args[-1]} watch", "riffle schedule watch")


def _watch_jobs() -> list:
    from riffle import schedule as sched
    from riffle import watching

    return [sched.watch_job(store) for store in watching.STORES]


def _show_every(job, what: str, start: str) -> None:
    """A job that runs every few minutes: whether it's loaded, and how its runs went."""
    from riffle import schedule as sched

    st = sched.status(job=job)
    if not st.installed and not st.loaded:
        typer.echo(f"no {what} job — start one with: {start}")
        return
    typer.echo(job.label)
    typer.echo(f"  every      {(job.interval or 0) // 60} minutes: riffle {' '.join(job.args)}")
    typer.echo(f"  loaded     {'yes' if st.loaded else 'NO — reload with: ' + start}")
    if st.loaded:
        typer.echo(
            f"  runs       {st.runs or '0'}   last exit {st.last_exit or '—'}   state {st.state or '—'}"
        )
    typer.echo(f"  log        {sched.log_path(job)}")
    typer.echo(f"  plist      {sched.plist_path(job)}")


def _show_sync() -> None:
    from riffle import schedule as sched

    st = sched.status()
    if not st.installed and not st.loaded:
        typer.echo("no schedule — set one with: riffle schedule set 07:00")
        return
    now = times.now().astimezone()  # launchd runs the job by the Mac's clock
    at = ", ".join(sched.fmt(t) for t in st.times) or "(none in plist)"
    nxt = sched.next_run(st.times, now.replace(tzinfo=None))
    typer.echo(f"{sched.LABEL}")
    typer.echo(f"  times      {at}  (24-hour, daily, the Mac's time: {now.tzname()})")
    typer.echo(
        f"  next run   {times.local(nxt.astimezone(), '%a %Y-%m-%d %H:%M')}" if nxt else "  next run   —"
    )
    reload = "riffle schedule set " + " ".join(sched.fmt(t) for t in st.times)
    typer.echo(f"  loaded     {'yes' if st.loaded else 'NO — reload with: ' + reload}")
    if st.loaded:
        typer.echo(
            f"  runs       {st.runs or '0'}   last exit {st.last_exit or '—'}   state {st.state or '—'}"
        )
    typer.echo(f"  log        {sched.log_path()}")
    typer.echo(f"  plist      {sched.plist_path()}")


@schedule_app.callback(invoke_without_command=True)
def schedule_main(ctx: typer.Context) -> None:
    """Show every job (times, next run, last result). Subcommands: set, remove, trickle, watch."""
    if ctx.invoked_subcommand is None:
        _show_schedule()


@schedule_app.command("show")
def schedule_show() -> None:
    """Times, next run, whether each job is loaded, and how its last run went."""
    _show_schedule()


@schedule_app.command("set")
def schedule_set(
    times: list[str] = typer.Argument(..., help="24-hour HH:MM times, e.g. 07:00 19:30"),
) -> None:
    """Run `riffle sync` daily at these times. Replaces any existing schedule."""
    from riffle import schedule as sched

    try:
        parsed = sched.parse_times(times)
        sched.install(parsed)
    except (ValueError, RuntimeError) as e:
        raise typer.BadParameter(str(e)) from e
    _show_schedule()


@schedule_app.command("remove")
def schedule_remove() -> None:
    """Unload and delete the daily sync job."""
    from riffle import schedule as sched

    typer.echo(f"removed {sched.LABEL}" if sched.remove() else "no schedule to remove")


@schedule_app.command("trickle")
def schedule_trickle(
    remove: bool = typer.Option(False, "--remove", help="Unload and delete the trickle job instead"),
) -> None:
    """Run `riffle mtgo trickle` every 10 minutes. Replaces any existing trickle job."""
    from riffle import schedule as sched

    if remove:
        removed = sched.remove(job=sched.TRICKLE)
        typer.echo(f"removed {sched.TRICKLE.label}" if removed else "no trickle job to remove")
        return
    try:
        sched.install([], job=sched.TRICKLE)
    except RuntimeError as e:
        raise typer.BadParameter(str(e)) from e
    _show_every(sched.TRICKLE, "trickle", "riffle schedule trickle")


@schedule_app.command("watch")
def schedule_watch(
    remove: bool = typer.Option(False, "--remove", help="Unload and delete the watch jobs instead"),
) -> None:
    """Run `riffle watch <store>` every 5 minutes for each store, each its own job, so a long
    fetch holds up only its own store. Replaces any existing watch jobs."""
    from riffle import schedule as sched

    jobs = _watch_jobs()
    if remove:
        for job in jobs:
            typer.echo(f"removed {job.label}" if sched.remove(job=job) else f"no {job.label} to remove")
        return
    try:
        for job in jobs:
            sched.install([], job=job)
    except RuntimeError as e:
        raise typer.BadParameter(str(e)) from e
    for n, job in enumerate(jobs):
        if n:
            typer.echo()
        _show_every(job, f"{job.args[-1]} watch", "riffle schedule watch")


db_app = typer.Typer(help="The Postgres database: start it, migrate it, check it.", no_args_is_help=True)
app.add_typer(db_app, name="db")


def _db_engine():
    from riffle import db

    try:
        return db.engine()
    except db.BadURL as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1) from e


def _unreachable(url: str, e: Exception) -> typer.Exit:
    from riffle import db

    typer.echo(f"can't reach Postgres at {db.display(url)}: {db.reason(e)}", err=True)
    typer.echo("start it with: riffle db up", err=True)
    return typer.Exit(1)


@db_app.command("up")
def db_up() -> None:
    """Start the local Postgres (Docker Compose) and wait until it's healthy."""
    from riffle.db import compose

    try:
        code = compose.up()
    except FileNotFoundError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1) from e
    raise typer.Exit(code)


@db_app.command("upgrade")
def db_upgrade() -> None:
    """Apply every migration the database doesn't have yet."""
    from sqlalchemy.exc import OperationalError

    from riffle.db import migrate

    eng = _db_engine()
    try:
        before = migrate.current(eng)
        migrate.upgrade(eng)
        after = migrate.current(eng)
    except OperationalError as e:
        raise _unreachable(eng.url.render_as_string(hide_password=False), e) from e
    if before == after:
        typer.echo(f"schema     {after} (head), nothing to apply")
    else:
        typer.echo(f"schema     {before or 'empty'} → {after} (head)")


@db_app.command("status")
def db_status() -> None:
    """Server, schema revision, and whether migrations are pending. Exits 1 unless reachable and current."""
    from sqlalchemy import text
    from sqlalchemy.exc import OperationalError

    from riffle import db
    from riffle.db import migrate

    eng = _db_engine()
    url = eng.url.render_as_string(hide_password=False)
    typer.echo(f"database   {db.display(url)}")
    try:
        with eng.connect() as conn:
            version = conn.execute(text("SHOW server_version")).scalar_one()
        current = migrate.current(eng)
    except OperationalError as e:
        raise _unreachable(url, e) from e
    head = migrate.head()
    typer.echo(f"server     PostgreSQL {str(version).split()[0]}")
    if current == head:
        typer.echo(f"schema     {current} (head)")
        return
    have = f"{current}, head is {head}" if current else f"empty, head is {head}"
    typer.echo(f"schema     {have} — run: riffle db upgrade")
    raise typer.Exit(1)


if __name__ == "__main__":
    app()
