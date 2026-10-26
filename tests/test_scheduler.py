from datetime import date, time
import random

from app.config import Settings
from app.scheduler import _random_times, _schedule_payload


def test_random_times_are_spread_across_window_with_min_gap():
    times = _random_times(
        date(2026, 10, 26),
        timezone_name="Asia/Kolkata",
        count=30,
        start=time(8, 0),
        end=time(23, 30),
        min_gap_minutes=12,
        rng=random.Random(42),
    )

    assert len(times) == 30
    assert times == sorted(times)
    assert times[0].hour >= 8
    assert times[-1].hour == 23
    for previous, current in zip(times, times[1:]):
        assert (current - previous).total_seconds() / 60 >= 12


def test_schedule_payload_uses_configured_daily_counts(tmp_path):
    payload = _schedule_payload(
        Settings(state_dir=tmp_path),
        day=date(2026, 10, 26),
        rng=random.Random(7),
    )
    events = payload["events"]
    assert sum(event["kind"] == "post" for event in events) == 4
    assert sum(event["kind"] == "reply" for event in events) == 30
