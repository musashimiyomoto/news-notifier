from app.dedup.simhash import from_signed_64, hamming_distance, simhash, to_signed_64
from app.search.aggregator import normalize_url, url_hash


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


def test_simhash_near_duplicate_titles_are_close():
    a = simhash("Fed raises interest rates by 25 basis points")
    b = simhash("Fed raises interest rate by 25 basis points")
    assert hamming_distance(a, b) <= 3


def test_simhash_unrelated_titles_are_far():
    a = simhash("Fed raises interest rates by 25 basis points")
    b = simhash("Local team wins championship game tonight")
    assert hamming_distance(a, b) > 3


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
