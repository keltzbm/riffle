import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from riffle import disk, times

NOW = datetime(2026, 10, 5, 22, 0, tzinfo=UTC)
GB = 10**9


@pytest.fixture
def low(monkeypatch):
    """Riffle's threshold as shipped, and the clock at NOW."""
    monkeypatch.setattr(disk, "WARN_BELOW", 50 * GB)
    monkeypatch.setattr(times, "now", lambda: NOW)


def free(n):
    return lambda folder: SimpleNamespace(free=n)


def readings(*pairs):
    disk.log_path().parent.mkdir(parents=True, exist_ok=True)
    disk.log_path().write_text("".join(json.dumps({"at": t.isoformat(), "free": f}) + "\n" for t, f in pairs))


def test_plenty_of_space_is_recorded_and_not_shown(low, tracker):
    disk.check(tracker, usage=free(460 * GB))
    assert tracker.steps == []
    assert json.loads(disk.log_path().read_text()) == {"at": NOW.isoformat(), "free": 460 * GB}


def test_low_space_warns_without_a_rate_at_first(low, tracker):
    disk.check(tracker, usage=free(48 * GB))
    assert tracker.outcomes() == {"disk": ("warn", "48 GB free, under 50 GB")}


def test_low_space_warns_with_the_days_left_at_last_week_s_rate(low, tracker):
    readings(
        (NOW - timedelta(days=9), 70 * GB),  # older than a week: not used
        (NOW - timedelta(days=7), 58 * GB),
        (NOW - timedelta(hours=12), 49 * GB),  # under a day old: not used
    )
    disk.check(tracker, usage=free(47 * GB + 600_000_000))
    assert tracker.outcomes() == {
        "disk": ("warn", "48 GB free, under 50 GB; about 32 days left at 1.5 GB a day")
    }
    assert len(disk.log_path().read_text().splitlines()) == 4


def test_no_rate_when_space_grew(low, tracker):
    readings((NOW - timedelta(days=3), 40 * GB))
    disk.check(tracker, usage=free(45 * GB))
    assert tracker.outcomes() == {"disk": ("warn", "45 GB free, under 50 GB")}


def test_an_unreadable_reading_is_skipped(low, tracker):
    disk.log_path().parent.mkdir(parents=True, exist_ok=True)
    disk.log_path().write_text(
        'not json\n{"at": "never"}\n{"free": 1}\n'
        + json.dumps({"at": (NOW - timedelta(days=2)).isoformat(), "free": 50 * GB})
        + "\n"
    )
    disk.check(tracker, usage=free(48 * GB))
    assert tracker.outcomes() == {
        "disk": ("warn", "48 GB free, under 50 GB; about 48 days left at 1.0 GB a day")
    }


def test_a_disk_that_cant_be_read_fails_the_step(low, tracker):
    def broken(folder):
        raise OSError("Input/output error")

    disk.check(tracker, usage=broken)
    assert tracker.outcomes() == {"disk": ("fail", "free space unknown (Input/output error)")}
