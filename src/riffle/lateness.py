"""When a list is late, from its own publish times.

A list is late once the time since its last list is more than m times the longest gap it had in
the 30 days before that list. The margin m is the list's own: the least value, at least 1.25, that
would have raised at most one alarm on its own gaps over the last year. A clock-regular list keeps
1.25; a list whose gaps now and then run past its month's longest learns a wider one. Nothing is
judged until a list has 14 gaps.

Why these numbers (the vault's price-watch-numbers note has the proof and the charts):

- 1.25: GoatBots' 1,363 gaps over 3.7 years. Its normal gaps beat the longest of the 30 days
  before them by at most 1.0035 times; its two late publishes by 1.2506 and 1.9926 times. Any
  margin between catches both with no false alarm; 1.25 is the top, the most room for a less
  regular list. The rule it replaces, three times the median gap, caught neither.
- 30 days: 30 and 90 did the same on GoatBots; a year missed the 6-hour-late publish, because a
  48-hour gap held the bar up all year. The shorter window recovers sooner after a late publish.
- One alarm a year: a list's own late publishes are rare (GoatBots: two in 3.7 years), so one a
  year leaves the margin alone for them, while a list that runs past 1.25 more often than that is
  irregular, not late, and its margin rises to match.
- 14 gaps: from then a normal gap sets a new record with chance at most 1/15, even with no margin.
"""

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from statistics import median

MARGIN = 1.25
WINDOW = timedelta(days=30)
HISTORY = timedelta(days=365)
ALLOWED = 1
LEAST_GAPS = 14


@dataclass(frozen=True)
class Verdict:
    usual: timedelta  # the median of the last LEAST_GAPS gaps
    longest: timedelta  # the longest gap in the WINDOW before the last list
    margin: float  # this list's m
    since: timedelta  # from the last list to now
    late: bool


def ratios(made: list[datetime]) -> list[tuple[datetime, float]]:
    """For each gap after the first LEAST_GAPS: when it ended, and how many times the longest gap
    ending in the WINDOW before it began it was. Times sorted, oldest first."""
    gaps = [(b - a).total_seconds() for a, b in zip(made, made[1:], strict=False)]
    out: list[tuple[datetime, float]] = []
    window: deque[int] = deque()  # gaps ended by now, longest first
    for i, gap in enumerate(gaps):
        if i:  # the gap before this one has just ended
            while window and gaps[window[-1]] <= gaps[i - 1]:
                window.pop()
            window.append(i - 1)
        while window and made[window[0] + 1] < made[i] - WINDOW:
            window.popleft()
        if i >= LEAST_GAPS and window and gaps[window[0]] > 0:
            out.append((made[i + 1], gap / gaps[window[0]]))
    return out


def margin(made: list[datetime], now: datetime) -> float:
    """The least m, at least MARGIN, that would have raised at most ALLOWED alarms on the list's
    gaps ending in the last HISTORY: an alarm is a gap more than m times its window's longest."""
    recent = sorted((r for end, r in ratios(made) if now - end <= HISTORY), reverse=True)
    return max(MARGIN, recent[ALLOWED]) if len(recent) > ALLOWED else MARGIN


def usual(made: list[datetime]) -> timedelta | None:
    """The median of a list's last LEAST_GAPS gaps (times oldest first); None until it has them."""
    if len(made) <= LEAST_GAPS:
        return None
    recent = zip(made[-LEAST_GAPS - 1 :], made[-LEAST_GAPS:], strict=False)
    return timedelta(seconds=median((b - a).total_seconds() for a, b in recent))


def judge(made: list[datetime], now: datetime) -> Verdict | None:
    """Whether a list made at these times (oldest first) is late at `now`; None until it has
    LEAST_GAPS gaps."""
    gap = usual(made)
    if gap is None:
        return None
    last = made[-1]
    longest = max(b - a for a, b in zip(made, made[1:], strict=False) if b >= last - WINDOW)
    m = margin(made, now)
    since = now - last
    return Verdict(gap, longest, m, since, since > longest * m)
