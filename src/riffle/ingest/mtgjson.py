"""Daily Magic prices from MTGJSON: Card Kingdom, TCGplayer, Mana Pool, Cardmarket, and
Cardhoarder in one file, with the past 90 days to start from.

MTGJSON rebuilds its files once a day (live around 14:00 UTC) and serves them without a
key, each beside a .sha256 of itself:

    https://mtgjson.com/api/v5/Meta.json               the latest build's date and version
    https://mtgjson.com/api/v5/AllPricesToday.json.xz  every printing's prices that day
    https://mtgjson.com/api/v5/AllPrices.json.xz       the same for each of the past 90 days

Both price files are one JSON object, meta first:

    {"meta": {"date": "2026-09-27", "version": "5.3.0+20260927"},
     "data": {<uuid>: {"paper" | "mtgo": {<provider>: {"retail" | "buylist":
              {"normal" | "foil" | "etched": {<day>: <price>}}, "currency": "USD"}}}}}

Paper prices come from cardkingdom (retail and buylist, each only while Card Kingdom has
copies to sell or wants to buy them), tcgplayer, manapool, and cardmarket (in EUR); MTGO's
from cardhoarder, in tix, though MTGJSON labels them "USD". A uuid is MTGJSON's ID for one
face of a printing, usually derived from its Scryfall ID and side (older printings keep a
legacy one), so the loader maps it with MTGJSON's cardIdentifiers file. Riffle keeps the
files as returned, checked against their .sha256, checked to decompress whole, and named by
the date inside:

    <data_dir>/mtgjson/daily/<day>.json.xz      AllPricesToday: that day's prices
    <data_dir>/mtgjson/90-days/<day>.json.xz    AllPrices: <day> and the 90 days before it

A day is fetched once however often sync runs; Meta.json says whether there's a new one.
AllPrices is kept on the first run, and again when a day in its window has no prices kept
(the Mac was off that day), at most once every REFILL days, so a missed day is still inside
the next file's window with two months to spare. A day MTGJSON never built can't come back
that way; a kept AllPrices counts as covering its whole window. Nothing here reads the files
back: the price loader does. Headers, retries, and 429 handling: riffle.net.
"""

import hashlib
import json
import lzma
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

from riffle import net
from riffle.config import data_dir
from riffle.progress import SILENT, Step, Tracker

BASE = "https://mtgjson.com/api/v5"
TODAY = "AllPricesToday.json.xz"
HISTORY = "AllPrices.json.xz"
WINDOW = 90  # days an AllPrices file counts as covering: its date and the 89 before (it has one more)
REFILL = 30  # days at least between AllPrices downloads that fill a missing day
CHUNK = 1 << 20
DATE = re.compile(rb'"date"\s*:\s*"(\d{4}-\d{2}-\d{2})"')  # the first one is meta's

Fetch = Callable[[str], bytes | None]  # url -> body, or None for 404
Download = Callable[[str, Path, net.Progress | None], int | None]  # url, dest -> bytes, or None for 404


@dataclass
class Snapshot:
    day: date  # MTGJSON's latest build
    kept: list[str] = field(default_factory=list)  # files kept this run, TODAY and HISTORY


def _get(url: str) -> bytes | None:
    return net.get(url)


def _download(url: str, dest: Path, progress: net.Progress | None = None) -> int | None:
    return net.download(url, dest, progress=progress)


def daily_dir() -> Path:
    return data_dir() / "mtgjson" / "daily"


def history_dir() -> Path:
    return data_dir() / "mtgjson" / "90-days"


def kept_days(folder: Path) -> list[date]:
    """The days of the <day>.json.xz files in folder, oldest first."""
    days = []
    for path in folder.glob("*.json.xz"):
        try:
            days.append(date.fromisoformat(path.name.removesuffix(".json.xz")))
        except ValueError:
            continue
    return sorted(days)


def covered(daily: list[date], history: list[date]) -> set[date]:
    """Every day a kept file has prices for: each daily file's, and each AllPrices file's 90."""
    days = set(daily)
    for last in history:
        days.update(last - timedelta(n) for n in range(WINDOW))
    return days


