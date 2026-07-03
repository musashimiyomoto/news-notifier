from datetime import datetime, timedelta, timezone

from app.worker.tasks import _next_poll_at


def test_no_resolution_date_falls_back_to_default_interval():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    result = _next_poll_at(None, now, default_minutes=1440)
    assert result == now + timedelta(minutes=1440)


def test_far_resolution_date_uses_default_interval():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    resolution = now + timedelta(days=90)
    result = _next_poll_at(resolution, now, default_minutes=1440)
    assert result == now + timedelta(minutes=1440)


def test_close_resolution_date_polls_hourly():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    resolution = now + timedelta(hours=5)
    result = _next_poll_at(resolution, now, default_minutes=1440)
    assert result == now + timedelta(hours=1)


def test_within_a_week_polls_every_six_hours():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    resolution = now + timedelta(days=3)
    result = _next_poll_at(resolution, now, default_minutes=1440)
    assert result == now + timedelta(hours=6)


def test_past_resolution_date_still_polls_hourly_instead_of_going_quiet():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    resolution = now - timedelta(hours=2)
    result = _next_poll_at(resolution, now, default_minutes=1440)
    assert result == now + timedelta(hours=1)
