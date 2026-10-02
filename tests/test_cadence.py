"""When a watch asks a source that limits its requests: at every firing until it has 14 gaps;
then from the list's expected time, less its learned lead, until the list comes, and otherwise
every far interval, learned from its checks."""

from datetime import UTC, datetime, timedelta

from riffle import cadence

T0 = datetime(2026, 9, 1, 20, 5, tzinfo=UTC)  # tcgcsv publishes about 20:05 UTC
DAY = timedelta(days=1)
FETCH = timedelta(minutes=32)  # tcgcsv's day took 32 minutes to fetch on 2026-09-28


def daily(n: int) -> list[datetime]:
    return [T0 + i * DAY for i in range(n)]


def every(now: datetime, n: int, apart: timedelta, failed: int = -1) -> list[tuple[datetime, bool]]:
    """n checks before now, apart, the failed-th (from now) failing."""
    return [(now - (i + 1) * apart, i == failed) for i in range(n)]


def test_the_failure_bound_is_price_watch_numbers():
    assert round(cadence.upper(0, 90), 4) == 0.0327  # §3: 90 clean checks
    assert round(cadence.upper(1, 90), 4) == 0.0516
    assert round(cadence.upper(2, 90), 4) == 0.0683
    assert round(cadence.upper(0, 2016), 4) == 0.0015  # a clean week at every firing
    assert cadence.upper(1, 1) == cadence.upper(1, 0) == 1.0


def test_with_no_check_logged_or_under_two_lists_every_firing_asks():
    now = T0 + 20 * DAY
    assert cadence.far(daily(20), [], now) == cadence.EVERY
    assert cadence.far(daily(1), every(now, 500, timedelta(minutes=5)), now) == cadence.EVERY
    assert cadence.far([], [], now) == cadence.EVERY


def test_the_far_interval_meets_the_loss_target_from_the_list_s_own_log():
    made = daily(5)  # tcgcsv's four gaps
    now = made[-1] + timedelta(hours=1)
    # today's five checks, one failed: f at most 81%, 40 chances in 23 h 28 min
    assert cadence.far(made, every(now, 5, timedelta(hours=1), failed=1), now, FETCH) == timedelta(minutes=35)
    # its first hour clean at every firing: f at most 34%, 8 chances
    assert cadence.far(made, every(now, 12, timedelta(minutes=5)), now, FETCH) == timedelta(
        hours=2, minutes=56
    )
    # a week settled at about 42 checks: f at most 10.8%, 4 chances; one failure makes it 5
    week = every(now, 42, timedelta(hours=4))
    assert cadence.far(made, week, now, FETCH) == timedelta(hours=5, minutes=52)
    assert cadence.far(made, every(now, 42, timedelta(hours=4), failed=0), now, FETCH) == timedelta(
        hours=4, minutes=41
    )
    # a failure more than a week old no longer counts
    assert cadence.far(made, [*week, (now - 8 * DAY, True)], now, FETCH) == timedelta(hours=5, minutes=52)


def test_the_far_interval_is_never_under_a_firing():
    made = [T0 + timedelta(minutes=3 * i) for i in range(10)]  # a list every 3 minutes
    now = made[-1]
    assert cadence.far(made, every(now, 500, timedelta(minutes=1)), now) == cadence.EVERY
    assert cadence.far([T0, T0], every(T0, 500, timedelta(minutes=1)), T0) == cadence.EVERY  # no gap at all


def test_a_list_rarer_than_the_target_needs_one_chance():
    made = [T0, T0 + 4100 * DAY]  # once in eleven years: under TARGET lists a year
    now = made[-1] + timedelta(hours=1)
    assert cadence.far(made, every(now, 42, timedelta(hours=4)), now) == 4100 * DAY


def test_the_next_list_is_expected_its_usual_gap_after_the_last():
    assert cadence.expected(daily(14)) is None  # 13 gaps
    assert cadence.expected(daily(15)) == T0 + 15 * DAY
    made = [*daily(14), T0 + 13 * DAY + timedelta(hours=48)]  # one late publish
    assert cadence.expected(made) == made[-1] + DAY  # the median ignores it


def drifting(early: dict[int, int], n: int = 20) -> list[datetime]:
    """n lists a day apart but for early: list i that many minutes before its expected time."""
    made = [T0]
    for i in range(1, n):
        made.append(made[-1] + DAY - timedelta(minutes=early.get(i, 0)))
    return made