def build_day(fetch: Fetch = _get) -> date:
    """The date of MTGJSON's latest build, from Meta.json."""
    body = fetch(f"{BASE}/Meta.json")
    if body is None:
        raise net.FetchError("Meta.json: HTTP 404")
    try:
        return date.fromisoformat(json.loads(body)["data"]["date"])
    except (ValueError, KeyError, TypeError, RecursionError) as e:
        raise net.FetchError("Meta.json: not the expected JSON") from e


def _sha256(path: Path) -> str:
    with path.open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def _file_day(path: Path, name: str) -> date:
    """The date in the file's meta, read from its first bytes, once the whole file has been
    read through: a file that doesn't decompress to its end isn't kept."""
    try:
        with lzma.open(path) as f:
            head = f.read(512)
            while f.read(CHUNK):
                pass
    except lzma.LZMAError as e:
        raise net.FetchError(f"{name}: not xz") from e
    except EOFError as e:
        raise net.FetchError(f"{name}: cut off before its end") from e
    found = DATE.search(head)
    try:
        if found is None or not head.lstrip().startswith(b"{"):
            raise ValueError("no meta date")
        return date.fromisoformat(found.group(1).decode())
    except ValueError as e:
        raise net.FetchError(f"{name}: not the expected JSON") from e


def _keep(name: str, folder: Path, fetch: Fetch, download: Download, step: Step) -> tuple[date, bool]:
    """Download name, check it against its .sha256, and keep it in folder as <day>.json.xz,
    <day> being the date inside. Returns that day, and whether the file is new."""
    url = f"{BASE}/{name}"
    digest = fetch(f"{url}.sha256")
    if digest is None:
        raise net.FetchError(f"{name}.sha256: HTTP 404")
    expected = (digest.decode("ascii", errors="replace").split() or [""])[0].lower()
    folder.mkdir(parents=True, exist_ok=True)
    fresh = folder / f"{name}.new"  # checked before it's kept
    try:
        if download(url, fresh, step.update) is None:
            raise net.FetchError(f"{name}: HTTP 404")
        if _sha256(fresh) != expected:
            raise net.FetchError(f"{name} doesn't match its .sha256; MTGJSON may be mid-update")
        day = _file_day(fresh, name)
        dest = folder / f"{day.isoformat()}.json.xz"
        if dest.exists():
            return day, False
        fresh.replace(dest)
        return day, True
    finally:
        fresh.unlink(missing_ok=True)


def _mb(path: Path) -> str:
    return f"{path.stat().st_size / 1e6:,.1f} MB"


def snapshot(
    fetch: Fetch = _get, download: Download = _download, tracker: Tracker = SILENT
) -> Snapshot | None:
    """Keep MTGJSON's prices for its latest day, and AllPrices when a day in its window is
    missing (see REFILL). Each file is a step on the tracker; a failure is reported on its
    step and keeps nothing, so the next run tries again. None when Meta.json can't be read."""
    step = tracker.step("MTGJSON prices", unit="bytes")
    try:
        day = build_day(fetch)
    except net.FetchError as e:
        step.fail(str(e))
        return None
    snap = Snapshot(day=day)
    if day in covered(kept_days(daily_dir()), kept_days(history_dir())):
        step.ok(f"already have {day}")
    else:
        try:
            got, new = _keep(TODAY, daily_dir(), fetch, download, step)
        except (net.FetchError, OSError) as e:
            step.fail(str(e))
        else:
            if new:
                snap.kept.append(TODAY)
                step.ok(f"kept {got}, {_mb(daily_dir() / f'{got.isoformat()}.json.xz')}")
            else:
                step.ok(f"already have {got}")
    history = kept_days(history_dir())
    have = covered(kept_days(daily_dir()), history)
    missing = [day - timedelta(n) for n in range(1, WINDOW) if day - timedelta(n) not in have]
    if not missing or (history and (day - history[-1]).days < REFILL):
        return snap
    back = tracker.step("MTGJSON 90 days", unit="bytes")
    try:
        last, new = _keep(HISTORY, history_dir(), fetch, download, back)
    except (net.FetchError, OSError) as e:
        back.fail(str(e))
    else:
        if new:
            snap.kept.append(HISTORY)
            back.ok(f"kept the 90 days to {last}, {_mb(history_dir() / f'{last.isoformat()}.json.xz')}")
        else:
            back.ok(f"already have the 90 days to {last}")
    return snap
