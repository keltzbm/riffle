"""Times: kept in UTC, shown on a terminal in the Mac's time with the zone."""

from datetime import UTC, date, datetime

from riffle import times

AT = datetime(2026, 9, 28, 9, 41, tzinfo=UTC)


def test_local_time_says_its_zone(denver):
    assert times.local(AT) == "2026-09-28 03:41 MDT"
    assert times.local(datetime(2026, 12, 1, 9, 41, tzinfo=UTC), "%H:%M") == "02:41 MST"


def test_utc_says_so(denver):
    assert times.utc(AT) == "2026-09-28 09:41 UTC"


def test_shown_is_local_on_a_terminal(on_a_terminal):
    assert times.shown(AT) == "2026-09-28 03:41 MDT"


def test_shown_is_utc_anywhere_else(denver):
    assert times.shown(AT, "%H:%M") == "09:41 UTC"  # pytest's captured stdout isn't a terminal


def test_now_is_in_utc():
    assert times.now().tzinfo is UTC


def test_today_is_the_utc_date(monkeypatch):
    monkeypatch.setattr(times, "now", lambda: datetime(2026, 9, 29, 0, 30, tzinfo=UTC))  # 18:30 in Denver
    assert times.today() == date(2026, 9, 29)
