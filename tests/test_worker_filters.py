from datetime import datetime, timedelta, timezone

from app.worker.tasks import _domain_of, _filter_by_recency, _filter_older_than_watermark

NOW = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)


def _candidate(published_at):
    return {"url": "https://example.com/a", "published_at": published_at}


# --- _filter_by_recency -------------------------------------------------------

def test_recency_keeps_fresh_and_drops_stale():
    fresh = _candidate(NOW - timedelta(days=5))
    stale = _candidate(NOW - timedelta(days=45))
    assert _filter_by_recency([fresh, stale], max_age_days=30, now=NOW) == [fresh]


def test_recency_keeps_candidates_with_missing_or_unparseable_dates():
    # Policy: losing a good article to a bad date string is worse than
    # processing one extra — see Settings.candidate_max_age_days.
    missing = _candidate(None)
    garbage = _candidate("not a date")
    assert _filter_by_recency([missing, garbage], max_age_days=30, now=NOW) == [missing, garbage]


def test_recency_handles_rfc822_string_without_zone():
    # Regression companion to the parse_published_at fix: a zoneless RFC822
    # string must be compared, not crash the aware-vs-naive comparison.
    candidate = _candidate("Mon, 13 Jul 2026 10:00:00")
    assert _filter_by_recency([candidate], max_age_days=30, now=NOW) == [candidate]


def test_recency_zero_disables_filter():
    stale = _candidate(NOW - timedelta(days=400))
    assert _filter_by_recency([stale], max_age_days=0, now=NOW) == [stale]


# --- _filter_older_than_watermark ----------------------------------------------

def test_watermark_none_keeps_everything():
    old = _candidate(NOW - timedelta(days=300))
    assert _filter_older_than_watermark([old], None) == [old]


def test_watermark_drops_older_keeps_equal_and_newer():
    watermark = NOW - timedelta(days=2)
    older = _candidate(watermark - timedelta(hours=1))
    equal = _candidate(watermark)
    newer = _candidate(watermark + timedelta(hours=1))
    assert _filter_older_than_watermark([older, equal, newer], watermark) == [equal, newer]


def test_watermark_keeps_missing_dates():
    assert _filter_older_than_watermark([_candidate(None)], NOW) == [_candidate(None)]


# --- _domain_of -----------------------------------------------------------------

def test_domain_of_strips_www_only():
    assert _domain_of("https://www.reuters.com/markets/article") == "reuters.com"
    assert _domain_of("https://edition.cnn.com/2026/article") == "edition.cnn.com"
