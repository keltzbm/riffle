"""Handle card, deck, ownership, and comparison commands."""

import unicodedata
from collections import Counter
from collections.abc import Collection, Sequence
from pathlib import Path

import typer

from riffle import config, vault
from riffle import sync as syncmod
from riffle.cli import _common as common
from riffle.export import formats
from riffle.models import Deck


def legal(
    deck: common.DecksRef,
    fmt: str = typer.Option(
        None,
        "--format",
        "-f",
        help="Check against another format; default: the note's",
        autocompletion=common._complete_legal_format,
    ),
) -> None:
    """Is a deck legal? Size, copies, bans, sideboard, commander color identity. 'all' checks every deck."""
    from riffle.analysis import legality

    cfg = config.load()
    targets = vault.decks(common._mtg_dir(cfg)) if deck == "all" else [common._deck(cfg, deck)]
    with common._catalog() as cat:
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


def decks(
    fmt: str = typer.Option(None, "--format", help="Only this format: commander, modern, …"),
    strategy: str = typer.Option(None, "--strategy", help="Only this strategy: aggro, control, …"),
    colors: str = typer.Option(None, "--colors", help="Exactly these colors, any order: wu; c for colorless"),
    archetype: str = typer.Option(None, "--archetype", help="Only this archetype: tribal, ramp, …"),
    to_buy: int = typer.Option(None, "--to-buy", min=0, help="At most N cards left to buy"),
    sort: str = typer.Option(
        "name", "--sort", help=" | ".join(SORTS), autocompletion=common._choices(*SORTS)
    ),
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
    found = vault.decks(common._mtg_dir(config.load()))
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

        with common._setup() as (_, cat, inv):
            short = {d.slug: summary(syncmod.analyse(d, inv, cat).rows)[BUY] for d in kept}
        if to_buy is not None:
            kept = [d for d in kept if short[d.slug] <= to_buy]
    if not kept:
        if found:
            common._echo("no decks match", err=True)
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


def own(
    deck: common.DeckRef,
    show: str = typer.Option(
        "buy", "--show", "-s", help="buy | own | all", autocompletion=common._choices(*SHOW)
    ),
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
    target = common._deck(cfg, deck)
    if on_arena and not cfg.arena_list.exists():
        raise typer.BadParameter("no Arena collection yet — run: riffle ingest arena <file>")
    with common._catalog() as cat:
        inv = syncmod.arena_inventory(cfg.arena_list, cat) if on_arena else common._inventory(cfg, cat)
        rep = syncmod.analyse(target, inv, cat)
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


def price(
    deck: common.DeckRef,
    budget_tix: float = typer.Option(None, help="Compare the MTGO total to a tix budget"),
) -> None:
    """Paper cost to finish the deck, and MTGO cost for the whole list."""
    target = common._deck(config.load(), deck)
    with common._setup() as (_, cat, inv):
        rep = syncmod.analyse(target, inv, cat)
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


def _share(value: float | None) -> str:
    return "—" if value is None else f"{value:.2f}"


def _reading_order(name: str) -> str:
    """A name as a reader sorts it, accents and case aside: Andúril with the A's, not after Z."""
    return "".join(c for c in unicodedata.normalize("NFKD", name) if not unicodedata.combining(c)).casefold()


def _diff(cards: Counter[str]) -> str:
    """Cards in reading order, the count before one held more than once more; none for none.
    Semicolons between them, since a legend's name may hold a comma."""
    if not cards:
        return "none"
    return "; ".join(
        f"{n} {name}" if n > 1 else name
        for name, n in sorted(cards.items(), key=lambda kv: _reading_order(kv[0]))
    )


def _find(cfg: config.Config, ref: str) -> Deck:
    """_deck, or exit 1 saying what wasn't found."""
    try:
        return common._deck(cfg, ref)
    except (LookupError, ValueError) as e:
        common._echo(str(e), err=True)
        raise typer.Exit(1) from e


def _zones(*decks: Deck) -> list[str]:
    """The zones to measure: the main deck, and the sideboard when any of the decks has one."""
    return ["main", "sideboard"] if any(d.board("sideboard") for d in decks) else ["main"]


def compare(first: common.DeckRef, second: common.DeckRef) -> None:
    """How alike two decks are, zone by zone, basic lands aside: the cards they share, the swaps
    between them, Jaccard, how much of each is in the other, and whether one is a variant."""
    from riffle.analysis import similarity

    cfg = config.load()
    a, b = _find(cfg, first), _find(cfg, second)
    zones = _zones(a, b)
    with common._catalog() as cat:
        found = {
            z: similarity.compare(similarity.counts(a, cat, z), similarity.counts(b, cat, z)) for z in zones
        }
        said = similarity.verdict(a, b, found["main"].swaps, cat)
    rows = [
        ["", *zones],
        ["shared", *(str(found[z].shared) for z in zones)],
        ["only in A", *(str(found[z].only_a) for z in zones)],
        ["only in B", *(str(found[z].only_b) for z in zones)],
        ["swaps", *(str(found[z].swaps) for z in zones)],
        ["Jaccard", *(_share(found[z].jaccard) for z in zones)],
        ["A in B", *(_share(found[z].a_in_b) for z in zones)],
        ["B in A", *(_share(found[z].b_in_a) for z in zones)],
    ]
    # the decks are A and B after the first line, so a long name doesn't widen the table
    typer.echo(f"{a.slug} (A) against {b.slug} (B), basic lands aside")
    for line in _columns(rows, right=set(range(1, len(zones) + 1))):
        typer.echo(f"  {line}")
    typer.echo(f"verdict: {said}")
    for z in zones:
        typer.echo(f"only in A, {z}: {_diff(found[z].only_a_cards)}")
        typer.echo(f"only in B, {z}: {_diff(found[z].only_b_cards)}")


def similar(
    deck: common.DeckRef,
    limit: int = typer.Option(10, "--limit", "-n", min=1, help="How many decks to list"),
) -> None:
    """The decks nearest one: the other deck notes in its format, and for Commander in its
    colors, by their main decks' swaps, basic lands aside."""
    from riffle.analysis import similarity

    cfg = config.load()
    mtg = common._mtg_dir(cfg)  # the vault's other decks are what it's compared with, whatever the deck
    target = _find(cfg, deck)
    # None for a list with neither a format nor a commander: every deck note is a candidate
    fmt = similarity.deck_format(target)
    colors = _colors(target) if fmt in similarity.MEASURED and target.meta.get("colors") is not None else None
    # the target's own note is left out by its path: a list given by path is no note, whatever its name
    here = Path(target.meta["path"]).resolve() if "path" in target.meta else None
    others = [
        d
        for d in vault.decks(mtg)
        if (fmt is None or similarity.deck_format(d) == fmt)
        and Path(d.meta["path"]).resolve() != here
        and (colors is None or set(_colors(d)) == set(colors))
    ]
    if not others:
        if fmt is None:
            typer.echo("no deck note to compare it with")
        else:
            typer.echo(
                f"no other {fmt} deck " + ("in these colors" if colors is not None else "in this format")
            )
        return
    side = len(_zones(target, *others)) > 1
    with common._catalog() as cat:
        mine = {z: similarity.counts(target, cat, z) for z in ("main", "sideboard")}
        found = []
        for d in others:
            main = similarity.compare(mine["main"], similarity.counts(d, cat))
            rest = (
                similarity.compare(mine["sideboard"], similarity.counts(d, cat, "sideboard"))
                if side
                else None
            )
            found.append((main, rest, d, similarity.is_variant(target, d, main.swaps, cat)))
    found.sort(key=lambda item: (item[0].swaps, -(item[0].jaccard or 0.0), item[2].slug))
    shown = found[:limit]
    where = f"{fmt}, {colors}" if colors is not None else fmt or "any format"
    noun = "deck" if len(others) == 1 else "decks"
    typer.echo(f"nearest to {target.slug}: {where}; {len(shown)} of {len(others)} {noun}, basic lands aside")
    rows = [["swaps", "Jaccard", *(["side"] if side else []), "verdict", "deck"]]
    for main, rest, d, call in shown:
        word = "—" if call is None else "variant" if call else "another deck"
        rows.append(
            [str(main.swaps), _share(main.jaccard), *([str(rest.swaps)] if rest else []), word, d.slug]
        )
    for line in _columns(rows, right={0, 1, 2} if side else {0, 1}):
        typer.echo(f"  {line}")


def export_deck(
    deck: common.DecksRef,
    to: str = typer.Option(
        "moxfield",
        help="moxfield | manabox | mtgo | arena | tcgplayer",
        autocompletion=common._choices(*formats.FORMATS),
    ),
    pin: str = typer.Option(
        "owned",
        help="owned | none — pin printings you own",
        autocompletion=common._choices("owned", "none"),
    ),
    out: Path = typer.Option(None, "-o", "--out", help="File, or a folder when the deck is 'all'"),
) -> None:
    """Write a deck — or every deck, with 'all' — in a format another app imports."""
    if to not in formats.FORMATS:
        raise typer.BadParameter(f"--to must be one of {', '.join(formats.FORMATS)}")
    if deck == "all" and out is None:
        raise typer.BadParameter("exporting all decks needs --out <folder>")
    cfg = config.load()
    targets = vault.decks(common._mtg_dir(cfg)) if deck == "all" else [common._deck(cfg, deck)]
    with common._setup() as (_, cat, inv):
        pins = formats.owned_printings(inv.holdings) if pin == "owned" else {}
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


def card_cmd(
    name: str = typer.Argument(..., help="The card's name, full, front face, or as a link writes it"),
    open_page: bool = typer.Option(False, "--open", help="Open the card's page on Scryfall"),
) -> None:
    """What a card's note holds: each face's text, the price, legality in the formats your lists
    are in, and the copies you own. Reads only."""
    import webbrowser

    from riffle.export import cards

    mtg = common._mtg_dir(config.load())
    with common._setup() as (_, cat, inv):
        card_id = cat.resolve(name)
        if card_id is None:
            typer.echo(f"no card named {name!r}", err=True)
            raise typer.Exit(1)
        view = cat.card_views([card_id]).get(card_id)
        day = cat.prices_day()
        facts = [
            cards.price_line(cat.prices([card_id]).get(card_id), day.isoformat() if day else None),
            cards.legality(cat.rules([card_id]).get(card_id), vault.formats(mtg)),
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
