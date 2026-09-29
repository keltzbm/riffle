"""When a watch asks a source that limits its requests, learned from the list's own publish times
and its own checks: tcgcsv asks for under 10,000 requests a day, so its watch doesn't ask at every
firing, and neither do Cardmarket's 21 guides.

A watch job fires every EVERY. At each firing a list is asked for when either holds:

- Its next list is due: past its expected time less its lead, and asked at every firing from
  then until the list comes, however late. Expected is the last list's time plus the median of
  its last 14 gaps (riffle.lateness.usual); there's none until it has 14. The lead is the list's
  own: the least that would have left at most one of its lists in the last year coming before
  the window opened (lateness.ALLOWED in lateness.HISTORY, the margin's allowance).
- The far interval has passed since its last check: the longest whole minute c with
  N · f^⌊L/c⌋ ≤ TARGET, where L is its shortest gap in the 30 days before its last list less its
  longest fetch, N its lists a year, and f the CONFIDENCE upper bound on its checks' failure rate
  over the last WEEK, counting one failure more than seen. The far checks alone keep the loss
  under TARGET even when the expected time is wrong; the near ones only shorten the wait. With
  no check logged, or under two lists, every firing asks.

Why these (the vault's price-watch-numbers note has the proof):

- The median of 14 gaps: replayed on GoatBots' 1,349 publishes, it put a list 4 s from where it
  came typically and 5.3 minutes at the 99th percentile, missing by more than 10 minutes 4
  times in 3.7 years. The mean of the same gaps missed 8 times, and the last gap 20: one late
  publish throws it a day off.
- The lead: on the same replay, 1.4 minutes typically and 5.4 at most. It kept 99% of lists
  within one firing of their publish, as a 30-minute lead did, with half the requests; with no
  lead the 99th percentile was 7.7 minutes.
- The far interval is price-watch-numbers §3's check interval, from each list's own log. On
  tcgcsv it settles near 6 hours once a week of checks is logged: about 5 requests a day in all,
  against 288 at every firing. A failed check shortens it.
"""

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from statistics import median

from riffle import lateness

EVERY = timedelta(minutes=5)  # a watch job's interval: a Mana Pool list lasts about 30 minutes, six tries
TARGET = 0.1  # lists a year the checks may cost: one a decade (price-watch-numbers §3)
CONFIDENCE = 0.95
WEEK = timedelta(days=7)  # the checks a failure rate is judged from
YEAR = timedelta(days=365)
_NONE = timedelta(0)


@dataclass(frozen=True)
class Plan:
    ask: bool  # ask at this firing
    next: datetime  # when it's asked next, if not now
    expected: datetime | None  # when its next list is expected; None until it has 14 gaps
    far: timedelta  # the far interval
    gaps: int  # gaps between its lists so far


def upper(failures: int, checks: int) -> float:
    """The CONFIDENCE upper bound on a failure rate seen as failures in checks (Clopper-Pearson,
    one-sided). 0 in 90 is 3.27%."""
    if failures >= checks:
        return 1.0
    low, high = 0.0, 1.0
    for _ in range(60):
        mid = (low + high) / 2
        at_most = sum(math.comb(checks, i) * mid**i * (1 - mid) ** (checks - i) for i in range(failures + 1))
        low, high = (mid, high) if at_most > 1 - CONFIDENCE else (low, mid)
    return high


def far(
    made: Sequence[datetime], checks: Sequence[tuple[datetime, bool]], now: datetime, busy: timedelta = _NONE
) -> timedelta:
    """The far interval for a list made at these times (oldest first), from its checks, each
    (when, failed), and busy, its longest fetch: never under EVERY."""
    gaps = [b - a for a, b in zip(made, made[1:], strict=False)]
    if not gaps:
        return EVERY
    life = min(g for g, end in zip(gaps, made[1:], strict=True) if end >= made[-1] - lateness.WINDOW) - busy
    usual = timedelta(seconds=median(g.total_seconds() for g in gaps[-lateness.LEAST_GAPS :]))
    recent = [failed for at, failed in checks if now - at <= WEEK]
    rate = upper(sum(recent) + 1, len(recent))
    if rate >= 1 or life <= EVERY or usual <= _NONE:
        return EVERY
    per_year = YEAR / usual
    chances = max(1, math.ceil(math.log(TARGET / per_year) / math.log(rate)))
    return max(EVERY, timedelta(minutes=(life / chances) // timedelta(minutes=1)))


def expected(made: Sequence[datetime]) -> datetime | None:
    """When the next list is expected: the last plus its usual gap; None until it has 14 gaps."""
    gap = lateness.usual(list(made))
    return None if gap is None else made[-1] + gap


def lead(made: Sequence[datetime], now: datetime) -> timedelta:
    """How early the window opens before the expected time: the least that would have left at
    most lateness.ALLOWED of the list's lists in the last year coming before their window."""
    early = []
    for i in range(lateness.LEAST_GAPS + 1, len(made)):
        if now - made[i] > lateness.HISTORY:
            continue
        guess = expected(made[i - lateness.LEAST_GAPS - 1 : i])
        if guess is not None and made[i] < guess:
            early.append(guess - made[i])
    early.sort(reverse=True)
    return early[lateness.ALLOWED] if len(early) > lateness.ALLOWED else _NONE


def plan(
    made: Sequence[datetime], checks: Sequence[tuple[datetime, bool]], now: datetime, busy: timedelta = _NONE
) -> Plan:
    """Whether to ask for a list at this firing, and when it's asked next if not: made is when
    its lists were made, checks each check (when, failed), busy its longest fetch."""
    made = sorted(made)
    interval = far(made, checks, now, busy)
    last = max((at for at, _ in checks), default=None)
    due = now if last is None else last + interval
    coming = expected(made)
    if coming is not None:
        due = min(due, coming - lead(made, now))
    return Plan(due <= now, max(due, now), coming, interval, max(0, len(made) - 1))
