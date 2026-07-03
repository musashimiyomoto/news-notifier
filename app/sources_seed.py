"""Seed the source-reputation table. Run once after migrations:
    python -m app.sources_seed
"""

import asyncio

from app.db.models import ReliabilityTier, Source
from app.db.session import async_session_factory

SEED: list[tuple[str, ReliabilityTier, float]] = [
    # tier 1 — official/primary
    ("reuters.com", ReliabilityTier.tier1_official, 0.97),
    ("apnews.com", ReliabilityTier.tier1_official, 0.97),
    ("federalreserve.gov", ReliabilityTier.tier1_official, 0.98),
    ("sec.gov", ReliabilityTier.tier1_official, 0.98),
    # tier 2 — major media
    ("bloomberg.com", ReliabilityTier.tier2_major_media, 0.9),
    ("bbc.com", ReliabilityTier.tier2_major_media, 0.9),
    ("nytimes.com", ReliabilityTier.tier2_major_media, 0.88),
    ("wsj.com", ReliabilityTier.tier2_major_media, 0.88),
    ("theguardian.com", ReliabilityTier.tier2_major_media, 0.85),
    ("cnn.com", ReliabilityTier.tier2_major_media, 0.82),
    ("politico.com", ReliabilityTier.tier2_major_media, 0.85),
    ("axios.com", ReliabilityTier.tier2_major_media, 0.83),
    ("npr.org", ReliabilityTier.tier2_major_media, 0.87),
    # tier 3 — aggregators / secondary
    ("forbes.com", ReliabilityTier.tier3_aggregator, 0.6),
    ("businessinsider.com", ReliabilityTier.tier3_aggregator, 0.58),
    ("wikipedia.org", ReliabilityTier.tier3_aggregator, 0.55),
    ("finance.yahoo.com", ReliabilityTier.tier3_aggregator, 0.55),
    # tier 4 — social / blogs
    ("reddit.com", ReliabilityTier.tier4_social_blog, 0.3),
    ("x.com", ReliabilityTier.tier4_social_blog, 0.3),
    ("twitter.com", ReliabilityTier.tier4_social_blog, 0.3),
    ("medium.com", ReliabilityTier.tier4_social_blog, 0.35),
    ("substack.com", ReliabilityTier.tier4_social_blog, 0.4),
]


async def seed() -> None:
    async with async_session_factory() as session:
        for domain, tier, score in SEED:
            await session.merge(Source(domain=domain, reliability_tier=tier, reliability_score=score))
        await session.commit()
    print(f"Seeded {len(SEED)} sources.")


if __name__ == "__main__":
    asyncio.run(seed())