def test_the_lead_leaves_at_most_one_list_a_year_before_its_window():
    assert cadence.lead(daily(20), T0 + 20 * DAY) == timedelta(0)  # clock-regular: nothing comes early
    made = drifting({16: 6, 17: 2, 18: 4})
    assert cadence.lead(made, made[-1]) == timedelta(minutes=4)  # the second earliest
    made = drifting({16: 6})
    assert cadence.lead(made, made[-1]) == timedelta(0)  # one early list is the year's allowance
    made = drifting({8: 30, 9: 30})
    assert cadence.lead(made, made[-1]) == timedelta(0)  # before 14 gaps nothing was expected


def test_a_list_early_more_than_a_year_ago_is_forgotten():
    made = drifting({16: 6, 17: 8})
    assert cadence.lead(made, made[-1]) == timedelta(minutes=6)
    assert cadence.lead(made, made[16] + 366 * DAY) == timedelta(0)


LAG = timedelta(hours=6, minutes=48)  # MTGJSON's build of 2026-09-29: made 06:12 UTC, not served at 13:00


def test_the_lead_is_measured_to_when_each_list_could_first_be_online():
    made = daily(20)
    late = {m: m + LAG for m in made}
    assert cadence.lead(made, made[-1], late) == -LAG  # the window opens after the expected time
    assert cadence.lead(made, made[-1], {m: m for m in made}) == timedelta(0)  # online when made: as before
    one = {m: t for m, t in late.items() if m != made[17]}  # online when made: the year's allowance
    assert cadence.lead(made, made[-1], one) == -LAG
    two = {m: t for m, t in one.items() if m != made[18]}
    assert cadence.lead(made, made[-1], two) == timedelta(0)


def test_the_delay_is_learned_in_the_lead_not_beside_it():
    made = drifting({16: 6, 17: 2, 18: 4})  # a lead of 4 minutes when online as made
    ten = {m: m + timedelta(minutes=10) for m in made}
    assert cadence.lead(made, made[-1], ten) == timedelta(minutes=-6)  # 4 minutes early, online 10 later


def test_with_nothing_logged_the_first_firing_asks():
    plan = cadence.plan(daily(3), [], T0 + 3 * DAY)
    assert plan.ask and plan.expected is None and plan.opens is None
    assert plan.far == cadence.EVERY and plan.gaps == 2


def test_a_list_that_goes_online_hours_after_it_s_made_is_asked_from_then():
    made = daily(17)
    expected = T0 + 17 * DAY
    week = every(expected + timedelta(hours=5), 42, timedelta(hours=4))  # the last an hour after expected
    late = {m: m + LAG for m in made}
    before = cadence.plan(made, week, expected + timedelta(hours=2), online=late)
    assert not before.ask and before.expected == expected and before.next == before.opens == expected + LAG
    assert cadence.plan(made, week, expected + LAG, online=late).ask
    assert cadence.plan(made, week, expected + timedelta(hours=2)).ask  # by when it's made: every firing


def test_a_learned_list_is_asked_from_its_expected_time_until_it_comes():
    made = daily(15)
    expected = T0 + 15 * DAY
    week = every(expected - timedelta(hours=2), 42, timedelta(hours=4))
    before = cadence.plan(made, week, expected - timedelta(minutes=1))
    assert not before.ask and before.next == expected and before.expected == expected
    assert cadence.plan(made, week, expected).ask
    late = expected + timedelta(hours=3)
    assert cadence.plan(made, [*week, (late - timedelta(minutes=5), False)], late).ask  # however late


def test_until_it_has_14_gaps_a_list_is_asked_at_every_firing():
    for n in (2, 3, 14):  # 1, 2 and 13 gaps
        made = daily(n)
        now = made[-1] + timedelta(hours=6)
        plan = cadence.plan(made, [(now - timedelta(minutes=1), False)], now)  # asked a minute ago
        assert plan.ask and plan.learning and plan.next == now and plan.far == cadence.EVERY
        assert plan.gaps == n - 1 and plan.expected is None


def test_from_14_gaps_a_list_is_asked_each_far_interval():
    made = daily(15)
    now = made[-1] + timedelta(hours=6)
    checks = every(now - timedelta(minutes=30), 42, timedelta(hours=4))
    plan = cadence.plan(made, checks, now)
    last = max(at for at, _ in checks)
    assert not plan.ask and not plan.learning and plan.next == last + plan.far
    assert plan.far == cadence.far(made, checks, now) == timedelta(hours=6)  # a day over 4 chances
    assert cadence.plan(made, checks, last + plan.far).ask


def test_a_list_kept_twice_is_one_list():
    made = daily(15)
    twice = [*made, *made[-3:]]  # Cardmarket's guides of 2026-09-27 to 29, in daily/ and lists/
    now = made[-1] + timedelta(hours=6)
    checks = every(now - timedelta(minutes=30), 42, timedelta(hours=4))
    assert cadence.plan(twice, checks, now) == cadence.plan(made, checks, now)
    assert cadence.plan(twice, checks, now).gaps == 14
