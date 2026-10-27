"""Parse MTGO index pages and event payloads."""

import json
import re
from collections.abc import Iterable
from datetime import date

from riffle.models.metagame import Card, Event, MtgoDeck

BASE = "https://www.mtgo.com"


KINDS = ("league", "challenge", "showcase", "qualifier", "preliminary", "other")


FORMATS = ("standard", "pioneer", "modern", "legacy", "vintage", "pauper", "premodern", "duel-commander")


_DATA = re.compile(r"window\.MTGO\.decklists\.data\s*=\s*")


_LINK = re.compile(r'href="(?:https?://(?:www\.)?mtgo\.com)?/decklist/([a-z0-9-]+)"', re.I)


_SLUG = re.compile(r"^(?P<name>[a-z0-9-]+?)-(?P<date>\d{4}-\d{2}-\d{2})(?P<id>\d+)$")


def parse_slug(slug: str) -> tuple[str, str, str] | None:
    """(event name, ISO date, event id), or None if it isn't an event slug. A name whose
    date isn't a day isn't one: mtgo.com has listed a league dated 31 September."""
    m = _SLUG.match(slug)
    if m is None:
        return None
    try:
        date.fromisoformat(m["date"])
    except ValueError:
        return None
    return m["name"], m["date"], m["id"]


def classify(name: str) -> str:
    for kind in ("league", "showcase", "qualifier", "preliminary", "challenge"):
        if kind in name:
            return kind
    return "other"


def event_format(name: str) -> str:
    """Format from the event name: the part before the event type."""
    parts = name.split("-")
    for i, p in enumerate(parts):
        if p in {"league", "challenge", "showcase", "qualifier", "preliminary", "super", "last", "chance"}:
            return "-".join(parts[:i]) or parts[0]
    return parts[0]


def event_slugs(index_html: str) -> list[str]:
    """Event slugs linked from a monthly index page, in page order, deduplicated."""
    return list(dict.fromkeys(s.lower() for s in _LINK.findall(index_html) if parse_slug(s.lower())))


def undated_slugs(index_html: str) -> list[str]:
    """Links on an index page shaped like an event's but dated a day that doesn't exist."""
    links = dict.fromkeys(s.lower() for s in _LINK.findall(index_html))
    return [s for s in links if _SLUG.match(s) and parse_slug(s) is None]


def extract_data(page_html: str) -> dict:
    m = _DATA.search(page_html)
    if not m:
        raise ValueError("no decklist data on page — mtgo.com's layout may have changed")
    obj, _ = json.JSONDecoder().raw_decode(page_html, m.end())
    return obj


def _cards(
    main_rows: Iterable[dict] | None,
    side_rows: Iterable[dict] | None,
) -> tuple[list[Card], list[Card]]:
    """MTGO lists a card once per printing; merge by name, keep first-seen order.
    A row flagged "sideboard": "true" goes to the sideboard whichever list it's in."""
    boards: dict[str, dict[str, int]] = {"main": {}, "side": {}}
    for default, rows in (("main", main_rows), ("side", side_rows)):
        for r in rows or []:
            attrs = r.get("card_attributes") or {}
            name = attrs.get("card_name") or r.get("card_name")
            if not name:
                continue
            board = boards["side" if str(r.get("sideboard", "")).lower() == "true" else default]
            board[name] = board.get(name, 0) + int(r.get("qty") or r.get("quantity") or 0)
    return ([Card(n, q) for n, q in boards["main"].items()], [Card(n, q) for n, q in boards["side"].items()])


def _record(d: dict, kind: str) -> str | None:
    w = d.get("wins")
    if isinstance(w, dict) and w.get("wins") is not None:
        return f"{w['wins']}-{w.get('losses', 0)}"
    return "5-0" if kind == "league" else None


def parse_event(slug: str, data: dict) -> Event:
    parsed = parse_slug(slug)
    if not parsed:
        raise ValueError(f"not an event slug: {slug}")
    name, day, event_id = parsed
    kind = classify(name)

    ranks: dict[str, int] = {}
    for s in data.get("standings") or []:
        rank = s.get("rank")
        if rank is None:
            continue
        for key in (s.get("loginid"), s.get("login_name")):
            if key is not None:
                ranks[str(key).lower()] = int(rank)

    decks = []
    for d in data.get("decklists") or []:
        player = d.get("player") or d.get("login_name") or "?"
        main, side = _cards(d.get("main_deck"), d.get("sideboard_deck"))
        rank = ranks.get(str(d.get("loginid")).lower()) or ranks.get(player.lower())
        decks.append(MtgoDeck(player, main, side, rank, _record(d, kind)))
    decks.sort(key=lambda x: (x.rank is None, x.rank or 0))
    return Event(slug, event_id, name, event_format(name), kind, day, decks)
