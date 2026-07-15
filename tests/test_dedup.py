from app.dedup.simhash import from_signed_64, hamming_distance, simhash, to_signed_64
from app.search.aggregator import normalize_url, parse_published_at, url_hash


def test_signed_64_round_trip_preserves_simhash():
    # A title whose simhash has the top bit set is the case to_signed_64 exists
    # for; from_signed_64 must recover the original unsigned fingerprint exactly.
    for text in ("Fed raises interest rates by 25 basis points", "Local team wins championship"):
        fingerprint = simhash(text)
        assert from_signed_64(to_signed_64(fingerprint)) == fingerprint


def test_hamming_after_signed_round_trip_matches_direct():
    # Dedup in process_candidate compares a fresh simhash against values read
    # back from the signed int8 column; the distance must be identical to
    # comparing the raw unsigned fingerprints.
    a = simhash("Fed raises interest rates by 25 basis points")
    b = simhash("Fed raises interest rate by 25 basis points")
    assert hamming_distance(a, from_signed_64(to_signed_64(b))) == hamming_distance(a, b)


def test_simhash_same_token_multiset_is_identical():
    # The simhash is over a bag of word tokens (case-normalized), so identical
    # wording — regardless of case or word order — is distance 0. This is the
    # duplicate class the simhash layer is responsible for; anything fuzzier
    # (one-word-different rewrites) lands at distance ~8 on short titles and is
    # deliberately NOT caught here (threshold 3), because opposite-meaning
    # titles ("Fed raises..." vs "Fed cuts...") sit only a few bits further
    # (~11-13) — semantic near-dups are the vector layer's job (see
    # app.dedup.vector_dedup), where opposite-meaning pairs can at least be
    # separated by the summary embedding, not just title word overlap.
    a = simhash("Fed Raises Interest Rates by 25 Basis Points")
    b = simhash("fed raises interest rates by 25 basis points")
    assert hamming_distance(a, b) == 0


def test_simhash_one_word_rewrite_is_beyond_threshold():
    # Documents the deliberate miss: a one-word rewrite is NOT within the
    # production threshold (SIMHASH_HAMMING_THRESHOLD=3) — see the rationale
    # in test_simhash_same_token_multiset_is_identical.
    a = simhash("Fed raises interest rates by 25 basis points")
    b = simhash("Fed raises interest rate by 25 basis points")
    assert hamming_distance(a, b) > 3


def test_simhash_unrelated_titles_are_far():
    a = simhash("Fed raises interest rates by 25 basis points")
    b = simhash("Local team wins championship game tonight")
    assert hamming_distance(a, b) > 3


def test_parse_published_at_rfc822_without_zone_is_coerced_to_utc():
    # Regression: an RFC822 date with no zone parses to a naive datetime, and
    # the recency/watermark filters then compare it against an aware cutoff —
    # which raises TypeError and fails the whole process_market cycle.
    parsed = parse_published_at("Mon, 14 Jul 2026 10:00:00")
    assert parsed is not None and parsed.tzinfo is not None


def test_parse_published_at_all_branches_return_aware_or_none():
    from datetime import datetime

    cases = [
        "Mon, 14 Jul 2026 10:00:00 GMT",  # RFC822 with zone
        "2026-07-14T10:00:00Z",           # ISO with Z
        "2026-07-01",                      # date-only ISO (naive -> coerced)
        datetime(2026, 7, 1),              # naive datetime instance
    ]
    for value in cases:
        parsed = parse_published_at(value)
        assert parsed is not None and parsed.tzinfo is not None, value
    assert parse_published_at("garbage") is None
    assert parse_published_at(None) is None


def test_normalize_url_strips_tracking_params_and_fragment():
    url = "https://example.com/article?id=42&utm_source=twitter&fbclid=abc#section2"
    assert normalize_url(url) == "https://example.com/article?id=42"


def test_normalize_url_strips_trailing_slash():
    assert normalize_url("https://example.com/article/") == "https://example.com/article"


def test_url_hash_is_stable_across_equivalent_urls():
    a = "https://example.com/article?utm_source=twitter&id=42"
    b = "https://example.com/article?id=42&utm_source=facebook"
    assert url_hash(a) == url_hash(b)


def test_normalize_url_ignores_non_tracking_param_order():
    a = "https://example.com/article?id=5&cat=news"
    b = "https://example.com/article?cat=news&id=5"
    assert normalize_url(a) == normalize_url(b)
