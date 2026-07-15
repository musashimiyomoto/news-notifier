from app.scoring.credibility import _domain_candidates


def test_exact_domain_is_first_candidate():
    assert _domain_candidates("cnn.com") == ["cnn.com"]


def test_subdomain_falls_back_to_parents_most_specific_first():
    # An article scraped from a regional/section subdomain must be able to
    # match the seeded parent domain instead of landing on the unknown tier.
    assert _domain_candidates("edition.cnn.com") == ["edition.cnn.com", "cnn.com"]
    assert _domain_candidates("uk.finance.yahoo.com") == [
        "uk.finance.yahoo.com",
        "finance.yahoo.com",
        "yahoo.com",
    ]


def test_single_label_and_empty_yield_no_candidates():
    # Bare TLDs / hostnames can't meaningfully match the reputation table.
    assert _domain_candidates("localhost") == []
    assert _domain_candidates("") == []
