"""The only user-facing surface. Everything here is a thin wrapper."""

import shutil
from collections.abc import Callable, Collection, Iterable, Iterator, Sequence
from contextlib import ExitStack, contextmanager
from datetime import date, timedelta
from pathlib import Path
from typing import Annotated

import typer

from riffle import config, net, vault
from riffle import sync as syncmod
from riffle.export import formats
from riffle.progress import Tracker, Watched, elapsed, open_tracker
from riffle.store import Catalog

app = typer.Typer(help="Collection, decks, prices, and the Obsidian vault.", no_args_is_help=True)
ingest_app = typer.Typer(help="Load outside data.", no_args_is_help=True)
app.add_typer(ingest_app, name="ingest")
meta_app = typer.Typer(help="MTGO metagame: league 5-0s, challenges, showcases.", no_args_is_help=True)
app.add_typer(meta_app, name="meta")

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


@contextmanager
def _catalog() -> Iterator[Catalog]:
    """The card catalog for the rest of the block, or exit 1 saying what to run."""
    from riffle.store import postgres

    with ExitStack() as stack:
        try:
            cat = stack.enter_context(postgres.open_catalog())
        except postgres.Unavailable as e:
            typer.echo(str(e), err=True)
            raise typer.Exit(1) from e
        yield cat


@contextmanager
def _tracked(title: str) -> Iterator[Tracker]:
    """A command's steps. A failed step doesn't stop the command: it finishes its work, then
    names what failed and exits 1, so the scheduled job's last exit shows it."""
    with open_tracker(title) as tracker:
        watched = Watched(tracker)
        yield watched
    if watched.failed:
        n = len(watched.failed)
        typer.echo(f"{n} step{'s' * (n != 1)} failed: {', '.join(watched.failed)}", err=True)
        raise typer.Exit(1)


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
    typer.echo(f"data       {config.data_dir()}")
    typer.echo(f"database   {cfg.database_url}")
    for key, why in (cfg.obsolete or {}).items():
        typer.echo(f"  ! `{key}` in config is ignored: {why}", err=True)


def _refresh(tracker: Tracker, force: bool = False) -> None:
    """Scryfall's bulk file and set list, downloaded and loaded into the card catalog."""
    from riffle.ingest import scryfall, scryfall_catalog

    scryfall.refresh(force=force, tracker=tracker)
    scryfall_catalog.update(tracker=tracker, force=force)


@ingest_app.command("scryfall")
def ingest_scryfall(
    force: bool = typer.Option(False, help="Download and load even if under a day old"),
    no_sync: bool = typer.Option(False, "--no-sync", help="Don't resync the vault afterwards"),
) -> None:
    """Download Scryfall's bulk card data and set list, and load them."""
    with _tracked("riffle ingest scryfall") as tracker:
        _refresh(tracker, force=force)
        if no_sync:
            _snapshot_prices(tracker, online=False)
        else:
            _run_sync(tracker, offline=True)


def _copy_manabox(src: Path) -> Path:
    dest = config.load().collection_csv
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src.expanduser(), dest)
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


@ingest_app.command("mtgo")
def ingest_mtgo(
    fmt: list[str] = FormatsOpt,
    days: int = typer.Option(7, help="How far back to look"),
    kind: list[str] = KindOpt,
    delay: float = typer.Option(1.0, help="Seconds between page requests"),
    max_events: int | None = typer.Option(
        None,
        "--max-events",
        min=1,
        help="Fetch at most this many events, newest first; later runs go further back",
    ),
) -> None:
    """Fetch MTGO decklists (league 5-0s, challenges, showcases) from mtgo.com."""
    from riffle.ingest import mtgo

    since, fmts, kinds = date.today() - timedelta(days=days), _formats(fmt), _kinds(kind)
    with _tracked("riffle ingest mtgo") as tracker:  # report inside: a failed step exits 1 on leaving
        res = mtgo.ingest(fmts, since, kinds=kinds, delay=delay, max_events=max_events, tracker=tracker)
        typer.echo(f"{len(res.fetched)} new events · {res.skipped} already stored · {mtgo.store_dir()}")
        for slugs, what in (
            (res.pending, "not published yet — retried next run"),
            (res.empty, "empty though old enough to have lists, likely throttled — retried next run"),
            (res.given_up, f"empty on {mtgo.GIVE_UP_AFTER} runs — skipped from now on"),
        ):
            if slugs:
                typer.echo(f"{len(slugs)} {what}: {', '.join(slugs)}")
        if res.missed:
            typer.echo(f"{res.missed} skipped, given up on earlier runs")
        if res.given_up or res.missed:
            typer.echo(f"  to retry them, delete {mtgo.misses_path()}")
        if res.left:
            typer.echo(f"{res.left} left for the next run")
        if res.stopped:
            typer.echo(f"stopped early: {res.stopped}", err=True)
        for slug, err in res.failed:
            typer.echo(f"  ! {slug}: {err}", err=True)


@ingest_app.command("prices")
def ingest_prices(
    delay: float = typer.Option(0.1, help="Seconds between tcgcsv requests"),
) -> None:
    """Keep today's prices: tcgcsv's for everything it carries, Scryfall's and MTGJSON's for Magic."""
    with _tracked("riffle ingest prices") as tracker:
        _snapshot_prices(tracker, online=True, delay=delay)


