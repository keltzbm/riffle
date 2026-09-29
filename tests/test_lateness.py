"""When a list is late: past its own margin times the longest gap of its last 30 days."""

import random
from datetime import UTC, datetime, timedelta

from riffle import lateness

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def series(*hours: float) -> list[datetime]:
    made = [T0]
    for h in hours:
        made.append(made[-1] + timedelta(hours=h))
    return made


def test_nothing_is_judged_before_14_gaps():
    made = series(*[24] * 14)
    assert lateness.judge(made[:-1], made[-1] + timedelta(days=9)) is None
    assert lateness.judge(made, made[-1] + timedelta(days=9)) is not None


def test_a_clock_regular_list_is_late_a_quarter_past_its_longest_gap():
    made = series(*[24] * 20)
    v = lateness.judge(made, made[-1] + timedelta(hours=30))
    assert v is not None and not v.late and v.margin == 1.25 and v.longest == timedelta(hours=24)
    v = lateness.judge(made, made[-1] + timedelta(hours=30, seconds=1))
    assert (
        v is not None
        and v.late
        and v.usual == timedelta(hours=24)
        and v.since == timedelta(hours=30, seconds=1)
    )


def test_the_longest_gap_is_the_last_30_days_only():
    made = series(48, *[24] * 40)  # the 48-hour gap is over 30 days before the last list
    v = lateness.judge(made, made[-1])
    assert v is not None and v.longest == timedelta(hours=24)
    made = series(*[24] * 20, 48, *[24] * 5)  # inside it
    v = lateness.judge(made, made[-1])
    assert v is not None and v.longest == timedelta(hours=48)


def test_one_run_past_the_margin_a_year_leaves_it_alone():
    made = series(*[24] * 20, 40, *[24] * 20)  # a late publish: 40/24 = 1.67
    assert lateness.margin(made, made[-1]) == 1.25


def test_two_runs_past_it_in_a_year_widen_it_to_the_second():
    made = series(
        *[24] * 20, 40, *[24] * 20, 36, *[24] * 20
    )  # 1.67, then 36/40 = 0.9 (40 still in the window)...
    assert lateness.margin(made, made[-1]) == 1.25
    made = series(*[24] * 20, 40, *[24] * 40, 36, *[24] * 20)  # ...and 36/24 = 1.5 once 40 has left it
    assert lateness.margin(made, made[-1]) == 1.5


def test_what_ran_past_it_over_a_year_ago_no_longer_counts():
    made = series(*[24] * 20, 40, *[24] * 40, 36, *[24] * 400)
    assert lateness.margin(made, made[-1]) == 1.25


def test_ratios_match_a_plain_count(tmp_path):
    rng = random.Random(4)
    made = series(*[rng.choice([1, 2, 3, 5, 8, 13, 400]) for _ in range(300)])
    gaps = [(b - a).total_seconds() for a, b in zip(made, made[1:], strict=False)]
    expected = []
    for i in range(lateness.LEAST_GAPS, len(gaps)):
        window = [gaps[j] for j in range(i) if made[j + 1] >= made[i] - lateness.WINDOW]
        if window and max(window) > 0:
            expected.append((made[i + 1], gaps[i] / max(window)))
    assert lateness.ratios(made) == expected


def test_same_time_stamps_leave_no_ratio_to_divide_by():
    made = [T0] * 20 + [T0 + timedelta(hours=1)]
    assert lateness.ratios(made) == []
