from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ReliabilityTier, Source

TIER_BASE_SCORE = {
    ReliabilityTier.tier1_official: 0.95,
    ReliabilityTier.tier2_major_media: 0.85,
    ReliabilityTier.tier3_aggregator: 0.6,
    ReliabilityTier.tier4_social_blog: 0.35,
    ReliabilityTier.unknown: 0.5,
}

# Weight given to the static source-reputation table vs. the per-article LLM
# content-quality signal (concrete facts/figures/named sources in the text).
SOURCE_WEIGHT = 0.6
LLM_SIGNAL_WEIGHT = 0.4


async def compute_credibility(session: AsyncSession, domain: str, llm_signal: float) -> float:
    source = (await session.execute(select(Source).where(Source.domain == domain))).scalar_one_or_none()
    base = TIER_BASE_SCORE[source.reliability_tier] if source else TIER_BASE_SCORE[ReliabilityTier.unknown]
    llm_signal = max(0.0, min(1.0, llm_signal))
    return round(SOURCE_WEIGHT * base + LLM_SIGNAL_WEIGHT * llm_signal, 3)
