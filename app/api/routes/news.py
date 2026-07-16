"""Global news feed: latest stored items across every market, newest first,
each tagged with its market_id. The per-market view is GET /markets/{id}/news
(app.api.routes.markets); this one backs the UI's "All news" tab."""

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas import NewsItemResponse
from app.db.models import Market, NewsItem
from app.db.session import get_session
from app.security import require_api_key

router = APIRouter(tags=["news"], dependencies=[Depends(require_api_key)])


@router.get("/news", response_model=list[NewsItemResponse])
async def list_all_news(
    limit: int = Query(100, ge=1, le=500),
    session: AsyncSession = Depends(get_session),
) -> list[NewsItemResponse]:
    rows = (
        await session.execute(
            select(NewsItem, Market.external_market_id)
            .join(Market, Market.id == NewsItem.market_id)
            .order_by(NewsItem.published_at.desc().nulls_last(), NewsItem.discovered_at.desc())
            .limit(limit)
        )
    ).all()

    return [
        NewsItemResponse(
            id=str(item.id),
            market_id=external_market_id,
            title=item.title,
            summary=item.summary,
            url=item.url,
            source_domain=item.source_domain,
            published_at=item.published_at,
            credibility_score=item.credibility_score,
            relevance_score=item.relevance_score,
            impact_hint=item.impact_hint.value,
            proofs=item.proofs,
        )
        for item, external_market_id in rows
    ]
