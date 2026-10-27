"""Handle MTGO collection and metagame commands."""

from datetime import date, timedelta
from pathlib import Path

import typer

import riffle.ingest.mtgo.pages as ingest_mtgo_pages
import riffle.ingest.mtgo.storage as ingest_mtgo_storage
import riffle.ingest.mtgo.trickle as ingest_mtgo_trickle
import riffle.models.metagame as models_metagame
from riffle import times
from riffle.cli import _common as common
from riffle.progress import failure

KindOpt = typer.Option(
    None,
    "--kind",
    "-k",
    help="league | challenge | showcase | qualifier | preliminary; repeatable",
    autocompletion=common._complete_kind,
)

FormatsOpt = typer.Option(
    ["modern"],
    "--format",
    "-f",
    help="modern, pioneer, pauper, ... or all; repeatable",
    autocompletion=common._complete_mtgo_format,
)


def _kinds(kind: list[str] | None) -> list[str] | None:

    bad = [k for k in kind or [] if k not in ingest_mtgo_pages.KINDS]
    if bad:
        raise typer.BadParameter(f"--kind must be one of {', '.join(ingest_mtgo_pages.KINDS)}")
    return kind or None


def _formats(fmt: list[str]) -> list[str] | None:
    """None means every format."""
    fmts = [f.lower() for f in fmt]
    return None if "all" in fmts else fmts


def _events(fmt: list[str], days: int, kind: list[str] | None):

    events = ingest_mtgo_storage.load(_formats(fmt), date.today() - timedelta(days=days), _kinds(kind))
    if not events:
        names = "/".join(fmt)
        typer.echo(
            f"no stored {names} events in the last {days} days — see what's owed: riffle mtgo status",
            err=True,
        )
        raise typer.Exit(1)
    return events


def mtgo_trickle() -> None:
    """One run of the trickle: at most one index page, then owed events (a page that failed
    at one of the last two runs first, then those never asked for, then the other
    retries), as many as the pace allows. The job (riffle schedule trickle) runs this
    every 10 minutes."""
    from riffle import trickle

    with common._tracked("riffle mtgo trickle") as tracker:  # report inside: a failed step exits 1 on leaving
        res = ingest_mtgo_trickle.run_trickle(tracker=tracker)
        if res.busy:
            common._echo("another trickle run is in progress; nothing asked")
            return
        if res.set_aside:
            pages = "page" if len(res.set_aside) == 1 else "pages"
            kept = trickle.set_aside_path(ingest_mtgo_trickle.SOURCE).name
            common._echo(
                f"set aside {len(res.set_aside)} owed {pages} whose name holds no real date: "
                f"{', '.join(res.set_aside)} (kept in {kept})"
            )
        if res.carried:
            common._echo(
                f"{res.carried} events from {ingest_mtgo_trickle.misses_path().name} moved to the owed list"
            )
        if res.moved:
            common._echo(f"moved {res.moved} stored events into mtgo/<year>/<month>/")
        if res.forgotten:
            months = "month" if len(res.forgotten) == 1 else "months"
            listed, n = ", ".join(res.forgotten), len(res.forgotten)
            common._echo(f"forgot {n} {months} saved as listing no events: {listed}; each is read again")
        if res.undated:
            names = "name" if len(res.undated) == 1 else "names"
            listed = ", ".join(res.undated)
            common._echo(
                f"the {res.index} index lists {len(res.undated)} {names} with no real date: {listed}"
            )
        if res.paused_until:
            common._echo(
                f"paused until {times.shown(res.paused_until)}: {res.why or 'mtgo.com asked'}; nothing asked"
            )
        elif not res.budget:
            common._echo("the last 15 minutes hold as many requests as allowed; nothing asked")
        retried = f" ({res.retried} on a retry)" if res.retried else ""
        young = f", {res.waiting} too new to ask" if res.waiting else ""
        common._echo(
            f"{len(res.fetched)} new events{retried} · {len(res.missed)} missed · "
            f"{res.owed} owed, {res.due} due{young} · {res.level} pages a run"
        )
        if res.pending:
            common._echo(f"{len(res.pending)} not published yet: {', '.join(res.pending)}")
        now = trickle.now()
        for slug, outcome, when in res.missed:
            common._echo(f"  {slug}: {outcome}, still owed; {ingest_mtgo_trickle.next_try(now, when)}")
        if res.throttled and res.resume:
            common._echo(f"paused until {times.shown(res.resume)}: {res.throttled}")
        if res.raised:
            common._echo(f"every answer whole: up to {res.level} pages a run")
        if res.stopped:
            common._echo(f"stopped early: {res.stopped}")
        for slug, err in res.broken:
            common._echo(f"  ! {slug}: its lists wouldn't parse: {err}", err=True)
        for slug, e in res.undue:
            common._echo(f"  ! {slug}: can't say when it's due: {failure(e)}; skipped", err=True)


def mtgo_status() -> None:
    """Pace, pause, recent requests, what's owed by month, and how far back the indexes are read."""
    from collections import Counter

    from riffle import trickle

    st = ingest_mtgo_trickle.status()
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
        for key, miss, days, when in ingest_mtgo_trickle.unread_months(st.months, st.at):
            asked = "once" if miss.asks == 1 else f"{miss.asks} times"
            empty = f", believed empty on {days} {'day' if days == 1 else 'days'}" if days else ""
            typer.echo(f"           {key}  not read yet: {miss.last}, asked {asked}{empty}; {when}")
        undated = [s for m in st.months.values() for s in m.get("undated", ())]
        if undated:
            listed = ", ".join(undated)
            typer.echo(f"           {len(undated)} listed with no real date, not taken as events: {listed}")
    else:
        typer.echo("indexes    none read yet")
    typer.echo(f"log        {trickle.RequestLog(ingest_mtgo_trickle.SOURCE).path}")


def mtgo_forget(
    slug: str = typer.Argument(..., help="An owed event's slug, as mtgo-owed.json lists it"),
) -> None:
    """Drop an event from the owed list, so the trickle stops asking for it."""

    try:
        dropped = ingest_mtgo_trickle.forget(slug)
    except RuntimeError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1) from e
    if not dropped:
        typer.echo(f"{slug} isn't owed", err=True)
        raise typer.Exit(1)
    typer.echo(f"forgot {slug}")


def meta_cards(
    fmt: list[str] = FormatsOpt,
    days: int = typer.Option(14),
    kind: list[str] = KindOpt,
    board: str = typer.Option(
        "all", help="all | main | side", autocompletion=common._choices("all", "main", "side")
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


def meta_show(
    event: str = typer.Argument(..., help="Event slug, as `riffle meta decks` lists it"),
    player: str = typer.Argument(...),
    out: Path = typer.Option(None, "-o", "--out", help="Write an MTGO .txt that `riffle own` can read"),
) -> None:
    """Print one stored decklist in MTGO .txt form."""
    import json

    path = ingest_mtgo_storage.stored_path(event)
    if not path.exists():
        raise typer.BadParameter(f"no stored event {event} — see what's still owed: riffle mtgo status")
    ev = models_metagame.Event.from_dict(json.loads(path.read_text(encoding="utf-8")))
    deck = next((d for d in ev.decks if d.player.lower() == player.lower()), None)
    if deck is None:
        raise typer.BadParameter(f"{player} has no list in {event}")
    if out:
        out.expanduser().write_text(deck.to_text(), encoding="utf-8")
        typer.echo(f"wrote {out}")
    else:
        typer.echo(deck.to_text(), nl=False)
