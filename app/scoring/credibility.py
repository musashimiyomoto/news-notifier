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


def _domain_candidates(domain: str) -> list[str]:
    """Exact domain first, then each parent suffix: "edition.cnn.com" ->
    ["edition.cnn.com", "cnn.com"]. News sites publish under regional/section
    subdomains far more often than the seed table can enumerate, and without
    this every such article silently fell back to the `unknown` tier score.
    Stops before bare TLDs (needs >= 2 labels). More-specific entries win —
    see the ordering logic in compute_credibility — so a deliberately seeded
    subdomain (e.g. finance.yahoo.com) is never shadowed by a parent."""
    parts = domain.split(".")
    return [".".join(parts[i:]) for i in range(len(parts) - 1)]


async def compute_credibility(session: AsyncSession, domain: str, llm_signal: float) -> float:
    candidates = _domain_candidates(domain)
    source = None
    if candidates:
        rows = (
            await session.execute(select(Source).where(Source.domain.in_(candidates)))
        ).scalars().all()
        by_domain = {row.domain: row for row in rows}
        # candidates[] is ordered most-specific first; take the first hit.
        source = next((by_domain[c] for c in candidates if c in by_domain), None)
    base = TIER_BASE_SCORE[source.reliability_tier] if source else TIER_BASE_SCORE[ReliabilityTier.unknown]
    llm_signal = max(0.0, min(1.0, llm_signal))
    return round(SOURCE_WEIGHT * base + LLM_SIGNAL_WEIGHT * llm_signal, 3)
