from app.dedup.simhash import hamming_distance, simhash
from app.search.aggregator import normalize_url, url_hash


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
