"""When a watch asks a source that limits its requests, learned from the list's own publish times
and its own checks: tcgcsv asks for under 10,000 requests a day, so once a list's schedule is
learned its watch doesn't ask at every firing, and neither do Cardmarket's, MTGJSON's or GoatBots'.

A watch job fires every EVERY. At each firing a list is asked for when any of these holds:

- Its next list is due: past its expected time less its lead, and asked at every firing from
  then until the list comes, however late. Expected is the last list's time plus the median of
  its last 14 gaps (riffle.lateness.usual); there's none until it has 14. The lead is the list's
  own: the least that would have left at most one of its lists in the last year online before
  the window opened (lateness.ALLOWED in lateness.HISTORY, the margin's allowance). A list counts
  as online from the later of when it was made and the last check that didn't get it
  (riffle.watching.online). The lead goes below zero when a list's lists go online after they're
  expected, and the window then opens after the expected time: MTGJSON's build is made about
  06:12 UTC and served after 13:00.
- The far interval has passed since its last check: the longest whole minute c with
  N · f^⌊L/c⌋ ≤ TARGET, where L is its shortest gap in the 30 days before its last list less its
  longest fetch, N its lists a year, and f the CONFIDENCE upper bound on its checks' failure rate
  over the last WEEK, counting one failure more than seen. The far checks alone keep the loss
  under TARGET even when the expected time is wrong; the near ones only shorten the wait.
- It's still learning: with no check logged, or under 14 gaps (lateness.LEAST_GAPS) between
  its lists, every firing asks. One publish is counted once, however many times it's kept.

Why these (the vault's price-watch-numbers note has the proof):

- The median of 14 gaps: replayed on GoatBots' 1,349 publishes, it put a list 4 s from where it
  came typically and 5.3 minutes at the 99th percentile, missing by more than 10 minutes 4
  times in 3.7 years. The mean of the same gaps missed 8 times, and the last gap 20: one late
  publish throws it a day off.
- The lead: on the same replay, 1.4 minutes typically and 5.4 at most. It kept 99% of lists
  within one firing of their publish, as a 30-minute lead did, with half the requests; with no
  lead the 99th percentile was 7.7 minutes.
- Online from the last check that didn't get a list, not from the check that got it: a list
  found late would open the next window late, where its first check finds the list at once, so
  the window would never learn the list was online sooner. The last miss is at most when the
  list went online, so the window opens no later than the lists' true times would open it: the
  bound's error costs requests, never a late find. And the delay is learned in the lead, not
  beside it, so at most one list a year comes before its window, not one for each. The Mac's
  clock against the source's is learned there too.
- The far interval is price-watch-numbers §3's check interval, from each list's own log. On
  tcgcsv it settles near 6 hours once a week of checks is logged: about 5 requests a day in all,
  against 288 at every firing. A failed check shortens it.
- No far interval before 14 gaps: it stands on the shortest gap seen, and with g gaps seen the
  chance the next is shorter than all of them is 1/(g+1). From one gap of a day it was 3 hours
  after a dozen clean checks; on 2026-09-30 Cardmarket published a second guide 7 hours after
  its first. A list replaced between two checks can't be fetched again; a check that finds
  nothing new is a conditional request answered without a body. 14 is the count the expected
  time already waits for (lateness.LEAST_GAPS): the next gap is shorter than every one seen 1
  time in 15.
"""

import math
from collections.abc import Mapping, Sequence
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
    opens: datetime | None  # when it's asked at every firing from: expected less the lead
    far: timedelta  # the far interval; EVERY while it's learning
    gaps: int  # gaps between its lists so far

    @property
    def learning(self) -> bool:
        """Asked at every firing until it has lateness.LEAST_GAPS gaps."""
        return self.gaps < lateness.LEAST_GAPS


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
    (when, failed), and busy, its longest fetch: never under EVERY. plan uses it only once the
    list has lateness.LEAST_GAPS gaps."""
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


def lead(
    made: Sequence[datetime], now: datetime, online: Mapping[datetime, datetime] | None = None
) -> timedelta:
    """How early the window opens before the expected time: the least that would have left at
    most lateness.ALLOWED of the list's lists in the last year online before their window. A list
    is online from online[its made], or from its made if it has none. Below zero when its lists
    go online after they're expected."""
    online = online or {}
    early = []
    for i in range(lateness.LEAST_GAPS + 1, len(made)):
        if now - made[i] > lateness.HISTORY:
            continue
        guess = expected(made[i - lateness.LEAST_GAPS - 1 : i])
        assert guess is not None  # LEAST_GAPS gaps
        early.append(guess - online.get(made[i], made[i]))
    early.sort(reverse=True)
    return early[lateness.ALLOWED] if len(early) > lateness.ALLOWED else _NONE


def plan(
    made: Sequence[datetime],
    checks: Sequence[tuple[datetime, bool]],
    now: datetime,
    busy: timedelta = _NONE,
    online: Mapping[datetime, datetime] | None = None,
) -> Plan:
    """Whether to ask for a list at this firing, and when it's asked next if not: made is when
    its lists were made, checks each check (when, failed), busy its longest fetch, online when
    each list could first have been online, by its made (riffle.watching.online). A list made
    at the same time twice (kept twice) is one list. While it's learning, every firing asks."""
    made = sorted(set(made))
    gaps = max(0, len(made) - 1)
    learning = gaps < lateness.LEAST_GAPS
    interval = EVERY if learning else far(made, checks, now, busy)
    last = max((at for at, _ in checks), default=None)
    due = now if last is None or learning else last + interval
    coming = expected(made)
    opens = None if coming is None else coming - lead(made, now, online)
    if opens is not None:
        due = min(due, opens)
    return Plan(due <= now, max(due, now), coming, opens, interval, gaps)
