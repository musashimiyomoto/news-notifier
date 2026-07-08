from datetime import datetime, timedelta, timezone

from app.worker.job_ids import process_market_job_id
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


def test_zero_jitter_fraction_is_deterministic():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    resolution = now + timedelta(hours=5)
    result = _next_poll_at(resolution, now, default_minutes=1440, jitter_fraction=0.0)
    assert result == now + timedelta(hours=1)


def test_jitter_fraction_stays_within_bounds_over_many_samples():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    resolution = now + timedelta(hours=5)
    base_step = timedelta(hours=1)
    lower = now + base_step * 0.85
    upper = now + base_step * 1.15

    for _ in range(200):
        result = _next_poll_at(resolution, now, default_minutes=1440, jitter_fraction=0.15)
        assert lower <= result <= upper


def test_jitter_actually_varies_the_result():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    resolution = now + timedelta(hours=5)
    results = {
        _next_poll_at(resolution, now, default_minutes=1440, jitter_fraction=0.15) for _ in range(50)
    }
    # With 50 draws from a continuous uniform distribution, collapsing to a
    # single value would mean jitter isn't actually being applied.
    assert len(results) > 1


def test_process_market_job_id_differs_per_scheduled_run():
    market_id = "11111111-1111-1111-1111-111111111111"
    run_1 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    run_2 = datetime(2026, 1, 1, 1, tzinfo=timezone.utc)
    assert process_market_job_id(market_id, run_1) != process_market_job_id(market_id, run_2)


def test_process_market_job_id_is_stable_for_same_inputs():
    market_id = "11111111-1111-1111-1111-111111111111"
    run_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert process_market_job_id(market_id, run_at) == process_market_job_id(market_id, run_at)