def _snapshot_prices(tracker: Tracker, online: bool, delay: float = 0.1) -> None:
    """Today's price snapshot. Problems are reported, never raised: a sync must finish without it."""
    from riffle.ingest import mtgjson, scryfall, tcgcsv

    step = tracker.step("Scryfall prices")
    try:
        path, written = scryfall.snapshot_prices()
    except FileNotFoundError:
        step.fail("no bulk file yet — run: riffle ingest scryfall")
    except (OSError, ValueError, KeyError) as e:
        step.fail(str(e))
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
    try:
        mtgjson.snapshot(tracker=tracker)
    except OSError as e:
        tracker.step("MTGJSON prices").fail(str(e))
    try:
        snap = tcgcsv.snapshot(delay=delay, tracker=tracker)
    except (OSError, net.FetchError) as e:
        tracker.step("tcgcsv prices").fail(str(e))
        return
    typer.echo(f"tcgcsv prices: {snap.day} · {snap.requests} requests")


def _events(fmt: list[str], days: int, kind: list[str] | None):
    from riffle.ingest import mtgo

    events = mtgo.load(_formats(fmt), date.today() - timedelta(days=days), _kinds(kind))
    if not events:
        names = "/".join(fmt)
        typer.echo(
            f"no stored {names} events in the last {days} days — run: riffle ingest mtgo -f {fmt[0]}",
            err=True,
        )
        raise typer.Exit(1)
    return events


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

    path = mtgo.store_dir() / f"{event}.json"
    if not path.exists():
        raise typer.BadParameter(f"no stored event {event} — run: riffle ingest mtgo")
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


@app.command()
def decks() -> None:
    """List the deck notes the vault holds."""
    cfg = config.load()
    rows = [
        (d.slug, _text(d.meta.get("format")), _text(d.meta.get("status")), f"{d.count()} cards")
        for d in vault.decks(cfg.mtg_dir)
    ]
    for line in _columns(rows, right={3}):
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
    newest = manabox.newest_export(cfg0.downloads)
    stored = cfg0.collection_csv
    if newest and (not stored.exists() or newest.stat().st_mtime > stored.stat().st_mtime):
        _copy_manabox(newest)
        typer.echo(f"picked up {newest.name} from Downloads")
    if not cfg0.collection_csv.exists():
        typer.echo(
            "no collection yet — export from ManaBox to ~/Downloads, or: riffle ingest manabox <csv>",
            err=True,
        )
    with _setup() as (cfg, cat, inv):
        res = syncmod.run(cfg.mtg_dir, inv, cat)
    typer.echo(
        f"{len(res.decks)} decks · {res.changed_notes} notes updated · "
        f"{res.prices_logged} prices logged · versions changed: {', '.join(res.versions) or 'none'}"
    )
    if res.removed:
        typer.echo(f"removed stale generated notes: {', '.join(res.removed)}")
    for w in res.warnings:
        typer.echo(f"  ! {w}", err=True)


NETWORK_WAIT = 120.0  # seconds a sync waits for the network before carrying on offline


def _online(tracker: Tracker) -> bool:
    """Whether Scryfall can be reached, after waiting up to NETWORK_WAIT for it: the scheduled
    job runs as the Mac wakes, before the network is back. Without it the sync goes offline."""
    from urllib.parse import urlparse

    from riffle.ingest import scryfall

    step = tracker.step("network")
    waited = net.wait_online(urlparse(scryfall.BULK_INDEX).hostname or "", timeout=NETWORK_WAIT)
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
    with _tracked("riffle sync") as tracker:
        _run_sync(tracker, offline=True)


@app.command("sync")
def sync_cmd(
    offline: bool = typer.Option(False, help="Skip the Scryfall refresh and price downloads"),
) -> None:
    """Refresh card data, keep today's prices, pick up a ManaBox export, rewrite _generated/, append _log/."""
    with _tracked("riffle sync") as tracker:
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


@app.command()
def watch(interval: float = typer.Option(5.0, help="Seconds between checks")) -> None:
    """Resync whenever a deck note is saved or a new ManaBox export lands. Ctrl-C to stop."""
    import time
    from datetime import datetime

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
                typer.echo(f"\n{datetime.now():%H:%M:%S} changed: {', '.join(changed)}")
                _resync_and_keep_watching()
                seen = _watched(cfg)
    except KeyboardInterrupt:
        typer.echo("\nstopped")


schedule_app = typer.Typer(help="The daily launchd job that runs `riffle sync` (macOS).")
app.add_typer(schedule_app, name="schedule")


def _show_schedule() -> None:
    from datetime import datetime

    from riffle import schedule as sched

    st = sched.status()
    if not st.installed and not st.loaded:
        typer.echo("no schedule — set one with: riffle schedule set 07:00")
        return
    times = ", ".join(sched.fmt(t) for t in st.times) or "(none in plist)"
    nxt = sched.next_run(st.times, datetime.now())
    typer.echo(f"{sched.LABEL}")
    typer.echo(f"  times      {times}  (24-hour, daily)")
    typer.echo(f"  next run   {nxt:%a %Y-%m-%d %H:%M}" if nxt else "  next run   —")
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
    """Show the schedule (times, next run, last result). Subcommands: set, remove."""
    if ctx.invoked_subcommand is None:
        _show_schedule()


@schedule_app.command("show")
def schedule_show() -> None:
    """Times, next run, whether it's loaded, and how the last run went."""
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
    """Unload and delete the job."""
    from riffle import schedule as sched

    typer.echo(f"removed {sched.LABEL}" if sched.remove() else "no schedule to remove")


db_app = typer.Typer(help="The Postgres database: start it, migrate it, check it.", no_args_is_help=True)
app.add_typer(db_app, name="db")


def _db_engine():
    from riffle import db

    return db.engine()


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
