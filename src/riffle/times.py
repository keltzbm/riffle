"""Times as Riffle keeps and shows them.

Everything Riffle writes to disk is UTC: its files, its logs, and the dates in its notes. What
it prints to a terminal is local time, with the zone. A source's data is dated by the source's
own clock, never the Mac's, and a price day is never converted.
"""

import sys
from datetime import UTC, date, datetime

MINUTE = "%Y-%m-%d %H:%M"


def now() -> datetime:
    return datetime.now(UTC)


def today() -> date:
    """Today in UTC: the date Riffle's own records go under."""
    return now().date()


def local(t: datetime, fmt: str = MINUTE) -> str:
    """t in the Mac's time, with the zone's name: 2026-09-28 05:00 MDT."""
    t = t.astimezone()
    return f"{t.strftime(fmt)} {t.tzname()}"


def utc(t: datetime, fmt: str = MINUTE) -> str:
    """t in UTC, saying so: 2026-09-28 11:00 UTC."""
    return f"{t.astimezone(UTC).strftime(fmt)} UTC"


def shown(t: datetime, fmt: str = MINUTE) -> str:
    """t as printed: local time on a terminal, UTC anywhere else, the job logs included."""
    return local(t, fmt) if sys.stdout.isatty() else utc(t, fmt)
