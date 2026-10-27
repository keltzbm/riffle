"""Handle status and integrity check commands."""

from datetime import UTC, timedelta

import typer

from riffle import config, times, vault
from riffle.cli import _common as common
from riffle.ingest.local import manabox, precons


def status_cmd(
    days: int = typer.Option(None, "--days", "-d", min=1, max=90, help="Days each strip shows [default: 14]"),
) -> None:
    """What Riffle has kept, source by source and day by day, with gaps marked; then the MTGO
    backlog, the scheduled jobs, the store's size and free disk, and the backup. Reads only;
    exits 1 if a source's files can't be read."""
    from riffle.ops import status

    lines, readable = status.report(times.now(), days or status.DAYS, config.data_dir())
    for line in lines:
        typer.echo(line)
    if not readable:
        raise typer.Exit(1)


CHECKS = ("prices", "vault", "collection")


def check_cmd(
    what: str = typer.Argument("prices", help=" | ".join(CHECKS), autocompletion=common._choices(*CHECKS)),
) -> None:
    """Check every kept price file: filed under the day its own stamp says, and made before it
    was fetched. With vault, check every card link in the notes opens the card's note. With
    collection, list the copies read as their precon box's printing, and those a box holds that
    the collection doesn't record as it. Reads only, but for the precon boxes' lists Riffle keeps;
    exits 1 if anything is wrong."""
    from riffle.ops import checks

    if what not in CHECKS:
        raise typer.BadParameter(f"what to check: {', '.join(CHECKS[:-1])}, or {CHECKS[-1]}, not {what!r}")
    if what == "vault":
        _check_vault()
        return
    if what == "collection":
        _check_collection()
        return
    from riffle.ops import status

    wrong = 0
    for rep in checks.run():
        typer.echo(f"{rep.source:<14}{rep.summary()}")
        for note in rep.notes:
            typer.echo(f"{'':<14}{note}")
        for problem in rep.problems:
            typer.echo(f"  ! {problem}", err=True)
        wrong += len(rep.problems)
    try:
        installed = [job for job, _ in status.installed_jobs()]
    except OSError:
        # no scheduler to ask, so no jobs to judge
        installed = []
    logs, unread = status.job_logs(installed)
    today = times.now().astimezone(UTC).date()
    said = status.short_runs(logs, today, today - timedelta(days=status.DAYS - 1)) + unread
    for n, line in enumerate(said):
        typer.echo(f"{'jobs' if n == 0 else '':<14}{line}")
    if wrong:
        typer.echo(f"{wrong} price file{'s' * (wrong != 1)} dated wrong", err=True)
    if wrong or unread:
        raise typer.Exit(1)


def _check_vault() -> None:
    """Each card link that can't open the card's note as written, with the link that would, and
    each card name another note has."""
    from riffle.export import cards
    from riffle.links import safe_name, written

    root, notes_dir, mtg = common._vault(config.load())
    with common._catalog() as cat:
        found = cards.gather(vault.linking(notes_dir))
        own = cards.gather(vault.riffle_notes(mtg))
        links = cards.sort(found, cat, vault.names(root), own)
    n = len(links.cards)
    typer.echo(f"card links: {n:,} card{'s' * (n != 1)} linked in {config.tilde(notes_dir)}")
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


def _check_collection() -> None:
    """How many ManaBox exports are kept, then each copy read as its precon box's printing, then
    each copy a box holds that isn't recorded as the box's printing, with where the card is
    recorded instead. A box that can't be read is wrong; a copy not found isn't, since a card may
    have left its box."""
    cfg = config.load()
    mtg = common._mtg_dir(cfg)
    if not cfg.collection_csv.exists():
        common._echo(
            "no collection yet — export from ManaBox to ~/Downloads, or: riffle ingest manabox <csv>",
            err=True,
        )
        raise typer.Exit(1)
    boxes = precons.load(vault.precon_boxes(mtg))
    with common._setup(boxes) as (_, _, inv):
        copies = sum(h.quantity for h in inv.holdings)
        typer.echo(f"collection: {copies:,} copies in {config.tilde(cfg.collection_csv)}")
        exports = manabox.kept(cfg.manabox_exports)
        typer.echo(
            f"exports kept: {len(exports)}, newest {times.shown(exports[-1].at)}, "
            f"in {config.tilde(cfg.manabox_exports)}"
            if exports
            else "exports kept: none yet"
        )
        c = inv.corrected
        if c is None and not boxes.problems:
            typer.echo("precon boxes: none registered (a deck note's source: precon:<box> registers one)")
        elif c is not None:
            typer.echo(f"precon boxes: {len(boxes.boxes)}, {c.printings:,} printings, basic lands aside")
            fixed = sum(f.copies for f in c.fixes)
            typer.echo(f"read as their box's printing, recorded under another with the same art: {fixed:,}")
            for f in c.fixes:
                typer.echo(f"  {f.name}: {f.recorded} → {f.read_as}, {f.copies} ({', '.join(f.boxes)})")
            short = sum(s.copies for s in c.short)
            typer.echo(f"not found as their box's printing: {short:,}")
            for s in c.short:
                where = f"recorded as {', '.join(s.elsewhere)}, other art" if s.elsewhere else "no other copy"
                typer.echo(f"  {s.name}: {s.place}, {s.copies} ({', '.join(s.boxes)}); {where}")
    for problem in boxes.problems:
        typer.echo(f"  ! {problem}", err=True)
    if boxes.problems:
        n = len(boxes.problems)
        typer.echo(f"{n} precon box{'es' * (n != 1)} can't be read", err=True)
        raise typer.Exit(1)
