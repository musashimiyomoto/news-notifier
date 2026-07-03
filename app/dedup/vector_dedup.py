import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import NewsItem


async def find_similar(
    session: AsyncSession, market_id: uuid.UUID, embedding: list[float], threshold: float
) -> NewsItem | None:
    """Semantic dedup: find the nearest existing news item for this market by
    cosine similarity. Scoped strictly to market_id — cross-market similarity
    is expected (same real-world event) and must NOT be deduped away, since
    each market's subscriber gets its own independent news stream."""
    distance = NewsItem.embedding.cosine_distance(embedding)
    stmt = select(NewsItem, distance.label("distance")).where(NewsItem.market_id == market_id).order_by(distance).limit(1)
    row = (await session.execute(stmt)).first()
    if row is None:
        return None

    item, distance_value = row
    similarity = 1 - distance_value
    return item if similarity >= threshold else None
