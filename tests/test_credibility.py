from app.db.models import ReliabilityTier, Source
from app.scoring.credibility import (
    LLM_SIGNAL_WEIGHT,
    SOURCE_WEIGHT,
    TIER_BASE_SCORE,
    _domain_candidates,
    compute_credibility,
)


class FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


class FakeSession:
    """Stands in for AsyncSession: returns the given Source rows for any query."""

    def __init__(self, rows):
        self.rows = rows

    async def execute(self, _stmt):
        return FakeResult(self.rows)


def _source(domain, tier):
    return Source(domain=domain, reliability_tier=tier, reliability_score=0.9)


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


async def test_known_domain_blends_tier_base_with_llm_signal():
    session = FakeSession([_source("cnn.com", ReliabilityTier.tier2_major_media)])
    score = await compute_credibility(session, "cnn.com", llm_signal=1.0)
    expected = SOURCE_WEIGHT * TIER_BASE_SCORE[ReliabilityTier.tier2_major_media] + LLM_SIGNAL_WEIGHT * 1.0
    assert score == round(expected, 3)


async def test_most_specific_seeded_domain_wins_over_parent():
    # finance.yahoo.com is deliberately seeded separately from yahoo.com —
    # an article from uk.finance.yahoo.com must match the former, not the latter.
    session = FakeSession(
        [
            _source("yahoo.com", ReliabilityTier.tier4_social_blog),
            _source("finance.yahoo.com", ReliabilityTier.tier3_aggregator),
        ]
    )
    score = await compute_credibility(session, "uk.finance.yahoo.com", llm_signal=0.0)
    assert score == round(SOURCE_WEIGHT * TIER_BASE_SCORE[ReliabilityTier.tier3_aggregator], 3)


async def test_unknown_domain_falls_back_to_unknown_tier():
    session = FakeSession([])
    score = await compute_credibility(session, "obscure-blog.example", llm_signal=0.5)
    expected = SOURCE_WEIGHT * TIER_BASE_SCORE[ReliabilityTier.unknown] + LLM_SIGNAL_WEIGHT * 0.5
    assert score == round(expected, 3)


async def test_llm_signal_is_clamped_to_unit_interval():
    session = FakeSession([])
    too_high = await compute_credibility(session, "a.example", llm_signal=5.0)
    too_low = await compute_credibility(session, "a.example", llm_signal=-3.0)
    assert too_high == round(SOURCE_WEIGHT * 0.5 + LLM_SIGNAL_WEIGHT * 1.0, 3)
    assert too_low == round(SOURCE_WEIGHT * 0.5, 3)
